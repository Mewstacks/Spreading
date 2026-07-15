require('dotenv').config();
const { Client, LocalAuth, MessageMedia } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const express = require('express');
const helmet = require('helmet');
const rateLimit = require('express-rate-limit');
const crypto = require('crypto');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const {
    reconnectDelay, shouldPurgeAuth, reconnectOutcome, isRevokedReason, ocupaSlot,
    groupRetryDelay,
} = require('./session_policy');
const { iniciarSync } = require('./group_sync');
const { coletarGrupos, descreverErro } = require('./group_reader');
const {
    buildSessionPayload, buildGruposPayload, buildInativoPayload,
} = require('./payloads');
const authStore = require('./auth_store');

const app = express();

app.use(helmet());
app.use(express.json({ limit: '50mb' }));
app.use(express.urlencoded({ limit: '50mb', extended: true }));

const limiter = rateLimit({
    windowMs: 1 * 60 * 1000,
    max: 30,
    message: { erro: 'Muitas requisições. O limite é de 30 mensagens por minuto para proteger a conta.' }
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
// 15s e folgado: a leitura so percorre a collection em memoria da pagina, sem
// round-trip de rede. Estourar aqui significa pagina morta, nao lentidao — por
// isso nao vale mais os 45s que existiam quando isto era um getChats completo.
const GROUP_SYNC_TIMEOUT_MS = parseInt(process.env.GROUP_SYNC_TIMEOUT_MS, 10) || 15000;
const QR_IDLE_DESTROY_MS = parseInt(process.env.QR_IDLE_DESTROY_MS, 10) || 180000;
const SEND_TIMEOUT_MS = parseInt(process.env.SEND_TIMEOUT_MS, 10) || 60000;
const MIN_SEND_INTERVAL_MS = parseInt(process.env.MIN_SEND_INTERVAL_MS, 10) || 2500;

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
    reconnectAttempts: 0,
    initTimer: null,
    qrIdleTimer: null,
    requestedAt: 0,
    initFailures: 0,
    authPurges: 0,           // purgas de auth neste ciclo de recuperacao
    encerrandoManual: false, // logout pedido pelo usuario: suprime o auto-reconnect
    authenticatedInAttempt: false,
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
    session.initTimer = null;
    session.qrIdleTimer = null;
    session.reconnectTimer = null;
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
    const delay = session.isConnected ? groupRetryDelay(session.gruposSyncFalhas) : null;
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
        session.fase = 'conectado';
        session.faseMsg = `Conectado - ${session.gruposCache.length} grupos.`;
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
        session.fase = 'conectado';
        agendarRetryGrupos(session);
        return false;
    }
};

// `forcar` = pedido explicito do usuario (botao "Sincronizar grupos"). Um sync ja
// em voo comecou ANTES do clique, entao seu resultado nao reflete o que a pessoa
// acabou de mudar no celular: reaproveita-lo e responder dado velho dizendo
// sucesso. A orquestracao (repique, coalescencia, promise) vive em group_sync.js.
const syncGroups = async (session, reason = 'auto', { forcar = false } = {}) => {
    if (!session.isConnected || !session.client) return false;
    return iniciarSync(session, (r) => lerGrupos(session, r), reason, { forcar });
};

