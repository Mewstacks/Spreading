'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
    resetSessionForQr, markResetFailure, markQrBootstrap,
    finalizeQrBootstrapFailure, decidirRestauracao, MOTIVO_FALHA_RESET,
} = require('../session_reset');

const sessionState = (id = 'u1') => ({
    id,
    client: { id: 'antigo' },
    initialized: true,
    isConnected: false,
    preparando: false,
    ultimoQR: 'qr-antigo',
    fase: 'reconectando',
    faseMsg: 'Recuperando sessão…',
    progresso: 10,
    reconnectAttempts: 5,
    authPurges: 1,
    initTimer: 1,
    qrIdleTimer: 2,
    reconnectTimer: 3,
    qrBootstrapTimer: 4,
    preparationTimer: 5,
    gruposRetryTimer: 6,
    encerrandoManual: false,
    resetPromise: null,
});

const freshState = (id) => ({
    ...sessionState(id),
    client: null,
    initialized: false,
    ultimoQR: null,
    fase: 'iniciando',
    faseMsg: 'Iniciando serviço…',
    progresso: 0,
    reconnectAttempts: 0,
    authPurges: 0,
    initTimer: null,
    qrIdleTimer: null,
    reconnectTimer: null,
    qrBootstrapTimer: null,
    preparationTimer: null,
    gruposRetryTimer: null,
    encerrandoManual: false,
    resetPromise: null,
});

test('reset cancela timers, apaga auth e inicia exatamente uma sessao limpa', async () => {
    const session = sessionState();
    const timersCancelados = [];
    const eventos = [];
    let atual = session;
    let inicializacoes = 0;

    const resultado = await resetSessionForQr(session, {
        clearTimer: (timer) => timersCancelados.push(timer),
        destroyRuntime: async (old) => {
            eventos.push('destroy');
            assert.equal(old.fase, 'reiniciando_qr');
            assert.equal(old.encerrandoManual, true);
            assert.equal(old.reconnectTimer, null);
            old.client = null;
            old.initialized = false;
        },
        cleanupProfile: async () => {
            eventos.push('cleanup');
            return true;
        },
        purgeAuth: () => {
            eventos.push('purge');
            return true;
        },
        createState: freshState,
        replaceSession: (fresh) => {
            eventos.push('replace');
            atual = fresh;
        },
        initialize: (fresh) => {
            eventos.push('initialize');
            inicializacoes += 1;
            fresh.initialized = true;
        },
    });

    assert.deepEqual(timersCancelados, [1, 2, 3, 4, 5, 6]);
    assert.deepEqual(eventos, ['destroy', 'cleanup', 'purge', 'replace', 'initialize']);
    assert.equal(resultado.sucesso, true);
    assert.equal(resultado.auth_removido, true);
    assert.equal(inicializacoes, 1);
    assert.equal(atual.reconnectAttempts, 0);
    assert.equal(atual.authPurges, 0);
    assert.equal(atual.fase, 'reiniciando_qr');
    assert.equal(atual.qrBootstrapAtivo, true);
    assert.equal(atual.qrBootstrapAttempts, 1);
});

test('falha ao apagar auth nao inicializa nem reutiliza a sessao antiga', async () => {
    const session = sessionState();
    let inicializacoes = 0;
    let substituicoes = 0;

    const resultado = await resetSessionForQr(session, {
        clearTimer: () => {},
        destroyRuntime: async (old) => {
            old.client = null;
            old.initialized = false;
        },
        purgeAuth: () => false,
        createState: freshState,
        replaceSession: () => { substituicoes += 1; },
        initialize: () => { inicializacoes += 1; },
    });

    assert.equal(resultado.sucesso, false);
    assert.equal(resultado.auth_removido, false);
    assert.equal(inicializacoes, 0);
    assert.equal(substituicoes, 0);
    assert.equal(session.fase, 'falha_reset');
    assert.equal(session.motivoFalhaReset, MOTIVO_FALHA_RESET.PURGE_FALHOU);
    assert.equal(session.encerrandoManual, true);
    assert.equal(session.client, null);
});

test('falha ao encerrar Chromium antigo para antes de apagar auth', async () => {
    const session = sessionState();
    let purges = 0;
    let inicializacoes = 0;

    const resultado = await resetSessionForQr(session, {
        clearTimer: () => {},
        destroyRuntime: async (old) => {
            old.client = null;
            old.initialized = false;
        },
        cleanupProfile: async () => false,
        purgeAuth: () => {
            purges += 1;
            return true;
        },
        createState: freshState,
        replaceSession: () => {},
        initialize: () => { inicializacoes += 1; },
    });

    assert.equal(resultado.sucesso, false);
    assert.equal(resultado.auth_removido, false);
    assert.equal(purges, 0);
    assert.equal(inicializacoes, 0);
    assert.equal(session.fase, 'falha_reset');
    assert.equal(session.motivoFalhaReset, MOTIVO_FALHA_RESET.CHROMIUM_NAO_ENCERROU);
    assert.equal(session.qrBootstrapTimer, null);
});

test('markResetFailure grava a fase, o motivo e a mensagem legivel', () => {
    const session = { id: 'u1', qrBootstrapAtivo: true };
    markResetFailure(session, 'Não foi possível gerar o QR.', MOTIVO_FALHA_RESET.QR_NAO_GERADO);
    assert.equal(session.fase, 'falha_reset');
    assert.equal(session.faseMsg, 'Não foi possível gerar o QR.');
    assert.equal(session.motivoFalhaReset, MOTIVO_FALHA_RESET.QR_NAO_GERADO);
    assert.equal(session.qrBootstrapAtivo, false);
    // Sem motivo explicito cai em 'desconhecido', nunca undefined.
    const outra = { id: 'u2' };
    markResetFailure(outra, 'falhou');
    assert.equal(outra.motivoFalhaReset, MOTIVO_FALHA_RESET.DESCONHECIDO);
});

