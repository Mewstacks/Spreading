'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
    reconnectDelay,
    shouldPurgeAuth,
    reconnectOutcome,
    reconnectAction,
    deveReviverRecuperacaoPausada,
    qrBootstrapOutcome,
    preAuthEventIsStale,
    isRevokedReason,
    classificarEstadoWa,
    estadoIndicaQueda,
    keepaliveIndicaQueda,
    deveReciclarAposTimeoutDeEnvio,
    veredictoDeTimeoutDeEnvio,
    STALLS_ATE_RECICLAR,
    KEEPALIVE_FALHAS_ATE_QUEDA,
    syncGroupsOutcome,
    groupRetryDelay,
    syncPollDelay,
    ocupaSlot,
} = require('../session_policy');

test('reconnect uses bounded exponential backoff', () => {
    assert.equal(reconnectDelay(1, 5000, 60000), 5000);
    assert.equal(reconnectDelay(2, 5000, 60000), 10000);
    assert.equal(reconnectDelay(5, 5000, 60000), 60000);
    assert.equal(reconnectDelay(20, 5000, 60000), 60000);
});

test('authenticated corruption is purged earlier than pre-auth failure', () => {
    assert.equal(shouldPurgeAuth(1, true), false);
    assert.equal(shouldPurgeAuth(2, true), true);
    assert.equal(shouldPurgeAuth(2, false), false);
    assert.equal(shouldPurgeAuth(3, false), true);
});

test('reconnectOutcome retries up to the cap, then purges, then expires', () => {
    for (let i = 1; i <= 6; i += 1) {
        assert.equal(reconnectOutcome(i, 0, 6), 'retry', `tentativa ${i}`);
    }
    assert.equal(reconnectOutcome(7, 0, 6), 'purge');
    assert.equal(reconnectOutcome(7, 1, 6), 'expire');
    assert.equal(reconnectOutcome(99, 1, 6), 'expire');
});

test('reconnectOutcome honours a custom cap', () => {
    assert.equal(reconnectOutcome(10, 0, 10), 'retry');
    assert.equal(reconnectOutcome(11, 0, 10), 'purge');
});

test('paired credential pauses instead of being purged after retries', () => {
    assert.equal(reconnectAction(7, 0, true, 6), 'pause');
    assert.equal(reconnectAction(7, 0, false, 6), 'purge');
    assert.equal(reconnectAction(7, 1, true, 6), 'expire');
});

test('paused paired session revives only after the external cooldown', () => {
    const now = Date.parse('2026-09-01T18:00:00.000Z');
    const fifteenMinutes = 15 * 60 * 1000;
    assert.equal(deveReviverRecuperacaoPausada(
        'recuperacao_pausada', true, '2026-09-01T17:44:59.000Z', now, fifteenMinutes,
    ), true);
    assert.equal(deveReviverRecuperacaoPausada(
        'recuperacao_pausada', true, '2026-09-01T17:50:00.000Z', now, fifteenMinutes,
    ), false);
    assert.equal(deveReviverRecuperacaoPausada(
        'expirado', true, '2026-09-01T16:00:00.000Z', now, fifteenMinutes,
    ), false);
    assert.equal(deveReviverRecuperacaoPausada(
        'recuperacao_pausada', false, '2026-09-01T16:00:00.000Z', now, fifteenMinutes,
    ), false);
    assert.equal(deveReviverRecuperacaoPausada(
        'recuperacao_pausada', true, '', now, fifteenMinutes,
    ), false);
});

test('novo QR retenta ate o teto e nunca escolhe a escada de reconnect', () => {
    for (const motivo of [
        'timeout em inicializacao',
        'Navigating frame was detached',
        'Execution context was destroyed',
    ]) {
        // Teto atual = 3: duas primeiras tentativas retentam, a terceira encerra.
        assert.equal(qrBootstrapOutcome(1, 3), 'retry', motivo);
        assert.equal(qrBootstrapOutcome(2, 3), 'retry', motivo);
        assert.equal(qrBootstrapOutcome(3, 3), 'fail', motivo);
        assert.notEqual(qrBootstrapOutcome(1, 3), 'reconnect', motivo);
    }
});

