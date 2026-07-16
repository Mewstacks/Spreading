'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { inspecionarGrupo, idChatValido, descreverErro } = require('../group_reader');

// window de mentira: inspecionarGrupo so le a collection Chat e o WidFactory,
// entao roda em Node sem Chromium — mesma disciplina do group_reader.test.js.
// `presentes` sao os _serialized que a conta conhece.
const criarWindow = (presentes = [], { widErro = null, colecoes } = {}) => ({
    require: (nome) => {
        if (nome === 'WAWebWidFactory') {
            return {
                createWid: (id) => {
                    if (widErro) throw widErro;
                    return { _serialized: id };
                },
            };
        }
        if (nome === 'WAWebCollections') {
            if (colecoes !== undefined) return colecoes;
            return { Chat: { get: (wid) => (presentes.includes(wid._serialized) ? { id: wid } : null) } };
        }
        throw new Error(`modulo inesperado: ${nome}`);
    },
});

// --- idChatValido: a guarda que roda ANTES de entrar no Chromium -------------

test('aceita os formatos que o createWid entende', () => {
    assert.equal(idChatValido('120363043211234567@g.us'), true);
    assert.equal(idChatValido('5511999998888-1234567890@g.us'), true, 'grupo legado');
    assert.equal(idChatValido('5511999998888@c.us'), true);
});

// O caso real: a UI cai num input de texto livre quando a lista de grupos nao
// carrega, e o usuario digita o NOME do grupo. Isso ia direto pro createWid, que
// lancava minificado — o "[ERRO] r" que o usuario via.
test('rejeita o nome do grupo e outros ids malformados', () => {
    assert.equal(idChatValido('MillStack'), false);
    assert.equal(idChatValido('120363043211234567'), false, 'sem @server');
    assert.equal(idChatValido('120363043211234567@lid'), false, 'server errado');
    assert.equal(idChatValido(''), false);
    assert.equal(idChatValido(null), false);
    assert.equal(idChatValido(undefined), false);
    assert.equal(idChatValido({ _serialized: '123@g.us' }), false, 'objeto Wid, nao string');
});

// --- inspecionarGrupo: nunca lanca, sempre devolve envelope -----------------

test('reconhece um grupo que a conta tem', () => {
    const win = criarWindow(['1203@g.us']);

    assert.deepEqual(inspecionarGrupo(win, '1203@g.us'), { ok: true, existe: true });
});

test('grupo que saiu da conta vira existe:false, nao erro', () => {
    const win = criarWindow(['1203@g.us']);

    assert.deepEqual(inspecionarGrupo(win, '999@g.us'), { ok: true, existe: false });
});

// A regressao que o worker real pegou: WWebJS.getChat cai em
// findOrCreateLatestChat e CRIA um chat para id desconhecido — "existe" dava
// sempre true e a guarda virava enfeite. Ler a collection nao tem esse efeito.
test('id desconhecido nao e criado — a guarda continua valendo', () => {
    const criados = [];
    const win = {
        require: (nome) => {
            if (nome === 'WAWebWidFactory') return { createWid: (id) => ({ _serialized: id }) };
            if (nome === 'WAWebCollections') {
                return {
                    Chat: {
                        get: () => null,
                        find: (wid) => { criados.push(wid); return {}; },
                    },
                };
            }
            throw new Error(`modulo inesperado: ${nome}`);
        },
    };

    assert.deepEqual(inspecionarGrupo(win, '999@g.us'), { ok: true, existe: false });
    assert.deepEqual(criados, [], 'nao pode criar chat fantasma na conta');
});

// O contrato: inspecionarGrupo roda dentro do Chromium via evaluate. Se lancasse,
// o erro voltaria minificado e estariamos de volta ao "r".
test('throw do bundle vira envelope ok:false, nunca excecao', () => {
    const win = criarWindow([], { widErro: { message: 'r' } });

    assert.deepEqual(inspecionarGrupo(win, '1203@g.us'), { ok: false, erro: 'r' });
});

test('throw sem message tambem e capturado', () => {
    const win = criarWindow([], { widErro: 'falha crua' });

    assert.deepEqual(inspecionarGrupo(win, '1203@g.us'), { ok: false, erro: 'falha crua' });
});

// O nome do modulo muda entre versoes do WA Web: reportar em vez de estourar.
test('collection ausente vira envelope, nao excecao', () => {
    const win = criarWindow([], { colecoes: {} });

    const res = inspecionarGrupo(win, '1203@g.us');

    assert.equal(res.ok, false);
    assert.match(res.erro, /Chat\.get/);
});

// --- descreverErro no caminho de envio --------------------------------------

// O bug exato: o bundle minificado lanca um nao-Error cujo .message e uma
// variavel de nome curto. `erro.message` cru mandava "r" pro usuario.
test('descreverErro preserva o objeto que virava "r"', () => {
    const descrito = descreverErro({ message: 'r' });

    assert.notEqual(descrito, 'r');
    assert.match(descrito, /object/);
    assert.match(descrito, /"message":"r"/);
});

test('descreverErro mantem nome e message de um Error de verdade', () => {
    const descrito = descreverErro(new Error('sendMessage timeout'));

    assert.match(descrito, /^Error: sendMessage timeout/);
});
