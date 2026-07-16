'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
    extrairMensagemId, opcoesDeEnvio, erroFrameDestacado, confirmarMensagem,
    repetirSeFrameDestacado,
} = require('../message_confirmation');

test('extrai o Wid serializado normal do whatsapp-web.js', () => {
    assert.equal(extrairMensagemId({ id: { _serialized: 'true_123@g.us_ABC' } }), 'true_123@g.us_ABC');
});

test('aceita id direto e o modelo bruto das versoes recentes do WA Web', () => {
    assert.equal(extrairMensagemId({ id: 'ABC' }), 'ABC');
    assert.equal(extrairMensagemId({ _data: { id: { id: 'DEF' } } }), 'DEF');
});

test('nao inventa confirmacao quando o envio nao devolve mensagem', () => {
    assert.equal(extrairMensagemId(undefined), null);
    assert.equal(extrairMensagemId({}), null);
});

test('preserva só a legenda e não prolonga o evaluate esperando ACK', () => {
    assert.deepEqual(opcoesDeEnvio(), {});
    assert.deepEqual(opcoesDeEnvio('Oferta'), { caption: 'Oferta' });
});

test('reconhece a troca de frame que torna o resultado do envio ambíguo', () => {
    assert.equal(erroFrameDestacado(new Error("Attempted to use detached Frame 'abc'.")), true);
    assert.equal(erroFrameDestacado(new Error('sendMessage timeout')), false);
});

test('repete com segurança uma verificação anterior ao envio após frame destacado', async () => {
    let chamadas = 0;
    const valor = await repetirSeFrameDestacado(() => {
        chamadas += 1;
        if (chamadas === 1) throw new Error('Attempted to use detached Frame');
        return 'CONNECTED';
    }, { esperar: async () => {} });

    assert.equal(valor, 'CONNECTED');
    assert.equal(chamadas, 2);
});

test('não repete erro que não é frame destacado', async () => {
    await assert.rejects(
        repetirSeFrameDestacado(() => { throw new Error('falha real'); }),
        /falha real/,
    );
});

test('preserva o ID nativo quando o WhatsApp o devolve', () => {
    assert.deepEqual(
        confirmarMensagem({ id: { _serialized: 'true_123@g.us_ABC' } }, '1'),
        { mensagemId: 'true_123@g.us_ABC', confirmacao: 'nativa' },
    );
});

test('gera rastreio local quando o WA Web aceita mas omite o modelo da mensagem', () => {
    assert.deepEqual(
        confirmarMensagem(undefined, '1', { agora: () => 123, uuid: () => 'abc' }),
        { mensagemId: 'local-1-123-abc', confirmacao: 'aceita_sem_id' },
    );
});
