require('dotenv').config();
const { Client, LocalAuth, MessageMedia } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const express = require('express');
const helmet = require('helmet');
const rateLimit = require('express-rate-limit');
const crypto = require('crypto');
const path = require('path');
const fs = require('fs');

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
const INITIAL_INSTANCES = (process.env.WHATSAPP_INSTANCES || '')
    .split(',')
    .map((value) => value.trim())
    .filter(Boolean);

const sanitizeInstanceId = (value) => {
    const raw = (value || '').toString().trim().toLowerCase();
    const normalized = raw.replace(/[^a-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '');
    return normalized || DEFAULT_INSTANCE_ID;
};

const RECONNECT_DELAY_MS = parseInt(process.env.RECONNECT_DELAY_MS, 10) || 5000;
const PUPPETEER_EXECUTABLE_PATH = process.env.PUPPETEER_EXECUTABLE_PATH || undefined;

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
    fase: 'iniciando',
    progresso: 0,
    faseMsg: 'Iniciando serviço…',
});

const sessions = new Map();

const buildSessionPayload = (session) => ({
    instancia: session.id,
    conectado: session.isConnected,
    fase: session.fase,
    progresso: session.progresso,
    mensagem: session.faseMsg,
    grupos: session.gruposCarregados ? session.gruposCache.length : 0,
    qr: session.ultimoQR,
});

const initializeSession = (session) => {
    if (session.initialized) return session;

    removerLocksChromium(session.authPath);
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
                '--no-first-run',
            ]
        }
    });

    session.client = client;
    session.initialized = true;

    client.on('qr', (qr) => {
        session.ultimoQR = qr;
        session.fase = 'qr';
        session.progresso = 0;
        session.faseMsg = 'Aguardando leitura do QR Code…';
        console.log(`[${session.id}] Sessão não encontrada ou expirada. Leia o QR Code:`);
        qrcode.generate(qr, { small: true });
    });

    client.on('loading_screen', (percent, message) => {
        session.fase = 'carregando';
        session.progresso = parseInt(percent, 10) || 0;
        session.faseMsg = message || 'Carregando WhatsApp Web…';
        console.log(`[${session.id}] ⏳ Carregando: ${session.progresso}% — ${session.faseMsg}`);
    });

    client.on('authenticated', () => {
        session.ultimoQR = null;
        session.fase = 'autenticado';
        session.faseMsg = 'Autenticado — preparando sessão…';
        console.log(`[${session.id}] 🔑 Autenticado.`);
    });

    client.on('auth_failure', (msg) => {
        session.fase = 'falha_auth';
        session.faseMsg = 'Falha na autenticação — gere um novo QR.';
        console.error(`[${session.id}] ❌ Falha de autenticação:`, msg);
    });

    client.on('ready', async () => {
        session.isConnected = true;
        session.ultimoQR = null;
        session.fase = 'sincronizando';
        session.progresso = 100;
        session.faseMsg = 'Sincronizando grupos…';
        console.log(`[${session.id}] ✅ WhatsApp conectado!`);

        try {
            const chats = await client.getChats();
            session.gruposCache = chats
                .filter((c) => c.isGroup)
                .map((c) => ({ id: c.id._serialized, nome: c.name }));
            session.gruposCarregados = true;
            session.fase = 'conectado';
            session.faseMsg = `Conectado — ${session.gruposCache.length} grupos.`;
            console.log(`[${session.id}] 📋 Sincronização concluída! ${session.gruposCache.length} grupos carregados.`);
        } catch (err) {
            session.fase = 'conectado';
            session.faseMsg = 'Conectado (falha ao listar grupos).';
            console.error(`[${session.id}] ❌ Erro ao pré-carregar grupos:`, err.message);
        }
    });

    client.on('disconnected', async (reason) => {
        session.isConnected = false;
        session.gruposCarregados = false;
        session.fase = 'desconectado';
        session.progresso = 0;
        session.faseMsg = 'Desconectado — reconectando…';
        console.log(`[${session.id}] ❌ WhatsApp foi desconectado. Motivo:`, reason);

        // Fecha o Chromium antigo para liberar memória antes de reconectar.
        try { await client.destroy(); } catch (err) { /* ignora */ }
        session.client = null;
        session.initialized = false;

        // LOGOUT: o celular desvinculou → o LocalAuth some, gera novo QR na reinicialização.
        // Outros motivos (queda de rede, etc.): a sessão no volume ainda é válida → reconecta sozinho.
        setTimeout(() => {
            if (session.initialized) return; // já reinicializado por outra via
            console.log(`[${session.id}] ♻️ Tentando reconectar…`);
            initializeSession(session);
        }, RECONNECT_DELAY_MS);
    });

    client.initialize().catch((error) => {
        session.fase = 'falha_auth';
        session.faseMsg = 'Falha ao inicializar a sessão';
        console.error(`[${session.id}] ❌ Falha na inicialização:`, error.message);
    });

    return session;
};

