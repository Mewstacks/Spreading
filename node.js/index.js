// O dotenv resolve o .env a partir do cwd, nao deste arquivo. Sem o path
// explicito, `node node.js/index.js` rodado da raiz do repo nao acha este .env:
// o PORT cai no default 3000, colide com o dev server de outro projeto e o
// worker morre no boot com EADDRINUSE.
require('dotenv').config({ path: require('path').join(__dirname, '.env') });
const { Client, LocalAuth, MessageMedia } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const express = require('express');
const helmet = require('helmet');
const rateLimit = require('express-rate-limit');
const crypto = require('crypto');
const path = require('path');
const fs = require('fs');
const { spawn, execFileSync } = require('child_process');
const {
    reconnectDelay, shouldPurgeAuth, reconnectAction, isRevokedReason, ocupaSlot,
    groupRetryDelay, qrBootstrapOutcome,
} = require('./session_policy');
const {
    resetSessionForQr, markResetFailure, markQrBootstrap,
    decidirRestauracao, MOTIVO_FALHA_RESET,
} = require('./session_reset');
const { iniciarSync } = require('./group_sync');
const {
    coletarGrupos, inspecionarGrupo, idChatValido, descreverErro,
} = require('./group_reader');
const {
    buildSessionPayload, buildGruposPayload, buildInativoPayload,
} = require('./payloads');
const {
    confirmarMensagem, erroReloadEmVoo, opcoesDeEnvio, repetirSeFrameDestacado,
} = require('./message_confirmation');
const {
    TRANSITORIO, PERMANENTE, erroClassificado, classificarErro, erroStoreQuebrado,
} = require('./error_taxonomy');
const {
    donoDoSingletonLock, decidirSobreDono, pidsDoPerfil,
} = require('./chromium_locks');
const authStore = require('./auth_store');
const { runtimePronto } = require('./session_readiness');
const { criarPrazo, expirou, timeoutDaEtapa, timeoutComEnvioIniciado } = require('./send_deadline');
const {
    timeoutPreflight, mensagemPreflight, registrarStoreIndisponivel,
    mensagemEstabilizacao, deveReciclarTimeoutPreflight, iniciarRecuperacaoPreflight,
} = require('./preflight_recovery');
const { aguardarStorePronto } = require('./store_ready');

const app = express();

app.use(helmet());
app.use(express.json({ limit: '50mb' }));
app.use(express.urlencoded({ limit: '50mb', extended: true }));

// Path-scoped de proposito: montado global, alcancaria /api/status e /api/grupos
// e injetaria `erro` neles — a chave que o Django le como "Node inalcancavel"
// (whatsapp_client.py) e que o front usa para dizer "servico fora do ar".
const limiter = rateLimit({
    windowMs: 1 * 60 * 1000,
    max: 30,
    // classe transitoria: ser barrado pelo limite e o oposto de um problema de
    // configuracao. Sem isto o Django contava o 429 como falha da config e, na
    // quinta, desligava a automacao de quem so estava enviando rapido demais.
    message: {
        erro: 'Muitas requisições. O limite é de 30 mensagens por minuto para proteger a conta.',
        classe: TRANSITORIO,
    },
});
app.use('/api/enviar', limiter);

const apiKeyAuth = (req, res, next) => {
    const key = req.headers['x-api-key'];
    const expected = process.env.API_KEY;
    const keyValid =
        key &&
        expected &&
        key.length === expected.length &&
        crypto.timingSafeEqual(Buffer.from(key), Buffer.from(expected));

    if (!keyValid) {
        return res.status(401).json({ erro: 'Acesso não autorizado. API Key inválida ou ausente.' });
    }
    next();
};

const MIMETYPES_PERMITIDOS = new Set([
    'image/jpeg', 'image/png', 'image/gif', 'image/webp',
    'video/mp4', 'video/3gpp',
    'audio/mpeg', 'audio/ogg', 'audio/opus',
    'application/pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
]);


const authRootPath = path.join(process.cwd(), '.wwebjs_auth');
const DEFAULT_INSTANCE_ID = process.env.DEFAULT_INSTANCE_ID || 'default';

const sanitizeInstanceId = (value) => {
    const raw = (value || '').toString().trim().toLowerCase();
    const normalized = raw.replace(/[^a-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '');
    return normalized || DEFAULT_INSTANCE_ID;
};

const RECONNECT_DELAY_MS = parseInt(process.env.RECONNECT_DELAY_MS, 10) || 5000;
const RECONNECT_MAX_DELAY_MS = parseInt(process.env.RECONNECT_MAX_DELAY_MS, 10) || 60000;
// Teto por ciclo de recuperacao. Com o backoff (5s..60s), 6 tentativas ~= 3,2min;
// dois ciclos (retry -> purge -> retry) ~= 6,5min ate a sessao expirar de vez.
// Sem teto, o contador so crescia e o usuario via "tentativa 38..." para sempre.
const RECONNECT_MAX_ATTEMPTS = parseInt(process.env.RECONNECT_MAX_ATTEMPTS, 10) || 6;
const SESSION_START_STAGGER_MS = parseInt(process.env.SESSION_START_STAGGER_MS, 10) || 12000;
const PUPPETEER_EXECUTABLE_PATH = process.env.PUPPETEER_EXECUTABLE_PATH || undefined;
const PRINT_QR_TO_LOGS = process.env.PRINT_QR_TO_LOGS === '1';
const WATCHDOG_TIMEOUT_MS = parseInt(process.env.WATCHDOG_TIMEOUT_MS, 10) || 45000;
const WATCHDOG_INTERVAL_MS = parseInt(process.env.WATCHDOG_INTERVAL_MS, 10) || 5000;
const MAX_WHATSAPP_SESSIONS = parseInt(process.env.MAX_WHATSAPP_SESSIONS, 10) || 4;
const SESSION_INIT_TIMEOUT_MS = parseInt(process.env.SESSION_INIT_TIMEOUT_MS, 10) || 90000;
// O teto por tentativa fica ABAIXO de WATCHDOG_TIMEOUT_MS (45s): um boot frio de
// Chromium sob pressão de memória no Fly podia estourar os 60s antigos e, pior,
// deixar a janela da tentativa passar do limite do watchdog — que então matava o
// worker inteiro no meio do bootstrap. Menos por tentativa, mais tentativas.
const QR_BOOTSTRAP_TIMEOUT_MS = parseInt(process.env.QR_BOOTSTRAP_TIMEOUT_MS, 10) || 40000;
const QR_BOOTSTRAP_MAX_ATTEMPTS =
    parseInt(process.env.QR_BOOTSTRAP_MAX_ATTEMPTS, 10) || 3;
const QR_BOOTSTRAP_RETRY_MS = parseInt(process.env.QR_BOOTSTRAP_RETRY_MS, 10) || 2000;
// 15s e folgado: a leitura so percorre a collection em memoria da pagina, sem
// round-trip de rede. Estourar aqui significa pagina morta, nao lentidao — por
// isso nao vale mais os 45s que existiam quando isto era um getChats completo.
const GROUP_SYNC_TIMEOUT_MS = parseInt(process.env.GROUP_SYNC_TIMEOUT_MS, 10) || 15000;
const QR_IDLE_DESTROY_MS = parseInt(process.env.QR_IDLE_DESTROY_MS, 10) || 180000;
const SEND_TIMEOUT_MS = parseInt(process.env.SEND_TIMEOUT_MS, 10) || 60000;
// Tem de ser menor que o read timeout do Django. Inclui o tempo esperando a
// cadeia da sessao, nao apenas o sendMessage do Chromium.
const SEND_REQUEST_TIMEOUT_MS = parseInt(process.env.SEND_REQUEST_TIMEOUT_MS, 10) || 55000;
const MIN_SEND_INTERVAL_MS = parseInt(process.env.MIN_SEND_INTERVAL_MS, 10) || 2500;
// O evento `ready` do whatsapp-web.js pode chegar antes de WWebJS terminar de
// injetar. O worker só libera envios depois do primeiro sync de grupos, para
// não disputar o Chromium durante o pareamento.
const STORE_READY_WAIT_MS = parseInt(process.env.STORE_READY_WAIT_MS, 10) || 8000;
const READY_STORE_WAIT_MS = parseInt(process.env.READY_STORE_WAIT_MS, 10) || 10000;
const CONNECTION_STABILIZATION_MS = parseInt(process.env.CONNECTION_STABILIZATION_MS, 10) || 120000;
const READY_RETRY_MS = parseInt(process.env.READY_RETRY_MS, 10) || 5000;

const startWatchdog = () => {
    if (process.env.DISABLE_WATCHDOG === '1') return null;

    const script = `
        const pid = Number(process.argv[1]);
        const timeoutMs = Number(process.argv[2]);
        let lastHeartbeat = Date.now();
        process.stdin.on('data', () => { lastHeartbeat = Date.now(); });
        setInterval(() => {
            if (Date.now() - lastHeartbeat > timeoutMs) {
                console.error('Watchdog: processo sem resposta. Reiniciando.');
                try { process.kill(pid, 'SIGKILL'); } catch (err) { process.exit(1); }
            }
        }, 5000);
    `;

    const watchdog = spawn(process.execPath, ['-e', script, String(process.pid), String(WATCHDOG_TIMEOUT_MS)], {
        stdio: ['pipe', 'inherit', 'inherit'],
    });

    setInterval(() => {
        if (!watchdog.killed && watchdog.stdin.writable) watchdog.stdin.write('.');
    }, WATCHDOG_INTERVAL_MS).unref();

    return watchdog;
};

const watchdog = startWatchdog();

// Le a linha de comando de um PID. Especifico de plataforma: no Linux do
// container o /proc e a fonte barata e sempre presente (node:20-slim nao traz
// procps, entao `ps` pode nao existir la); no macOS do desenvolvimento nao ha
// /proc e o `ps` e nativo. Devolve '' quando o processo sumiu no meio.
const lerCmdline = (pid) => {
    try {
        if (process.platform === 'linux') {
            // /proc/<pid>/cmdline separa os argumentos com NUL.
            return fs.readFileSync(`/proc/${pid}/cmdline`, 'utf8').replace(/\0/g, ' ').trim();
        }
        return execFileSync('ps', ['-o', 'command=', '-p', String(pid)], {
            encoding: 'utf8', timeout: 5000,
        }).trim();
    } catch (err) {
        return ''; // processo morto, ou ps indisponivel: trata como "nao confirmado"
    }
};

const processoVivo = (pid) => {
    try {
        process.kill(pid, 0); // sinal 0: so testa existencia/permissao
        return true;
    } catch (err) {
        return err.code === 'EPERM'; // existe, mas e de outro dono
    }
};

// Mata o Chromium orfao que ainda segura este perfil, ANTES de subir o nosso.
//
// Sem isto, removerLocksChromium apagava o SingletonLock de um processo VIVO e o
// Client subia um segundo Chromium no mesmo --user-data-dir. Dois Chromiums sobre
// um perfil o corrompem: o pareamento nao conclui, o `.paired` nunca e escrito e a
// sessao fica "desconectada" para sempre — um ciclo que cada restart sujo repetia.
//
// Matar (em vez de recusar a subir) e deliberado: o watchdog derruba o worker com
// SIGKILL por desenho, e SIGKILL nao roda o shutdown(). Se o boot desistisse ao
// achar o perfil ocupado, um unico watchdog kill deixaria o worker quebrado ate
// alguem aparecer. E o orfao e, por definicao, uma encarnacao morta nossa: quando
// initializeSession roda, este processo ainda nao tem filho nenhum.
//
// Quem decide e chromium_locks (modulo puro, testado). Aqui so ha I/O.
const liberarPerfilChromium = (authPath) => {
    const lockPath = path.join(authPath, 'session', 'SingletonLock');
    let alvo;
    try {
        alvo = fs.readlinkSync(lockPath);
    } catch (err) {
        return; // sem lock: caminho normal
    }

    const dono = donoDoSingletonLock(alvo);
    const vivo = Boolean(dono) && processoVivo(dono.pid);
    const cmdline = vivo ? lerCmdline(dono.pid) : '';
    const perfilDir = path.join(authPath, 'session');

    if (decidirSobreDono({ dono, vivo, cmdline, perfilDir }) !== 'liberar') return;

    try {
        process.kill(dono.pid, 'SIGKILL');
        console.warn(
            `Chromium orfao ${dono.pid} ainda segurava ${perfilDir}; encerrado antes de subir o novo.`
        );
    } catch (err) {
        console.error(`Falha ao encerrar o Chromium orfao ${dono.pid}:`, err.message);
    }
};

const listarProcessos = () => {
    try {
        if (process.platform === 'linux') {
            const pids = fs.readdirSync('/proc')
                .filter((entry) => /^\d+$/.test(entry))
                .map(Number);
            return pids.map((pid) => ({ pid, cmdline: lerCmdline(pid) }));
        }
        return execFileSync('ps', ['-axo', 'pid=,command='], {
            encoding: 'utf8', timeout: 5000,
        }).split('\n').map((line) => {
            const match = /^\s*(\d+)\s+(.*)$/.exec(line);
            return match ? { pid: Number(match[1]), cmdline: match[2] } : null;
        }).filter(Boolean);
    } catch (err) {
        console.error('Falha ao listar processos do Chromium:', err.message);
        return null;
    }
};

// Limpeza forte e restrita a UM perfil. client.destroy() pode estourar o timeout
// e deixar processos sem SingletonLock; o scan pelo argumento exato fecha essa
// lacuna sem tocar nos Chromiums das outras sessões.
const encerrarChromiumsDoPerfil = async (authPath) => {
    const perfilDir = path.join(authPath, 'session');
    const encontrar = () => {
        const processos = listarProcessos();
        return processos === null ? null : pidsDoPerfil(processos, perfilDir);
    };
    const encontrados = encontrar();
    if (encontrados === null) return false;
    for (const pid of encontrados) {
        try {
            process.kill(pid, 'SIGKILL');
        } catch (err) {
            if (err.code !== 'ESRCH') {
                console.error(`Falha ao encerrar Chromium ${pid} de ${perfilDir}:`, err.message);
            }
        }
    }
    if (encontrados.length) {
        console.warn(
            `Encerrando ${encontrados.length} processo(s) Chromium do perfil ${perfilDir}.`
        );
    }

    for (let tentativa = 0; tentativa < 20; tentativa += 1) {
        const restantes = encontrar();
        if (restantes === null) return false;
        if (!restantes.length) return true;
        await new Promise((resolve) => setTimeout(resolve, 100));
    }
    const restantes = encontrar();
    if (restantes === null) return false;
    if (restantes.length) {
        console.error(
            `Chromium do perfil ${perfilDir} continuou vivo: ${restantes.join(', ')}.`
        );
        return false;
    }
    return true;
};

const removerLocksChromium = (dir) => {
    if (!fs.existsSync(dir)) return;
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const fullPath = path.join(dir, entry.name);
        if (entry.isDirectory()) {
            removerLocksChromium(fullPath);
        } else if (entry.name === 'SingletonLock' || entry.name === 'SingletonCookie') {
            fs.unlinkSync(fullPath);
            console.log(`🔓 Lock removido: ${fullPath}`);
        }
    }
};