test('QR e loading tardios nao rebaixam uma sessao que ja passou da autenticacao', () => {
    assert.equal(preAuthEventIsStale({
        fase: 'reiniciando_qr',
        qrBootstrapAtivo: true,
        authenticatedInAttempt: false,
        readyReceived: false,
        preparando: false,
        isConnected: false,
    }), false, 'antes da autenticacao, loading e QR ainda sao validos');

    for (const state of [
        { fase: 'reiniciando_qr', authenticatedInAttempt: true },
        { fase: 'carregando', readyReceived: true },
        { fase: 'preparando', preparando: true },
        { fase: 'sincronizando' },
        { fase: 'conectado', isConnected: true },
    ]) {
        assert.equal(preAuthEventIsStale(state), true, JSON.stringify(state));
    }

    assert.equal(preAuthEventIsStale({
        fase: 'desconectado',
        authenticatedInAttempt: false,
        readyReceived: false,
        preparando: false,
        isConnected: false,
    }), false, 'depois de uma desconexao real, eventos pre-auth voltam a valer');
});

// Regressao do bug em producao: 'Recuperando sessao (tentativa 38)...'.
// A recuperacao tem de terminar; nenhum ciclo pode passar do teto.
test('recovery always terminates instead of looping forever', () => {
    const outcomes = [];
    let attempts = 0;
    let purges = 0;

    for (let tick = 0; tick < 100; tick += 1) {
        attempts += 1;
        const outcome = reconnectOutcome(attempts, purges, 6);
        outcomes.push(outcome);
        assert.ok(attempts <= 7, `contador estourou o teto: tentativa ${attempts}`);
        if (outcome === 'purge') {
            purges += 1;
            attempts = 1;
        }
        if (outcome === 'expire') break;
    }

    assert.equal(outcomes.at(-1), 'expire');
    assert.equal(outcomes.filter((o) => o === 'purge').length, 1);
    // retry*6, purge, retry*5, expire. O tick da purga ja e a tentativa 1 do
    // ciclo novo, por isso o segundo ciclo tem 5 retries e nao 6.
    assert.equal(outcomes.length, 13);
});

test('only unambiguous revocations purge the stored credential', () => {
    assert.equal(isRevokedReason('LOGOUT'), true);
    assert.equal(isRevokedReason('logout'), true);
    assert.equal(isRevokedReason(' UNPAIRED '), true);
    assert.equal(isRevokedReason('UNPAIRED_IDLE'), true);

    // Transitorios: NAO purgam. Apagar o auth aqui forcaria um QR novo por uma
    // queda de rede ou um reload de pagina. A escada shouldPurgeAuth ja cobre
    // corrupcao real. Nao "conserte" isto para true.
    assert.equal(isRevokedReason('NAVIGATION'), false);
    assert.equal(isRevokedReason('CONFLICT'), false);
    assert.equal(isRevokedReason(''), false);
    assert.equal(isRevokedReason(undefined), false);
    assert.equal(isRevokedReason(null), false);
});

// Regressao: uma sessao expirada fica no Map (para preservar a mensagem
// "Sessão expirada. Leia o QR novamente."), mas nao segura Chromium. Se ela
// contar para o limite, 4 sessoes mortas trancam o servico inteiro ocioso.
test('only sessions holding a Chromium count against the cap', () => {
    assert.equal(ocupaSlot({ client: {}, initialized: true }), true, 'conectada');
    assert.equal(ocupaSlot({ client: {}, initialized: false }), true, 'iniciando');
    assert.equal(ocupaSlot({ client: null, initialized: false, reconnectTimer: 1 }), true,
        'reconectando: vai subir um Chromium quando o timer disparar');
    assert.equal(ocupaSlot({ client: null, initialized: false, qrBootstrapTimer: 1 }), true,
        'gerando QR: vai subir um Chromium quando o timer disparar');
    assert.equal(ocupaSlot({ client: null, initialized: false, registryRestoreTimer: 1 }), true,
        'registry validado: reserva capacidade enquanto aguarda o stagger');

    // Terminais: sem client, sem init, sem timer.
    assert.equal(ocupaSlot({ client: null, initialized: false, reconnectTimer: null }), false,
        'expirada nao pode bloquear um usuario novo');
    assert.equal(ocupaSlot({}), false);
});

