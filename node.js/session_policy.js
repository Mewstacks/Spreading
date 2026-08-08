'use strict';

function reconnectDelay(attempt, baseMs, maxMs) {
    const safeAttempt = Math.max(1, Number(attempt) || 1);
    return Math.min(maxMs, baseMs * (2 ** Math.min(safeAttempt - 1, 4)));
}

function shouldPurgeAuth(failures, authenticatedAttempt) {
    return failures >= (authenticatedAttempt ? 2 : 3);
}

// Decide o destino de uma tentativa de reconexao. `attempts` e o contador DEPOIS
// do incremento; `authPurges` e quantas vezes o auth ja foi purgado neste ciclo.
// Sem isto o reconnect nunca para: o contador so crescia ('tentativa 38'...).
//   'retry'  -> agenda com backoff
//   'purge'  -> credencial provavelmente morta: apaga, zera o contador, novo QR
//   'expire' -> ja purgou e ainda falha: estado terminal, para de reagendar
function reconnectOutcome(attempts, authPurges, maxAttempts = 6) {
    const safeAttempts = Math.max(1, Number(attempts) || 1);
    const safePurges = Math.max(0, Number(authPurges) || 0);
    if (safeAttempts <= maxAttempts) return 'retry';
    return safePurges > 0 ? 'expire' : 'purge';
}

// Uma credencial que ja foi pareada nunca deve ser removida como reação a uma
// sequência de timeouts do Chromium. Nesse caso a recuperação para em estado
// acionável e um novo pedido de conexão reutiliza o mesmo LocalAuth.
function reconnectAction(attempts, authPurges, hasStoredAuth, maxAttempts = 6) {
    const outcome = reconnectOutcome(attempts, authPurges, maxAttempts);
    return outcome === 'purge' && hasStoredAuth ? 'pause' : outcome;
}

// O bootstrap de QR tem orçamento próprio e nunca participa da escada de
// reconexão de uma credencial pareada. `attempt` inclui a tentativa atual.
function qrBootstrapOutcome(attempt, maxAttempts = 2) {
    const safeAttempt = Math.max(1, Number(attempt) || 1);
    const safeMax = Math.max(1, Number(maxAttempts) || 1);
    return safeAttempt < safeMax ? 'retry' : 'fail';
}

// QR e loading_screen pertencem somente ao trecho anterior à autenticação.
// A biblioteca pode entregar ambos atrasados enquanto o handler de `ready`
// ainda sonda WWebJS ou enquanto o primeiro sync de grupos está em voo. Nesse
// intervalo isConnected=false de propósito, portanto ele sozinho não serve
// como trava: aceitar o evento rebaixa a UI para "preparando o leitor" mesmo
// depois de o celular já ter concluído o pareamento.
const POST_AUTH_PHASES = new Set([
    'autenticado',
    'preparando',
    'sincronizando',
    'conectado',
]);

function preAuthEventIsStale(session) {
    if (!session) return false;
    return Boolean(
        session.authenticatedInAttempt
        || session.readyReceived
        || session.preparando
        || session.isConnected
        || POST_AUTH_PHASES.has(session.fase)
    );
}

const REVOKED_REASONS = new Set(['LOGOUT', 'UNPAIRED', 'UNPAIRED_IDLE']);

// Motivos de 'disconnected' que significam credencial revogada no celular:
// reconectar com ela e inutil e so queima Chromium em loop.
// NAVIGATION e CONFLICT ficam DE FORA de proposito: purgar neles apagaria um auth
// valido e forcaria QR novo sem necessidade. Atencao: a versao anterior deste
// comentario dizia que "takeoverOnConflict ja cobre" o CONFLICT — nao cobre mais,
// ele nasce desligado (WA_TAKEOVER_ON_CONFLICT). Quem cobre CONFLICT agora e
// classificarEstadoWa + o handler de change_state, reconectando sem purgar.
function isRevokedReason(reason) {
    if (!reason) return false;
    return REVOKED_REASONS.has(String(reason).trim().toUpperCase());
}

