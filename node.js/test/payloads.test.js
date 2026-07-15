'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
    gruposIndisponivel,
    buildSessionPayload,
    buildGruposPayload,
    buildInativoPayload,
} = require('../payloads');

const fakeSession = (over = {}) => ({
    id: 'u1',
    isConnected: false,
    fase: 'iniciando',
    progresso: 0,
    faseMsg: 'Iniciando serviço…',
    gruposCache: [],
    gruposCarregados: false,
    gruposSincronizando: false,
    ultimoQR: null,
    ...over,
});

const conectado = (over = {}) => fakeSession({
    isConnected: true, fase: 'conectado', progresso: 100, faseMsg: 'Conectado', ...over,
});

const TODOS_OS_ESTADOS = [
    fakeSession(),
    fakeSession({ fase: 'qr', ultimoQR: 'data:image/png;base64,xxx' }),
    fakeSession({ fase: 'reconectando', faseMsg: 'Recuperando sessão (tentativa 3)…' }),
    fakeSession({ fase: 'expirado', faseMsg: 'Sessão expirada. Leia o QR novamente.' }),
    fakeSession({ fase: 'desconectado' }),
    fakeSession({ fase: 'capacidade', faseMsg: 'Capacidade atingida.' }),
    conectado({ gruposCarregados: true, gruposCache: [{ id: '1@g.us', nome: 'G' }] }),
    conectado({ gruposSincronizando: true }),
    conectado(), // getChats falhou: conectado, lista indisponivel
];

// A aba Envios trata `erro` como "servico indisponivel". Se um payload normal
// vazar esse campo, o banner "WhatsApp desconectado" volta a mentir — foi
// exatamente o bug. `erro` so pode vir do Django, quando o Node nao responde.
test('no payload ever carries an `erro` field', () => {
    for (const s of TODOS_OS_ESTADOS) {
        assert.ok(!('erro' in buildGruposPayload(s)), `grupos payload / fase ${s.fase}`);
        assert.ok(!('erro' in buildSessionPayload(s)), `session payload / fase ${s.fase}`);
    }
    assert.ok(!('erro' in buildInativoPayload('u1')));
});

test('conectado is exactly `fase === conectado`, in every payload', () => {
    for (const s of TODOS_OS_ESTADOS) {
        const g = buildGruposPayload(s);
        const p = buildSessionPayload(s);
        assert.equal(g.conectado, g.fase === 'conectado', `grupos / fase ${s.fase}`);
        assert.equal(p.conectado, p.fase === 'conectado', `session / fase ${s.fase}`);
    }
});

test('grupos_indisponivel isolates "connected but the list never came"', () => {
    // O estado do print: Conectado, 0 grupos, sem sync em andamento.
    assert.equal(gruposIndisponivel(conectado()), true);
    // Sincronizando agora: e transitorio, nao "indisponivel".
    assert.equal(gruposIndisponivel(conectado({ gruposSincronizando: true })), false);
    // Lista carregada.
    assert.equal(gruposIndisponivel(conectado({ gruposCarregados: true })), false);
    // Desconectado: o eixo relevante e `conectado`, nao este.
    assert.equal(gruposIndisponivel(fakeSession({ fase: 'desconectado' })), false);
});

test('a connected session with an empty group list is not "indisponivel"', () => {
    // Conta real sem nenhum grupo: getChats respondeu, a lista e vazia mesmo.
    const s = conectado({ gruposCarregados: true, gruposCache: [] });
    const g = buildGruposPayload(s);
    assert.equal(g.conectado, true);
    assert.equal(g.grupos_indisponivel, false);
    assert.deepEqual(g.grupos, []);
});

test('capacity is reported as its own phase, not as a disconnection', () => {
    const g = buildGruposPayload(fakeSession({ fase: 'capacidade' }));
    assert.equal(g.fase, 'capacidade');
    assert.equal(g.conectado, false);
    assert.ok(!('erro' in g));
});

test('inativo payload is a valid, non-error state', () => {
    const p = buildInativoPayload('u1');
    assert.equal(p.conectado, false);
    assert.equal(p.fase, 'inativo');
    assert.deepEqual(p.grupos, []);
});

test('grupos list is only exposed once actually loaded', () => {
    const s = conectado({ gruposCache: [{ id: '1@g.us', nome: 'G' }], gruposCarregados: false });
    assert.deepEqual(buildGruposPayload(s).grupos, []);
    assert.equal(buildSessionPayload(s).grupos, 0);
});
