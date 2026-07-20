'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { resetSessionForQr } = require('../session_reset');

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
    assert.equal(session.qrBootstrapTimer, null);
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