// Estados do WAState que significam "a pagina esta trabalhando", nao queda.
// OPENING/PAIRING aparecem durante o proprio boot da sessao; trata-los como queda
// faria o keepalive reciclar um Chromium que ainda esta subindo.
const ESTADOS_EM_TRANSICAO = new Set(['OPENING', 'PAIRING']);

// Classifica um WAState (de client.getState() ou do evento change_state):
//   'ok'        -> CONNECTED
//   'transicao' -> ainda subindo: espera, nao mexe
//   'queda'     -> perdeu o socket: precisa reconectar AGORA
//   'indefinido'-> getState devolveu null/undefined (pagina sem WWebJS ainda)
//
// Existe porque o worker nao escutava 'change_state': CONFLICT, UNPAIRED, TIMEOUT
// e UNLAUNCHED nao geravam evento nenhum e a sessao morria em silencio — o
// primeiro sintoma era um envio falhando no preflight, minutos ou horas depois.
// CONFLICT entra aqui como queda mesmo com takeoverOnConflict desligado (o padrao,
// e deliberado: ligar o takeover reabria o spam de sync de multi-sessao). Sem
// takeover o socket nao volta sozinho, entao reciclar e a unica saida.
function classificarEstadoWa(estado) {
    if (estado === null || estado === undefined || estado === '') return 'indefinido';
    const normalizado = String(estado).trim().toUpperCase();
    if (normalizado === 'CONNECTED') return 'ok';
    if (ESTADOS_EM_TRANSICAO.has(normalizado)) return 'transicao';
    return 'queda';
}

function estadoIndicaQueda(estado) {
    return classificarEstadoWa(estado) === 'queda';
}

// Quantas leituras seguidas de WAState podem falhar antes de a sessao ser tratada
// como caida. Nao e 1: um getState perdido e rotina (a pagina pode estar ocupada
// com um sync, e o proprio timeout de 10s e apertado sob carga).
const KEEPALIVE_FALHAS_ATE_QUEDA = 3;

// Falha de LEITURA do keepalive nao e o mesmo que estado de queda: ali o WhatsApp
// respondeu 'UNPAIRED'/'CONFLICT', aqui ele nao respondeu nada. O codigo antigo
// so reagendava, e o resultado foi o pior caso possivel para o usuario: em
// 30/07 a pagina parou de responder as 18:05, o log repetiu "Keepalive nao leu o
// estado" e a sessao seguiu marcada como CONECTADA na tela ate o envio falhar
// tres minutos depois. Falha repetida e sim um veredito.
function keepaliveIndicaQueda(falhasConsecutivas) {
    return Number(falhasConsecutivas) >= KEEPALIVE_FALHAS_ATE_QUEDA;
}

// Quantos timeouts de envio seguidos com a PAGINA VIVA a sessao aguenta antes
// de ser reciclada assim mesmo. Nao e 1: um upload lento e rotina.
const STALLS_ATE_RECICLAR = 3;

// Decide se um timeout de envio deve derrubar o Chromium.
//
// O veredito vem de sondarVivacidadePagina, e 'stall_no_upload' significa que a
// pagina RESPONDEU a sonda: o Chromium esta vivo e quem travou foi o
// prep/upload da midia, do lado do WhatsApp/rede. Reciclar ali nao conserta
// nada e custa caro — derruba um Chromium saudavel, varre o perfil, sobe outro
// e ressincroniza centenas de grupos. Ate 08/08 o codigo calculava o veredito,
// escrevia no log que reciclar "nao resolve nada" e reciclava mesmo assim, nos
// tres vereditos. Com a CPU no limite isso virou tempestade: cada envio que
// estourava derrubava a sessao, a sessao voltava consumindo CPU, e o envio
// seguinte estourava de novo.
//
// A excecao e o pipeline de upload morto de vez: a pagina responde '1+1' e
// nenhuma midia sai nunca mais. Por isso o veredito so segura a sessao ate o
// terceiro timeout seguido; dai em diante reciclar e a unica saida que resta.
function deveReciclarAposTimeoutDeEnvio(veredito, stallsSeguidos = 0) {
    if (veredito !== 'stall_no_upload') return true;
    return (Number(stallsSeguidos) || 0) >= STALLS_ATE_RECICLAR;
}

