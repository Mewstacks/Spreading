'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
    criarPrazo, restante, expirou, timeoutDaEtapa, timeoutDePreflight, timeoutComEnvioIniciado,
} = require('../send_deadline');

test('o prazo total limita cada etapa ao tempo que ainda resta', () => {
    const prazo = criarPrazo(55000, 1000);
    assert.equal(timeoutDaEtapa(prazo, 60000, 1000), 55000);
    assert.equal(timeoutDaEtapa(prazo, 15000, 45000), 11000);
    assert.equal(restante(prazo, 56000), 0);
    assert.equal(expirou(prazo, 56000), true);
});

test('timeout depois de iniciar sendMessage é resultado incerto, não retry cego', () => {
    const prazo = criarPrazo(55000, 1000);
    assert.equal(timeoutComEnvioIniciado(true, 'sendMessage', new Error('sendMessage timeout'), prazo, 2000), true);
    assert.equal(timeoutComEnvioIniciado(false, 'sendMessage', new Error('sendMessage timeout'), prazo, 2000), false);
    assert.equal(timeoutComEnvioIniciado(true, 'verificar_grupo', new Error('timeout'), prazo, 2000), false);
});

test('o preflight cede um piso de tempo ao sendMessage', () => {
    const prazo = criarPrazo(55000, 1000);
    // Cenario que quebrava: getState(10s) + store(8s) + grupo(15s) podiam comer
    // 33s dos 55s e o envio herdava as sobras. Com reserva de 30s, o preflight
    // inteiro fica limitado a 25s e o sendMessage sempre alcanca os 30s.
    assert.equal(timeoutDePreflight(prazo, 10000, 30000, 1000), 10000);
    assert.equal(timeoutDePreflight(prazo, 15000, 30000, 11000), 15000);
    // Conforme o preflight gasta, o teto encolhe — mas so ate a reserva, nunca
    // alem dela: aos 21s corridos restam 35s, dos quais 30s sao do envio.
    assert.equal(timeoutDePreflight(prazo, 15000, 30000, 21000), 5000);
    // Faltando 30s ou menos, o preflight nao pode mais consumir nada.
    assert.equal(timeoutDePreflight(prazo, 15000, 30000, 26000), 0);
    assert.equal(timeoutDePreflight(prazo, 15000, 30000, 40000), 0);
    // Reserva negativa/ausente degrada para o comportamento antigo.
    assert.equal(timeoutDePreflight(prazo, 15000, 0, 1000), 15000);
});