test('sem sync em voo, qualquer pedido inicia uma leitura', () => {
    assert.equal(syncGroupsOutcome(false, false), 'iniciar');
    assert.equal(syncGroupsOutcome(false, true), 'iniciar');
});

// Regressao: o botao "Sincronizar grupos" devolvia o snapshot lido ANTES do
// clique. Quem criava um grupo no celular e clicava recebia sucesso e a lista
// velha. Pedido explicito durante um voo TEM de gerar leitura nova.
test('pedido explicito durante um voo repica; automatico so aproveita', () => {
    assert.equal(syncGroupsOutcome(true, true), 'repicar');
    assert.equal(syncGroupsOutcome(true, false), 'aguardar');
});

test('group retry backs off and eventually gives up', () => {
    assert.equal(groupRetryDelay(1), 5000);
    assert.equal(groupRetryDelay(2), 10000);
    assert.equal(groupRetryDelay(3), 20000);
    assert.equal(groupRetryDelay(5), 80000);
    assert.equal(groupRetryDelay(6), 120000); // satura no teto
    assert.equal(groupRetryDelay(7), null);   // esgotou: o botao assume
});

// O ponto do retry e cobrir a falha transitoria sem pedir clique nenhum. Se a
// janela fosse curta demais, a lista voltaria a depender do usuario.
test('group retry window spans minutes before giving up', () => {
    let total = 0;
    for (let i = 1; ; i += 1) {
        const delay = groupRetryDelay(i);
        if (delay === null) break;
        total += delay;
    }
    assert.ok(total >= 240000, `janela de retry curta demais: ${total}ms`);
});

test('sync polling backs off and eventually gives up', () => {
    assert.equal(syncPollDelay(1), 3000);
    assert.equal(syncPollDelay(2), 6000);
    assert.equal(syncPollDelay(3), 12000);
    assert.equal(syncPollDelay(4), 15000); // satura no teto
    assert.equal(syncPollDelay(8), 15000);
    assert.equal(syncPollDelay(9), null);  // esgotou: para de pollar
});

// A leitura de grupos tem GROUP_SYNC_TIMEOUT_MS=15s no worker. A janela de
// repoll precisa cobrir uma leitura lenta inteira, senao o front desiste antes
// do Node. Sobra folga para as duas primeiras tentativas do groupRetryDelay,
// entao a recuperacao tipica acontece com o usuario ainda olhando a tela.
test('sync polling window outlasts a slow group read', () => {
    let total = 0;
    for (let i = 1; ; i += 1) {
        const delay = syncPollDelay(i);
        if (delay === null) break;
        total += delay;
    }
    assert.ok(total >= 15000 + 5000 + 10000, `janela de repoll curta demais: ${total}ms`);
});


// ── classificarEstadoWa ────────────────────────────────────────────────────────
// O worker nao escutava 'change_state', e CONFLICT/UNPAIRED/TIMEOUT/UNLAUNCHED
// chegam SO por esse evento: a conexao caia em silencio e o primeiro sintoma era
// um envio falhando ("o canal de envio nao e mais valido"). Estes casos fixam
// quais estados exigem reconexao imediata e quais sao apenas a sessao subindo.
test('CONNECTED e o unico estado saudavel', () => {
    assert.equal(classificarEstadoWa('CONNECTED'), 'ok');
    assert.equal(classificarEstadoWa('connected'), 'ok'); // WAState vem maiusculo, mas nao dependemos disso
    assert.equal(estadoIndicaQueda('CONNECTED'), false);
});

test('OPENING e PAIRING sao transicao, nunca queda', () => {
    for (const estado of ['OPENING', 'PAIRING']) {
        assert.equal(classificarEstadoWa(estado), 'transicao', estado);
        // Reciclar aqui derrubaria um Chromium que ainda esta subindo.
        assert.equal(estadoIndicaQueda(estado), false, estado);
    }
});

