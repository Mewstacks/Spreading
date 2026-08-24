'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { headlessFromEnv } = require('../browser_mode');

test('WA_HEADLESS=0 usa Chromium visível dentro do Xvfb', () => {
    assert.equal(headlessFromEnv('0'), false);
});

test('modo headless continua sendo o default fora de produção', () => {
    assert.equal(headlessFromEnv(undefined), true);
    assert.equal(headlessFromEnv('1'), true);
});