const createSessionState = (instanceId) => ({
    id: instanceId,
    authPath: path.join(authRootPath, instanceId),
    client: null,
    initialized: false,
    isConnected: false,
    ultimoQR: null,
    gruposCache: [],
    gruposCarregados: false,
    gruposSincronizando: false,
    gruposSyncFalhou: false, // esgotou os retries: so o botao reabre
    gruposSyncFalhas: 0,     // falhas seguidas; alimenta o backoff do retry
    gruposRetryTimer: null,
    groupSyncPromise: null,
    syncPedidoDurante: false, // pedido explicito chegou com um sync em voo
    fase: 'iniciando',
    progresso: 0,
    reconnectTimer: null,
    qrBootstrapTimer: null,
    reconnectAttempts: 0,
    initTimer: null,
    qrIdleTimer: null,
    requestedAt: 0,
    initFailures: 0,
    authPurges: 0,           // purgas de auth neste ciclo de recuperacao
    encerrandoManual: false, // logout pedido pelo usuario: suprime o auto-reconnect
    resetPromise: null,      // coalesce requisicoes simultaneas de novo QR
    qrBootstrapAtivo: false, // reset pediu QR: nunca cair no recovery generico
    qrBootstrapAttempts: 0,
    authenticatedInAttempt: false,
    preparando: false,
    preparationTimer: null,
    pairedAt: null,
    readyAt: null,
    estabilizandoAte: 0,
    lastRecoveryReason: null,
    lastRecoveryAt: null,
    whatsappId: null,
    sendChain: Promise.resolve(),
    lastSendAt: 0,
    faseMsg: 'Iniciando serviço…',
});

const createCapacitySessionState = (instanceId) => ({
    ...createSessionState(instanceId),
    fase: 'capacidade',
    faseMsg: `Capacidade do serviço WhatsApp atingida (${MAX_WHATSAPP_SESSIONS} sessões).`,
});

const sessions = new Map();
// Marca uma sessao encerrada de proposito (logout, duplicata). Lido pelo
// restore do boot para nao religar quem foi desligado deliberadamente.
const DISABLED_MARKER = '.runtime-disabled';
const disabledMarkerPathFor = (authPath) => path.join(authPath, DISABLED_MARKER);
const disabledMarkerPath = (session) => disabledMarkerPathFor(session.authPath);

// Marca "QR sendo gerado agora". Escrito enquanto o bootstrap de um novo QR está
// em voo (reset ou retry) e apagado quando a sessao autentica/conecta ou quando
// o reset falha em definitivo. Por que existe: o reset apaga o `.paired` ANTES de
// o QR novo aparecer; se o worker reiniciar nessa janela (deploy/OOM/SIGKILL do
// watchdog), o restore do boot ignorava a pasta (sem `.paired`) e a tela ficava
// presa em 'inativo', sem QR — exatamente o "novo QR nao volta". Com este
// marcador, o boot RE-ARMA um QR novo. Consumido por decidirRestauracao.
const QR_BOOTSTRAP_MARKER = '.qr-bootstrap';
const qrBootstrapMarkerPathFor = (authPath) => path.join(authPath, QR_BOOTSTRAP_MARKER);
const marcarQrBootstrap = (session) => {
    try {
        fs.mkdirSync(session.authPath, { recursive: true });
        fs.writeFileSync(qrBootstrapMarkerPathFor(session.authPath), new Date().toISOString());
    } catch (err) {
        console.error(`[${session.id}] Falha ao marcar QR em preparo:`, err.message);
    }
};
const limparMarcadorQrBootstrap = (session) => {
    try {
        fs.unlinkSync(qrBootstrapMarkerPathFor(session.authPath));
    } catch (err) {
        if (err.code !== 'ENOENT') {
            console.error(`[${session.id}] Falha ao limpar marca de QR em preparo:`, err.message);
        }
    }
};

// Fecha o estado terminal de um "novo QR" que nao vingou: apaga o rastro do
// bootstrap (para o boot nao re-armar em loop uma sessao insalvavel) e emite UMA
// linha estruturada para o `fly logs` dizer qual das seis etapas falhou. Chamar
// sempre logo apos markResetFailure.
const finalizarFalhaReset = (session, causa = '') => {
    limparMarcadorQrBootstrap(session);
    console.error(
        `[${session.id}] falha_reset`
        + ` motivo=${session.motivoFalhaReset || MOTIVO_FALHA_RESET.DESCONHECIDO}`
        + ` tentativas=${session.qrBootstrapAttempts || 0}`
        + (causa ? ` causa="${causa}"` : '')
    );
};

const encerrarSessoesDuplicadas = async (current) => {
    if (!current.whatsappId) return;
    const duplicates = Array.from(sessions.values()).filter((other) => (
        other !== current && other.whatsappId === current.whatsappId
    ));
    for (const duplicate of duplicates) {
        console.error(
            `[${current.id}] Conta WhatsApp duplicada na sessao ${duplicate.id}; encerrando duplicata.`
        );
        await destroySessionRuntime(
            duplicate, `conta transferida para a sessao ${current.id}`, true
        );
    }
};

// Wrappers finos: os modulos puros nao conhecem authRootPath.
const authPathDe = (instanceId) => path.join(authRootPath, instanceId);
const purgeAuthDir = (session, reason) => authStore.purgeAuthDir(authRootPath, session.authPath, reason);
const purgeAuthDirPorId = (instanceId, reason) => authStore.purgeAuthDir(
    authRootPath, authPathDe(instanceId), reason
);
const markPaired = (session) => authStore.markPaired(authRootPath, session.authPath);
const hasStoredAuth = (instanceId) => authStore.hasStoredAuth(authRootPath, authPathDe(instanceId));

const withTimeout = (promise, timeoutMs, label) => {
    let timer;
    const timeout = new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`${label} timeout`)), timeoutMs);
    });
    return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
};

