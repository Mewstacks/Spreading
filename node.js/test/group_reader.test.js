'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { coletarGrupos, descreverErro } = require('../group_reader');

// window de mentira: coletarGrupos so precisa de `require('WAWebCollections')`,
// e por isso roda em Node sem Chromium nenhum.
const criarWindow = (chats, { moduloErro = null, colecoes = null } = {}) => ({
    require: (nome) => {
        if (moduloErro) throw moduloErro;
        if (nome !== 'WAWebCollections') throw new Error(`modulo inesperado: ${nome}`);
        return colecoes || { Chat: { getModelsArray: () => chats } };
    },
});

const grupo = (numero, nome) => ({
    id: { _serialized: `${numero}@g.us`, server: 'g.us' },
    formattedTitle: nome,
});

const contato = (numero, nome) => ({
    id: { _serialized: `${numero}@c.us`, server: 'c.us' },
    formattedTitle: nome,
});

test('devolve so os grupos, com nome vindo de formattedTitle', () => {
    const win = criarWindow([
        grupo('1203', 'Equipe'),
        contato('5511999', 'Fulano'),
        grupo('1204', 'Clientes'),
    ]);

    const res = coletarGrupos(win);

    assert.equal(res.ok, true);
    assert.equal(res.totalChats, 3);
    assert.deepEqual(res.grupos.map((g) => g.id), ['1203@g.us', '1204@g.us']);
    assert.deepEqual(res.grupos.map((g) => g.nome), ['Equipe', 'Clientes']);
    assert.deepEqual(res.ignorados, []);
});

// A regressao que originou tudo: com Promise.all(getChatModel), UM chat que
// lanca zerava a lista inteira e o usuario via "conectado, 0 grupos" para
// sempre. Os grupos saudaveis tem que passar.
test('um grupo que lanca nao derruba os outros', () => {
    const podre = {
        id: { _serialized: '1205@g.us', server: 'g.us' },
        get formattedTitle() { throw new Error('boom'); },
        get name() { throw new Error('boom'); },
        get subject() { throw new Error('boom'); },
        get groupMetadata() { throw new Error('boom'); },
    };
    const win = criarWindow([grupo('1203', 'Equipe'), podre, grupo('1204', 'Clientes')]);

    const res = coletarGrupos(win);

    assert.equal(res.ok, true);
    assert.deepEqual(res.grupos.map((g) => g.id), ['1203@g.us', '1205@g.us', '1204@g.us']);
    // Sobrevive sem nome legivel: cai no local-part do id e se declara.
    assert.equal(res.grupos[1].nome, '1205');
    assert.equal(res.grupos[1].nomeAusente, true);
});

test('grupo sem id._serialized e ignorado sem derrubar a lista', () => {
    const semId = { id: { server: 'g.us' }, formattedTitle: 'Sem id' };
    const win = criarWindow([grupo('1203', 'Equipe'), semId]);

    const res = coletarGrupos(win);

    assert.equal(res.ok, true);
    assert.deepEqual(res.grupos.map((g) => g.id), ['1203@g.us']);
    assert.deepEqual(res.ignorados, [{ id: null, erro: 'chat sem id._serialized' }]);
});

test('reconhece grupo por groupMetadata quando o server nao e g.us', () => {
    const comunidade = {
        id: { _serialized: '1206@lid', server: 'lid' },
        formattedTitle: 'Comunidade',
        groupMetadata: { subject: 'Comunidade' },
    };
    const win = criarWindow([comunidade, contato('5511999', 'Fulano')]);

    const res = coletarGrupos(win);

    assert.deepEqual(res.grupos, [{ id: '1206@lid', nome: 'Comunidade', nomeAusente: false }]);
});

test('cai em groupMetadata.subject quando formattedTitle vem vazio', () => {
    const semTitulo = {
        id: { _serialized: '1207@g.us', server: 'g.us' },
        formattedTitle: '',
        groupMetadata: { subject: 'Financeiro' },
    };
    const win = criarWindow([semTitulo]);

    const res = coletarGrupos(win);

    assert.equal(res.grupos[0].nome, 'Financeiro');
    assert.equal(res.grupos[0].nomeAusente, false);
});

test('sem getModelsArray, usa a propriedade models', () => {
    const chats = [grupo('1203', 'Equipe')];
    const win = criarWindow(null, { colecoes: { Chat: { models: chats } } });

    const res = coletarGrupos(win);

    assert.equal(res.ok, true);
    assert.deepEqual(res.grupos.map((g) => g.nome), ['Equipe']);
});

test('aceita WAWebChatCollection como nome alternativo do collection', () => {
    const chats = [grupo('1203', 'Equipe')];
    const win = criarWindow(null, {
        colecoes: { WAWebChatCollection: { getModelsArray: () => chats } },
    });

    assert.deepEqual(coletarGrupos(win).grupos.map((g) => g.id), ['1203@g.us']);
});

// Degradacao com diagnostico: o objetivo e o log dizer QUAL passo quebrou, que
// e o que faltava quando a unica pista era "r".
test('require lancando devolve passo=collections sem lancar', () => {
    const win = criarWindow(null, { moduloErro: new Error('modulo sumiu') });

    const res = coletarGrupos(win);

    assert.deepEqual(res, { ok: false, passo: 'collections', erro: 'modulo sumiu' });
});

test('collections sem Chat reporta os modulos disponiveis', () => {
    const win = criarWindow(null, { colecoes: { Msg: {}, Contact: {} } });

    const res = coletarGrupos(win);

    assert.equal(res.ok, false);
    assert.equal(res.passo, 'collections');
    assert.deepEqual(res.modulos, ['Msg', 'Contact']);
});

test('collection sem array de chats devolve passo=models', () => {
    const win = criarWindow(null, { colecoes: { Chat: { getModelsArray: () => null } } });

    const res = coletarGrupos(win);

    assert.equal(res.ok, false);
    assert.equal(res.passo, 'models');
});

test('lista vazia e sucesso com zero grupos, nao falha', () => {
    const res = coletarGrupos(criarWindow([]));

    assert.deepEqual(res, { ok: true, grupos: [], ignorados: [], totalChats: 0 });
});

test('descreverErro preserva mensagem e origem de um Error', () => {
    const descricao = descreverErro(new TypeError('quebrou'));

    assert.match(descricao, /^TypeError: quebrou \| at /);
});

// O caso "r": o bundle minificado lanca coisas que nao sao Error.
test('descreverErro nao apaga um throw que nao e Error', () => {
    assert.equal(descreverErro('r'), 'string "r"');
    assert.equal(descreverErro({ code: 'r' }), 'object {"code":"r"}');
});

test('descreverErro aguenta objeto ciclico', () => {
    const ciclico = { nome: 'r' };
    ciclico.self = ciclico;

    assert.match(descreverErro(ciclico), /^object /);
});