test('markQrBootstrap arma o bootstrap com contadores zerados', () => {
    const fresh = markQrBootstrap({ id: 'u1' });
    assert.equal(fresh.qrBootstrapAtivo, true);
    assert.equal(fresh.qrBootstrapAttempts, 1);
    assert.equal(fresh.encerrandoManual, false);
    assert.equal(fresh.fase, 'reiniciando_qr');
});

test('falha final do QR encerra runtime e apaga auth antes de liberar nova tentativa', async () => {
    const session = {
        ...sessionState(),
        qrBootstrapAtivo: true,
        qrBootstrapAttempts: 2,
    };
    const eventos = [];

    const resultado = await finalizeQrBootstrapFailure(session, {
        destroyRuntime: async (current) => {
            eventos.push('destroy');
            assert.equal(current.encerrandoManual, true);
            assert.equal(current.qrBootstrapAtivo, false);
            current.client = null;
            current.initialized = false;
        },
        cleanupProfile: async () => {
            eventos.push('cleanup');
            return true;
        },
        purgeAuth: () => {
            eventos.push('purge');
            return true;
        },
        message: 'Não foi possível gerar o QR após 2 tentativas.',
        motivo: MOTIVO_FALHA_RESET.QR_NAO_GERADO,
    });

    assert.deepEqual(eventos, ['destroy', 'cleanup', 'purge']);
    assert.equal(resultado.runtime_limpo, true);
    assert.equal(resultado.auth_removido, true);
    assert.equal(session.client, null);
    assert.equal(session.initialized, false);
    assert.equal(session.qrBootstrapAtivo, false);
    assert.equal(session.fase, 'falha_reset');
    assert.equal(session.motivoFalhaReset, MOTIVO_FALHA_RESET.QR_NAO_GERADO);

    let atual = session;
    let inicializacoes = 0;
    const novaTentativa = await resetSessionForQr(session, {
        clearTimer: () => {},
        destroyRuntime: async (current) => {
            current.client = null;
            current.initialized = false;
        },
        cleanupProfile: async () => true,
        purgeAuth: () => true,
        createState: freshState,
        replaceSession: (fresh) => { atual = fresh; },
        initialize: (fresh) => {
            inicializacoes += 1;
            fresh.initialized = true;
        },
    });

    assert.equal(novaTentativa.sucesso, true);
    assert.equal(inicializacoes, 1);
    assert.notEqual(atual, session);
    assert.equal(atual.qrBootstrapAtivo, true);
    assert.equal(atual.qrBootstrapAttempts, 1);
});

test('falha final do QR nao apaga auth enquanto Chromium continua vivo', async () => {
    const session = {
        ...sessionState(),
        qrBootstrapAtivo: true,
        qrBootstrapAttempts: 2,
    };
    let purges = 0;

    const resultado = await finalizeQrBootstrapFailure(session, {
        destroyRuntime: async (current) => {
            current.client = null;
            current.initialized = false;
        },
        cleanupProfile: async () => false,
        purgeAuth: () => {
            purges += 1;
            return true;
        },
        message: 'Não foi possível gerar o QR.',
    });

    assert.equal(purges, 0);
    assert.equal(resultado.runtime_limpo, false);
    assert.equal(resultado.auth_removido, false);
    assert.equal(session.fase, 'falha_reset');
    assert.equal(session.motivoFalhaReset, MOTIVO_FALHA_RESET.CHROMIUM_NAO_ENCERROU);
});

test('decidirRestauracao: pareado restaura, QR em preparo re-arma, logout ignora', () => {
    // Credencial pareada intacta -> restaura e reconecta.
    assert.equal(
        decidirRestauracao({ pareado: true, desabilitado: false, qrEmPreparo: false }),
        'restaurar',
    );
    // Novo QR interrompido por restart (sem .paired) -> re-arma o QR.
    assert.equal(
        decidirRestauracao({ pareado: false, desabilitado: false, qrEmPreparo: true }),
        'rearmar',
    );
    // Logout explicito vence qualquer marcador: nunca ressuscita sozinho.
    assert.equal(
        decidirRestauracao({ pareado: true, desabilitado: true, qrEmPreparo: true }),
        'ignorar',
    );
    assert.equal(
        decidirRestauracao({ pareado: false, desabilitado: true, qrEmPreparo: true }),
        'ignorar',
    );
    // Lixo sem credencial nem QR em voo -> ignora.
    assert.equal(
        decidirRestauracao({ pareado: false, desabilitado: false, qrEmPreparo: false }),
        'ignorar',
    );
});

test('resets concorrentes compartilham a mesma operacao e um unico Chromium', async () => {
    const session = sessionState();
    let liberarDestroy;
    const destroyBloqueado = new Promise((resolve) => { liberarDestroy = resolve; });
    let purges = 0;
    let inicializacoes = 0;

    const ops = {
        clearTimer: () => {},
        destroyRuntime: () => destroyBloqueado,
        purgeAuth: () => {
            purges += 1;
            return true;
        },
        createState: freshState,
        replaceSession: () => {},
        initialize: () => { inicializacoes += 1; },
    };

    const primeiro = resetSessionForQr(session, ops);
    const segundo = resetSessionForQr(session, ops);
    assert.equal(primeiro, segundo);

    liberarDestroy();
    const [a, b] = await Promise.all([primeiro, segundo]);

    assert.equal(a.sucesso, true);
    assert.equal(b.sucesso, true);
    assert.equal(purges, 1);
    assert.equal(inicializacoes, 1);
    assert.equal(session.resetPromise, null);
});