const destroySessionRuntime = async (session, reason, removeFromMap = false) => {
    console.log(`[${session.id}] Encerrando runtime da sessao. Motivo: ${reason}`);
    if (session.initTimer) clearTimeout(session.initTimer);
    if (session.qrIdleTimer) clearTimeout(session.qrIdleTimer);
    if (session.reconnectTimer) clearTimeout(session.reconnectTimer);
    if (session.qrBootstrapTimer) clearTimeout(session.qrBootstrapTimer);
    if (session.preparationTimer) clearTimeout(session.preparationTimer);
    session.initTimer = null;
    session.qrIdleTimer = null;
    session.reconnectTimer = null;
    session.qrBootstrapTimer = null;
    session.preparationTimer = null;
    try {
        if (session.client) await withTimeout(session.client.destroy(), 10000, 'client.destroy');
    } catch (err) {
        console.warn(`[${session.id}] Chromium nao encerrou limpo:`, err.message);
    }
    session.client = null;
    session.initialized = false;
    session.isConnected = false;
    session.whatsappId = null;
    session.gruposSincronizando = false;
    session.syncPedidoDurante = false; // sem Chromium nao ha o que repicar
    limparRetryGrupos(session);        // sem Chromium nao ha o que retentar
    if (removeFromMap) {
        try {
            fs.mkdirSync(session.authPath, { recursive: true });
            fs.writeFileSync(disabledMarkerPath(session), reason);
        } catch (err) {
            console.error(`[${session.id}] Falha ao marcar sessao inativa:`, err.message);
        }
        sessions.delete(session.id);
    }
};

const scheduleQrIdleDestroy = (session) => {
    if (session.qrIdleTimer) return;
    session.qrIdleTimer = setTimeout(async () => {
        const idleMs = Date.now() - (session.requestedAt || 0);
        if (!session.isConnected && session.ultimoQR && idleMs >= QR_IDLE_DESTROY_MS) {
            await destroySessionRuntime(session, 'QR ocioso de sessao restaurada', true);
        } else {
            session.qrIdleTimer = null;
            scheduleQrIdleDestroy(session);
        }
    }, QR_IDLE_DESTROY_MS).unref();
};

const limparRetryGrupos = (session) => {
    if (session.gruposRetryTimer) clearTimeout(session.gruposRetryTimer);
    session.gruposRetryTimer = null;
};

// Uma falha de leitura costuma ser transitoria (pagina ainda hidratando, rede
// oscilando). Reagenda com backoff em vez de exigir clique no botao. Quando o
// backoff esgota, `gruposSyncFalhou` assume como estado terminal e a rota para
// de insistir — que era o comportamento antigo, agora so no fim da linha.
const agendarRetryGrupos = (session) => {
    limparRetryGrupos(session);
    session.gruposSyncFalhas += 1;
    const delay = (session.isConnected || session.preparando)
        ? groupRetryDelay(session.gruposSyncFalhas) : null;
    if (!delay) {
        session.gruposSyncFalhou = true;
        session.faseMsg = 'Conectado - lista de grupos indisponivel temporariamente.';
        return;
    }
    session.faseMsg = 'Conectado - atualizando a lista de grupos…';
    session.gruposRetryTimer = setTimeout(() => {
        session.gruposRetryTimer = null;
        syncGroups(session, `retry-${session.gruposSyncFalhas}`);
    }, delay).unref();
    console.log(
        `[${session.id}] Nova tentativa de sincronizar grupos em ${delay}ms`
        + ` (falha ${session.gruposSyncFalhas}).`
    );
};

// A leitura roda dentro do Chromium. Passamos a funcao como string porque
// pupPage.evaluate(fn) serializa fn e quebraria qualquer closure — com `window`
// entrando por parametro, coletarGrupos continua um modulo puro, testavel em
// Node sem navegador (test/group_reader.test.js).
const lerGruposDaPagina = (session) => session.client.pupPage.evaluate(
    `(${coletarGrupos.toString()})(window)`
);

// Mesma tecnica do lerGruposDaPagina, com o chatId serializado junto. So chame
// com um id ja validado por idChatValido.
const lerGrupoDaPagina = (session, chatId) => session.client.pupPage.evaluate(
    `(${inspecionarGrupo.toString()})(window, ${JSON.stringify(chatId)})`
);

// O sendMessage resolve o destino via window.WWebJS.getChat DENTRO da pagina.
// A versao atual do whatsapp-web.js nao expoe window.Store, portanto Store nao
// pode ser usado como sinal de prontidao.
const storeInjetado = (session) => session.client.pupPage.evaluate(
    `(${runtimePronto.toString()})(window)`
);
// Uma checagem do store, protegida por timeout e pelo retry de frame destacado.
// probeTimeoutMs pode ser funcao para derivar do prazo compartilhado do envio.
const sondarStore = (session, probeTimeoutMs = 10000) => repetirSeFrameDestacado(
    () => withTimeout(
        storeInjetado(session),
        typeof probeTimeoutMs === 'function' ? probeTimeoutMs() : probeTimeoutMs,
        'verificarStore'
    )
);

// Uma leitura de grupos. CONTRATO: nunca lanca — as rotas chamam syncGroups sem
// await, entao uma rejeicao viraria unhandled rejection e derrubaria o processo.
const lerGrupos = async (session, reason) => {
    try {
        const resultado = await withTimeout(
            lerGruposDaPagina(session), GROUP_SYNC_TIMEOUT_MS, 'lerGrupos'
        );
        if (!resultado || !resultado.ok) {
            // `passo` diz onde o bundle do WA Web mudou (collections/models);
            // sem ele o unico sinal era um throw minificado sem contexto.
            throw new Error(
                `leitura falhou no passo '${resultado && resultado.passo || 'desconhecido'}': `
                + `${resultado && resultado.erro || 'sem envelope'}`
                + (resultado && resultado.modulos ? ` | modulos: ${resultado.modulos.join(',')}` : '')
            );
        }

        session.gruposCache = resultado.grupos.map(({ id, nome }) => ({ id, nome }));
        session.gruposCarregados = true;
        limparRetryGrupos(session);
        session.gruposSyncFalhas = 0;
        session.gruposSyncFalhou = false;
        if (!session.preparando) {
            session.fase = 'conectado';
            session.faseMsg = `Conectado - ${session.gruposCache.length} grupos.`;
        }
        console.log(
            `[${session.id}] Grupos sincronizados (${reason}): ${session.gruposCache.length}`
            + ` de ${resultado.totalChats} chats; ignorados=${resultado.ignorados.length}.`
        );
        // Grupos ignorados sao o sinal precoce de que a leitura por grupo comecou
        // a quebrar — antes, isso aparecia como lista vazia e nada no log.
        if (resultado.ignorados.length) {
            console.warn(
                `[${session.id}] Grupos ignorados (${reason}):`,
                JSON.stringify(resultado.ignorados.slice(0, 5))
            );
        }
        return true;
    } catch (err) {
        session.gruposCarregados = false;
        console.error(`[${session.id}] Erro ao sincronizar grupos (${reason}):`, descreverErro(err));
        // A lista de chats e secundaria. `ready` ja comprovou a conexao;
        // nunca destrua uma sessao saudavel porque a leitura falhou.
        if (!session.preparando) session.fase = 'conectado';
        agendarRetryGrupos(session);
        return false;
    }
};

// `forcar` = pedido explicito do usuario (botao "Sincronizar grupos"). Um sync ja
// em voo comecou ANTES do clique, entao seu resultado nao reflete o que a pessoa
// acabou de mudar no celular: reaproveita-lo e responder dado velho dizendo
// sucesso. A orquestracao (repique, coalescencia, promise) vive em group_sync.js.
const syncGroups = async (session, reason = 'auto', { forcar = false } = {}) => {
    if ((!session.isConnected && !session.preparando) || !session.client) return false;
    return iniciarSync(session, (r) => lerGrupos(session, r), reason, { forcar });
};

const limparPreparationTimer = (session) => {
    if (session.preparationTimer) clearTimeout(session.preparationTimer);
    session.preparationTimer = null;
};

const agendarProbeProntidao = (session, client) => {
    if (session.preparationTimer || session.client !== client) return;
    session.preparationTimer = setTimeout(async () => {
        session.preparationTimer = null;
        if (session.client !== client || !session.preparando) return;
        const pronto = await sondarStore(session, 5000).catch(() => false);
        if (session.client !== client || !session.preparando) return;
        if (!pronto) {
            console.warn(`[${session.id}] WWebJS ainda nao pronto; mantendo sessao pareada em preparacao.`);
            agendarProbeProntidao(session, client);
            return;
        }
        concluirPreparacao(session, client);
    }, READY_RETRY_MS);
    session.preparationTimer.unref();
};

const concluirPreparacao = (session, client) => {
    if (session.client !== client || !session.preparando) return;
    limparPreparationTimer(session);
    session.preparando = false;
    session.isConnected = true;
    session.readyAt = Date.now();
    session.estabilizandoAte = session.readyAt + CONNECTION_STABILIZATION_MS;
    session.fase = 'conectado';
    session.progresso = 100;
    session.faseMsg = 'Conectado. Sincronizando grupos antes de liberar envios…';
    console.log(`[${session.id}] WhatsApp pronto; iniciando sincronizacao inicial de grupos.`);

    // A versao e apenas telemetria. A chamada pode travar durante um rollout do
    // WhatsApp Web, entao nunca participa do gate de conexao ou do sync.
    setTimeout(() => {
        if (session.client !== client) return;
        withTimeout(client.getWWebVersion(), 10000, 'getWWebVersion')
            .then((versao) => console.log(`[${session.id}] WA Web ${versao}`))
            .catch((err) => console.warn(`[${session.id}] Versao WA Web indisponivel: ${err.message}`));
    }, CONNECTION_STABILIZATION_MS).unref();

    // Enquanto o primeiro sync lê a collection, envios ficam bloqueados para
    // não enfileirar evaluate/getState contra a mesma página logo após o QR.
    session.preparando = true;
    session.isConnected = false;
    session.fase = 'preparando';
    session.faseMsg = 'WhatsApp conectado. Sincronizando grupos antes de liberar envios…';
    Promise.resolve(syncGroups(session, 'ready'))
        .catch(() => false)
        .finally(() => {
            if (session.client !== client || !session.preparando) return;
            session.preparando = false;
            session.isConnected = true;
            session.fase = 'conectado';
            session.faseMsg = session.gruposCarregados
                ? `Conectado - ${session.gruposCache.length} grupos.`
                : 'Conectado - atualizando a lista de grupos em segundo plano.';
            console.log(`[${session.id}] Sessao estabilizada; envios liberados.`);
        });
};

