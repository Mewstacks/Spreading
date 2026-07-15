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

const REVOKED_REASONS = new Set(['LOGOUT', 'UNPAIRED', 'UNPAIRED_IDLE']);

// Motivos de 'disconnected' que significam credencial revogada no celular:
// reconectar com ela e inutil e so queima Chromium em loop.
// NAVIGATION e CONFLICT ficam DE FORA de proposito: sao transitorios (e
// takeoverOnConflict ja cobre o segundo). Purgar neles apagaria um auth valido
// e forcaria QR novo sem necessidade.
function isRevokedReason(reason) {
    if (!reason) return false;
    return REVOKED_REASONS.has(String(reason).trim().toUpperCase());
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
    return Boolean(session.client || session.initialized || session.reconnectTimer);
}

module.exports = {
    reconnectDelay,
    shouldPurgeAuth,
    reconnectOutcome,
    isRevokedReason,
    syncPollDelay,
    ocupaSlot,
};
