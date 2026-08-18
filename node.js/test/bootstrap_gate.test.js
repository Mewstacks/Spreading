'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { criarPortao } = require('../bootstrap_gate');

// Temporizador de mentira: nada de relogio real, o teto dispara quando o teste
// mandar. `agendar` devolve um id opaco, como o setTimeout de verdade.
const criarTemporizador = () => {
    const pendentes = new Map();
    let proximoId = 1;
    return {
        agendar: (fn, ms) => {
            const id = proximoId++;
            pendentes.set(id, { fn, ms });
            return id;
        },
        cancelar: (id) => { pendentes.delete(id); },
        disparar: () => {
            const [id, alvo] = [...pendentes.entries()][0] || [];
            if (!alvo) throw new Error('nenhum teto agendado');
            pendentes.delete(id);
            alvo.fn();
        },
        pendentes: () => pendentes.size,
    };
};

test('a primeira sessao entra direto e a segunda espera a vez', async () => {
    const t = criarTemporizador();
    const portao = criarPortao({ agendar: t.agendar, cancelar: t.cancelar });

    const liberarA = await portao.adquirir('A');
    let bEntrou = false;
    const pedidoB = portao.adquirir('B').then((liberar) => { bEntrou = true; return liberar; });

    await Promise.resolve();
    assert.equal(bEntrou, false, 'B nao pode subir Chromium enquanto A segura a vez');
    assert.deepEqual(portao.estado(), { dono: 'A', fila: ['B'] });

    liberarA();
    await pedidoB;
    assert.equal(bEntrou, true);
    assert.equal(portao.estado().dono, 'B');
});

test('a fila e atendida em ordem de chegada', async () => {
    const t = criarTemporizador();
    const portao = criarPortao({ agendar: t.agendar, cancelar: t.cancelar });
    const ordem = [];

    const liberarA = await portao.adquirir('A');
    const b = portao.adquirir('B').then((l) => { ordem.push('B'); return l; });
    const c = portao.adquirir('C').then((l) => { ordem.push('C'); return l; });

    liberarA();
    (await b)();
    (await c)();
    assert.deepEqual(ordem, ['B', 'C']);
});

test('liberar duas vezes nao adianta a vez de ninguem', async () => {
    const t = criarTemporizador();
    const portao = criarPortao({ agendar: t.agendar, cancelar: t.cancelar });

    const liberarA = await portao.adquirir('A');
    const b = portao.adquirir('B');
    const c = portao.adquirir('C');

    assert.equal(liberarA(), true);
    assert.equal(liberarA(), false, 'a segunda liberacao e no-op');
    await b;
    // C continua na fila: o duplo liberar de A nao pode ter puxado os dois.
    assert.deepEqual(portao.estado(), { dono: 'B', fila: ['C'] });
    void c;
});

test('o teto devolve a vez quando o dono trava sem liberar', async () => {
    const t = criarTemporizador();
    const estourados = [];
    const portao = criarPortao({
        agendar: t.agendar,
        cancelar: t.cancelar,
        aoEstourarTeto: (id) => estourados.push(id),
    });

    await portao.adquirir('A');           // A trava: nunca chama liberar
    let bEntrou = false;
    const b = portao.adquirir('B').then((l) => { bEntrou = true; return l; });

    assert.equal(bEntrou, false);
    t.disparar();                          // teto de A
    await b;
    assert.deepEqual(estourados, ['A']);
    assert.equal(portao.estado().dono, 'B');
});

test('o teto do dono e cancelado quando ele libera na hora certa', async () => {
    const t = criarTemporizador();
    const portao = criarPortao({ agendar: t.agendar, cancelar: t.cancelar });

    const liberarA = await portao.adquirir('A');
    assert.equal(t.pendentes(), 1);
    liberarA();
    // Sem ninguem na fila, nao pode sobrar teto armado para disparar depois.
    assert.equal(t.pendentes(), 0);
    assert.deepEqual(portao.estado(), { dono: null, fila: [] });
});

test('portao vazio nao segura ninguem', async () => {
    const t = criarTemporizador();
    const portao = criarPortao({ agendar: t.agendar, cancelar: t.cancelar });
    const liberar = await portao.adquirir('A');   // resolve sem esperar nada
    assert.equal(typeof liberar, 'function');
});