const scheduleQrBootstrapRetry = async (session, reason) => {
    if (!session.qrBootstrapAtivo) return false;
    if (session.qrBootstrapTimer) return true;
    if (session.qrIdleTimer) clearTimeout(session.qrIdleTimer);
    session.qrIdleTimer = null;

    if (qrBootstrapOutcome(
        session.qrBootstrapAttempts, QR_BOOTSTRAP_MAX_ATTEMPTS
    ) === 'fail') {
        const mensagem = `Não foi possível gerar o QR após ${session.qrBootstrapAttempts} `
            + 'tentativa(s) — o leitor não respondeu a tempo. Clique para tentar novamente.';
        markResetFailure(session, mensagem, MOTIVO_FALHA_RESET.QR_NAO_GERADO);
        finalizarFalhaReset(session, reason);
        return true;
    }

    const proximaTentativa = session.qrBootstrapAttempts + 1;
    session.fase = 'reiniciando_qr';
    session.progresso = 0;
    session.ultimoQR = null;
    session.faseMsg =
        `Preparando um novo QR (tentativa ${proximaTentativa}/${QR_BOOTSTRAP_MAX_ATTEMPTS})…`;

    // So encerra o Chromium anterior; NAO repurga o auth. Pos-reset nao ha
    // credencial a limpar, e o initializeSession ja zera locks e caches. Repurgar
    // a cada tentativa so gastava I/O no volume do Fly sem tornar o QR mais provavel.
    const runtimeClean = await encerrarChromiumsDoPerfil(session.authPath);
    if (sessions.get(session.id) !== session || !session.qrBootstrapAtivo) return true;
    if (!runtimeClean) {
        markResetFailure(
            session, 'Não foi possível limpar o leitor anterior. Clique para tentar novamente.',
            MOTIVO_FALHA_RESET.LIMPEZA_RETRY_FALHOU
        );
        finalizarFalhaReset(session, reason);
        return true;
    }

    session.qrBootstrapAttempts = proximaTentativa;
    session.qrBootstrapTimer = setTimeout(() => {
        session.qrBootstrapTimer = null;
        if (
            sessions.get(session.id) !== session
            || !session.qrBootstrapAtivo
            || session.initialized
        ) return;
        console.log(
            `[${session.id}] Nova tentativa de gerar QR `
            + `(${session.qrBootstrapAttempts}/${QR_BOOTSTRAP_MAX_ATTEMPTS}).`
        );
        initializeSession(session);
    }, QR_BOOTSTRAP_RETRY_MS);
    session.qrBootstrapTimer.unref();
    return true;
};

// msgOverride sobrevive ao agendamento. Antes, quem quisesse explicar ao usuario
// o que estava acontecendo (ex.: "sessao corrompida, gerando novo QR") setava
// faseMsg e via a mensagem ser sobrescrita aqui na linha seguinte.
const scheduleReconnect = (session, reason, msgOverride = null) => {
    if (session.qrBootstrapAtivo) {
        scheduleQrBootstrapRetry(session, reason).catch((err) => {
            markResetFailure(
                session, 'Não foi possível preparar o novo QR. Clique para tentar novamente.',
                MOTIVO_FALHA_RESET.RETRY_FALHOU
            );
            finalizarFalhaReset(session, err.message);
        });
        return;
    }
    if (session.reconnectTimer) return;
    if (session.encerrandoManual) return; // logout do usuario: nao ressuscitar
    session.reconnectAttempts += 1;

    const outcome = reconnectAction(
        session.reconnectAttempts, session.authPurges, hasStoredAuth(session.id), RECONNECT_MAX_ATTEMPTS
    );

    if (outcome === 'expire') {
        session.fase = 'expirado';
        session.progresso = 0;
        session.faseMsg = 'Sessão expirada. Leia o QR novamente.';
        session.isConnected = false;
        session.ultimoQR = null;
        // Sem QR, o coletor de QR ocioso nunca dispara e ficaria se reagendando
        // a cada QR_IDLE_DESTROY_MS para sempre.
        if (session.qrIdleTimer) clearTimeout(session.qrIdleTimer);
        session.qrIdleTimer = null;
        console.error(
            `[${session.id}] Sessao expirada apos ${session.authPurges} purga(s). Motivo final: ${reason}`
        );
        return; // TERMINAL: nao reagenda. reviveSession() e o caminho de volta.
    }

    if (outcome === 'pause') {
        session.fase = 'recuperacao_pausada';
        session.progresso = 0;
        session.preparando = false;
        session.isConnected = false;
        session.faseMsg = 'Não foi possível estabilizar o WhatsApp. Tente conectar novamente; sua sessão foi preservada.';
        console.error(`[${session.id}] Recuperacao pausada sem apagar credencial. Motivo: ${reason}`);
        return;
    }

    if (outcome === 'purge') {
        // Uma credencial que ja pareou nao pode ser apagada por timeouts de
        // Chromium: isso desloga o aparelho vinculado e obriga novo QR. Pausa
        // para que um POST /api/sessoes tente novamente com o mesmo LocalAuth.
        purgeAuthDir(session, `teto de ${RECONNECT_MAX_ATTEMPTS} tentativas`);
        session.authPurges += 1;
        session.reconnectAttempts = 1; // o tick da purga ja e a tentativa 1 do ciclo novo
        msgOverride = 'Credencial expirada — gerando um novo QR…';
    }

    const delay = reconnectDelay(
        session.reconnectAttempts, RECONNECT_DELAY_MS, RECONNECT_MAX_DELAY_MS
    );
    session.fase = 'reconectando';
    session.progresso = 0;
    session.faseMsg = msgOverride || `Recuperando sessão (tentativa ${session.reconnectAttempts})…`;
    console.log(`[${session.id}] Reconnect agendado em ${delay}ms. Motivo: ${reason}`);

    session.reconnectTimer = setTimeout(() => {
        session.reconnectTimer = null;
        if (session.initialized) return;
        console.log(`[${session.id}] Tentando reconectar...`);
        initializeSession(session);
    }, delay);
};

// ensureSession so inicializa sessao ausente do Map, e initializeSession sai
// cedo se ja inicializada. Uma sessao terminal fica no Map com initialized=false
// e sem timer: sem isto o usuario ficaria preso em 'expirado' para sempre, sem QR.
const FASES_TERMINAIS = new Set(['expirado', 'falha_auth', 'recuperacao_pausada']);
const reviveSession = (session) => {
    if (session.initialized || session.client) return session;
    if (!FASES_TERMINAIS.has(session.fase)) return session;
    console.log(`[${session.id}] Revivendo sessao terminal (${session.fase}).`);
    session.reconnectAttempts = 0;
    session.authPurges = 0;
    session.initFailures = 0;
    session.encerrandoManual = false;
    session.preparando = false;
    session.estabilizandoAte = 0;
    session.fase = 'iniciando';
    session.progresso = 0;
    session.faseMsg = 'Iniciando serviço…';
    initializeSession(session);
    return session;
};

const recycleSession = async (session, reason, purgeAuth = false, msgOverride = null) => {
    const client = session.client;
    if (!client) return;
    session.lastRecoveryReason = reason;
    session.lastRecoveryAt = new Date().toISOString();
    console.error(`[${session.id}] Reciclando Chromium. Motivo: ${reason}`);
    session.client = null;
    session.initialized = false;
    session.isConnected = false;
    session.preparando = false;
    limparPreparationTimer(session);
    session.whatsappId = null;
    session.gruposCarregados = false;
    session.gruposSincronizando = false;
    session.gruposSyncFalhou = false; // conexao nova merece tentativa nova
    session.gruposSyncFalhas = 0;
    limparRetryGrupos(session);
    session.syncPedidoDurante = false; // sem Chromium nao ha o que repicar
    session.authenticatedInAttempt = false;
    if (session.initTimer) clearTimeout(session.initTimer);
    session.initTimer = null;
    try { await withTimeout(client.destroy(), 10000, 'client.destroy'); } catch (err) {
        console.warn(`[${session.id}] Chromium nao encerrou limpo:`, err.message);
    }
    if (session.qrBootstrapAtivo) {
        await scheduleQrBootstrapRetry(session, reason);
        return;
    }
    if (purgeAuth) purgeAuthDir(session, `perfil corrompido: ${reason}`);
    scheduleReconnect(session, reason, msgOverride);
};

const limparCachesChromium = (dir) => {
    if (!fs.existsSync(dir)) return;
    const caches = new Set(['Cache', 'Code Cache', 'GPUCache', 'DawnCache']);
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const fullPath = path.join(dir, entry.name);
        if (entry.isDirectory() && caches.has(entry.name)) {
            try { fs.rmSync(fullPath, { recursive: true, force: true }); } catch (err) {
                console.warn(`Falha ao limpar cache Chromium ${fullPath}:`, err.message);
            }
        } else if (entry.isDirectory()) {
            limparCachesChromium(fullPath);
        }
    }
};

