'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
    collectBrowserDiagnostic,
    limited,
    sanitizeUrl,
} = require('../browser_diagnostics');

test('sanitizeUrl remove query e fragmento potencialmente sensiveis', () => {
    assert.equal(
        sanitizeUrl('https://web.whatsapp.com/path?token=segredo#estado'),
        'https://web.whatsapp.com/path'
    );
});

test('collectBrowserDiagnostic coleta apenas o estado limitado do navegador', async () => {
    const diagnostic = await collectBrowserDiagnostic({
        pupBrowser: {
            version: async () => 'Chrome/150.0.7871.124',
        },
        pupPage: {
            url: () => 'https://web.whatsapp.com/?token=nao-logar',
            title: async () => 'WhatsApp',
            evaluate: async () => ({
                readyState: 'complete',
                wwebVersion: '2.3000.123',
                socketState: 'UNPAIRED',
            }),
        },
    });

    assert.deepEqual(diagnostic, {
        browser: true,
        page: true,
        browserVersion: 'Chrome/150.0.7871.124',
        url: 'https://web.whatsapp.com/',
        title: 'WhatsApp',
        readyState: 'complete',
        wwebVersion: '2.3000.123',
        socketState: 'UNPAIRED',
    });
});

test('collectBrowserDiagnostic tolera pagina ausente ou morta', async () => {
    const semPagina = await collectBrowserDiagnostic({});
    assert.equal(semPagina.browser, false);
    assert.equal(semPagina.page, false);

    const morta = await collectBrowserDiagnostic({
        pupBrowser: { version: async () => { throw new Error('closed'); } },
        pupPage: {
            url: () => { throw new Error('detached'); },
            title: async () => { throw new Error('detached'); },
            evaluate: async () => { throw new Error('context destroyed'); },
        },
    });
    assert.equal(morta.browser, true);
    assert.equal(morta.page, true);
    assert.equal(morta.browserVersion, null);
    assert.equal(morta.url, null);
    assert.equal(morta.socketState, null);
});

test('limited encerra diagnostico pendurado sem propagar erro', async () => {
    const value = await limited(() => new Promise(() => {}), 'timeout', 5);
    assert.equal(value, 'timeout');
});
