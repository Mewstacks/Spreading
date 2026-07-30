'use strict';

// Um timeout por etapa nao protege a request inteira: 10s de getState + 15s de
// grupo + 60s de sendMessage ultrapassavam os 75s do Django. Este modulo deixa
// um unico relogio absoluto ser compartilhado por fila, preflight e envio.
const criarPrazo = (duracaoMs, agora = Date.now()) => agora + duracaoMs;
const restante = (prazo, agora = Date.now()) => Math.max(0, prazo - agora);
const expirou = (prazo, agora = Date.now()) => restante(prazo, agora) === 0;
const timeoutDaEtapa = (prazo, tetoMs, agora = Date.now()) => {
    const sobra = restante(prazo, agora);
    return sobra ? Math.min(tetoMs, sobra) : 0;
};
const timeoutComEnvioIniciado = (envioIniciado, etapa, erro, prazo, agora = Date.now()) => (
    envioIniciado
    && etapa === 'sendMessage'
    && (/timeout/i.test(String(erro?.message || erro || '')) || expirou(prazo, agora))
);

module.exports = { criarPrazo, restante, expirou, timeoutDaEtapa, timeoutComEnvioIniciado };