const initializeSession = (session) => {
    if (session.initialized) return session;

    try {
        fs.unlinkSync(disabledMarkerPath(session));
    } catch (err) {
        if (err.code !== 'ENOENT') {
            console.error(`[${session.id}] Falha ao reativar sessao:`, err.message);
        }
    }
    // Ordem obrigatoria: liberar ANTES de remover os locks. Invertido, apagariamos
    // o SingletonLock e perderiamos o unico ponteiro para o orfao que segura o perfil.
    liberarPerfilChromium(session.authPath);
    removerLocksChromium(session.authPath);
    limparCachesChromium(session.authPath);
    // Enquanto o QR nao chega, deixa um rastro no volume. Se o worker reiniciar
    // agora, o boot re-arma um QR novo em vez de largar a sessao em 'inativo'.
    if (session.qrBootstrapAtivo) marcarQrBootstrap(session);
    const client = new Client({
        authStrategy: new LocalAuth({ dataPath: session.authPath }),
        takeoverOnConflict: true,
        takeoverTimeoutMs: 10000,
        puppeteer: {
            protocolTimeout: 300000,
            executablePath: PUPPETEER_EXECUTABLE_PATH,
            args: [
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--disk-cache-size=16777216',
                '--media-cache-size=16777216',
                '--no-first-run',
            ]
        }
    });

    session.client = client;
    session.initialized = true;
    const armInitializationTimeout = (stage) => {
        if (session.initTimer) clearTimeout(session.initTimer);
        const timeoutMs = session.qrBootstrapAtivo
            ? QR_BOOTSTRAP_TIMEOUT_MS : SESSION_INIT_TIMEOUT_MS;
        session.initTimer = setTimeout(() => {
            session.initTimer = null;
            if (session.client !== client || session.isConnected || session.ultimoQR) return;
            console.error(
                `[${session.id}] Sessao travada em "${stage}" por ${timeoutMs}ms. Reiniciando Chromium.`
            );
            const authenticatedFailure = (
                session.authenticatedInAttempt || stage === 'pos-autenticacao'
            );
            session.initFailures += 1;
            const purgeAuth = !hasStoredAuth(session.id)
                && shouldPurgeAuth(session.initFailures, authenticatedFailure);
            if (purgeAuth) session.initFailures = 0;
            const msg = purgeAuth ? 'Sessão corrompida — gerando um novo QR…' : null;
            recycleSession(session, `timeout em ${stage}`, purgeAuth, msg).catch((err) => {
                console.error(`[${session.id}] Falha ao reciclar sessao travada:`, err.message);
            });
        }, timeoutMs);
    };
    armInitializationTimeout('inicializacao');

    client.on('qr', (qr) => {
        if (session.client !== client) return;
        if (session.initTimer) clearTimeout(session.initTimer);
        session.initTimer = null;
        session.ultimoQR = qr;
        session.fase = 'qr';
        session.progresso = 0;
        session.faseMsg = 'Aguardando leitura do QR Code…';
        console.log(`[${session.id}] Sessão não encontrada ou expirada. QR disponivel na API.`);
        scheduleQrIdleDestroy(session);
        if (PRINT_QR_TO_LOGS) qrcode.generate(qr, { small: true });
    });

    client.on('loading_screen', (percent, message) => {
        if (session.client !== client) return;
        // This event can arrive late, even after ready. Do not downgrade a
        // session that was already proven connected.
        if (session.isConnected) return;
        session.progresso = parseInt(percent, 10) || 0;
        if (session.qrBootstrapAtivo) {
            session.fase = 'reiniciando_qr';
            session.faseMsg = 'Preparando o leitor para gerar um novo QR…';
        } else {
            session.fase = 'carregando';
            session.faseMsg = message || 'Carregando WhatsApp Web…';
        }
        if (!session.initTimer) armInitializationTimeout('carregamento do WhatsApp Web');
        console.log(`[${session.id}] ⏳ Carregando: ${session.progresso}% — ${session.faseMsg}`);
    });

    client.on('authenticated', () => {
        if (session.client !== client) return;
        session.qrBootstrapAtivo = false;
        session.qrBootstrapAttempts = 0;
        if (session.qrBootstrapTimer) clearTimeout(session.qrBootstrapTimer);
        session.qrBootstrapTimer = null;
        // Bootstrap venceu: o `.paired` volta a existir logo abaixo, então o
        // rastro de "QR em preparo" já não é necessário e não pode re-armar.
        limparMarcadorQrBootstrap(session);
        session.authenticatedInAttempt = true;
        // A credencial no volume agora vale a pena restaurar num boot futuro.
        // O layout do LocalAuth nao serve como sinal: ver auth_store.js.
        markPaired(session);
        session.pairedAt = Date.now();
        session.ultimoQR = null;
        session.fase = 'autenticado';
        session.faseMsg = 'Autenticado — preparando sessão…';
        // "authenticated" can be followed by a permanent loading hang without
        // a "ready" event. Keep recovery armed through the post-login phase.
        armInitializationTimeout('pos-autenticacao');
        console.log(`[${session.id}] 🔑 Autenticado.`);
    });

    client.on('auth_failure', (msg) => {
        if (session.client !== client) return;
        if (session.initTimer) clearTimeout(session.initTimer);
        session.initTimer = null;
        session.fase = session.qrBootstrapAtivo ? 'reiniciando_qr' : 'falha_auth';
        session.faseMsg = session.qrBootstrapAtivo
            ? 'O leitor falhou antes da autenticação. Preparando outro QR…'
            : 'Falha na autenticação — gere um novo QR.';
        console.error(`[${session.id}] ❌ Falha de autenticação:`, msg);
        recycleSession(session, 'falha de autenticacao', true).catch((err) => {
            console.error(`[${session.id}] Falha ao renovar autenticacao:`, err.message);
        });
    });

    client.on('ready', async () => {
        if (session.client !== client) return;
        // `ready` pode anteceder a injeção de WWebJS. Nesse caso a sessão fica
        // em preparação e nenhum envio ou sync concorre com o Chromium.
        const storePronto = await aguardarStorePronto({
            sondar: () => sondarStore(session),
            tetoMs: READY_STORE_WAIT_MS,
        }).catch(() => false);
        if (session.client !== client) return; // pode ter reciclado durante a espera
        if (session.initTimer) clearTimeout(session.initTimer);
        session.initTimer = null;
        session.initFailures = 0;
        session.qrBootstrapAtivo = false;
        session.qrBootstrapAttempts = 0;
        if (session.qrBootstrapTimer) clearTimeout(session.qrBootstrapTimer);
        session.qrBootstrapTimer = null;
        limparMarcadorQrBootstrap(session); // conectou: o rastro de QR em preparo saiu de cena
        session.authenticatedInAttempt = false;
        session.reconnectAttempts = 0;
        session.authPurges = 0; // ciclo de recuperacao fechado com sucesso
        markPaired(session);    // rede de seguranca: 'authenticated' pode nao vir num restore
        session.whatsappId = client.info?.wid?._serialized || null;
        await encerrarSessoesDuplicadas(session);
        session.ultimoQR = null;
        if (session.qrIdleTimer) clearTimeout(session.qrIdleTimer);
        session.qrIdleTimer = null;
        session.preparando = true;
        session.isConnected = false;
        session.fase = 'preparando';
        session.progresso = 100;
        session.faseMsg = 'WhatsApp autenticado. Preparando a sessão…';
        if (storePronto) {
            concluirPreparacao(session, client);
        } else {
            console.warn(`[${session.id}] 'ready' recebido sem WWebJS; aguardando sem reciclar a sessao.`);
            agendarProbeProntidao(session, client);
        }
    });

    client.on('disconnected', async (reason) => {
        if (session.client !== client) return;
        const faseAnterior = session.fase;
        if (session.initTimer) clearTimeout(session.initTimer);
        session.initTimer = null;
        session.isConnected = false;
        session.preparando = false;
        limparPreparationTimer(session);
        session.gruposCarregados = false;
        session.gruposSincronizando = false;
        session.gruposSyncFalhou = false; // conexao nova merece tentativa nova
        session.gruposSyncFalhas = 0;
        limparRetryGrupos(session);
        session.syncPedidoDurante = false; // sem Chromium nao ha o que repicar
        session.fase = session.qrBootstrapAtivo ? 'reiniciando_qr' : 'desconectado';
        session.progresso = 0;
        session.faseMsg = session.qrBootstrapAtivo
            ? 'Leitor interrompido. Preparando novamente o QR…'
            : 'Desconectado — reconectando…';
        const idadePareamento = session.pairedAt ? `${Date.now() - session.pairedAt}ms` : 'desconhecida';
        const contextoRecuperacao = session.lastRecoveryReason
            ? ` Última recuperação: ${session.lastRecoveryReason} em ${session.lastRecoveryAt}.`
            : '';
        console.log(
            `[${session.id}] ❌ WhatsApp foi desconectado. Motivo: ${reason}. `
            + `Fase anterior=${faseAnterior}; idade do pareamento=${idadePareamento}.${contextoRecuperacao}`
        );

        // Fecha o Chromium antigo para liberar memória antes de reconectar.
        session.client = null;
        session.initialized = false;
        session.whatsappId = null;
        try { await withTimeout(client.destroy(), 10000, 'client.destroy'); } catch (err) {
            console.warn(`[${session.id}] Chromium nao encerrou limpo:`, err.message);
        }

        // Logout pedido pelo usuário: a rota /api/sessoes/logout cuida do resto.
        // Sem esta guarda, o client.logout() de lá dispara este handler e nós
        // devolveríamos um QR novo na cara de quem acabou de clicar "Desconectar".
        if (session.encerrandoManual) return;

        if (session.qrBootstrapAtivo) {
            await scheduleQrBootstrapRetry(session, reason);
            return;
        }

        if (isRevokedReason(reason)) {
            // O celular desvinculou: a credencial no volume está morta. Reconectar
            // com ela é o que produzia o loop infinito de "tentativa N".
            purgeAuthDir(session, `desconectado: ${reason}`);
            session.authPurges = 0;
            session.reconnectAttempts = 0;
            session.gruposCache = [];
            session.fase = 'qr';
            session.faseMsg = 'Aparelho desvinculado — leia o QR para reconectar.';
            initializeSession(session);
            return;
        }

        // Queda de rede e afins: a sessão no volume ainda é válida, reconecta sozinho.
        scheduleReconnect(session, reason);
    });

    client.initialize().catch((error) => {
        if (session.client !== client) return;
        if (session.initTimer) clearTimeout(session.initTimer);
        session.initTimer = null;
        session.fase = session.qrBootstrapAtivo ? 'reiniciando_qr' : 'falha_auth';
        session.faseMsg = session.qrBootstrapAtivo
            ? 'O leitor de QR falhou ao iniciar. Preparando nova tentativa…'
            : 'Falha ao inicializar a sessão';
        console.error(`[${session.id}] ❌ Falha na inicialização:`, error.message);
        session.initFailures += 1;
        const purgeAuth = !hasStoredAuth(session.id) && shouldPurgeAuth(
            session.initFailures, session.authenticatedInAttempt
        );
        if (purgeAuth) session.initFailures = 0;
        recycleSession(session, error.message, purgeAuth).catch((err) => {
            console.error(`[${session.id}] Falha ao recuperar inicializacao:`, err.message);
        });
    });

    return session;
};

const sessoesOcupandoSlot = () => Array.from(sessions.values()).filter(ocupaSlot).length;

const ensureSession = (instanceId) => {
    const normalizedId = sanitizeInstanceId(instanceId);
    if (!sessions.has(normalizedId)) {
        if (sessoesOcupandoSlot() >= MAX_WHATSAPP_SESSIONS) {
            console.warn(`[${normalizedId}] Capacidade maxima atingida: ${MAX_WHATSAPP_SESSIONS} sessoes.`);
            return createCapacitySessionState(normalizedId);
        }
        const session = createSessionState(normalizedId);
        sessions.set(normalizedId, session);
        initializeSession(session);
    }
    const session = sessions.get(normalizedId);
    session.requestedAt = Date.now();
    return session;
};

const findSession = (instanceId, touch = true) => {
    const session = sessions.get(sanitizeInstanceId(instanceId));
    if (session && touch) session.requestedAt = Date.now();
    return session || null;
};

const resolveInstanceId = (req) => sanitizeInstanceId(
    req.params.instance || req.query.instance || req.query.session
    || req.body?.instance || req.body?.session || req.body?.userId || req.body?.usuario
);

