'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { iniciarSync } = require('../group_sync');

const novoEstado = () => ({
    groupSyncPromise: null,
    gruposSincronizando: false,
    syncPedidoDurante: false,
});

// Leitor de mentira com controle manual: `liberar()` resolve a leitura em voo.
// Conta as leituras e vigia a invariante central — nunca duas ao mesmo tempo,
// porque na vida real sao dois getChats no mesmo Chromium.
const criarLeitor = () => {
    const leitor = { chamadas: [], ativos: 0, maxAtivos: 0, pendentes: [] };
    leitor.ler = (reason) => {
        leitor.chamadas.push(reason);
        leitor.ativos += 1;
        leitor.maxAtivos = Math.max(leitor.maxAtivos, leitor.ativos);
        return new Promise((resolve) => {
            leitor.pendentes.push((valor) => {
                leitor.ativos -= 1;
                resolve(valor);
            });
        });
    };
    leitor.liberar = (valor = true) => {
        const proximo = leitor.pendentes.shift();
        assert.ok(proximo, 'nada em voo para liberar');
        proximo(valor);
    };
    return leitor;
};

test('sem sync em voo, le uma vez e limpa o estado ao terminar', async () => {
    const estado = novoEstado();
    const leitor = criarLeitor();

    const p = iniciarSync(estado, leitor.ler, 'ready');
    assert.equal(estado.gruposSincronizando, true);
    assert.ok(estado.groupSyncPromise);

    leitor.liberar(true);
    assert.equal(await p, true);

    assert.deepEqual(leitor.chamadas, ['ready']);
    assert.equal(estado.gruposSincronizando, false);
    assert.equal(estado.groupSyncPromise, null);
});

test('pedido automatico durante um voo aproveita a leitura em curso', async () => {
    const estado = novoEstado();
    const leitor = criarLeitor();

    const p1 = iniciarSync(estado, leitor.ler, 'ready');
    const p2 = iniciarSync(estado, leitor.ler, 'api-grupos');
    assert.equal(p1, p2, 'deveria reaproveitar a mesma promise');

    leitor.liberar(true);
    assert.equal(await p1, true);
    assert.deepEqual(leitor.chamadas, ['ready'], 'automatico nao pode reler');
});

// A regressao. O botao "Sincronizar grupos" devolvia o snapshot lido ANTES do
// clique: quem criava um grupo no celular e clicava recebia sucesso e a lista
// velha. O pedido explicito TEM de gerar uma leitura posterior ao clique.
test('pedido explicito durante um voo forca uma leitura nova, depois da atual', async () => {
    const estado = novoEstado();
    const leitor = criarLeitor();

    const p = iniciarSync(estado, leitor.ler, 'ready');
    iniciarSync(estado, leitor.ler, 'refresh-manual', { forcar: true });

    assert.deepEqual(leitor.chamadas, ['ready'], 'nao pode abrir getChats paralelo');

    leitor.liberar('voo-1');                       // a leitura pre-clique termina
    await new Promise((r) => setImmediate(r));     // deixa o repique arrancar

    assert.deepEqual(leitor.chamadas, ['ready', 'ready-repique']);
    assert.equal(estado.gruposSincronizando, true, 'repique ainda e sincronizacao');
    assert.ok(estado.groupSyncPromise, 'promise nao pode zerar entre as leituras');

    leitor.liberar('voo-2');
    assert.equal(await p, 'voo-2', 'o resultado tem de ser o do repique');
    assert.equal(leitor.maxAtivos, 1, 'jamais dois getChats no mesmo Chromium');
    assert.equal(estado.gruposSincronizando, false);
    assert.equal(estado.groupSyncPromise, null);
});

test('N cliques durante um voo coalescem em UM repique', async () => {
    const estado = novoEstado();
    const leitor = criarLeitor();

    const p = iniciarSync(estado, leitor.ler, 'ready');
    for (let i = 0; i < 5; i += 1) {
        iniciarSync(estado, leitor.ler, 'refresh-manual', { forcar: true });
    }

    leitor.liberar(true);
    await new Promise((r) => setImmediate(r));
    leitor.liberar(true);
    await p;

    assert.deepEqual(leitor.chamadas, ['ready', 'ready-repique'],
        '5 cliques nao podem virar 5 getChats');
    assert.equal(leitor.maxAtivos, 1);
});

test('clique DEPOIS do voo terminar comeca uma leitura nova', async () => {
    const estado = novoEstado();
    const leitor = criarLeitor();

    const p1 = iniciarSync(estado, leitor.ler, 'ready');
    leitor.liberar(true);
    await p1;

    const p2 = iniciarSync(estado, leitor.ler, 'refresh-manual', { forcar: true });
    leitor.liberar(true);
    await p2;

    assert.deepEqual(leitor.chamadas, ['ready', 'refresh-manual']);
    assert.equal(leitor.maxAtivos, 1);
});

// Documenta o contrato: `ler` nunca deveria lancar (as rotas nao dao await, entao
// seria unhandled rejection). Se lancar mesmo assim, o estado nao pode ficar
// preso em 'sincronizando' para sempre — isso travaria a sessao ate o restart.
test('leitor que lanca nao deixa a sessao presa em sincronizando', async () => {
    const estado = novoEstado();
    const p = iniciarSync(estado, async () => { throw new Error('boom'); }, 'ready');

    await assert.rejects(p, /boom/);
    assert.equal(estado.gruposSincronizando, false);
    assert.equal(estado.groupSyncPromise, null);
});
