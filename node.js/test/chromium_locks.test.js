'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
    donoDoSingletonLock, ehChromiumDoPerfil, decidirSobreDono, pidsDoPerfil,
} = require('../chromium_locks');

const PERFIL = '/app/.wwebjs_auth/1/session';
const CMDLINE_NOSSA = `/usr/bin/chromium --no-sandbox --user-data-dir=${PERFIL} --headless=new`;

// Valor real lido do laptop: o hostname tem hifens, entao dividir no primeiro
// daria host='MacBook' e pid='Air-de-Pedro.local-76620'.
test('le o dono de um lock com hifen no hostname', () => {
    assert.deepEqual(
        donoDoSingletonLock('MacBook-Air-de-Pedro.local-76620'),
        { host: 'MacBook-Air-de-Pedro.local', pid: 76620 }
    );
});

test('le o dono de um lock com hostname simples', () => {
    assert.deepEqual(donoDoSingletonLock('fly-worker-42'), { host: 'fly-worker', pid: 42 });
});

test('recusa alvo sem pid, sem hifen ou vazio', () => {
    for (const alvo of ['semhifen', 'host-', '-123', '', '   ', 'host-abc']) {
        assert.equal(donoDoSingletonLock(alvo), null, `alvo: ${JSON.stringify(alvo)}`);
    }
});

test('recusa entrada que nem string e (readlink falhou)', () => {
    for (const alvo of [undefined, null, 0, {}, []]) {
        assert.equal(donoDoSingletonLock(alvo), null);
    }
});

test('reconhece o Chromium que usa este perfil', () => {
    assert.equal(ehChromiumDoPerfil(CMDLINE_NOSSA, PERFIL), true);
});

// A protecao central: PID e reciclado. Sem ela, um SIGKILL cego mataria o que
// quer que hoje ocupe o numero que o lock velho aponta.
test('nao reconhece um Chromium de OUTRO perfil', () => {
    const outro = '/usr/bin/chromium --user-data-dir=/app/.wwebjs_auth/2/session';
    assert.equal(ehChromiumDoPerfil(outro, PERFIL), false);
});

test('nao reconhece um processo qualquer que so mencione o diretorio', () => {
    assert.equal(ehChromiumDoPerfil(`tail -f ${PERFIL}/chrome_debug.log`, PERFIL), false);
    assert.equal(ehChromiumDoPerfil(`vim ${PERFIL}`, PERFIL), false);
});

test('cmdline ausente ou perfil ausente nunca casa', () => {
    assert.equal(ehChromiumDoPerfil('', PERFIL), false);
    assert.equal(ehChromiumDoPerfil(null, PERFIL), false);
    assert.equal(ehChromiumDoPerfil(undefined, PERFIL), false);
    assert.equal(ehChromiumDoPerfil(CMDLINE_NOSSA, ''), false);
    assert.equal(ehChromiumDoPerfil(CMDLINE_NOSSA, null), false);
});

// O cenario real: worker morto por SIGKILL (watchdog), Chromium reparentado no
// PID 1 e ainda segurando o lock. E o unico caso que autoriza matar.
test('orfao nosso e vivo: libera o perfil', () => {
    const dono = donoDoSingletonLock('MacBook-Air-de-Pedro.local-76620');
    assert.equal(
        decidirSobreDono({ dono, vivo: true, cmdline: CMDLINE_NOSSA, perfilDir: PERFIL }),
        'liberar'
    );
});

test('dono morto: nao mata ninguem, o lock velho e so sujeira', () => {
    const dono = donoDoSingletonLock('host-4242');
    assert.equal(
        decidirSobreDono({ dono, vivo: false, cmdline: '', perfilDir: PERFIL }),
        'ignorar'
    );
});

test('PID reciclado por outro processo: ignora em vez de matar inocente', () => {
    const dono = donoDoSingletonLock('host-4242');
    assert.equal(
        decidirSobreDono({
            dono, vivo: true, cmdline: '/usr/bin/postgres -D /var/lib/pg', perfilDir: PERFIL,
        }),
        'ignorar'
    );
});

test('sem lock no perfil: nada a fazer', () => {
    assert.equal(
        decidirSobreDono({ dono: null, vivo: false, cmdline: '', perfilDir: PERFIL }),
        'ignorar'
    );
});

test('limpeza forte seleciona somente processos do perfil exato', () => {
    const processos = [
        { pid: 101, cmdline: CMDLINE_NOSSA },
        {
            pid: 102,
            cmdline: '/usr/bin/chromium --user-data-dir=/app/.wwebjs_auth/2/session',
        },
        { pid: 103, cmdline: `tail -f ${PERFIL}/chrome_debug.log` },
        { pid: 104, cmdline: `${CMDLINE_NOSSA} --type=renderer` },
        { pid: 0, cmdline: CMDLINE_NOSSA },
    ];

    assert.deepEqual(pidsDoPerfil(processos, PERFIL), [101, 104]);
    assert.deepEqual(pidsDoPerfil(processos, '/app/.wwebjs_auth/2/session'), [102]);
});

test('snapshot de processos hostil ou ausente nao seleciona ninguem', () => {
    assert.deepEqual(pidsDoPerfil(null, PERFIL), []);
    assert.deepEqual(pidsDoPerfil({}, PERFIL), []);
    assert.deepEqual(pidsDoPerfil([{ pid: 1, cmdline: null }], PERFIL), []);
});