const executarEnvioInteligente = async (instanceId, chatId, tipo, dados, opcoes = {}) => {
    const session = ensureSession(instanceId);
    const iniciadoEm = Date.now();
    const prazo = criarPrazo(SEND_REQUEST_TIMEOUT_MS, iniciadoEm);
    const duracao = () => Date.now() - iniciadoEm;
    const semTempo = (etapa) => erroClassificado(
        `Prazo total de envio esgotado na etapa ${etapa}.`, TRANSITORIO
    );
    const timeoutEtapa = (etapa, tetoMs) => {
        const timeout = timeoutDaEtapa(prazo, tetoMs);
        if (!timeout) throw semTempo(etapa);
        return timeout;
    };
    // Mesmo quando a resposta ao Django expira, o CDP pode terminar o envio mais
    // tarde. Mantemos a fila travada até ele assentar para nunca sobrepor outro
    // sendMessage à mesma sessão.
    let envioAindaEmVoo = null;

    if (session.preparando) {
        return {
            sucesso: false,
            erro: mensagemEstabilizacao(),
            classe: TRANSITORIO,
            repetir: true,
            instancia: session.id,
            etapa: 'preparacao',
            duracao_ms: duracao(),
        };
    }

    if (!session.isConnected || !session.client) {
        // Transitorio: quem religa e o gate de sessao do Django (POST /api/sessoes)
        // ou o restore do boot. Contar isto como falha da config era o que
        // desligava a automacao sozinha depois de ~5h de sessao caida.
        return {
            sucesso: false,
            erro: 'WhatsApp não está conectado. Leia o QR Code.',
            classe: TRANSITORIO,
            instancia: session.id,
            etapa: 'sessao',
            duracao_ms: duracao(),
        };
    }

    const executar = async () => {
      let etapa = 'fila';
      let envioIniciado = false;
      try {
        const espera = Math.max(0, MIN_SEND_INTERVAL_MS - (Date.now() - session.lastSendAt));
        if (espera) {
            await withTimeout(new Promise((resolve) => setTimeout(resolve, espera)),
                timeoutEtapa(etapa, espera), 'filaEnvio');
        }
        if (expirou(prazo)) throw semTempo(etapa);
        // `ready` can become stale if Chromium loses connectivity without a
        // disconnected event. Check the live state immediately before sending.
        etapa = 'getState';
        const estado = await repetirSeFrameDestacado(
            () => withTimeout(session.client.getState(), timeoutEtapa(etapa, 10000), 'getState')
        );
        if (estado !== 'CONNECTED') {
            session.isConnected = false;
            session.fase = 'reconectando';
            session.faseMsg = `WhatsApp sem conexao (${estado || 'estado desconhecido'}).`;
            setTimeout(() => recycleSession(
                session, `estado ${estado || 'desconhecido'} antes do envio`
            ), 0).unref();
            throw erroClassificado(
                'WhatsApp nao esta conectado. Reconecte antes de enviar.', TRANSITORIO
            );
        }

        // O sendMessage resolve o destino via window.WWebJS.getChat DENTRO da
        // pagina; quando o bundle do WA Web recarrega, esses modulos somem e o
        // envio quebra com "reading 'getChat'" (incidente real em producao).
        // Checar aqui fecha a janela entre o getState e o sendMessage.
        etapa = 'verificar_store';
        // Nao falhe na primeira olhada: se os modulos ainda estao carregando,
        // espere por eles dentro do prazo compartilhado. Mesmo esgotado esse
        // prazo, Store ausente ainda pode ser apenas a hidratacao tardia do WA
        // Web; destruir o Chromium aqui derrubaria uma sessao autenticada.
        const storePronto = await aguardarStorePronto({
            sondar: () => sondarStore(session, () => timeoutEtapa(etapa, 10000)), // mantem o prazo compartilhado
            tetoMs: STORE_READY_WAIT_MS,
            expirou: () => expirou(prazo),
        });
        if (!storePronto) {
            const mensagem = registrarStoreIndisponivel(session);
            console.warn(`[${session.id}] Store do WhatsApp ainda indisponivel no preflight; mantendo a sessao ativa.`);
            throw erroClassificado(mensagem, TRANSITORIO);
        }

        // Validate that the destination still exists in this account. This
        // rejects stale group IDs instead of reporting a false success.
        //
        // A checagem e barata de proposito: o getChatById que estava aqui descia
        // no getChatModel e era ele — nao o envio — que devolvia "r". Ver
        // group_reader.inspecionarGrupo.
        //
        // So para grupos: um numero novo (@c.us) ainda nao tem chat na collection
        // e nem por isso e destino invalido.
        if (chatId.endsWith('@g.us')) {
            etapa = 'verificar_grupo';
            const grupo = await repetirSeFrameDestacado(
                () => withTimeout(
                    lerGrupoDaPagina(session, chatId), timeoutEtapa(etapa, 15000), 'inspecionarGrupo'
                )
            );
            // ok=false e existe=false sao coisas MUITO diferentes, e tratar as duas
            // como a mesma falha era o que pausava a config de quem nao tinha
            // problema nenhum: 'nao consegui olhar' (pagina hidratando, bundle
            // mudou) vira transitorio; so 'olhei e nao esta la' e permanente.
            if (!grupo.ok) {
                throw erroClassificado(
                    `Nao foi possivel verificar o grupo de destino: ${grupo.erro}`, TRANSITORIO
                );
            }
            if (!grupo.existe) {
                throw erroClassificado(
                    'Grupo de destino nao encontrado nesta conta do WhatsApp.', PERMANENTE
                );
            }
        }

        let enviada;
        etapa = 'sendMessage';
        if (tipo === 'texto') {
            envioIniciado = true;
            const promessaEnvio = session.client.sendMessage(chatId, dados, opcoesDeEnvio());
            envioAindaEmVoo = Promise.resolve(promessaEnvio).then(() => undefined, () => undefined);
            enviada = await withTimeout(
                promessaEnvio,
                timeoutEtapa(etapa, SEND_TIMEOUT_MS),
                'sendMessage'
            );
        } else {
            const midia = new MessageMedia(opcoes.mimetype, dados, opcoes.nomeArquivo);
            envioIniciado = true;
            const promessaEnvio = session.client.sendMessage(chatId, midia, opcoesDeEnvio(opcoes.legenda));
            envioAindaEmVoo = Promise.resolve(promessaEnvio).then(() => undefined, () => undefined);
            enviada = await withTimeout(
                promessaEnvio,
                timeoutEtapa(etapa, SEND_TIMEOUT_MS),
                'sendMessage'
            );
        }
        const confirmacao = confirmarMensagem(enviada, session.id);
        if (confirmacao.confirmacao !== 'nativa') {
            // Não é falha: sendMessage resolveu e a mensagem foi aceita pelo WA
            // Web, mas a versão atual não devolveu o modelo com Wid. O ID local
            // só rastreia esta publicação no Spreading; não se passa por ID do WA.
            console.warn(
                `[${session.id}] Envio aceito sem ID nativo do WhatsApp; `
                + `usando rastreio local ${confirmacao.mensagemId}.`
            );
        }
        session.lastSendAt = Date.now();
        console.log(`[${session.id}] Envio confirmado: ${confirmacao.mensagemId} -> ${chatId}`);
        return {
            sucesso: true,
            via: 'local',
            tipo,
            instancia: session.id,
            mensagem_id: confirmacao.mensagemId,
            confirmacao: confirmacao.confirmacao,
            // Na variante "aceita_sem_id", enviada e undefined por definição.
            // ACK é telemetria opcional; jamais pode transformar um envio aceito
            // em erro depois que já chegou ao grupo.
            ack: Number.isInteger(enviada?.ack) ? enviada.ack : null,
            etapa,
            duracao_ms: duracao(),
        };
      } catch (erro) {
        if (envioIniciado && erroReloadEmVoo(erro)) {
            // Não retente: o usuário confirmou no caso real que o WA entrega a
            // mensagem antes de Puppeteer perceber que a página foi recarregada
            // (frame destacado OU "Execution context was destroyed" — o mesmo
            // reload do WA Web, assinaturas diferentes). Marcar como falha
            // causaria reenvio e duplicata no grupo.
            const confirmacao = confirmarMensagem(undefined, session.id);
            session.lastSendAt = Date.now();
            console.warn(
                `[${session.id}] Frame do WhatsApp foi recarregado após iniciar o envio; `
                + `mantendo como envio protegido (${confirmacao.mensagemId}).`
            );
            setTimeout(() => recycleSession(session, 'frame destacado durante envio'), 0).unref();
            return {
                sucesso: true,
                via: 'local',
                tipo,
                instancia: session.id,
                mensagem_id: confirmacao.mensagemId,
                confirmacao: 'incerta_pos_frame',
                ack: null,
                etapa,
                duracao_ms: duracao(),
            };
        }
        // Depois que sendMessage comecou, nem o timeout nem o cancelamento do
        // Puppeteer provam que a mensagem NAO chegou. Retentar cegamente duplica
        // oferta; devolvemos um resultado explicito para o Django bloquear retry.
        const timeoutDuranteEnvio = timeoutComEnvioIniciado(envioIniciado, etapa, erro, prazo);
        if (timeoutDuranteEnvio) {
            console.warn(`[${session.id}] Resultado incerto apos timeout de envio; reciclando sessao.`);
            setTimeout(() => recycleSession(session, 'timeout com entrega incerta'), 0).unref();
            return {
                sucesso: false,
                erro: 'O WhatsApp não confirmou o envio a tempo; confirme no grupo antes de tentar novamente.',
                classe: TRANSITORIO,
                resultado: 'incerto',
                repetir: false,
                instancia: session.id,
                etapa,
                duracao_ms: duracao(),
            };
        }
        if (timeoutPreflight(etapa, erro)) {
            if (!deveReciclarTimeoutPreflight(session)) {
                console.warn(`[${session.id}] Timeout em ${etapa} durante estabilizacao; mantendo sessao pareada.`);
                return {
                    sucesso: false,
                    erro: mensagemEstabilizacao(),
                    classe: TRANSITORIO,
                    repetir: true,
                    instancia: session.id,
                    etapa,
                    duracao_ms: duracao(),
                    falha_infra: false,
                };
            }
            // getState/inspecionarGrupo travados significam Chromium morto ou WA Web
            // congelado. Ainda não houve sendMessage, portanto é seguro recuperar e
            // orientar a pessoa sem expor a stack interna do Puppeteer.
            console.warn(`[${session.id}] Timeout em ${etapa}; reciclando sessão antes de novo envio.`);
            iniciarRecuperacaoPreflight(session, etapa, recycleSession);
            return {
                sucesso: false,
                erro: mensagemPreflight(etapa),
                classe: TRANSITORIO,
                repetir: true,
                instancia: session.id,
                etapa,
                duracao_ms: duracao(),
                falha_infra: true,
            };
        }
        if (erroReloadEmVoo(erro)) {
            // Ainda não chamamos sendMessage: não há risco de duplicar. A sessão
            // está no meio de uma recarga e será restaurada para a próxima ação.
            console.warn(`[${session.id}] WhatsApp Web ainda instável antes do envio; reciclando sessão.`);
            setTimeout(() => recycleSession(session, 'recarga do WA Web antes do envio'), 0).unref();
            return {
                sucesso: false,
                erro: 'WhatsApp Web estava recarregando. A conexão será recuperada automaticamente; aguarde alguns segundos e tente novamente.',
                classe: TRANSITORIO,
                instancia: session.id,
                etapa,
                duracao_ms: duracao(),
            };
        }
        if (erroStoreQuebrado(erro)) {
            // O getChat interno e o PRIMEIRO passo do sendMessage: o erro veio
            // da resolucao do destino, antes de qualquer envio — retentar nao
            // duplica. O mesmo erro tambem aparece na hidratacao tardia logo
            // apos `ready`; manter o Chromium evita perder a sessao por uma
            // condicao que costuma se resolver sozinha.
            const mensagem = registrarStoreIndisponivel(session);
            console.warn(`[${session.id}] Store do WhatsApp indefinido durante envio; mantendo a sessao ativa.`);
            return {
                sucesso: false,
                erro: mensagem,
                classe: TRANSITORIO,
                repetir: true,
                instancia: session.id,
                etapa,
                duracao_ms: duracao(),
                falha_infra: false,
            };
        }
        // descreverErro, nao erro.message: o bundle minificado lanca objetos que
        // nao sao Error, e era isso que chegava ao usuario como "[ERRO] r".
        const descrito = descreverErro(erro);
        console.error(`[${session.id}] Falha no envio:`, descrito);
        // Comparacao segue em erro.message: o texto descrito traz nome e stack.
        if (erro && erro.message === 'sendMessage timeout') {
            setTimeout(() => recycleSession(session, 'timeout ao enviar mensagem'), 0).unref();
        }
        return {
            sucesso: false,
            erro: descrito || 'Falha ao enviar a mensagem.',
            // Le a classe que os throws acima anexaram; o throw minificado do
            // bundle (o "r") nao tem nenhuma e cai em 'desconhecido', que conta
            // falha — o comportamento que ja existia antes desta taxonomia.
            classe: classificarErro(erro),
            instancia: session.id,
            etapa,
            duracao_ms: duracao(),
            falha_infra: /timeout|prazo total/i.test(String(erro && erro.message || erro)),
        };
      }
    };
    const resultado = session.sendChain.then(executar, executar);
    session.sendChain = resultado.then(
        () => envioAindaEmVoo || undefined,
        () => envioAindaEmVoo || undefined,
    ).then(() => undefined, () => undefined);
    return resultado;
};

