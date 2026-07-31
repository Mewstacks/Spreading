'use strict';

const fs = require('fs');
const path = require('path');

// Credencial do WhatsApp em disco (LocalAuth do whatsapp-web.js).
//
// Layout, confirmado na fonte da lib (src/authStrategies/LocalAuth.js, v1.34.7):
//   <authPath>/session/            <- userDataDir do Chromium
//   <authPath>/.paired             <- marcador NOSSO (ver abaixo)
//
// Por que um marcador proprio: LocalAuth.beforeBrowserInitialized() faz
// mkdirSync(session/) no INICIO do initialize, antes de qualquer pareamento, e
// o Chromium cria session/Default/ ao abrir a pagina. Ou seja, a existencia dos
// diretorios NAO prova que alguem escaneou o QR — quem tentou conectar uma vez
// e desistiu deixa a mesma estrutura no volume. Usar o layout como sinal daria
// falso positivo e o restore no boot subiria um Chromium (~350MB) por sessao
// nunca pareada, estourando MAX_WHATSAPP_SESSIONS. O marcador e escrito no
// evento `authenticated` e some junto com o diretorio no purge.

const PAIRED_MARKER = '.paired';

// Sinal legado, para sessoes ja pareadas antes de o marcador existir: o
// IndexedDB do web.whatsapp.com so aparece depois que a pagina persistiu dados.
// E menos preciso que o marcador (pode existir sem pareamento), mas o custo de
// um falso positivo e limitado: sobe o Chromium, nao acha credencial, mostra QR,
// e o QR_IDLE_DESTROY_MS libera o slot. Auto-corrige no primeiro pareamento.
const LEGACY_HINT = path.join(
    'session', 'Default', 'IndexedDB', 'https_web.whatsapp.com_0.indexeddb.leveldb'
);

// Impede que um instanceId vindo do cliente escape da raiz de auth.
// O id passa por sanitizeInstanceId no index.js, mas esta guarda e a ultima
// linha de defesa antes de um rm -rf recursivo. Nao remover.
const dentroDaRaiz = (authRootPath, authPath) => {
    const raiz = path.resolve(authRootPath);
    const alvo = path.resolve(authPath);
    return alvo.startsWith(`${raiz}${path.sep}`);
};

// true se esta sessao ja foi pareada e a credencial continua no volume.
const hasStoredAuth = (authRootPath, authPath) => {
    if (!dentroDaRaiz(authRootPath, authPath)) return false;
    if (fs.existsSync(path.join(authPath, PAIRED_MARKER))) return true;
    return fs.existsSync(path.join(authPath, LEGACY_HINT));
};

// Chamado no evento `authenticated`: a partir daqui a credencial vale a pena
// restaurar num boot futuro.
const markPaired = (authRootPath, authPath) => {
    if (!dentroDaRaiz(authRootPath, authPath)) return false;
    try {
        fs.mkdirSync(authPath, { recursive: true });
        fs.writeFileSync(path.join(authPath, PAIRED_MARKER), new Date().toISOString());
        return true;
    } catch (err) {
        console.error(`Falha ao marcar sessao pareada em ${authPath}:`, err.message);
        return false;
    }
};

// Remove apenas o nosso marcador de pareamento, sem tocar no perfil que o
// whatsapp-web.js ainda está limpando/recriando. Isso é necessário no redirect
// post_logout=1: a própria biblioteca apaga session/ e injeta um QR novo. Apagar
// authPath inteiro em paralelo interrompe essa reinjeção e deixa frames órfãos.
const clearPaired = (authRootPath, authPath) => {
    if (!dentroDaRaiz(authRootPath, authPath)) return false;
    try {
        fs.unlinkSync(path.join(authPath, PAIRED_MARKER));
        return true;
    } catch (err) {
        if (err.code === 'ENOENT') return true;
        console.error(`Falha ao desmarcar sessao pareada em ${authPath}:`, err.message);
        return false;
    }
};

// Apaga o perfil LocalAuth inteiro (credencial + marcador). Idempotente.
const purgeAuthDir = (authRootPath, authPath, reason) => {
    if (!dentroDaRaiz(authRootPath, authPath)) {
        console.error(`Recusando purge fora da raiz de auth: ${authPath}`);
        return false;
    }
    try {
        fs.rmSync(path.resolve(authPath), { recursive: true, force: true });
        console.error(`Perfil LocalAuth removido (${reason}): ${authPath}`);
        return true;
    } catch (err) {
        console.error(`Falha ao limpar perfil LocalAuth ${authPath}:`, err.message);
        return false;
    }
};

module.exports = { hasStoredAuth, markPaired, clearPaired, purgeAuthDir, PAIRED_MARKER };
