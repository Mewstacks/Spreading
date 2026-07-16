'use strict';

// Quem esta segurando o perfil do Chromium (LocalAuth) desta sessao.
//
// Por que isto existe: o watchdog (index.js) mata o worker com SIGKILL, e SIGKILL
// nao roda o `shutdown()`. O Chromium que o worker tinha aberto sobrevive, e o
// init reparenta ele no PID 1. No boot seguinte, removerLocksChromium apagava o
// SingletonLock — que o orfao VIVO ainda mantinha — e o initializeSession subia um
// SEGUNDO Chromium sobre o mesmo --user-data-dir.
//
// Dois Chromiums sobre um perfil corrompem o perfil: o pareamento nunca conclui,
// o `.paired` nunca e escrito (auth_store), o restore do boot nunca pega a sessao
// e a tela fica em "desconectado" para sempre. Cada restart sujo empilhava mais um
// orfao. Observado no laptop: dois processos (14:02 e 14:28) sobre .wwebjs_auth/1.
//
// Modulo puro: sem fs, sem process.kill. Recebe strings, devolve decisoes.
// Testavel em test/chromium_locks.test.js — os testes nunca importam o index.js.

// O Chromium aponta o SingletonLock para `<hostname>-<pid>`.
// O hostname pode conter hifens ('MacBook-Air-de-Pedro.local-76620'), entao o PID
// tem de ser casado pelo FIM da string — dividir no primeiro hifen daria o host
// errado e um PID nao numerico.
const donoDoSingletonLock = (alvoDoSymlink) => {
    const texto = typeof alvoDoSymlink === 'string' ? alvoDoSymlink.trim() : '';
    const match = /^(.+)-(\d+)$/.exec(texto);
    if (!match) return null;
    const pid = Number(match[2]);
    if (!Number.isInteger(pid) || pid <= 0) return null;
    return { host: match[1], pid };
};

// Este processo e mesmo um Chromium usando ESTE perfil?
//
// A checagem e inegociavel e nao e paranoia de estilo: PID e reciclado. O lock
// pode apontar para um numero que hoje pertence a outra coisa qualquer do laptop,
// e um SIGKILL cego mataria um processo inocente. Sem confirmacao, nao se mexe.
const ehChromiumDoPerfil = (cmdline, perfilDir) => {
    if (typeof cmdline !== 'string' || !cmdline) return false;
    if (typeof perfilDir !== 'string' || !perfilDir) return false;
    // O Chromium recebe o perfil como --user-data-dir=<dir>. Comparar o argumento
    // inteiro (e nao so procurar o caminho solto na linha) evita casar com um
    // processo que apenas menciona o diretorio — um `ps`, um editor, um tail.
    return cmdline.includes(`--user-data-dir=${perfilDir}`);
};

// Decide o que fazer com o dono do lock. Separado das duas funcoes acima para que
// a regra inteira caiba num teste, sem fs nem processos de verdade.
//
//   'liberar'  -> orfao nosso comprovado: pode matar antes de subir o Chromium
//   'ignorar'  -> lock sem dono, dono morto, ou PID que nao e este Chromium
const decidirSobreDono = ({ dono, vivo, cmdline, perfilDir }) => {
    if (!dono) return 'ignorar';                 // sem lock, ou formato inesperado
    if (!vivo) return 'ignorar';                 // lock velho: removerLocksChromium resolve
    if (!ehChromiumDoPerfil(cmdline, perfilDir)) return 'ignorar';  // PID reciclado
    return 'liberar';
};

module.exports = { donoDoSingletonLock, ehChromiumDoPerfil, decidirSobreDono };