// Fechamento gracioso: o Fly envia SIGTERM a cada deploy. Fechar o Chromium
// corretamente evita locks corrompidos que fariam a sessão "sumir".
let encerrando = false;
const shutdown = async (signal) => {
    if (encerrando) return;
    encerrando = true;
    console.log(`🛑 ${signal} recebido — encerrando sessões…`);
    if (watchdog && !watchdog.killed) watchdog.kill();
    await Promise.allSettled(
        Array.from(sessions.values()).map((s) => (s.client ? s.client.destroy() : null))
    );
    process.exit(0);
};
process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));
process.on('uncaughtException', (error) => {
    console.error('Excecao nao tratada:', error);
    process.exit(1);
});
process.on('unhandledRejection', (reason) => {
    console.error('Promise rejeitada sem tratamento:', reason);
});

// Liveness público p/ o load balancer: sem contadores (evita vazar quantas sessões
// existem/estão conectadas a quem não tem a API key). Detalhes ficam em /api/status.
app.get('/health', (req, res) => {
    res.json({ ok: true });
});

// Nunca usar ensureSession aqui: o monitor_conexao do Django chama esta rota
// para TODO perfil a cada tick, e o dashboard chama no render. Seria um Chromium
// por perfil por tick. Quem ressuscita sessao e POST /api/sessoes e o restore do boot.
app.get(['/api/status', '/api/status/:instance'], apiKeyAuth, (req, res) => {
    const instanceId = resolveInstanceId(req);
    const session = findSession(instanceId);
    if (!session) {
        return res.json({
            instancia: instanceId,
            conectado: false,
            fase: 'inativo',
            progresso: 0,
            mensagem: 'Sessao inativa.',
            grupos: 0,
            grupos_sincronizando: false,
            grupos_indisponivel: false,
            qr: null,
        });
    }
    const status = session.fase === 'capacidade' ? 503 : 200;
    res.status(status).json(buildSessionPayload(session));
});

app.get(['/api/sessoes', '/api/sessoes/:instance'], apiKeyAuth, (req, res) => {
    const requestedId = resolveInstanceId(req);
    if (requestedId && sessions.has(requestedId)) {
        return res.json({ sessao: buildSessionPayload(sessions.get(requestedId)) });
    }

    const list = Array.from(sessions.values())
        .sort((a, b) => a.id.localeCompare(b.id))
        .map((session) => buildSessionPayload(session));
    res.json({ sessoes: list });
});

app.post('/api/sessoes', apiKeyAuth, (req, res) => {
    const instanceId = sanitizeInstanceId(req.body?.instance || req.body?.session || req.body?.userId);
    const session = ensureSession(instanceId);
    // Pedido explicito do usuario (abrir a aba WhatsApp) e o unico caminho que
    // tira uma sessao de uma fase terminal.
    reviveSession(session);
    if (session.fase === 'capacidade') {
        return res.status(503).json({
            sucesso: false,
            erro: session.faseMsg,
            instancia: session.id,
            status: buildSessionPayload(session),
        });
    }
    res.json({ sucesso: true, instancia: session.id, status: buildSessionPayload(session) });
});

// Transição atômica para um novo QR. Manter logout + POST /api/sessoes como
// duas requests deixava uma janela em que o polling revivia a credencial antiga.
app.post('/api/sessoes/reset', apiKeyAuth, async (req, res) => {
    const instanceId = sanitizeInstanceId(req.body?.instance || req.body?.session || req.body?.userId);
    let session = findSession(instanceId, false);
    if (!session) {
        // Placeholder deliberadamente não inicializado: primeiro apaga qualquer
        // LocalAuth órfão; só depois cria o Chromium que produzirá o QR.
        session = createSessionState(instanceId);
        sessions.set(instanceId, session);
    }

    const resultado = await resetSessionForQr(session, {
        destroyRuntime: (current) => destroySessionRuntime(
            current, 'novo QR solicitado pelo usuario', false
        ),
        cleanupProfile: (current) => encerrarChromiumsDoPerfil(current.authPath),
        purgeAuth: (current) => purgeAuthDir(current, 'novo QR solicitado pelo usuario'),
        createState: createSessionState,
        createCapacityState: createCapacitySessionState,
        replaceSession: (fresh) => sessions.set(instanceId, fresh),
        hasCapacity: () => sessoesOcupandoSlot() < MAX_WHATSAPP_SESSIONS,
        initialize: initializeSession,
    });

    // Falha ainda dentro da transição do reset (encerrar/purgar/iniciar): registra
    // o motivo e limpa o rastro de bootstrap. O caminho de timeout do QR já é
    // finalizado dentro de scheduleQrBootstrapRetry.
    if (!resultado.sucesso && resultado.status && resultado.status.fase === 'falha_reset') {
        finalizarFalhaReset(resultado.status);
    }

    res.json({
        ...resultado,
        // Nunca exponha o objeto interno (client, timers, paths). O contrato da
        // API usa apenas o payload serializado compartilhado com /api/status.
        status: buildSessionPayload(resultado.status),
    });
});

// Desfaz o pareamento: revoga no celular (quando da) e apaga a credencial do
// volume. Escape manual do usuario — antes so o proprio worker decidia purgar,
// e nao havia como trocar de numero nem forcar um QR novo pela UI.
app.post('/api/sessoes/logout', apiKeyAuth, async (req, res) => {
    const instanceId = sanitizeInstanceId(req.body?.instance || req.body?.session || req.body?.userId);
    const session = findSession(instanceId, false);

    // Sem sessao viva no Map, ainda assim limpar o volume: o usuario quer desparear.
    if (!session) {
        const removido = purgeAuthDirPorId(instanceId, 'logout sem sessao ativa');
        return res.json({
            sucesso: true, logout_remoto: false, auth_removido: removido,
            ...buildInativoPayload(instanceId),
        });
    }

    // ANTES de qualquer destroy: client.logout() dispara 'disconnected' com
    // reason LOGOUT, e sem este flag o handler purgaria e abriria um QR novo.
    session.encerrandoManual = true;

    let logoutRemoto = false;
    if (session.isConnected && session.client) {
        try {
            // Sem timeout, um Chromium morto pendura a request ate o watchdog
            // (45s) matar o processo inteiro, derrubando as outras sessoes.
            await withTimeout(session.client.logout(), 15000, 'client.logout');
            logoutRemoto = true;
        } catch (err) {
            console.warn(`[${session.id}] Logout remoto falhou (${err.message}); seguindo com destroy local.`);
        }
    }

    await destroySessionRuntime(session, 'logout solicitado pelo usuario', true);
    // Depois do destroy: ele escreve .runtime-disabled dentro do authPath, e
    // este purge leva o diretorio inteiro. Na ordem inversa, o marker seria
    // recriado e viraria lixo permanente no volume.
    const authRemovido = purgeAuthDir(session, 'logout solicitado pelo usuario');

    res.json({
        sucesso: true, logout_remoto: logoutRemoto, auth_removido: authRemovido,
        ...buildInativoPayload(session.id),
    });
});

// Resolve a sessao para as rotas de grupo. Ressuscita SO quem ja tem credencial
// pareada no volume: a aba Envios chama /api/grupos no load para todo usuario,
// inclusive quem so usa Telegram, e um ensureSession incondicional queimaria um
// dos MAX_WHATSAPP_SESSIONS slots com um Chromium que ninguem pediu.
const resolveSessionParaGrupos = (instanceId) => {
    const normalizedId = sanitizeInstanceId(instanceId);
    const session = findSession(normalizedId);
    if (session) return session;
    if (!hasStoredAuth(normalizedId)) return null;
    // Sessao pareada some do Map em restart/deploy/watchdog. Antes, so a aba
    // WhatsApp a ressuscitava — por isso a aba Envios acusava "desconectado"
    // com a credencial intacta no volume.
    console.log(`[${normalizedId}] Sessao pareada ausente do Map; restaurando sob demanda.`);
    return ensureSession(normalizedId);
};

app.get(['/api/grupos', '/api/grupos/:instance'], apiKeyAuth, async (req, res) => {
    const instanceId = resolveInstanceId(req);
    const session = resolveSessionParaGrupos(instanceId);
    if (!session) return res.json(buildInativoPayload(sanitizeInstanceId(instanceId)));

    // Nao insiste se os retries automaticos ja esgotaram, e nao atropela um
    // retry agendado: o payload reporta grupos_indisponivel e o usuario decide,
    // pelo botao "Sincronizar grupos".
    if (session.isConnected && !session.gruposCarregados
        && !session.gruposSyncFalhou && !session.groupSyncPromise
        && !session.gruposRetryTimer) {
        syncGroups(session, 'api-grupos');
    }
    return res.json(buildGruposPayload(session));
});