const ensureSession = (instanceId) => {
    const normalizedId = sanitizeInstanceId(instanceId);
    if (!sessions.has(normalizedId)) {
        const session = createSessionState(normalizedId);
        sessions.set(normalizedId, session);
        initializeSession(session);
    }
    return sessions.get(normalizedId);
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

    try {
        if (tipo === 'texto') {
            await session.client.sendMessage(chatId, dados);
        } else {
            const midia = new MessageMedia(opcoes.mimetype, dados, opcoes.nomeArquivo);
            await session.client.sendMessage(chatId, midia, { caption: opcoes.legenda });
        }
        return { sucesso: true, via: 'local', tipo, instancia: session.id };
    } catch (erro) {
        console.error(`[${session.id}] Falha no envio:`, erro.message);
        return { sucesso: false, erro: 'Falha ao enviar a mensagem.', instancia: session.id };
    }
};

// Revive toda sessão já persistida no volume: cada subpasta de .wwebjs_auth é uma
// instância que já logou. Assim, após um deploy/restart, todos os WhatsApps voltam
// a conectar sozinhos, sem precisar reescanear o QR.
const rehydrateSessions = () => {
    if (!fs.existsSync(authRootPath)) return;
    for (const entry of fs.readdirSync(authRootPath, { withFileTypes: true })) {
        if (entry.isDirectory()) ensureSession(entry.name);
    }
};

[DEFAULT_INSTANCE_ID, ...INITIAL_INSTANCES].forEach((instanceId) => {
    if (!instanceId) return;
    ensureSession(instanceId);
});
rehydrateSessions();

// Fechamento gracioso: o Fly envia SIGTERM a cada deploy. Fechar o Chromium
// corretamente evita locks corrompidos que fariam a sessão "sumir".
let encerrando = false;
const shutdown = async (signal) => {
    if (encerrando) return;
    encerrando = true;
    console.log(`🛑 ${signal} recebido — encerrando sessões…`);
    await Promise.allSettled(
        Array.from(sessions.values()).map((s) => (s.client ? s.client.destroy() : null))
    );
    process.exit(0);
};
process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));

app.get(['/api/status', '/api/status/:instance'], apiKeyAuth, (req, res) => {
    const session = ensureSession(resolveInstanceId(req));
    res.json(buildSessionPayload(session));
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
    res.json({ sucesso: true, instancia: session.id, status: buildSessionPayload(session) });
});

app.get(['/api/grupos', '/api/grupos/:instance'], apiKeyAuth, async (req, res) => {
    const session = ensureSession(resolveInstanceId(req));
    if (!session.isConnected) {
        return res.status(503).json({ erro: 'WhatsApp não está conectado.', instancia: session.id });
    }

    if (session.gruposCarregados) {
        return res.json({ instancia: session.id, grupos: session.gruposCache });
    }

    return res.status(503).json({ erro: 'Sincronizando chats com o celular, tente novamente em alguns segundos.', instancia: session.id });
});

app.post(['/api/grupos/refresh', '/api/grupos/refresh/:instance'], apiKeyAuth, async (req, res) => {
    const session = ensureSession(resolveInstanceId(req));
    if (!session.isConnected || !session.client) {
        return res.status(503).json({ erro: 'WhatsApp não está conectado.', instancia: session.id });
    }

    try {
        session.gruposCarregados = false;
        const chats = await session.client.getChats();
        session.gruposCache = chats
            .filter((c) => c.isGroup)
            .map((c) => ({ id: c.id._serialized, nome: c.name }));
        session.gruposCarregados = true;
        console.log(`[${session.id}] 🔄 Refresh manual: ${session.gruposCache.length} grupos atualizados.`);
        return res.json({ sucesso: true, instancia: session.id, total: session.gruposCache.length, grupos: session.gruposCache });
    } catch (err) {
        console.error(`[${session.id}] ❌ Erro no refresh de grupos:`, err.message);
        return res.status(500).json({ erro: 'Falha ao atualizar a lista de grupos.', instancia: session.id });
    }
});

app.get(['/api/qrcode', '/api/qrcode/:instance'], apiKeyAuth, (req, res) => {
    const session = ensureSession(resolveInstanceId(req));
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
app.listen(PORT, () => {
    console.log(`Servidor rodando na porta ${PORT}`);
});