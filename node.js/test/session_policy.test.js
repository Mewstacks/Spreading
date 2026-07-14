'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { reconnectDelay, shouldPurgeAuth } = require('../session_policy');

test('reconnect uses bounded exponential backoff', () => {
    assert.equal(reconnectDelay(1, 5000, 60000), 5000);
    assert.equal(reconnectDelay(2, 5000, 60000), 10000);
    assert.equal(reconnectDelay(5, 5000, 60000), 60000);
    assert.equal(reconnectDelay(20, 5000, 60000), 60000);
});

test('authenticated corruption is purged earlier than pre-auth failure', () => {
    assert.equal(shouldPurgeAuth(1, true), false);
    assert.equal(shouldPurgeAuth(2, true), true);
    assert.equal(shouldPurgeAuth(2, false), false);
    assert.equal(shouldPurgeAuth(3, false), true);
});