app.post(['/api/grupos/refresh', '/api/grupos/refresh/:instance'], apiKeyAuth, async (req, res) => {
    const instanceId = resolveInstanceId(req);
    const session = resolveSessionParaGrupos(instanceId);
    if (!session) {
        return res.json({ sucesso: false, ...buildInativoPayload(sanitizeInstanceId(instanceId)) });
    }
    if (!session.isConnected || !session.client) {
        return res.json({ sucesso: false, ...buildGruposPayload(session) });
    }

    session.gruposCarregados = false;
    // Pedido explicito: reabre o estado terminal e devolve o ciclo de retries
    // inteiro. O timer pendente sai de cena — este sync o substitui agora.
    session.gruposSyncFalhou = false;
    session.gruposSyncFalhas = 0;
    limparRetryGrupos(session);
    // forcar: um sync em voo leu o WhatsApp ANTES deste clique. Sem isto o
    // usuario que acabou de criar um grupo no celular recebia o snapshot velho.
    syncGroups(session, 'refresh-manual', { forcar: true });
    return res.json({ sucesso: true, ...buildGruposPayload(session) });
});

// Diagnóstico sem publicação. O painel de Saúde usa esta rota para comprovar que
// a sessão e o grupo voltaram a responder sem repetir uma oferta.
app.post(['/api/diagnostico', '/api/diagnostico/:instance'], apiKeyAuth, async (req, res) => {
    const instanceId = resolveInstanceId(req);
    const chatId = String(req.body?.grupoid || '').trim();
    const session = resolveSessionParaGrupos(instanceId);
    if (!session || !session.isConnected || !session.client) {
        return res.status(503).json({ sucesso: false, causa: 'whatsapp_desconectado',
            escopo: chatId || instanceId, mensagem: 'WhatsApp não está conectado.', classe: TRANSITORIO });
    }
    if (chatId && (!chatId.endsWith('@g.us') || !idChatValido(chatId))) {
        return res.status(400).json({ sucesso: false, causa: 'destino_invalido',
            escopo: chatId, mensagem: 'O código do grupo é inválido.', classe: PERMANENTE });
    }
    try {
        const estado = await withTimeout(session.client.getState(), 10000, 'getState');
        if (estado !== 'CONNECTED') {
            throw erroClassificado(`WhatsApp sem conexão (${estado || 'estado desconhecido'}).`, TRANSITORIO);
        }
        if (chatId) {
            const grupo = await withTimeout(lerGrupoDaPagina(session, chatId), 15000, 'inspecionarGrupo');
            if (!grupo.ok) throw erroClassificado('Não foi possível validar o grupo.', TRANSITORIO);
            if (!grupo.existe) throw erroClassificado('Grupo não encontrado nesta conta.', PERMANENTE);
        }
        return res.json({ sucesso: true, causa: 'whatsapp_preflight', escopo: chatId || instanceId,
            mensagem: chatId ? 'Sessão e grupo validados sem enviar mensagem.' : 'Sessão validada sem enviar mensagem.' });
    } catch (err) {
        const etapa = /inspecionarGrupo/.test(String(err && err.message || err)) ? 'verificar_grupo' : 'getState';
        const timeout = timeoutPreflight(etapa, err);
        if (timeout) {
            iniciarRecuperacaoPreflight(session, etapa, recycleSession);
        }
        return res.status(503).json({ sucesso: false,
            causa: etapa === 'getState' ? 'whatsapp_preflight_timeout' : 'whatsapp_grupo_timeout',
            escopo: chatId || instanceId,
            mensagem: timeout ? mensagemPreflight(etapa) : 'O diagnóstico não conseguiu validar o WhatsApp.',
            classe: classificarErro(err), etapa });
    }
});

app.get(['/api/qrcode', '/api/qrcode/:instance'], apiKeyAuth, (req, res) => {
    const instanceId = resolveInstanceId(req);
    const session = findSession(instanceId);
    if (!session) {
        return res.status(404).json({ conectado: false, instancia: instanceId, qr: null, mensagem: 'Sessao inativa.' });
    }
    if (session.isConnected) {
        return res.json({ conectado: true, instancia: session.id, qr: null, mensagem: 'WhatsApp já está conectado.' });
    }
    if (!session.ultimoQR) {
        return res.status(503).json({ conectado: false, instancia: session.id, qr: null, mensagem: 'QR Code ainda não gerado. Aguarde alguns segundos e tente novamente.' });
    }
    res.json({ conectado: false, instancia: session.id, qr: session.ultimoQR });
});

app.post(['/api/enviar', '/api/enviar/:instance'], apiKeyAuth, async (req, res) => {
    const instanceId = resolveInstanceId(req);
    const { numero, grupoid, mensagem, base64, mimetype, nomeArquivo, legenda } = req.body;

    // Os 400 desta rota sao todos permanentes: repetir com o mesmo corpo da o
    // mesmo resultado. Sao exatamente os casos em que pausar a config e a atitude
    // certa — alguem precisa corrigir o destino ou a mensagem.
    if (!numero && !grupoid) {
        return res.status(400).json({
            erro: 'Você precisa informar um numero ou grupoid.',
            classe: PERMANENTE,
            instancia: instanceId,
        });
    }

    const chatId = grupoid || `${numero}@c.us`;

    // Rejeitar aqui, e nao no Chromium: um id fora do formato faz o createWid
    // lancar minificado la dentro, e o usuario recebia "[ERRO] r". O caso real e
    // o nome do grupo ("MillStack") chegando pelo input de texto livre que a UI
    // usa quando a lista de grupos nao carrega.
    if (!idChatValido(chatId)) {
        return res.status(400).json({
            erro: `Destino invalido: "${chatId}". Use o codigo do grupo (termina em @g.us)`
                + ` ou um numero so com digitos.`,
            classe: PERMANENTE,
            instancia: instanceId,
        });
    }

    if (base64 && mimetype) {
        if (!MIMETYPES_PERMITIDOS.has(mimetype)) {
            return res.status(400).json({
                erro: 'Tipo de arquivo não permitido.',
                classe: PERMANENTE,
                instancia: instanceId,
            });
        }

        console.log(`[${instanceId}] [AUTO] Detectada Mídia para ${chatId}`);
        const resultado = await executarEnvioInteligente(instanceId, chatId, 'midia', base64, {
            mimetype,
            nomeArquivo: nomeArquivo || 'arquivo',
            legenda: legenda || mensagem
        });
        return res.status(resultado.sucesso ? 200 : 503).json(resultado);
    }

    if (mensagem) {
        console.log(`[${instanceId}] [AUTO] Detectado Texto para ${chatId}`);
        if (mensagem.length > 4096) {
            return res.status(400).json({
                erro: 'Mensagem muito longa.',
                classe: PERMANENTE,
                instancia: instanceId,
            });
        }

        const resultado = await executarEnvioInteligente(instanceId, chatId, 'texto', mensagem);
        return res.status(resultado.sucesso ? 200 : 503).json(resultado);
    }

    return res.status(400).json({
        erro: 'Corpo da requisição vazio. Envie "mensagem" ou "base64".',
        classe: PERMANENTE,
        instancia: instanceId,
    });
});

// Religa as sessoes ja pareadas depois de um restart/deploy. Sem isto o Map
// nasce vazio e a sessao so voltava quando alguem abria a aba WhatsApp — o que
// fazia a aba Envios acusar "desconectado", o primeiro envio pos-deploy falhar,
// e o monitor_conexao mandar e-mail de "WhatsApp caiu" para todo mundo.
const restaurarSessoesDoVolume = () => {
    if (process.env.DISABLE_SESSION_RESTORE === '1') {
        console.log('Restauracao de sessoes desabilitada por env.');
        return;
    }
    if (!fs.existsSync(authRootPath)) return;

    let candidatos = [];
    try {
        candidatos = fs.readdirSync(authRootPath, { withFileTypes: true })
            .filter((e) => e.isDirectory())
            .map((e) => e.name)
            .filter((id) => id === sanitizeInstanceId(id)) // ignora lixo no volume
            .map((id) => ({
                id,
                acao: decidirRestauracao({
                    pareado: hasStoredAuth(id),
                    desabilitado: fs.existsSync(disabledMarkerPathFor(authPathDe(id))),
                    qrEmPreparo: fs.existsSync(qrBootstrapMarkerPathFor(authPathDe(id))),
                }),
            }))
            .filter((c) => c.acao !== 'ignorar')
            .sort((a, b) => a.id.localeCompare(b.id))
            .slice(0, MAX_WHATSAPP_SESSIONS);
    } catch (err) {
        console.error('Falha ao varrer o volume de sessoes:', err.message);
        return;
    }

    if (!candidatos.length) {
        console.log('Nenhuma sessao pareada no volume para restaurar.');
        return;
    }

    const resumo = candidatos.map((c) => `${c.id}:${c.acao}`).join(', ');
    console.log(`Restaurando ${candidatos.length} sessao(oes) do volume: ${resumo}.`);
    candidatos.forEach(({ id, acao }, i) => {
        // Escalonado: cada sessao sobe um Chromium (~350MB); subir todas juntas
        // faz um pico de memoria e de CPU no boot.
        setTimeout(() => {
            if (acao === 'rearmar') {
                console.log(`[${id}] QR estava em preparo no restart; re-armando um QR novo.`);
                rearmarQrBootstrap(id);
            } else {
                console.log(`[${id}] Restaurando sessao do volume (${i + 1}/${candidatos.length}).`);
                ensureSession(id);
            }
        }, i * SESSION_START_STAGGER_MS).unref();
    });
};

// Recomeça um bootstrap de QR para uma sessao cujo "novo QR" foi interrompido por
// um restart (marcador .qr-bootstrap, sem .paired). Espelha o /api/sessoes/reset,
// mas sem destroy: no boot nao ha client vivo, so um perfil parcial em disco.
const rearmarQrBootstrap = (instanceId) => {
    const id = sanitizeInstanceId(instanceId);
    purgeAuthDirPorId(id, 're-armar QR apos restart'); // descarta o perfil parcial
    const fresh = markQrBootstrap(createSessionState(id));
    sessions.set(id, fresh);
    try {
        initializeSession(fresh); // reescreve o marcador; se cair de novo, re-arma de novo
    } catch (err) {
        markResetFailure(
            fresh, 'Não foi possível gerar o QR após reinício. Clique para tentar novamente.',
            MOTIVO_FALHA_RESET.INIT_FALHOU
        );
        finalizarFalhaReset(fresh, err.message);
    }
};

const PORT = process.env.PORT || 3000;
app.listen(PORT, '::', () => {
    console.log(`Servidor rodando na porta ${PORT}`);
    // Depois do listen: /health tem de responder dentro do grace_period do Fly
    // sem esperar Chromium nenhum.
    restaurarSessoesDoVolume();
});
