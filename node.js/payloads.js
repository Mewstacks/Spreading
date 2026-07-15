'use strict';

// Serializacao do estado de sessao para o front.
//
// Invariante que sustenta as duas abas (WhatsApp e Envios):
//   1. `conectado === (fase === 'conectado')` — um unico criterio de conexao.
//   2. NENHUM payload daqui carrega o campo `erro`. No Django, `erro` no corpo
//      significa exclusivamente "o worker Node esta inalcancavel"
//      (whatsapp_client._request_json). Emitir `erro` para estados normais
//      (sincronizando, desconectado) e o que fazia a aba Envios acusar
//      "WhatsApp desconectado" com a sessao viva.
//
// Modulo puro: sem fs, sem express. Testavel em test/payloads.test.js.

// Uma leitura em voo e um retry agendado sao a mesma coisa para quem olha a
// tela: a lista ainda esta a caminho. Sem juntar os dois, a janela entre duas
// tentativas apareceria como "indisponivel" e o front pararia de pollar
// justamente enquanto o worker ia tentar de novo sozinho.
const gruposEmRecuperacao = (session) => Boolean(
    session.gruposSincronizando || session.gruposRetryTimer
);

// Ortogonal a `conectado`. syncGroups mantem fase='conectado' de proposito
// quando a leitura falha (a conexao ja foi provada pelo evento `ready`; a lista
// de chats e secundaria). Isso produz um estado legitimo que antes nao tinha
// nome e aparecia como "Conectado / 0 grupos sincronizados".
const gruposIndisponivel = (session) => Boolean(
    session.isConnected && !session.gruposCarregados && !gruposEmRecuperacao(session)
);

const buildSessionPayload = (session) => ({
    instancia: session.id,
    conectado: session.isConnected,
    fase: session.fase,
    progresso: session.progresso,
    mensagem: session.faseMsg,
    grupos: session.gruposCarregados ? session.gruposCache.length : 0,
    grupos_sincronizando: gruposEmRecuperacao(session),
    grupos_indisponivel: gruposIndisponivel(session),
    qr: session.ultimoQR,
});

const buildGruposPayload = (session) => ({
    instancia: session.id,
    conectado: session.isConnected,
    fase: session.fase,
    mensagem: session.faseMsg,
    sincronizando: gruposEmRecuperacao(session),
    grupos_indisponivel: gruposIndisponivel(session),
    grupos: session.gruposCarregados ? session.gruposCache : [],
});

// Sessao que nao existe no Map e nao tem credencial no volume: nada a
// restaurar. Nao e erro — e "ninguem pareou este WhatsApp ainda".
const buildInativoPayload = (instanceId) => ({
    instancia: instanceId,
    conectado: false,
    fase: 'inativo',
    mensagem: 'Sessao inativa.',
    sincronizando: false,
    grupos_indisponivel: false,
    grupos: [],
});

module.exports = {
    gruposIndisponivel,
    buildSessionPayload,
    buildGruposPayload,
    buildInativoPayload,
};