// msgOverride sobrevive ao agendamento. Antes, quem quisesse explicar ao usuario
// o que estava acontecendo (ex.: "sessao corrompida, gerando novo QR") setava
// faseMsg e via a mensagem ser sobrescrita aqui na linha seguinte.
const scheduleReconnect = (session, reason, msgOverride = null) => {
    if (session.reconnectTimer) return;
    if (session.encerrandoManual) return; // logout do usuario: nao ressuscitar
    session.reconnectAttempts += 1;

    const outcome = reconnectOutcome(
        session.reconnectAttempts, session.authPurges, RECONNECT_MAX_ATTEMPTS
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

    if (outcome === 'purge') {
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
const FASES_TERMINAIS = new Set(['expirado', 'falha_auth']);
const reviveSession = (session) => {
    if (session.initialized || session.client) return session;
    if (!FASES_TERMINAIS.has(session.fase)) return session;
    console.log(`[${session.id}] Revivendo sessao terminal (${session.fase}).`);
    session.reconnectAttempts = 0;
    session.authPurges = 0;
    session.initFailures = 0;
    session.encerrandoManual = false;
    session.fase = 'iniciando';
    session.progresso = 0;
    session.faseMsg = 'Iniciando serviço…';
    initializeSession(session);
    return session;
};

const recycleSession = async (session, reason, purgeAuth = false, msgOverride = null) => {
    const client = session.client;
    if (!client) return;
    console.error(`[${session.id}] Reciclando Chromium. Motivo: ${reason}`);
    session.client = null;
    session.initialized = false;
    session.isConnected = false;
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
    removerLocksChromium(session.authPath);
    limparCachesChromium(session.authPath);
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
        session.initTimer = setTimeout(() => {
            session.initTimer = null;
            if (session.client !== client || session.isConnected || session.ultimoQR) return;
            console.error(
                `[${session.id}] Sessao travada em "${stage}" por ${SESSION_INIT_TIMEOUT_MS}ms. Reiniciando Chromium.`
            );
            const authenticatedFailure = (
                session.authenticatedInAttempt || stage === 'pos-autenticacao'
            );
            session.initFailures += 1;
            const purgeAuth = shouldPurgeAuth(session.initFailures, authenticatedFailure);
            if (purgeAuth) session.initFailures = 0;
            const msg = purgeAuth ? 'Sessão corrompida — gerando um novo QR…' : null;
            recycleSession(session, `timeout em ${stage}`, purgeAuth, msg).catch((err) => {
                console.error(`[${session.id}] Falha ao reciclar sessao travada:`, err.message);
            });
        }, SESSION_INIT_TIMEOUT_MS);
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
        session.fase = 'carregando';
        session.progresso = parseInt(percent, 10) || 0;
        session.faseMsg = message || 'Carregando WhatsApp Web…';
        if (!session.initTimer) armInitializationTimeout('carregamento do WhatsApp Web');
        console.log(`[${session.id}] ⏳ Carregando: ${session.progresso}% — ${session.faseMsg}`);
    });

    client.on('authenticated', () => {
        if (session.client !== client) return;
        session.authenticatedInAttempt = true;
        // A credencial no volume agora vale a pena restaurar num boot futuro.
        // O layout do LocalAuth nao serve como sinal: ver auth_store.js.
        markPaired(session);
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
        session.fase = 'falha_auth';
        session.faseMsg = 'Falha na autenticação — gere um novo QR.';
        console.error(`[${session.id}] ❌ Falha de autenticação:`, msg);
        recycleSession(session, 'falha de autenticacao', true).catch((err) => {
            console.error(`[${session.id}] Falha ao renovar autenticacao:`, err.message);
        });
    });

    client.on('ready', async () => {
        if (session.client !== client) return;
        if (session.initTimer) clearTimeout(session.initTimer);
        session.initTimer = null;
        session.isConnected = true;
        session.initFailures = 0;
        session.authenticatedInAttempt = false;
        session.reconnectAttempts = 0;
        session.authPurges = 0; // ciclo de recuperacao fechado com sucesso
        markPaired(session);    // rede de seguranca: 'authenticated' pode nao vir num restore
        session.whatsappId = client.info?.wid?._serialized || null;
        await encerrarSessoesDuplicadas(session);
        session.ultimoQR = null;
        if (session.qrIdleTimer) clearTimeout(session.qrIdleTimer);
        session.qrIdleTimer = null;
        session.fase = 'conectado';
        session.progresso = 100;
        session.faseMsg = 'Conectado. Atualizando grupos em segundo plano.';
        // A versao do WA Web e o que correlaciona uma quebra de leitura com um
        // rollout do WhatsApp. Sem ela, um erro vindo do bundle nao tem contexto.
        const versaoWeb = await client.getWWebVersion().catch((err) => `desconhecida (${err.message})`);
        console.log(`[${session.id}] WhatsApp conectado! WA Web ${versaoWeb}`);
        syncGroups(session, 'ready');
    });

    client.on('disconnected', async (reason) => {
        if (session.client !== client) return;
        if (session.initTimer) clearTimeout(session.initTimer);
        session.initTimer = null;
        session.isConnected = false;
        session.gruposCarregados = false;
        session.gruposSincronizando = false;
        session.gruposSyncFalhou = false; // conexao nova merece tentativa nova
        session.gruposSyncFalhas = 0;
        limparRetryGrupos(session);
        session.syncPedidoDurante = false; // sem Chromium nao ha o que repicar
        session.fase = 'desconectado';
        session.progresso = 0;
        session.faseMsg = 'Desconectado — reconectando…';
        console.log(`[${session.id}] ❌ WhatsApp foi desconectado. Motivo:`, reason);

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
        session.fase = 'falha_auth';
        session.faseMsg = 'Falha ao inicializar a sessão';
        console.error(`[${session.id}] ❌ Falha na inicialização:`, error.message);
        session.initFailures += 1;
        const purgeAuth = shouldPurgeAuth(
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

    if (!session.isConnected || !session.client) {
        return { sucesso: false, erro: 'WhatsApp não está conectado. Leia o QR Code.', instancia: session.id };
    }

    const executar = async () => {
      const espera = Math.max(0, MIN_SEND_INTERVAL_MS - (Date.now() - session.lastSendAt));
      if (espera) await new Promise((resolve) => setTimeout(resolve, espera));
      try {
        // `ready` can become stale if Chromium loses connectivity without a
        // disconnected event. Check the live state immediately before sending.
        const estado = await withTimeout(session.client.getState(), 10000, 'getState');
        if (estado !== 'CONNECTED') {
            session.isConnected = false;
            session.fase = 'reconectando';
            session.faseMsg = `WhatsApp sem conexao (${estado || 'estado desconhecido'}).`;
            setTimeout(() => recycleSession(
                session, `estado ${estado || 'desconhecido'} antes do envio`
            ), 0).unref();
            throw new Error('WhatsApp nao esta conectado. Reconecte antes de enviar.');
        }

        // Validate that the destination still exists in this account. This
        // rejects stale group IDs instead of reporting a false success.
        const chat = await withTimeout(session.client.getChatById(chatId), 15000, 'getChatById');
        if (!chat || (chatId.endsWith('@g.us') && !chat.isGroup)) {
            throw new Error('Grupo de destino nao encontrado nesta conta do WhatsApp.');
        }

        let enviada;
        if (tipo === 'texto') {
            enviada = await withTimeout(
                session.client.sendMessage(chatId, dados), SEND_TIMEOUT_MS, 'sendMessage'
            );
        } else {
            const midia = new MessageMedia(opcoes.mimetype, dados, opcoes.nomeArquivo);
            enviada = await withTimeout(
                session.client.sendMessage(chatId, midia, { caption: opcoes.legenda }),
                SEND_TIMEOUT_MS,
                'sendMessage'
            );
        }
        const mensagemId = enviada?.id?._serialized || enviada?.id?.id;
        if (!mensagemId) {
            throw new Error('WhatsApp nao confirmou a criacao da mensagem.');
        }
        session.lastSendAt = Date.now();
        console.log(`[${session.id}] Envio confirmado: ${mensagemId} -> ${chatId}`);
        return {
            sucesso: true,
            via: 'local',
            tipo,
            instancia: session.id,
            mensagem_id: mensagemId,
            ack: Number.isInteger(enviada.ack) ? enviada.ack : null,
        };
      } catch (erro) {
        console.error(`[${session.id}] Falha no envio:`, erro.message);
        if (erro.message === 'sendMessage timeout') {
            setTimeout(() => recycleSession(session, 'timeout ao enviar mensagem'), 0).unref();
        }
        return { sucesso: false, erro: erro.message || 'Falha ao enviar a mensagem.', instancia: session.id };
      }
    };
    const resultado = session.sendChain.then(executar, executar);
    session.sendChain = resultado.then(() => undefined, () => undefined);
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

    if (!numero && !grupoid) {
        return res.status(400).json({ erro: 'Você precisa informar um numero ou grupoid.', instancia: instanceId });
    }

    const chatId = grupoid || `${numero}@c.us`;

    if (base64 && mimetype) {
        if (!MIMETYPES_PERMITIDOS.has(mimetype)) {
            return res.status(400).json({ erro: 'Tipo de arquivo não permitido.', instancia: instanceId });
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
        if (mensagem.length > 4096) return res.status(400).json({ erro: 'Mensagem muito longa.', instancia: instanceId });

        const resultado = await executarEnvioInteligente(instanceId, chatId, 'texto', mensagem);
        return res.status(resultado.sucesso ? 200 : 503).json(resultado);
    }

    return res.status(400).json({ erro: 'Corpo da requisição vazio. Envie "mensagem" ou "base64".', instancia: instanceId });
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
            .filter((id) => hasStoredAuth(id))             // so quem foi pareado de fato
            .filter((id) => !fs.existsSync(disabledMarkerPathFor(authPathDe(id))))
            .sort()
            .slice(0, MAX_WHATSAPP_SESSIONS);
    } catch (err) {
        console.error('Falha ao varrer o volume de sessoes:', err.message);
        return;
    }

    if (!candidatos.length) {
        console.log('Nenhuma sessao pareada no volume para restaurar.');
        return;
    }

    console.log(`Restaurando ${candidatos.length} sessao(oes) do volume: ${candidatos.join(', ')}.`);
    candidatos.forEach((id, i) => {
        // Escalonado: cada sessao sobe um Chromium (~350MB); subir todas juntas
        // faz um pico de memoria e de CPU no boot.
        setTimeout(() => {
            console.log(`[${id}] Restaurando sessao do volume (${i + 1}/${candidatos.length}).`);
            ensureSession(id);
        }, i * SESSION_START_STAGGER_MS).unref();
    });
};

const PORT = process.env.PORT || 3000;
app.listen(PORT, '::', () => {
    console.log(`Servidor rodando na porta ${PORT}`);
    // Depois do listen: /health tem de responder dentro do grace_period do Fly
    // sem esperar Chromium nenhum.
    restaurarSessoesDoVolume();
});
