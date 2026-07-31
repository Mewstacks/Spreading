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
// Timeout de uma etapa de PREFLIGHT (getState, store, grupo), que precisa deixar
// um piso de tempo para o sendMessage.
//
// Sem a reserva, o preflight podia comer o orcamento inteiro: getState ate 10s +
// store ate 8s + verificar_grupo ate 15s = 33s dos 55s totais, e o envio herdava o
// que sobrasse — as vezes ~20s, apesar de SEND_TIMEOUT_MS anunciar 60s. Em maquina
// lenta, o preflight ficava caro justamente quando o envio mais precisava de folga,
// e a mensagem morria por construcao, nao por culpa do WhatsApp.
const timeoutDePreflight = (prazo, tetoMs, reservaMs, agora = Date.now()) => {
    const disponivel = Math.max(0, restante(prazo, agora) - Math.max(0, reservaMs));
    return Math.min(tetoMs, disponivel);
};

const timeoutComEnvioIniciado = (envioIniciado, etapa, erro, prazo, agora = Date.now()) => (
    envioIniciado
    && etapa === 'sendMessage'
    && (/timeout/i.test(String(erro?.message || erro || '')) || expirou(prazo, agora))
);

module.exports = {
    criarPrazo, restante, expirou, timeoutDaEtapa, timeoutDePreflight, timeoutComEnvioIniciado,
};
