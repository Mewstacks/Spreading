'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const ledger = require('../idempotency_ledger');
const manifests = require('../session_manifest');
const { redactSensitive } = require('../safe_logging');

const temporaryDirectory = (prefix) => fs.mkdtempSync(path.join(os.tmpdir(), prefix));

test('manifest vincula exatamente organização, diretório e instance_id', (t) => {
    const root = temporaryDirectory('spreading-wa-manifest-');
    t.after(() => fs.rmSync(root, { recursive: true, force: true }));
    const authPath = path.join(root, 'org-session-a');

    const bound = manifests.bindManifest(authPath, 'org-a', 'org-session-a');
    assert.deepEqual(bound, { ok: true, status: 'adopted' });
    assert.equal(manifests.verifyManifest(authPath, 'org-a', 'org-session-a').ok, true);
    assert.equal(manifests.readManifest(authPath).organization_id, 'org-a');
    assert.equal(manifests.readManifest(authPath).cookies, undefined);

    const mismatch = manifests.bindManifest(authPath, 'org-b', 'org-session-a');
    assert.deepEqual(mismatch, { ok: false, status: 'manifest_mismatch' });
    assert.equal(manifests.isQuarantined(authPath), true);
    assert.equal(manifests.verifyManifest(authPath, 'org-b', 'org-session-a').ok, false);
});

test('diretório sem manifest é órfão e nunca é adotado por suposição', (t) => {
    const root = temporaryDirectory('spreading-wa-orphan-');
    t.after(() => fs.rmSync(root, { recursive: true, force: true }));
    const authPath = path.join(root, 'orphan-session');
    fs.mkdirSync(authPath);

    assert.deepEqual(
        manifests.verifyManifest(authPath, 'org-a', 'orphan-session'),
        { ok: false, status: 'orphan' },
    );
    assert.equal(manifests.readManifest(authPath), null);
});

test('ledger durável reproduz confirmação sem duplicar após reinício lógico', (t) => {
    const root = temporaryDirectory('spreading-wa-ledger-');
    const previous = process.env.WA_SEND_LEDGER_DIR;
    process.env.WA_SEND_LEDGER_DIR = root;
    t.after(() => {
        if (previous === undefined) delete process.env.WA_SEND_LEDGER_DIR;
        else process.env.WA_SEND_LEDGER_DIR = previous;
        fs.rmSync(root, { recursive: true, force: true });
    });
    const scope = { organizationId: 'org-a', sessionId: 'session-a', operationKey: 'op-1' };
    const started = ledger.begin(scope);
    ledger.markTransportStarted(started.scope);
    ledger.finish(started.scope, 200, {
        sucesso: true, resultado: 'enviado', mensagem_id: 'native-1',
        mensagem: 'conteúdo que não pode ir ao ledger', base64: 'c2VncmVkbw==',
    });

    const replay = ledger.begin(scope).replay;
    assert.equal(replay.phase, 'confirmed');
    assert.equal(replay.body.mensagem_id, 'native-1');
    assert.equal(replay.body.mensagem, undefined);
    assert.equal(replay.body.base64, undefined);
    assert.equal(fs.readdirSync(root).length, 1);
});

test('resultado perdido depois de transport_started fica incerto e não reenvia', (t) => {
    const root = temporaryDirectory('spreading-wa-uncertain-');
    const previous = process.env.WA_SEND_LEDGER_DIR;
    process.env.WA_SEND_LEDGER_DIR = root;
    t.after(() => {
        if (previous === undefined) delete process.env.WA_SEND_LEDGER_DIR;
        else process.env.WA_SEND_LEDGER_DIR = previous;
        fs.rmSync(root, { recursive: true, force: true });
    });
    const scope = { organizationId: 'org-a', sessionId: 'session-a', operationKey: 'op-lost' };
    const first = ledger.begin(scope);
    ledger.markTransportStarted(first.scope);

    const replay = ledger.begin(scope).replay;
    assert.equal(replay.phase, 'uncertain');
    assert.equal(replay.body.repetir, false);
    assert.equal(replay.body.sucesso, false);
});

test('escopo do ledger inclui organização e sessão e mascara identificador em erro', (t) => {
    const root = temporaryDirectory('spreading-wa-scope-');
    const previous = process.env.WA_SEND_LEDGER_DIR;
    process.env.WA_SEND_LEDGER_DIR = root;
    t.after(() => {
        if (previous === undefined) delete process.env.WA_SEND_LEDGER_DIR;
        else process.env.WA_SEND_LEDGER_DIR = previous;
        fs.rmSync(root, { recursive: true, force: true });
    });
    const a = { organizationId: 'org-a', sessionId: 'session-a', operationKey: 'same-op' };
    const b = { organizationId: 'org-b', sessionId: 'session-b', operationKey: 'same-op' };
    assert.notEqual(ledger.digest(a.organizationId, a.sessionId, a.operationKey),
        ledger.digest(b.organizationId, b.sessionId, b.operationKey));
    const started = ledger.begin(a);
    ledger.finish(started.scope, 400, {
        sucesso: false, classe: 'permanente',
        erro: 'destino +55 11 99888-7766 inválido',
    });
    const persisted = ledger.getOperation(a);
    assert.equal(persisted.phase, 'permanent_failed');
    assert.doesNotMatch(persisted.body.erro, /99888|7766/);
    assert.match(persisted.body.erro, /mascarado/);
});

test('redação remove capabilities, cookies, query strings e conteúdo base64 longo', () => {
    const redacted = redactSensitive(
        `capability=header.payload.signature cookie=session-secret `
        + `https://worker.example/status?token=secret&x=1 ${'A'.repeat(300)}`,
    );

    assert.doesNotMatch(redacted, /header\.payload|session-secret|token=secret/);
    assert.match(redacted, /capability=\[redacted\]/);
    assert.match(redacted, /cookie=\[redacted\]/);
    assert.match(redacted, /\?\[redacted\]/);
    assert.match(redacted, /conteudo binario removido/);
});

test('console seguro redige objetos e texto de exceções antes de escrever', () => {
    const { installConsoleRedaction } = require('../safe_logging');
    const written = [];
    const target = { log: (...args) => written.push(args.join(' ')) };
    installConsoleRedaction(target);
    target.log({ token: 'object-secret' }, new Error('cookie=exception-secret'));
    assert.equal(written.length, 1);
    assert.doesNotMatch(written[0], /object-secret|exception-secret/);
    assert.match(written[0], /\[redacted\]/);
});
