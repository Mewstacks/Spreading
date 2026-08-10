'use strict';

const redactSensitive = (value, maxLength = 1000) => String(value || '')
    .replace(/(https?:\/\/[^\s?'"<>]+)\?[^\s'"<>]+/gi, '$1?[redacted]')
    .replace(/\b(Bearer\s+)[A-Za-z0-9._~+/=-]+/gi, '$1[redacted]')
    .replace(/(["']?(?:authorization|cookie|token|capability|password|storage[_ -]?state)["']?\s*[:=]\s*)(?:["'][^"']*["']|[^\s,;]+)/gi,
        '$1[redacted]')
    .replace(/(?<![\d-])(?:\+\d[\d\s().-]{8,}\d|\(?\d{2}\)?\s*9?\d{4}[-\s]?\d{4}|\d{10,15})(?!\d)/g,
        '[identificador mascarado]')
    .replace(/\b[A-Za-z0-9+/]{256,}={0,2}\b/g, '[conteudo binario removido]')
    .slice(0, Math.max(1, Number(maxLength) || 1000));

const safeArgument = (value) => {
    if (typeof value === 'string') return redactSensitive(value, 4000);
    if (value instanceof Error) return redactSensitive(value.stack || value.message, 4000);
    if (value && typeof value === 'object') {
        try { return redactSensitive(JSON.stringify(value), 4000); } catch (_) {
            return `[${value.constructor?.name || 'objeto'} nao serializavel]`;
        }
    }
    return value;
};

const installConsoleRedaction = (target = console) => {
    if (!target || target.__spreadingRedacted) return target;
    for (const method of ['log', 'info', 'warn', 'error', 'debug']) {
        if (typeof target[method] !== 'function') continue;
        const original = target[method].bind(target);
        target[method] = (...args) => original(...args.map(safeArgument));
    }
    Object.defineProperty(target, '__spreadingRedacted', { value: true });
    return target;
};

module.exports = { redactSensitive, installConsoleRedaction };
