'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { runtimePronto } = require('../session_readiness');

test('WWebJS com getChat libera a sessão mesmo sem window.Store', () => {
    assert.equal(runtimePronto({ WWebJS: { getChat() {} } }), true);
    assert.equal(runtimePronto({ WWebJS: {} }), false);
    assert.equal(runtimePronto({ Store: {} }), false);
});
