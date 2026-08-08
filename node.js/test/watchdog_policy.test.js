'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { decisaoWatchdog, SONDAS_HTTP_PARA_MATAR, GRACA_BOOT_MS } = require('../watchdog_policy');

// Ate 08/08 o watchdog so media o heartbeat do pai: o /health ficou mudo por
// 20 minutos (TCP aceitava, HTTP nao respondia) e ninguem derrubou o processo.
// Estes casos fixam o contrato novo: graca no boot, heartbeat vencido mata,
// sondas HTTP seguidas falhando matam.

test('janela de graca do boot nao conta falha nenhuma', () => {
    // Nem heartbeat vencido nem sonda falhando contam antes da graca: o
    // servidor pode ainda nao ter subido e o boot pesado segura o event loop.
    assert.equal(decisaoWatchdog({
        msDesdeBoot: GRACA_BOOT_MS - 1,
        msSemHeartbeat: 999999,
        sondasHttpFalhasSeguidas: 99,
    }, { heartbeatTimeoutMs: 45000 }), 'esperar');
    // Exatamente no fim da graca, ja vale.
    assert.equal(decisaoWatchdog({
        msDesdeBoot: GRACA_BOOT_MS,
        msSemHeartbeat: 999999,
        sondasHttpFalhasSeguidas: 0,
    }, { heartbeatTimeoutMs: 45000 }), 'matar');
});

test('heartbeat vencido mata, mesmo com o HTTP saudavel', () => {
    assert.equal(decisaoWatchdog({
        msDesdeBoot: GRACA_BOOT_MS + 1000,
        msSemHeartbeat: 45000,
        sondasHttpFalhasSeguidas: 0,
    }, { heartbeatTimeoutMs: 45000 }), 'matar');
    // Um milissegundo antes do vencimento, espera.
    assert.equal(decisaoWatchdog({
        msDesdeBoot: GRACA_BOOT_MS + 1000,
        msSemHeartbeat: 44999,
        sondasHttpFalhasSeguidas: 0,
    }, { heartbeatTimeoutMs: 45000 }), 'esperar');
});

test('o caso do incidente: heartbeat em dia, HTTP mudo', () => {
    // Era exatamente este o estado as 03:20-03:39: o heartbeat corria e o
    // /health nao respondia. O watchdog antigo nao fazia nada; o novo mata na
    // N-esima sonda seguida falhando.
    for (let falhas = 1; falhas < SONDAS_HTTP_PARA_MATAR; falhas += 1) {
        assert.equal(decisaoWatchdog({
            msDesdeBoot: GRACA_BOOT_MS + 60000,
            msSemHeartbeat: 0,
            sondasHttpFalhasSeguidas: falhas,
        }, { heartbeatTimeoutMs: 45000 }), 'esperar', `${falhas} falha(s) ainda nao mata`);
    }
    assert.equal(decisaoWatchdog({
        msDesdeBoot: GRACA_BOOT_MS + 60000,
        msSemHeartbeat: 0,
        sondasHttpFalhasSeguidas: SONDAS_HTTP_PARA_MATAR,
    }, { heartbeatTimeoutMs: 45000 }), 'matar');
});

test('tudo sadio depois da graca: esperar', () => {
    assert.equal(decisaoWatchdog({
        msDesdeBoot: GRACA_BOOT_MS + 60000,
        msSemHeartbeat: 1000,
        sondasHttpFalhasSeguidas: 0,
    }, { heartbeatTimeoutMs: 45000 }), 'esperar');
});

test('entradas faltantes degradam para esperar, nunca para matar', () => {
    assert.equal(decisaoWatchdog({}, { heartbeatTimeoutMs: 45000 }), 'esperar');
    assert.equal(decisaoWatchdog({
        msDesdeBoot: undefined, msSemHeartbeat: undefined, sondasHttpFalhasSeguidas: undefined,
    }, { heartbeatTimeoutMs: 45000 }), 'esperar');
});

test('limites configuraveis valem para os dois sinais', () => {
    assert.equal(decisaoWatchdog({
        msDesdeBoot: 10000, msSemHeartbeat: 0, sondasHttpFalhasSeguidas: 1,
    }, { gracaMs: 5000, heartbeatTimeoutMs: 45000, sondasParaMatar: 1 }), 'matar');
    assert.equal(decisaoWatchdog({
        msDesdeBoot: 4999, msSemHeartbeat: 0, sondasHttpFalhasSeguidas: 99,
    }, { gracaMs: 5000, heartbeatTimeoutMs: 45000, sondasParaMatar: 1 }), 'esperar');
});
