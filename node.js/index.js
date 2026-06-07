require('dotenv').config(); // Carrega as variáveis do arquivo .env
const { Client, LocalAuth, MessageMedia } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const express = require('express');
const helmet = require('helmet');
const rateLimit = require('express-rate-limit');
const crypto = require('crypto'); // Módulo nativo do Node — usado para comparação segura da API Key
const path = require('path');

const app = express();
const authPath = path.join(process.cwd(), '.wwebjs_auth');

// 1. SEGURANÇA: Oculta headers do Express
app.use(helmet());

app.use(express.json({ limit: '50mb' }));
app.use(express.urlencoded({ limit: '50mb', extended: true }));

// 2. SEGURANÇA: Rate Limiting
// Limita a 30 requisições por minuto por IP para evitar spam e banimento
const limiter = rateLimit({
    windowMs: 1 * 60 * 1000, // 1 minuto
    max: 30, 
    message: { erro: 'Muitas requisições. O limite é de 30 mensagens por minuto para proteger a conta.' }
});

// Aplica o limitador apenas nas rotas de envio
app.use('/api/enviar', limiter);

// 3. SEGURANÇA: Middleware de Autenticação via API Key
const apiKeyAuth = (req, res, next) => {
    const key = req.headers['x-api-key'];
    const expected = process.env.API_KEY;

    // SEGURANÇA: crypto.timingSafeEqual evita ataques de timing.
    // Uma comparação normal (===) pode vazar o tamanho da chave pelo tempo de resposta.
    // timingSafeEqual sempre demora o mesmo tempo, independente de onde a string difere.
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

const executarEnvioInteligente = async (chatId, tipo, dados, opcoes = {}) => {
    // Tenta Local primeiro
    if (isConnected) {
        try {
            if (tipo === 'texto') {
                await client.sendMessage(chatId, dados);
            } else {
                const midia = new MessageMedia(opcoes.mimetype, dados, opcoes.nomeArquivo);
                await client.sendMessage(chatId, midia, { caption: opcoes.legenda });
            }
            return { sucesso: true, via: 'local', tipo: tipo };
        } catch (erro) {
            console.error('Falha local, tentando Evolution...', erro.message);
        }
    }

    // Fallback para Evolution
    if (evolutionConfigurada()) {
        try {
            if (tipo === 'texto') {
                await enviarTextoEvolution(chatId, dados);
            } else {
                await enviarMidiaEvolution(chatId, dados, opcoes.mimetype, opcoes.nomeArquivo, opcoes.legenda);
            }
            return { 
                sucesso: true, 
                via: 'evolution', 
                tipo: tipo,
                aviso: !isConnected ? 'WhatsApp Web desconectado. Reconecte o QR Code.' : 'Erro técnico no envio local.'
            };
        } catch (erro) {
            return { sucesso: false, erro: 'Ambos os serviços falharam.' };
        }
    }

    return { sucesso: false, erro: 'Sem conexão disponível.' };
};





let isConnected = false;
let ultimoQR = null;
let gruposCache = [];
let gruposCarregados = false; // NOVA FLAG DE CONTROLE

// ─────────────────────────────────────────────────────────────
// 4. FALLBACK: Evolution API
// Usada automaticamente quando o WhatsApp Web não está conectado.
// Configure as três variáveis abaixo no seu .env.
// ─────────────────────────────────────────────────────────────
const EVOLUTION_URL      = process.env.EVOLUTION_API_URL;      // ex: http://localhost:8080
const EVOLUTION_KEY      = process.env.EVOLUTION_API_KEY;      // API key da Evolution
const EVOLUTION_INSTANCE = process.env.EVOLUTION_INSTANCE;     // nome da instância criada na Evolution

// Verifica se as variáveis da Evolution estão configuradas no .env.
// Se não estiverem, o fallback simplesmente não será usado.
const evolutionConfigurada = () => EVOLUTION_URL && EVOLUTION_KEY && EVOLUTION_INSTANCE;

// Helper: envia texto via Evolution API
const enviarTextoEvolution = async (numero, mensagem) => {
    const resposta = await fetch(`${EVOLUTION_URL}/message/sendText/${EVOLUTION_INSTANCE}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'apikey': EVOLUTION_KEY },
        body: JSON.stringify({ number: numero, text: mensagem })
    });
    if (!resposta.ok) throw new Error(`Evolution API retornou status ${resposta.status}`);
    return resposta.json();
};

// Helper: envia mídia via Evolution API
// A Evolution exige que o campo 'mediatype' seja 'image', 'video', 'audio' ou 'document'.
const enviarMidiaEvolution = async (numero, base64, mimetype, nomeArquivo, legenda) => {
    const mediatype = mimetype.startsWith('image/') ? 'image'
        : mimetype.startsWith('video/') ? 'video'
        : mimetype.startsWith('audio/') ? 'audio'
        : 'document';

    const resposta = await fetch(`${EVOLUTION_URL}/message/sendMedia/${EVOLUTION_INSTANCE}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'apikey': EVOLUTION_KEY },
        body: JSON.stringify({
            number: numero,
            mediatype,
            mimetype,
            caption: legenda || '',
            media: base64,
            fileName: nomeArquivo || 'arquivo'
        })
    });
    if (!resposta.ok) throw new Error(`Evolution API retornou status ${resposta.status}`);
    return resposta.json();
};

