'use strict';

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { redactSensitive } = require('./safe_logging');

const ledgerDir = () => process.env.WA_SEND_LEDGER_DIR
    || path.join(process.env.WWEBJS_AUTH_DIR || path.join(process.cwd(), '.wwebjs_auth'),
        '.send-ledger');
const retentionMs = () => (parseInt(process.env.WA_SEND_LEDGER_RETENTION_HOURS, 10) || 168)
    * 60 * 60 * 1000;
const retrySelectedAfterMs = 2 * 60 * 1000;

const digest = (organizationId, sessionId, operationKey) => crypto
    .createHash('sha256')
    .update(`${organizationId}\0${sessionId}\0${operationKey}`, 'utf8')
    .digest('hex');
const filename = (scope) => path.join(ledgerDir(), `${scope}.json`);

const atomicWrite = (target, value) => {
    fs.mkdirSync(path.dirname(target), { recursive: true, mode: 0o700 });
    const temporary = `${target}.${process.pid}.${Date.now()}.tmp`;
    fs.writeFileSync(temporary, JSON.stringify(value), { mode: 0o600 });
    fs.renameSync(temporary, target);
};

const read = (scope) => {
    try { return JSON.parse(fs.readFileSync(filename(scope), 'utf8')); } catch (_) { return null; }
};

const write = (scope, patch) => {
    const current = read(scope) || {};
    const next = { ...current, ...patch, updated_at: new Date().toISOString() };
    atomicWrite(filename(scope), next);
    return next;
};

const begin = ({ organizationId, sessionId, operationKey }, now = Date.now()) => {
    const scope = digest(organizationId, sessionId, operationKey);
    const current = read(scope);
    if (current) {
        if (['confirmed', 'uncertain', 'permanent_failed'].includes(current.phase)) {
            return { scope, replay: current };
        }
        if (current.phase === 'transport_started') {
            const uncertain = write(scope, {
                phase: 'uncertain',
                status: 503,
                body: {
                    sucesso: false, resultado: 'incerto', repetir: false,
                    classe: 'transitorio', etapa: 'sendMessage',
                    erro: 'O transporte foi iniciado, mas a confirmação não foi recuperada.',
                },
            });
            return { scope, replay: uncertain };
        }
        const age = now - Date.parse(current.updated_at || current.created_at || 0);
        if (current.phase === 'selected' && age < retrySelectedAfterMs) {
            return { scope, inProgress: true };
        }
    }
    const createdAt = new Date(now).toISOString();
    write(scope, {
        version: 1, phase: 'selected', created_at: current?.created_at || createdAt,
        status: null, body: null,
    });
    return { scope };
};

const markTransportStarted = (scope) => write(scope, { phase: 'transport_started' });

const safeBody = (body) => {
    const source = body && typeof body === 'object' ? body : {};
    const out = {};
    for (const key of [
        'sucesso', 'resultado', 'repetir', 'classe', 'etapa', 'confirmacao',
        'mensagem_id', 'via', 'canal', 'falha_infra', 'duracao_ms',
    ]) {
        if (source[key] !== undefined) out[key] = source[key];
    }
    if (source.erro) {
        out.erro = redactSensitive(source.erro, 300);
    }
    return out;
};

const finish = (scope, status, body) => {
    let phase = 'preflight_failed';
    if (body?.sucesso && body?.resultado !== 'incerto'
        && !String(body?.confirmacao || '').startsWith('incerta')) {
        phase = 'confirmed';
    } else if (body?.resultado === 'incerto' || body?.resultado_incerto
        || String(body?.confirmacao || '').startsWith('incerta')
        || body?.confirmacao === 'aceita_sem_id') {
        phase = 'uncertain';
    } else if (body?.classe === 'permanente') {
        phase = 'permanent_failed';
    }
    return write(scope, { phase, status, body: safeBody(body) });
};

const prune = (now = Date.now()) => {
    let removed = 0;
    try {
        for (const entry of fs.readdirSync(ledgerDir(), { withFileTypes: true })) {
            if (!entry.isFile() || !/^[a-f0-9]{64}\.json$/.test(entry.name)) continue;
            const target = path.join(ledgerDir(), entry.name);
            if (now - fs.statSync(target).mtimeMs > retentionMs()) {
                fs.unlinkSync(target);
                removed += 1;
            }
        }
    } catch (err) {
        if (err.code !== 'ENOENT') throw err;
    }
    return removed;
};

const getOperation = ({ organizationId, sessionId, operationKey }) => read(
    digest(organizationId, sessionId, operationKey),
);

const resetForTests = () => {
    const target = path.resolve(ledgerDir());
    const cwd = path.resolve(process.cwd());
    if (!target.startsWith(`${cwd}${path.sep}`)) return false;
    fs.rmSync(target, { recursive: true, force: true });
    return true;
};

module.exports = {
    begin, finish, markTransportStarted, getOperation, prune, digest, resetForTests,
};
