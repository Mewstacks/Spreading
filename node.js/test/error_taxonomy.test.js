'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
    TRANSITORIO, PERMANENTE, DESCONHECIDO, erroClassificado, classificarErro,
    erroStoreQuebrado,
} = require('../error_taxonomy');

test('a classe anexada no throw sobrevive ate o catch', () => {
    assert.equal(classificarErro(erroClassificado('grupo sumiu', PERMANENTE)), PERMANENTE);
    assert.equal(classificarErro(erroClassificado('pagina hidratando', TRANSITORIO)), TRANSITORIO);
});

// A distincao que motivou o modulo: 'nao consegui verificar o grupo' e 'o grupo
// nao existe' chegavam ao Django como a mesma falha, e cinco paginas hidratando
// desligavam a automacao de quem nao tinha problema nenhum.
test('falha ao LER o grupo e transitoria; grupo AUSENTE e permanente', () => {
    const naoLeu = erroClassificado(
        'Nao foi possivel verificar o grupo de destino: WAWebCollections sem .Chat.get',
        TRANSITORIO
    );
    const naoExiste = erroClassificado(
        'Grupo de destino nao encontrado nesta conta do WhatsApp.', PERMANENTE
    );
    assert.equal(classificarErro(naoLeu), TRANSITORIO);
    assert.equal(classificarErro(naoExiste), PERMANENTE);
});

test('erroClassificado nao aceita classe inventada', () => {
    assert.equal(erroClassificado('x', 'talvez').classe, DESCONHECIDO);
    assert.equal(erroClassificado('x').classe, DESCONHECIDO);
});

// withTimeout lanca `${label} timeout`. Sao operacoes do Chromium: a conta do
// usuario esta intacta, so a pagina nao respondeu a tempo.
test('timeout do withTimeout e transitorio, venha de onde vier', () => {
    for (const label of ['getState', 'inspecionarGrupo', 'sendMessage']) {
        assert.equal(classificarErro(new Error(`${label} timeout`)), TRANSITORIO);
    }
});

test('frame destacado e transitorio', () => {
    assert.equal(classificarErro(new Error('Execution context was destroyed: detached Frame')),
        TRANSITORIO);
});

// O throw que originou tudo isto: o bundle minificado do WA Web lanca objetos
// que nao sao Error, com message "r". Nao da para afirmar nada sobre ele — e
// 'desconhecido' conta falha, que era o comportamento antes desta taxonomia.
test('o throw minificado do WA Web e desconhecido, nunca um chute', () => {
    assert.equal(classificarErro({ message: 'r' }), DESCONHECIDO);
    assert.equal(classificarErro('r'), DESCONHECIDO);
    assert.equal(classificarErro(new Error('r')), DESCONHECIDO);
});

// O incidente real: o bundle do WA Web recarregou, window.WWebJS sumiu e o
// sendMessage quebrou resolvendo o destino (Client.js:1533). Nunca chegou a
// enviar, entao e transitorio e retentavel — nao pode contar falha da config.
test('store desmontado (getChat undefined) e detectado e transitorio', () => {
    const incidente = new TypeError(
        "Cannot read properties of undefined (reading 'getChat')"
    );
    assert.equal(erroStoreQuebrado(incidente), true);
    assert.equal(classificarErro(incidente), TRANSITORIO);

    // Variantes do mesmo colapso de Store que o evaluate pode devolver.
    assert.equal(erroStoreQuebrado(new Error('WWebJS is not defined')), true);
    assert.equal(erroStoreQuebrado(new ReferenceError('Store is not defined')), true);
    assert.equal(erroStoreQuebrado(
        new Error('window.Store voltou undefined apos reload')), true);
});

test('erroStoreQuebrado nao dispara em erros comuns nem lanca em entrada hostil', () => {
    assert.equal(erroStoreQuebrado(new Error('sendMessage timeout')), false);
    assert.equal(erroStoreQuebrado(new Error("Cannot read properties of undefined (reading 'foo')")), false);
    assert.equal(erroStoreQuebrado({ message: 'r' }), false);
    assert.equal(erroStoreQuebrado(undefined), false);
    const hostil = { get message() { throw new Error('boom'); } };
    assert.equal(erroStoreQuebrado(hostil), false);
});

// CONTRATO: roda dentro do catch do envio. Lancar aqui trocaria uma falha
// classificavel por um 500 e o Django perderia a `classe`.
test('classificarErro nunca lanca, seja qual for a entrada', () => {
    const hostil = { get message() { throw new Error('boom'); } };
    const ciclico = {};
    ciclico.self = ciclico;
    for (const entrada of [undefined, null, 0, '', [], ciclico, hostil, Symbol('x')]) {
        assert.equal(classificarErro(entrada), DESCONHECIDO);
    }
});
