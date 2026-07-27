let josePromise;
let keyCache;
const replayCache = new Map();
const idempotencyCache = new Map();

const loadJose = () => {
    if (!josePromise) josePromise = import('jose');
    return josePromise;
};

const publicKeys = async () => {
    if (keyCache) return keyCache;
    let parsed;
    try {
        const encoded = process.env.WA_CAPABILITY_PUBLIC_KEYS_JSON_B64 || '';
        const raw = process.env.WA_CAPABILITY_PUBLIC_KEYS_JSON
            || (encoded ? Buffer.from(encoded, 'base64url').toString('utf8') : '{}');
        parsed = JSON.parse(raw);
    } catch (_) {
        throw new Error('WA_CAPABILITY_PUBLIC_KEYS_JSON invalido');
    }
    const { importJWK } = await loadJose();
    const entries = await Promise.all(Object.entries(parsed).map(async ([kid, x]) => {
        if (!kid || typeof x !== 'string') throw new Error('chave publica invalida');
        const key = await importJWK({ kty: 'OKP', crv: 'Ed25519', x, kid }, 'EdDSA');
        return [kid, key];
    }));
    keyCache = new Map(entries);
    if (!keyCache.size) throw new Error('nenhuma chave publica WhatsApp configurada');
    return keyCache;
};

const prune = (cache, now = Date.now()) => {
    for (const [key, value] of cache) {
        if (value.expiresAt <= now) cache.delete(key);
    }
};

const verifyCapability = async (token, { action, sessionId, singleUse = false }) => {
    const { jwtVerify } = await loadJose();
    const keys = await publicKeys();
    const result = await jwtVerify(
        token,
        async (header) => {
            if (header.alg !== 'EdDSA' || header.typ !== 'JWT' || !header.kid) {
                throw new Error('cabecalho JWT invalido');
            }
            const key = keys.get(header.kid);
            if (!key) throw new Error('kid desconhecido');
            return key;
        },
        {
            algorithms: ['EdDSA'],
            issuer: process.env.WA_CAPABILITY_ISSUER || 'spreading-web',
            audience: process.env.WA_CAPABILITY_AUDIENCE || 'spreading-wa',
            clockTolerance: 5,
            requiredClaims: [
                'sub', 'organization_id', 'session_id', 'actions',
                'iat', 'exp', 'jti',
            ],
        },
    );
    const { payload } = result;
    if (payload.sub !== payload.organization_id) throw new Error('tenant divergente');
    if (!Array.isArray(payload.actions) || !payload.actions.includes(action)) {
        throw new Error('acao nao autorizada');
    }
    if (String(payload.session_id) !== String(sessionId)) {
        throw new Error('sessao divergente');
    }
    if (singleUse && payload.single_use !== true) {
        throw new Error('capacidade deveria ser de uso unico');
    }
    if (singleUse) {
        prune(replayCache);
        if (replayCache.has(payload.jti)) throw new Error('replay detectado');
        replayCache.set(payload.jti, { expiresAt: Number(payload.exp) * 1000 });
    }
    return payload;
};

const capabilityAuth = (action, resolveSessionId, { singleUse = false } = {}) => (
    async (req, res, next) => {
        const sessionId = resolveSessionId(req);
        const authorization = String(req.headers.authorization || '');
        const match = authorization.match(/^Bearer ([A-Za-z0-9._~-]+)$/);
        try {
            if (match) {
                req.capability = await verifyCapability(
                    match[1], { action, sessionId, singleUse },
                );
                return next();
            }
        } catch (_) {
            // Resposta deliberadamente genérica: não revela claim, kid ou sessão.
        }
        return res.status(401).json({ erro: 'Acesso não autorizado.' });
    }
);

const idempotencyGuard = (req, res, next) => {
    prune(idempotencyCache);
    const key = String(req.headers['idempotency-key'] || '');
    if (!/^[A-Za-z0-9._:-]{8,128}$/.test(key)) {
        return res.status(400).json({
            erro: 'Idempotency-Key ausente ou inválida.',
            classe: 'permanente',
        });
    }
    const scopedKey = [
        req.capability.organization_id,
        req.capability?.session_id || 'unknown',
        key,
    ].join(':');
    const cached = idempotencyCache.get(scopedKey);
    if (cached) return res.status(cached.status).json(cached.body);

    const originalJson = res.json.bind(res);
    res.json = (body) => {
        if (res.statusCode < 500 || body?.sucesso || body?.resultado_incerto) {
            idempotencyCache.set(scopedKey, {
                status: res.statusCode,
                body,
                expiresAt: Date.now() + 10 * 60 * 1000,
            });
        }
        return originalJson(body);
    };
    next();
};

const resetCachesForTests = () => {
    keyCache = null;
    replayCache.clear();
    idempotencyCache.clear();
};

module.exports = {
    capabilityAuth,
    idempotencyGuard,
    verifyCapability,
    resetCachesForTests,
};
