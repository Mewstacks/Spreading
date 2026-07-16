'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
    criarPrazo, restante, expirou, timeoutDaEtapa, timeoutComEnvioIniciado,
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
