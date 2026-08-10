'use strict';

const fs = require('fs');
const path = require('path');

const MANIFEST = '.spreading-session.json';
const QUARANTINE = '.spreading-quarantined';

const safeId = (value) => String(value || '').trim();

const atomicJson = (target, value) => {
    const dir = path.dirname(target);
    fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
    const temporary = `${target}.${process.pid}.${Date.now()}.tmp`;
    fs.writeFileSync(temporary, JSON.stringify(value), { mode: 0o600 });
    fs.renameSync(temporary, target);
};

const readManifest = (authPath) => {
    try {
        const parsed = JSON.parse(fs.readFileSync(path.join(authPath, MANIFEST), 'utf8'));
        if (parsed?.version !== 1 || !safeId(parsed.organization_id)
            || !safeId(parsed.instance_id)) return null;
        return parsed;
    } catch (_) {
        return null;
    }
};

const bindManifest = (authPath, organizationId, instanceId) => {
    const organization = safeId(organizationId);
    const instance = safeId(instanceId);
    if (!organization || !instance || path.basename(authPath) !== instance) {
        return { ok: false, status: 'invalid_binding' };
    }
    const current = readManifest(authPath);
    if (current && (current.organization_id !== organization
        || current.instance_id !== instance)) {
        quarantine(authPath, 'manifest_mismatch');
        return { ok: false, status: 'manifest_mismatch' };
    }
    atomicJson(path.join(authPath, MANIFEST), {
        version: 1,
        organization_id: organization,
        instance_id: instance,
        bound_at: current?.bound_at || new Date().toISOString(),
        updated_at: new Date().toISOString(),
    });
    try { fs.unlinkSync(path.join(authPath, QUARANTINE)); } catch (err) {
        if (err.code !== 'ENOENT') throw err;
    }
    return { ok: true, status: current ? 'consistent' : 'adopted' };
};

function quarantine(authPath, reason = 'orphan') {
    try {
        fs.mkdirSync(authPath, { recursive: true, mode: 0o700 });
        fs.writeFileSync(path.join(authPath, QUARANTINE), String(reason).slice(0, 80), {
            mode: 0o600,
        });
        return true;
    } catch (_) {
        return false;
    }
}

const isQuarantined = (authPath) => fs.existsSync(path.join(authPath, QUARANTINE));

const verifyManifest = (authPath, organizationId, instanceId) => {
    const current = readManifest(authPath);
    if (!current) return { ok: false, status: 'orphan' };
    const ok = current.organization_id === safeId(organizationId)
        && current.instance_id === safeId(instanceId)
        && path.basename(authPath) === safeId(instanceId);
    return { ok, status: ok ? 'consistent' : 'manifest_mismatch' };
};

module.exports = {
    MANIFEST, QUARANTINE, readManifest, bindManifest, verifyManifest,
    quarantine, isQuarantined,
};
