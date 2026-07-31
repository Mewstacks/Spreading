const assert = require('node:assert/strict');
const { generateKeyPairSync } = require('node:crypto');
const test = require('node:test');

const {
    idempotencyGuard,
    resetCachesForTests,
    verifyCapability,
} = require('./capability_auth');

let privateKey;

const sign = async ({
    action = 'send',
    sessionId = 'session-a',
    organizationId = 'org-a',
    singleUse = false,
    expiresAt,
} = {}) => {
    const { SignJWT } = await import('jose');
    const now = Math.floor(Date.now() / 1000);
    return new SignJWT({
        organization_id: organizationId,
        session_id: sessionId,
        actions: [action],
        single_use: singleUse,
    })
        .setProtectedHeader({ alg: 'EdDSA', typ: 'JWT', kid: 'test-v1' })
        .setIssuer('spreading-web')
        .setAudience('spreading-wa')
        .setSubject(organizationId)
        .setIssuedAt(now)
        .setJti(`jti-${Math.random()}`)
        .setExpirationTime(expiresAt ?? now + 30)
        .sign(privateKey);
};

test.beforeEach(() => {
    ({ privateKey } = generateKeyPairSync('ed25519'));
    const jwk = privateKey.export({ format: 'jwk' });
    process.env.WA_CAPABILITY_PUBLIC_KEYS_JSON = JSON.stringify({
        'test-v1': jwk.x,
    });
    delete process.env.WA_CAPABILITY_PUBLIC_KEYS_JSON_B64;
    process.env.WA_CAPABILITY_ISSUER = 'spreading-web';
    process.env.WA_CAPABILITY_AUDIENCE = 'spreading-wa';
    delete process.env.API_KEY;
    resetCachesForTests();
});

test('loads public keyring from base64url secret', async () => {
    const raw = process.env.WA_CAPABILITY_PUBLIC_KEYS_JSON;
    process.env.WA_CAPABILITY_PUBLIC_KEYS_JSON_B64 = Buffer.from(raw)
        .toString('base64url');
    delete process.env.WA_CAPABILITY_PUBLIC_KEYS_JSON;
    resetCachesForTests();

    const payload = await verifyCapability(await sign(), {
        action: 'send',
        sessionId: 'session-a',
    });
    assert.equal(payload.organization_id, 'org-a');
});

test('accepts only the bound tenant, session and action', async () => {
    const token = await sign();
    const payload = await verifyCapability(token, {
        action: 'send',
        sessionId: 'session-a',
    });

    assert.equal(payload.organization_id, 'org-a');
    assert.equal(payload.session_id, 'session-a');
    await assert.rejects(
        verifyCapability(token, { action: 'reset', sessionId: 'session-a' }),
    );
    await assert.rejects(
        verifyCapability(token, { action: 'send', sessionId: 'session-b' }),
    );
});

test('rejects expired capabilities', async () => {
    const token = await sign({
        expiresAt: Math.floor(Date.now() / 1000) - 30,
    });
    await assert.rejects(
        verifyCapability(token, { action: 'send', sessionId: 'session-a' }),
    );
});

test('single-use destructive capability rejects replay', async () => {
    const token = await sign({ action: 'reset', singleUse: true });
    const request = { action: 'reset', sessionId: 'session-a', singleUse: true };

    await verifyCapability(token, request);
    await assert.rejects(verifyCapability(token, request), /replay/);
});

const fakeResponse = () => {
    const response = {
        statusCode: 200,
        body: null,
        status(code) {
            this.statusCode = code;
            return this;
        },
        json(body) {
            this.body = body;
            return this;
        },
    };
    return response;
};

test('idempotency is isolated by tenant and session', () => {
    const key = 'operation-123';
    const firstReq = {
        headers: { 'idempotency-key': key },
        capability: { organization_id: 'org-a', session_id: 'session-a' },
    };
    const firstRes = fakeResponse();
    idempotencyGuard(firstReq, firstRes, () => firstRes.json({ sucesso: true }));

    const duplicateRes = fakeResponse();
    idempotencyGuard(firstReq, duplicateRes, () => {
        throw new Error('duplicate should have returned cached response');
    });
    assert.deepEqual(duplicateRes.body, { sucesso: true });

    const otherSessionRes = fakeResponse();
    idempotencyGuard({
        ...firstReq,
        capability: { organization_id: 'org-a', session_id: 'session-b' },
    }, otherSessionRes, () => otherSessionRes.json({ sucesso: true, other: true }));
    assert.deepEqual(otherSessionRes.body, { sucesso: true, other: true });
});

test('x-api-key is never accepted', async () => {
    const { capabilityAuth } = require('./capability_auth');
    process.env.API_KEY = 'legacy-secret';
    const middleware = capabilityAuth('status', () => 'session-a');
    const req = {
        headers: { 'x-api-key': 'legacy-secret' },
    };
    const res = fakeResponse();
    let called = false;

    await middleware(req, res, () => { called = true; });
    assert.equal(called, false);
    assert.equal(res.statusCode, 401);
});
