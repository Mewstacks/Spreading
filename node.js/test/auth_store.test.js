'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { hasStoredAuth, markPaired, purgeAuthDir } = require('../auth_store');

const comRaizTemp = (fn) => {
    const raiz = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-auth-'));
    try {
        return fn(raiz);
    } finally {
        fs.rmSync(raiz, { recursive: true, force: true });
    }
};

test('an unpaired session is not restorable', () => {
    comRaizTemp((raiz) => {
        const authPath = path.join(raiz, 'u1');
        assert.equal(hasStoredAuth(raiz, authPath), false, 'diretorio inexistente');

        // O estado que LocalAuth deixa ANTES de qualquer scan de QR:
        // beforeBrowserInitialized() cria session/ e o Chromium cria Default/.
        // Isto nao pode contar como pareado, senao o restore no boot sobe um
        // Chromium para todo mundo que abriu a aba uma vez e desistiu.
        fs.mkdirSync(path.join(authPath, 'session', 'Default'), { recursive: true });
        assert.equal(hasStoredAuth(raiz, authPath), false, 'session/Default vazio');
    });
});

test('a paired session is restorable', () => {
    comRaizTemp((raiz) => {
        const authPath = path.join(raiz, 'u1');
        fs.mkdirSync(path.join(authPath, 'session'), { recursive: true });
        assert.equal(markPaired(raiz, authPath), true);
        assert.equal(hasStoredAuth(raiz, authPath), true);
    });
});

test('sessions paired before the marker existed are still restorable', () => {
    comRaizTemp((raiz) => {
        const authPath = path.join(raiz, 'u1');
        fs.mkdirSync(
            path.join(authPath, 'session', 'Default', 'IndexedDB',
                'https_web.whatsapp.com_0.indexeddb.leveldb'),
            { recursive: true },
        );
        assert.equal(hasStoredAuth(raiz, authPath), true);
    });
});

test('purge removes the credential and makes it unrestorable', () => {
    comRaizTemp((raiz) => {
        const authPath = path.join(raiz, 'u1');
        fs.mkdirSync(path.join(authPath, 'session'), { recursive: true });
        markPaired(raiz, authPath);

        assert.equal(purgeAuthDir(raiz, authPath, 'teste'), true);
        assert.equal(fs.existsSync(authPath), false);
        assert.equal(hasStoredAuth(raiz, authPath), false);
        // Idempotente: purgar de novo nao explode.
        assert.equal(purgeAuthDir(raiz, authPath, 'teste'), true);
    });
});

test('purge never escapes the auth root', () => {
    comRaizTemp((raiz) => {
        const vitima = path.join(raiz, '..', `vitima-${path.basename(raiz)}`);
        fs.mkdirSync(vitima, { recursive: true });
        try {
            // Caminho montado a partir de um instanceId hostil.
            assert.equal(purgeAuthDir(raiz, path.join(raiz, '..', path.basename(vitima)), 'ataque'), false);
            assert.equal(purgeAuthDir(raiz, '/etc', 'ataque'), false);
            assert.equal(purgeAuthDir(raiz, raiz, 'ataque'), false, 'a propria raiz');
            assert.equal(fs.existsSync(vitima), true, 'diretorio fora da raiz foi apagado');

            assert.equal(hasStoredAuth(raiz, '/etc'), false);
            assert.equal(markPaired(raiz, path.join(raiz, '..', 'fora')), false);
        } finally {
            fs.rmSync(vitima, { recursive: true, force: true });
        }
    });
});
