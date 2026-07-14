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
const { reconnectDelay, shouldPurgeAuth } = require('./session_policy');

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
const PUPPETEER_EXECUTABLE_PATH = process.env.PUPPETEER_EXECUTABLE_PATH || undefined;
const PRINT_QR_TO_LOGS = process.env.PRINT_QR_TO_LOGS === '1';
const WATCHDOG_TIMEOUT_MS = parseInt(process.env.WATCHDOG_TIMEOUT_MS, 10) || 45000;
const WATCHDOG_INTERVAL_MS = parseInt(process.env.WATCHDOG_INTERVAL_MS, 10) || 5000;
const MAX_WHATSAPP_SESSIONS = parseInt(process.env.MAX_WHATSAPP_SESSIONS, 10) || 4;
const SESSION_INIT_TIMEOUT_MS = parseInt(process.env.SESSION_INIT_TIMEOUT_MS, 10) || 90000;
const GROUP_SYNC_TIMEOUT_MS = parseInt(process.env.GROUP_SYNC_TIMEOUT_MS, 10) || 45000;
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
    groupSyncPromise: null,
    fase: 'iniciando',
    progresso: 0,
    reconnectTimer: null,
    reconnectAttempts: 0,
    initTimer: null,
    qrIdleTimer: null,
    requestedAt: 0,
    initFailures: 0,
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
const disabledMarkerPath = (session) => path.join(session.authPath, '.runtime-disabled');

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

const buildSessionPayload = (session) => ({
    instancia: session.id,
    conectado: session.isConnected,
    fase: session.fase,
    progresso: session.progresso,
    mensagem: session.faseMsg,
    grupos: session.gruposCarregados ? session.gruposCache.length : 0,
    grupos_sincronizando: session.gruposSincronizando,
    qr: session.ultimoQR,
});

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

const syncGroups = async (session, reason = 'auto') => {
    if (!session.isConnected || !session.client) return false;
    if (session.groupSyncPromise) return session.groupSyncPromise;

    session.gruposSincronizando = true;
    session.groupSyncPromise = (async () => {
        try {
            const chats = await withTimeout(session.client.getChats(), GROUP_SYNC_TIMEOUT_MS, 'getChats');
            session.gruposCache = chats
                .filter((c) => c.isGroup)
                .map((c) => ({ id: c.id._serialized, nome: c.name }));
            session.gruposCarregados = true;
            session.fase = 'conectado';
            session.faseMsg = `Conectado - ${session.gruposCache.length} grupos.`;
            console.log(`[${session.id}] Grupos sincronizados (${reason}): ${session.gruposCache.length}.`);
            return true;
        } catch (err) {
            session.gruposCarregados = false;
            console.error(`[${session.id}] Erro ao sincronizar grupos (${reason}):`, err.message);
            // A lista de chats e secundaria. `ready` ja comprovou a conexao;
            // nunca destrua uma sessao saudavel porque getChats ficou lento.
            session.fase = 'conectado';
            session.faseMsg = 'Conectado - lista de grupos indisponivel temporariamente.';
            return false;
        } finally {
            session.gruposSincronizando = false;
            session.groupSyncPromise = null;
        }
    })();

    return session.groupSyncPromise;
};

const scheduleReconnect = (session, reason) => {
    if (session.reconnectTimer) return;
    session.reconnectAttempts += 1;
    const delay = reconnectDelay(
        session.reconnectAttempts, RECONNECT_DELAY_MS, RECONNECT_MAX_DELAY_MS
    );
    session.fase = 'reconectando';
    session.progresso = 0;
    session.faseMsg = `Recuperando sessao (tentativa ${session.reconnectAttempts})...`;
    console.log(`[${session.id}] Reconnect agendado em ${delay}ms. Motivo: ${reason}`);

    session.reconnectTimer = setTimeout(() => {
        session.reconnectTimer = null;
        if (session.initialized) return;
        console.log(`[${session.id}] Tentando reconectar...`);
        initializeSession(session);
    }, delay);
};

