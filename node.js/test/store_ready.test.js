'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { aguardarStorePronto } = require('../store_ready');

// Relogio de mentira controlado pelo `dormir` injetado: cada espera avanca o
// tempo em intervaloMs, entao o teste roda instantaneo e e deterministico.
const criarRelogio = (intervaloMs) => {
    let t = 0;
    const agora = () => t;
    const dormir = async (ms) => { t += ms; };
    // sanidade: o relogio so avanca pelos intervalos de polling
    void intervaloMs;
    return { agora, dormir };
};

test('retorna true assim que o store aparece (3a checagem)', async () => {
    let chamadas = 0;
    const { agora, dormir } = criarRelogio(500);

    const ok = await aguardarStorePronto({
        sondar: async () => { chamadas += 1; return chamadas >= 3; },
        tetoMs: 8000,
        intervaloMs: 500,
        agora,
        dormir,
    });

    assert.equal(ok, true);
    assert.equal(chamadas, 3, 'para de sondar assim que o store fica pronto');
});

test('retorna false quando o store nunca aparece dentro do orcamento', async () => {
    let chamadas = 0;
    const { agora, dormir } = criarRelogio(500);

    const ok = await aguardarStorePronto({
        sondar: async () => { chamadas += 1; return false; },
        tetoMs: 2000,        // 2000/500 => no maximo ~4 janelas
        intervaloMs: 500,
        agora,
        dormir,
    });

    assert.equal(ok, false);
    // Nao pode ficar em loop infinito: com teto 2000ms e intervalo 500ms,
    // sondamos poucas vezes e desistimos sem estourar o orcamento.
    assert.ok(chamadas >= 1 && chamadas <= 5, `checagens limitadas (foram ${chamadas})`);
});

test('expirou() do prazo global aborta na hora, mesmo com orcamento sobrando', async () => {
    let chamadas = 0;

    const ok = await aguardarStorePronto({
        sondar: async () => { chamadas += 1; return false; },
        tetoMs: 60000,          // orcamento proprio enorme...
        intervaloMs: 500,
        expirou: () => true,    // ...mas o prazo da request ja estourou
        agora: () => 0,
        dormir: async () => {},
    });

    assert.equal(ok, false);
    assert.equal(chamadas, 1, 'checa uma vez e para porque o prazo global expirou');
});

test('nao dorme depois de decidir — a ultima janela nao ultrapassa o teto', async () => {
    const esperas = [];
    let t = 0;

    const ok = await aguardarStorePronto({
        sondar: async () => false,
        tetoMs: 1000,
        intervaloMs: 500,
        agora: () => t,
        dormir: async (ms) => { esperas.push(ms); t += ms; },
    });

    assert.equal(ok, false);
    // Com teto 1000 e intervalo 500: sonda em t=0 (agenda 500), t=500
    // (500+500 >= 1000 -> desiste). So uma espera de 500ms aconteceu.
    assert.deepEqual(esperas, [500]);
});
