'use strict';

const DIAGNOSTIC_TIMEOUT_MS = 2000;

const limited = async (operation, fallback = null, timeoutMs = DIAGNOSTIC_TIMEOUT_MS) => {
    let timer;
    const timeout = new Promise((resolve) => {
        timer = setTimeout(() => resolve(fallback), timeoutMs);
    });
    try {
        return await Promise.race([
            Promise.resolve().then(operation).catch(() => fallback),
            timeout,
        ]);
    } finally {
        clearTimeout(timer);
    }
};

const sanitizeUrl = (value) => {
    if (!value) return null;
    try {
        const parsed = new URL(value);
        return `${parsed.origin}${parsed.pathname}`;
    } catch (_) {
        return String(value).split(/[?#]/, 1)[0].slice(0, 200);
    }
};

const collectBrowserDiagnostic = async (client) => {
    const browser = client?.pupBrowser || null;
    const page = client?.pupPage || null;
    const diagnostic = {
        browser: Boolean(browser),
        page: Boolean(page),
        browserVersion: null,
        url: null,
        title: null,
        readyState: null,
        wwebVersion: null,
        socketState: null,
    };

    if (browser) {
        diagnostic.browserVersion = await limited(() => browser.version());
    }
    if (!page) return diagnostic;

    diagnostic.url = sanitizeUrl(await limited(() => page.url()));
    diagnostic.title = await limited(() => page.title());
    const pageState = await limited(() => page.evaluate(() => {
        let socketState = null;
        try {
            socketState = window.require?.('WAWebSocketModel')?.Socket?.state || null;
        } catch (_) {
            socketState = null;
        }
        return {
            readyState: document.readyState || null,
            wwebVersion: window.Debug?.VERSION || null,
            socketState,
        };
    }), {});
    diagnostic.readyState = pageState?.readyState || null;
    diagnostic.wwebVersion = pageState?.wwebVersion || null;
    diagnostic.socketState = pageState?.socketState || null;
    return diagnostic;
};

module.exports = {
    collectBrowserDiagnostic,
    limited,
    sanitizeUrl,
};
