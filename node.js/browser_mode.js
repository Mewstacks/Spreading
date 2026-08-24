'use strict';

function headlessFromEnv(value) {
    return value !== '0';
}

module.exports = { headlessFromEnv };
