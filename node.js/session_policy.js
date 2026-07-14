'use strict';

function reconnectDelay(attempt, baseMs, maxMs) {
    const safeAttempt = Math.max(1, Number(attempt) || 1);
    return Math.min(maxMs, baseMs * (2 ** Math.min(safeAttempt - 1, 4)));
}

function shouldPurgeAuth(failures, authenticatedAttempt) {
    return failures >= (authenticatedAttempt ? 2 : 3);
}

module.exports = { reconnectDelay, shouldPurgeAuth };
