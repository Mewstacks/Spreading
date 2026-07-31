'use strict';

const { syncGroupsOutcome } = require('./session_policy');

// Orquestra as leituras de grupos de UMA sessao.
//
// Invariante que sustenta tudo aqui: NUNCA duas leituras em voo na mesma sessao.
// getChats abre dezenas de MB no Chromium; paralelizar e o que a guarda de
// `groupSyncPromise` sempre evitou. Um pedido explicito do usuario que chega
// durante um voo por isso nao paraleliza — ele repica: a leitura extra roda
// dentro da MESMA promise, logo depois da atual.
//
// Por que a leitura extra fica dentro da promise, e nao depois dela:
//   1. `groupSyncPromise` nunca fica nulo entre as duas leituras, entao nao ha
//      janela em que um pedido novo escape da guarda e abra um getChats paralelo.
//   2. `gruposSincronizando` segue true, entao o front continua pollando e
//      gruposIndisponivel() (payloads.js — exige !gruposSincronizando) nao
//      dispara falso no meio do repique.
//
// CONTRATO: `ler` NUNCA pode lancar. As rotas chamam sem await (respondem na
// hora e o front polla), entao uma rejeicao aqui viraria unhandled rejection e
// derrubaria o processo no Node 20.
//
// Modulo puro: sem fs, sem express, sem whatsapp-web.js. Testavel em
// test/group_sync.test.js.
//
// `estado` e a sessao, tipada apenas nestes campos:
//   groupSyncPromise, gruposSincronizando, syncPedidoDurante
const iniciarSync = (estado, ler, reason = 'auto', { forcar = false } = {}) => {
    const outcome = syncGroupsOutcome(Boolean(estado.groupSyncPromise), forcar);

    if (outcome === 'repicar') {
        // Flag booleana: N cliques durante um voo viram UM repique, nunca um loop.
        // E o rate limit natural do botao "Sincronizar grupos".
        estado.syncPedidoDurante = true;
        return estado.groupSyncPromise;
    }
    if (outcome === 'aguardar') return estado.groupSyncPromise;

    estado.gruposSincronizando = true;
    estado.groupSyncPromise = (async () => {
        try {
            let ok = await ler(reason);
            while (estado.syncPedidoDurante) {
                estado.syncPedidoDurante = false;
                ok = await ler(`${reason}-repique`);
            }
            return ok;
        } finally {
            estado.gruposSincronizando = false;
            estado.groupSyncPromise = null;
        }
    })();

    return estado.groupSyncPromise;
};

module.exports = { iniciarSync };
