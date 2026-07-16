'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { timeoutPreflight, mensagemPreflight, iniciarRecuperacaoPreflight } = require('../preflight_recovery');

test('timeouts antes do envio pedem recuperação da sessão', () => {
    assert.equal(timeoutPreflight('getState', new Error('getState timeout')), true);
    assert.equal(timeoutPreflight('verificar_grupo', new Error('inspecionarGrupo timeout')), true);
    assert.match(mensagemPreflight('getState'), /recuperada automaticamente/i);
});

test('timeout depois de iniciar o envio não usa a recuperação pré-envio', () => {
    assert.equal(timeoutPreflight('sendMessage', new Error('sendMessage timeout')), false);
    assert.equal(timeoutPreflight('getState', new Error('outro erro')), false);
});

test('timeout pré-envio responde de forma amigável, recicla uma vez e não envia mensagem', async () => {
    let envios = 0;
    const session = {
        isConnected: true,
        client: { sendMessage: () => { envios += 1; } },
    };
    const jobs = [];
    const reciclar = async () => { reciclar.chamadas += 1; };
    reciclar.chamadas = 0;
    const agendar = (fn) => { jobs.push(fn); return { unref() {} }; };

    assert.equal(iniciarRecuperacaoPreflight(session, 'getState', reciclar, agendar), true);
    assert.equal(iniciarRecuperacaoPreflight(session, 'getState', reciclar, agendar), false);
    assert.equal(session.isConnected, false);
    assert.equal(session.fase, 'reconectando');
    assert.match(mensagemPreflight('getState'), /aguarde alguns segundos/i);
    assert.equal(envios, 0);
    assert.equal(jobs.length, 1);
    jobs[0]();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(reciclar.chamadas, 1);
});