test('os estados que morriam em silencio agora sao queda', () => {
    for (const estado of [
        'CONFLICT',            // o numero abriu WhatsApp Web em outro lugar
        'UNPAIRED', 'UNPAIRED_IDLE',
        'TIMEOUT',
        'UNLAUNCHED',
        'DEPRECATED_VERSION',
        'PROXYBLOCK', 'TOS_BLOCK', 'SMB_TOS_BLOCK',
    ]) {
        assert.equal(classificarEstadoWa(estado), 'queda', estado);
        assert.equal(estadoIndicaQueda(estado), true, estado);
    }
});

test('estado ausente nao e veredito: nao dispara reconexao', () => {
    // getState() devolve null/undefined enquanto a pagina nao tem WWebJS. Tratar
    // isso como queda faria o keepalive reciclar a sessao a cada boot.
    for (const vazio of [null, undefined, '']) {
        assert.equal(classificarEstadoWa(vazio), 'indefinido');
        assert.equal(estadoIndicaQueda(vazio), false);
    }
});

// CONFLICT precisa ser queda justamente porque takeoverOnConflict nasce
// desligado (WA_TAKEOVER_ON_CONFLICT): sem takeover o socket nao volta sozinho,
// e sem reconexao a sessao ficaria "conectada" para sempre sem poder enviar.
test('CONFLICT nao e purgavel, mas e queda', () => {
    assert.equal(estadoIndicaQueda('CONFLICT'), true);
    assert.equal(isRevokedReason('CONFLICT'), false);
});

test('keepalive so declara queda depois de falhas seguidas', () => {
    // Uma leitura perdida e rotina (pagina ocupada com sync); repeticao e veredito.
    assert.equal(keepaliveIndicaQueda(1), false);
    assert.equal(keepaliveIndicaQueda(2), false);
    assert.equal(keepaliveIndicaQueda(KEEPALIVE_FALHAS_ATE_QUEDA), true);
    assert.equal(keepaliveIndicaQueda(KEEPALIVE_FALHAS_ATE_QUEDA + 1), true);
    assert.equal(keepaliveIndicaQueda(0), false);
});

test('stall_no_upload segura a sessao: pagina viva nao se conserta reciclando', () => {
    // O veredito significa que a sonda respondeu — o Chromium esta bem e quem
    // travou foi o upload. Ate 08/08 o codigo reciclava mesmo assim, e cada
    // envio lento derrubava um Chromium saudavel em plena falta de CPU.
    assert.equal(deveReciclarAposTimeoutDeEnvio('stall_no_upload', 1), false);
    assert.equal(deveReciclarAposTimeoutDeEnvio('stall_no_upload', 2), false);
    // Pagina viva mas nenhuma midia sai nunca mais: a partir do 3o stall
    // seguido reciclar e a unica saida que resta.
    assert.equal(deveReciclarAposTimeoutDeEnvio('stall_no_upload', STALLS_ATE_RECICLAR), true);
    assert.equal(deveReciclarAposTimeoutDeEnvio('stall_no_upload', STALLS_ATE_RECICLAR + 1), true);
});

test('pagina travada ou veredito indeterminado reciclam na hora', () => {
    assert.equal(deveReciclarAposTimeoutDeEnvio('pagina_travada', 0), true);
    assert.equal(deveReciclarAposTimeoutDeEnvio('indeterminado', 0), true);
    assert.equal(deveReciclarAposTimeoutDeEnvio(undefined, 0), true);
});

test('veredictoDeTimeoutDeEnvio traduz a sonda de vivacidade', () => {
    assert.equal(veredictoDeTimeoutDeEnvio(true), 'stall_no_upload');
    assert.equal(veredictoDeTimeoutDeEnvio(false), 'pagina_travada');
    assert.equal(veredictoDeTimeoutDeEnvio(null), 'indeterminado');
    assert.equal(veredictoDeTimeoutDeEnvio(undefined), 'indeterminado');
});
