'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
    timeoutPreflight, mensagemPreflight, registrarStoreIndisponivel,
    mensagemEstabilizacao, deveReciclarTimeoutPreflight, iniciarRecuperacaoPreflight,
    marcarStorePronto, deveReciclarStoreIndisponivel, STORE_INDISPONIVEL_RECYCLE_MS,
} = require('../preflight_recovery');

test('timeouts antes do envio pedem recuperação da sessão', () => {
    assert.equal(timeoutPreflight('getState', new Error('getState timeout')), true);
    assert.equal(timeoutPreflight('verificar_grupo', new Error('inspecionarGrupo timeout')), true);
    assert.match(mensagemPreflight('getState'), /recuperada automaticamente/i);
});

test('timeout depois de iniciar o envio não usa a recuperação pré-envio', () => {
    assert.equal(timeoutPreflight('sendMessage', new Error('sendMessage timeout')), false);
    assert.equal(timeoutPreflight('getState', new Error('outro erro')), false);
});

test('store ainda hidratando mantém a sessão conectada e não agenda recycle', () => {
    const client = {};
    const session = {
        isConnected: true,
        client,
        fase: 'conectado',
        faseMsg: 'Conectado.',
    };

    const mensagem = registrarStoreIndisponivel(session);

    assert.equal(session.isConnected, true);
    assert.equal(session.client, client);
    assert.equal(session.fase, 'conectado');
    assert.match(session.faseMsg, /preparando a sessão/i);
    assert.match(mensagem, /preparando a sessão/i);
    assert.doesNotMatch(mensagem, /qr|recuperad/i);
});

test('timeout durante preparação ou estabilização não recicla a sessão', () => {
    assert.equal(deveReciclarTimeoutPreflight({ preparando: true }, 100), false);
    assert.equal(deveReciclarTimeoutPreflight({ estabilizandoAte: 101 }, 100), false);
    assert.equal(deveReciclarTimeoutPreflight({ estabilizandoAte: 100 }, 100), true);
    assert.match(mensagemEstabilizacao(), /estabilizando a sessão/i);
});

test('timeout pré-envio responde de forma amigável, recicla uma vez e não envia mensagem', async () => {
    let envios = 0;
    const session = {
        isConnected: true,
        client: { sendMessage: () => { envios += 1; } },
    };
    const jobs = [];
    const reciclar = async () => { reciclar.chamadas += 1; };
    reciclar.chamadas = 0;
    const agendar = (fn) => { jobs.push(fn); return { unref() {} }; };

    assert.equal(iniciarRecuperacaoPreflight(session, 'getState', reciclar, agendar), true);
    assert.equal(iniciarRecuperacaoPreflight(session, 'getState', reciclar, agendar), false);
    assert.equal(session.isConnected, false);
    assert.equal(session.fase, 'reconectando');
    assert.match(mensagemPreflight('getState'), /aguarde alguns segundos/i);
    assert.equal(envios, 0);
    assert.equal(jobs.length, 1);
    jobs[0]();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(reciclar.chamadas, 1);
});

// Incidente 02/09/2026: sessão pareada às 16:09 com store_pronto=true; às 23:28
// todo envio falhava em `verificar_store` e a sessão seguia anunciando
// "conectado". O supervisor só vigia o HTTP do worker — que respondia. Sete
// horas de entrega bloqueada em silêncio, com 2 mil cupons prontos na fila.
test('store ausente por segundos é hidratação: não recicla', () => {
    const agora = Date.now();
    const session = { isConnected: true };
    registrarStoreIndisponivel(session, agora);
    assert.equal(deveReciclarStoreIndisponivel(session, agora + 5000), false);
});

test('store ausente além do teto recicla: bundle do WA Web levou os módulos', () => {
    const agora = Date.now();
    const session = { isConnected: true };
    registrarStoreIndisponivel(session, agora);
    assert.equal(
        deveReciclarStoreIndisponivel(session, agora + STORE_INDISPONIVEL_RECYCLE_MS + 1),
        true,
    );
});

test('a janela de pareamento nunca recicla — reciclar ali custa um QR novo', () => {
    const agora = Date.now();
    const preparando = { isConnected: true, preparando: true };
    registrarStoreIndisponivel(preparando, agora);
    assert.equal(
        deveReciclarStoreIndisponivel(preparando, agora + STORE_INDISPONIVEL_RECYCLE_MS + 1),
        false,
    );
    const estabilizando = {
        isConnected: true,
        estabilizandoAte: agora + STORE_INDISPONIVEL_RECYCLE_MS + 60000,
    };
    registrarStoreIndisponivel(estabilizando, agora);
    assert.equal(
        deveReciclarStoreIndisponivel(estabilizando, agora + STORE_INDISPONIVEL_RECYCLE_MS + 1),
        false,
    );
});

test('store que volta zera o relógio: só ausência CONTÍNUA escala', () => {
    const agora = Date.now();
    const session = { isConnected: true };
    registrarStoreIndisponivel(session, agora);
    marcarStorePronto(session);
    assert.equal(session.storeIndisponivelDesde, null);
    registrarStoreIndisponivel(session, agora + 100000);
    assert.equal(
        deveReciclarStoreIndisponivel(session, agora + 150000), false,
        'a contagem tem de reiniciar da segunda ausência, não da primeira',
    );
});

test('sessão desconectada não recicla por store: quem trata é o fluxo de reconexão', () => {
    const agora = Date.now();
    const session = { isConnected: false };
    registrarStoreIndisponivel(session, agora);
    assert.equal(
        deveReciclarStoreIndisponivel(session, agora + STORE_INDISPONIVEL_RECYCLE_MS + 1),
        false,
    );
});
