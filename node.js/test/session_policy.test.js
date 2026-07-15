'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
    reconnectDelay,
    shouldPurgeAuth,
    reconnectOutcome,
    isRevokedReason,
    syncGroupsOutcome,
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

test('sync polling backs off and eventually gives up', () => {
    assert.equal(syncPollDelay(1), 3000);
    assert.equal(syncPollDelay(2), 6000);
    assert.equal(syncPollDelay(3), 12000);
    assert.equal(syncPollDelay(4), 15000); // satura no teto
    assert.equal(syncPollDelay(8), 15000);
    assert.equal(syncPollDelay(9), null);  // esgotou: para de pollar
});

// getChats tem GROUP_SYNC_TIMEOUT_MS=45s no worker. A janela de repoll precisa
// cobrir um getChats lento inteiro, senao o front desiste antes do Node.
test('sync polling window outlasts a slow getChats', () => {
    let total = 0;
    for (let i = 1; ; i += 1) {
        const delay = syncPollDelay(i);
        if (delay === null) break;
        total += delay;
    }
    assert.ok(total >= 45000, `janela de repoll curta demais: ${total}ms`);
});
