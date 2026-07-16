'use strict';

// O sendMessage resolve o destino via window.WWebJS.getChat DENTRO da pagina;
// esses modulos (e window.Store) so existem depois que o bundle do WA Web
// termina de injetar — o que pode acontecer alguns segundos APOS o evento
// `ready`. aguardarStorePronto espera por eles com polling, respeitando um
// orcamento proprio (`tetoMs`) e, opcionalmente, o prazo global de uma request
// de envio (`expirou`).
//
// As dependencias sao injetadas para rodar em Node sem Chromium — mesma
// disciplina de preflight_recovery.js/send_deadline.js:
//   sondar()    -> Promise<boolean>: faz UMA checagem do store.
//   tetoMs      -> quanto esperar no total antes de desistir.
//   intervaloMs -> espera entre checagens.
//   expirou()   -> boolean opcional: prazo global da request estourou.
//   dormir(ms)  -> espera; agora() -> relogio. Substituiveis no teste.
const aguardarStorePronto = async (opcoes = {}) => {
    const {
        sondar,
        tetoMs,
        intervaloMs = 500,
        expirou,
        dormir = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
        agora = Date.now,
    } = opcoes;
    const limite = agora() + tetoMs;
    // eslint-disable-next-line no-constant-condition
    for (;;) {
        if (await sondar()) return true;                       // store apareceu
        if (expirou && expirou()) return false;                // prazo global da request estourou
        if (agora() + intervaloMs >= limite) return false;     // orcamento de espera do store esgotado
        await dormir(intervaloMs);
    }
};

module.exports = { aguardarStorePronto };