const recycleSession = async (session, reason, purgeAuth = false) => {
    const client = session.client;
    if (!client) return;
    console.error(`[${session.id}] Reciclando Chromium. Motivo: ${reason}`);
    session.client = null;
    session.initialized = false;
    session.isConnected = false;
    session.whatsappId = null;
    session.gruposCarregados = false;
    session.gruposSincronizando = false;
    session.authenticatedInAttempt = false;
    if (session.initTimer) clearTimeout(session.initTimer);
    session.initTimer = null;
    try { await withTimeout(client.destroy(), 10000, 'client.destroy'); } catch (err) {
        console.warn(`[${session.id}] Chromium nao encerrou limpo:`, err.message);
    }
    if (purgeAuth) {
        const resolvedRoot = path.resolve(authRootPath);
        const resolvedSession = path.resolve(session.authPath);
        if (resolvedSession.startsWith(`${resolvedRoot}${path.sep}`)) {
            try {
                fs.rmSync(resolvedSession, { recursive: true, force: true });
                console.error(`[${session.id}] Perfil LocalAuth corrompido removido; novo QR sera gerado.`);
            } catch (err) {
                console.error(`[${session.id}] Falha ao limpar perfil LocalAuth:`, err.message);
            }
        }
    }
    scheduleReconnect(session, reason);
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
            if (purgeAuth) {
                session.faseMsg = 'Sessao corrompida detectada - gerando um novo QR...';
                session.initFailures = 0;
            }
            recycleSession(session, `timeout em ${stage}`, purgeAuth).catch((err) => {
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
        session.whatsappId = client.info?.wid?._serialized || null;
        await encerrarSessoesDuplicadas(session);
        session.ultimoQR = null;
        if (session.qrIdleTimer) clearTimeout(session.qrIdleTimer);
        session.qrIdleTimer = null;
        session.fase = 'conectado';
        session.progresso = 100;
        session.faseMsg = 'Conectado. Atualizando grupos em segundo plano.';
        console.log(`[${session.id}] WhatsApp conectado!`);
        syncGroups(session, 'ready');
    });

    client.on('disconnected', async (reason) => {
        if (session.client !== client) return;
        if (session.initTimer) clearTimeout(session.initTimer);
        session.initTimer = null;
        session.isConnected = false;
        session.gruposCarregados = false;
        session.gruposSincronizando = false;
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

        // LOGOUT: o celular desvinculou → o LocalAuth some, gera novo QR na reinicialização.
        // Outros motivos (queda de rede, etc.): a sessão no volume ainda é válida → reconecta sozinho.
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

const ensureSession = (instanceId) => {
    const normalizedId = sanitizeInstanceId(instanceId);
    if (!sessions.has(normalizedId)) {
        if (sessions.size >= MAX_WHATSAPP_SESSIONS) {
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

app.get(['/api/grupos', '/api/grupos/:instance'], apiKeyAuth, async (req, res) => {
    const session = findSession(resolveInstanceId(req));
    if (!session || !session.isConnected) {
        return res.status(503).json({ erro: 'WhatsApp não está conectado.', instancia: session.id });
    }

    if (session.gruposCarregados) {
        return res.json({ instancia: session.id, grupos: session.gruposCache });
    }

    syncGroups(session, 'api-grupos');
    return res.status(202).json({
        sincronizando: true,
        erro: 'Sincronizando chats com o celular, tente novamente em alguns segundos.',
        instancia: session.id,
        grupos: [],
    });
});

app.post(['/api/grupos/refresh', '/api/grupos/refresh/:instance'], apiKeyAuth, async (req, res) => {
    const session = findSession(resolveInstanceId(req));
    if (!session || !session.isConnected || !session.client) {
        return res.status(503).json({ erro: 'WhatsApp não está conectado.', instancia: session.id });
    }

    session.gruposCarregados = false;
    syncGroups(session, 'refresh-manual');
    return res.status(202).json({
        sincronizando: true,
        instancia: session.id,
        mensagem: 'Sincronizacao iniciada.',
    });
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

const PORT = process.env.PORT || 3000;
app.listen(PORT, '::', () => {
    console.log(`Servidor rodando na porta ${PORT}`);
});