// Quando o Railway reinicia o container, o volume persistente guarda arquivos
// 'SingletonLock' e 'SingletonCookie' que o Chromium usa para evitar duas instâncias.
// Como o processo anterior foi morto pelo Railway, o lock fica órfão e impede o start.
// Percorremos TODA a pasta de auth recursivamente e deletamos qualquer lock encontrado,
// independente de onde o Chromium decidiu colocar (o path varia conforme a versão).
const fs = require('fs');
const removerLocksChromium = (dir) => {
    if (!fs.existsSync(dir)) return;
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const fullPath = path.join(dir, entry.name);
        if (entry.isDirectory()) {
            removerLocksChromium(fullPath); // desce nos subdiretórios
        } else if (entry.name === 'SingletonLock' || entry.name === 'SingletonCookie') {
            fs.unlinkSync(fullPath);
            console.log(`🔓 Lock removido: ${fullPath}`);
        }
    }
};
removerLocksChromium(authPath);

const client = new Client({
    authStrategy: new LocalAuth({
        dataPath: authPath
    }),
    puppeteer: {
        // getChats() em contas grandes estoura o timeout padrão do protocolo.
        protocolTimeout: 300000,
        args: [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--disable-gpu',
            '--no-first-run',
        ]
    }
});

client.on('qr', (qr) => {
    ultimoQR = qr;
    console.log('Sessão não encontrada ou expirada. Leia o QR Code:');
    qrcode.generate(qr, { small: true });
});

client.on('ready', async () => {
    isConnected = true;
    ultimoQR = null;
    console.log('✅ WhatsApp conectado! API protegida e pronta para uso.');
    
    // O backend faz a busca uma única vez
    console.log('⏳ Iniciando sincronização de chats (pode demorar no primeiro login)...');
    try {
        const chats = await client.getChats();
        gruposCache = chats
            .filter(c => c.isGroup)
            .map(c => ({ id: c.id._serialized, nome: c.name }));
            
        gruposCarregados = true; // Avisa que o cache está pronto
        console.log(`📋 Sincronização concluída! ${gruposCache.length} grupos carregados.`);
    } catch (err) {
        console.error('❌ Erro ao pré-carregar grupos:', err.message);
    }
});

client.on('disconnected', (reason) => {
    isConnected = false;
    console.log('❌ WhatsApp foi desconectado. Motivo:', reason);
});

// Rota de status (Aberta, sem autenticação, apenas para monitoramento)
app.get('/api/status', (req, res) => {
    res.json({ conectado: isConnected });
});