// Traduz a sonda de vivacidade (true/false/null) no veredito registrado no log.
function veredictoDeTimeoutDeEnvio(paginaViva) {
    if (paginaViva === true) return 'stall_no_upload';
    if (paginaViva === false) return 'pagina_travada';
    return 'indeterminado';
}

// Decide o que fazer quando syncGroups e chamado com um sync ja em voo.
// Um getChats por sessao de cada vez e inegociavel (dezenas de MB de Chromium),
// entao 'repicar' NAO paraleliza: reaproveita a promise em voo e re-roda depois.
//   'iniciar'  -> nao ha sync em voo: comeca um
//   'aguardar' -> sync em voo, pedido automatico: reaproveita o resultado
//   'repicar'  -> sync em voo, pedido explicito do usuario: reaproveita a promise
//                 MAS re-roda ao final. Sem isto o botao "Sincronizar grupos"
//                 devolvia o snapshot obtido ANTES do clique: quem criou um grupo
//                 no celular e clicou nao via o grupo novo, e recebia sucesso.
function syncGroupsOutcome(syncEmVoo, forcar) {
    if (!syncEmVoo) return 'iniciar';
    return forcar ? 'repicar' : 'aguardar';
}

// Backoff do retry automatico de sincronizacao de grupos DENTRO do worker.
// Antes, uma unica falha era terminal: o latch `gruposSyncFalhou` bloqueava todo
// GET /api/grupos e so o botao "Sincronizar grupos" reabria. Uma falha
// transitoria (pagina ainda hidratando, rede oscilando) virava assim um estado
// permanente de "lista indisponivel" que exigia acao manual.
// null = esgotou: para de tentar e devolve o controle ao botao.
function groupRetryDelay(attempt, baseMs = 5000, maxMs = 120000, maxAttempts = 6) {
    const safeAttempt = Math.max(1, Number(attempt) || 1);
    if (safeAttempt > maxAttempts) return null;
    return Math.min(maxMs, baseMs * (2 ** Math.min(safeAttempt - 1, 5)));
}

// Backoff do repoll de sincronizacao de grupos no front.
// Retorna null quando esgota: o front para de pollar e mostra o estado
// 'lista indisponivel' em vez de repollar de 3 em 3s para sempre.
function syncPollDelay(attempt, baseMs = 3000, maxMs = 15000, maxAttempts = 8) {
    const safeAttempt = Math.max(1, Number(attempt) || 1);
    if (safeAttempt > maxAttempts) return null;
    return Math.min(maxMs, baseMs * (2 ** Math.min(safeAttempt - 1, 4)));
}

// O limite de sessoes existe para nao estourar a memoria com Chromiums
// (~350MB cada), entao so conta quem tem um agora ou vai ter quando o timer de
// reconexao disparar. Sessoes terminais ('expirado') ficam no Map apenas para
// preservar a mensagem acionavel e nao seguram recurso nenhum: conta-las faria
// sessoes mortas bloquearem o servico estando ele ocioso.
function ocupaSlot(session) {
    return Boolean(
        session.client || session.initialized
        || session.reconnectTimer || session.qrBootstrapTimer
    );
}

module.exports = {
    reconnectDelay,
    shouldPurgeAuth,
    reconnectOutcome,
    reconnectAction,
    qrBootstrapOutcome,
    preAuthEventIsStale,
    isRevokedReason,
    classificarEstadoWa,
    estadoIndicaQueda,
    keepaliveIndicaQueda,
    KEEPALIVE_FALHAS_ATE_QUEDA,
    deveReciclarAposTimeoutDeEnvio,
    veredictoDeTimeoutDeEnvio,
    STALLS_ATE_RECICLAR,
    syncGroupsOutcome,
    groupRetryDelay,
    syncPollDelay,
    ocupaSlot,
};