// Rota para listar grupos (Protegida — expõe nomes e IDs dos grupos)
app.get('/api/grupos', apiKeyAuth, async (req, res) => {
    if (!isConnected) {
        return res.status(503).json({ erro: 'WhatsApp não está conectado.' });
    }
    
    // Verifica a flag, não o tamanho do array (suporta contas com 0 grupos)
    if (gruposCarregados) {
        return res.json({ grupos: gruposCache });
    } else {
        // Se ainda não carregou, apenas avisa o Python para esperar, sem encavalar requisições
        return res.status(503).json({ erro: 'Sincronizando chats com o celular, tente novamente em alguns segundos.' });
    }
});

// Rota para forçar o refresh da lista de grupos
app.post('/api/grupos/refresh', apiKeyAuth, async (req, res) => {
    if (!isConnected) {
        return res.status(503).json({ erro: 'WhatsApp não está conectado.' });
    }

    try {
        gruposCarregados = false;
        const chats = await client.getChats();
        gruposCache = chats
            .filter(c => c.isGroup)
            .map(c => ({ id: c.id._serialized, nome: c.name }));
        gruposCarregados = true;
        console.log(`🔄 Refresh manual: ${gruposCache.length} grupos atualizados.`);
        return res.json({ sucesso: true, total: gruposCache.length, grupos: gruposCache });
    } catch (err) {
        console.error('❌ Erro no refresh de grupos:', err.message);
        return res.status(500).json({ erro: 'Falha ao atualizar a lista de grupos.' });
    }
});

app.get('/api/qrcode', (req, res) => {
    if (isConnected) {
        return res.json({ conectado: true, qr: null, mensagem: 'WhatsApp já está conectado.' });
    }
    if (!ultimoQR) {
        return res.status(503).json({ conectado: false, qr: null, mensagem: 'QR Code ainda não gerado. Aguarde alguns segundos e tente novamente.' });
    }
    res.json({ conectado: false, qr: ultimoQR });
});

// Rota 1: Enviar texto (Protegida pelo middleware apiKeyAuth)
// Rota Única: Detecta automaticamente se é Texto ou Mídia
app.post('/api/enviar', apiKeyAuth, async (req, res) => {
    const { numero, grupoid, mensagem, base64, mimetype, nomeArquivo, legenda } = req.body;

    // 1. Validação básica de destino
    if (!numero && !grupoid) {
        return res.status(400).json({ erro: 'Você precisa informar um numero ou grupoid.' });
    }

    const chatId = grupoid || `${numero}@c.us`;

    // 2. Lógica de Decisão: Tem imagem (base64)?
    if (base64 && mimetype) {
        // É um envio de mídia
        if (!MIMETYPES_PERMITIDOS.has(mimetype)) {
            return res.status(400).json({ erro: 'Tipo de arquivo não permitido.' });
        }

        console.log(`[AUTO] Detectada Mídia para ${chatId}`);
        const resultado = await executarEnvioInteligente(chatId, 'midia', base64, {
            mimetype,
            nomeArquivo: nomeArquivo || 'arquivo',
            legenda: legenda || mensagem // Usa a legenda ou a mensagem como legenda da foto
        });

        return res.status(resultado.sucesso ? 200 : 503).json(resultado);
    } 
    
    // 3. Se não tem base64, verifica se tem texto
    if (mensagem) {
        console.log(`[AUTO] Detectado Texto para ${chatId}`);
        if (mensagem.length > 4096) return res.status(400).json({ erro: 'Mensagem muito longa.' });

        const resultado = await executarEnvioInteligente(chatId, 'texto', mensagem);
        return res.status(resultado.sucesso ? 200 : 503).json(resultado);
    }

    // 4. Se não tem nenhum dos dois
    return res.status(400).json({ erro: 'Corpo da requisição vazio. Envie "mensagem" ou "base64".' });
});

client.initialize();
const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
    console.log(`Servidor rodando na porta ${PORT}`);
});