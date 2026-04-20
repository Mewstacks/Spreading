require('dotenv').config(); // Carrega as variáveis do arquivo .env
const { Client, LocalAuth, MessageMedia } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const express = require('express');
const helmet = require('helmet');
const rateLimit = require('express-rate-limit');
const crypto = require('crypto'); // Módulo nativo do Node — usado para comparação segura da API Key

const app = express();

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

let isConnected = false;
let ultimoQR = null;

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

const client = new Client({
    authStrategy: new LocalAuth(),
    puppeteer: {
        args: ['--no-sandbox', '--disable-setuid-sandbox']
    }
});

client.on('qr', (qr) => {
    ultimoQR = qr;
    console.log('Sessão não encontrada ou expirada. Leia o QR Code:');
    qrcode.generate(qr, { small: true });
});

client.on('ready', () => {
    isConnected = true;
    ultimoQR = null; // limpa QR após conexão
    console.log('✅ WhatsApp conectado! API protegida e pronta para uso.');
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
    try {
        const chats = await client.getChats();
        const grupos = chats
            .filter(c => c.isGroup)
            .map(c => ({ id: c.id._serialized, nome: c.name }));
        res.json({ grupos });
    } catch (erro) {
        console.error('Erro ao listar grupos:', erro);
        res.status(500).json({ erro: 'Erro interno ao listar grupos.' });
    }
});

// Rota de QR Code (Aberta — o QR por si só não compromete a segurança, é só um código de pareamento)
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
app.post('/api/enviar/texto', apiKeyAuth, async (req, res) => {
    const { numero, grupoid, mensagem } = req.body;
    if ((!numero && !grupoid) || !mensagem) return res.status(400).json({ erro: 'numero (ou grupoid) e mensagem são obrigatórios.' });

    let chatId;
    if (grupoid) {
        // SEGURANÇA: Valida formato do ID de grupo.
        // IDs de grupo podem conter hífens (ex: 120363XXXX-XXXX@g.us), por isso [\d-]+
        if (!/^[\d-]+@g\.us$/.test(grupoid)) {
            return res.status(400).json({ erro: 'grupoid inválido. Formato esperado: 120363XXXXXX@g.us' });
        }
        chatId = grupoid;
    } else {
        if (!/^\d+$/.test(numero)) {
            return res.status(400).json({ erro: 'Número inválido. Use apenas dígitos.' });
        }
        chatId = `${numero}@c.us`;
    }

    // SEGURANÇA: Limita o tamanho da mensagem a 4096 caracteres.
    // Sem limite, alguém poderia enviar payloads gigantes e sobrecarregar o processo.
    if (mensagem.length > 4096) {
        return res.status(400).json({ erro: 'Mensagem muito longa. Máximo de 4096 caracteres.' });
    }

    // Caminho primário: WhatsApp Web conectado
    if (isConnected) {
        try {
            await client.sendMessage(chatId, mensagem);
            return res.status(200).json({ sucesso: true, mensagem: 'Texto enviado.' });
        } catch (erro) {
            console.error('Erro ao enviar texto via WhatsApp Web:', erro);
            return res.status(500).json({ sucesso: false, erro: 'Erro interno ao enviar texto.' });
        }
    }

    // Caminho de fallback: WhatsApp Web desconectado — tenta via Evolution API
    if (!evolutionConfigurada()) {
        // Evolution não está configurada no .env, nada mais a tentar
        return res.status(503).json({ erro: 'WhatsApp Web desconectado e Evolution API não configurada.' });
    }

    try {
        // Passa chatId (não numero) — chatId pode ser grupo (XXXX@g.us) ou contato (XXXX@c.us)
        await enviarTextoEvolution(chatId, mensagem);
        return res.status(200).json({
            sucesso: true,
            via: 'evolution',
            mensagem: 'Texto enviado via Evolution API (fallback).',
            aviso: 'WhatsApp Web está desconectado. Reconecte reiniciando o servidor e lendo o QR Code.'
        });
    } catch (erro) {
        console.error('Erro ao enviar texto via Evolution API (fallback):', erro);
        return res.status(500).json({ sucesso: false, erro: 'Erro interno ao enviar texto (fallback também falhou).' });
    }
});

// SEGURANÇA: Lista explícita de MIMEtypes permitidos.
// Sem isso, qualquer tipo de arquivo poderia ser enviado (executáveis, scripts, etc.).
// Inclui imagens de alta qualidade (PNG, JPEG, WEBP, TIFF), vídeos, áudios e PDFs.
const MIMETYPES_PERMITIDOS = new Set([
    'image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/tiff', 'image/bmp',
    'video/mp4', 'video/3gpp', 'video/avi', 'video/quicktime',
    'audio/mpeg', 'audio/ogg', 'audio/wav', 'audio/aac',
    'application/pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
]);

// Rota 2: Enviar Mídia (Protegida pelo middleware apiKeyAuth)
app.post('/api/enviar/midia', apiKeyAuth, async (req, res) => {
    const { numero, grupoid, base64, mimetype, nomeArquivo, legenda } = req.body;
    if ((!numero && !grupoid) || !base64 || !mimetype) return res.status(400).json({ erro: 'Dados incompletos.' });

    let chatId;
    if (grupoid) {
        // IDs de grupo podem conter hífens (ex: 120363XXXX-XXXX@g.us), por isso [\d-]+
        if (!/^[\d-]+@g\.us$/.test(grupoid)) {
            return res.status(400).json({ erro: 'grupoid inválido. Formato esperado: 120363XXXXXX@g.us' });
        }
        chatId = grupoid;
    } else {
        if (!/^\d+$/.test(numero)) {
            return res.status(400).json({ erro: 'Número inválido. Use apenas dígitos.' });
        }
        chatId = `${numero}@c.us`;
    }

    // SEGURANÇA: Rejeita tipos de arquivo não autorizados.
    // Impede que alguém tente enviar um executável ou tipo malicioso disfarçado.
    if (!MIMETYPES_PERMITIDOS.has(mimetype)) {
        return res.status(400).json({ erro: 'Tipo de arquivo não permitido.' });
    }

    // Caminho primário: WhatsApp Web conectado
    if (isConnected) {
        try {
            const midia = new MessageMedia(mimetype, base64, nomeArquivo);
            await client.sendMessage(chatId, midia, { caption: legenda || '' });
            return res.status(200).json({ sucesso: true, mensagem: 'Mídia enviada.' });
        } catch (erro) {
            console.error('Erro ao enviar mídia via WhatsApp Web:', erro);
            return res.status(500).json({ sucesso: false, erro: 'Erro interno ao enviar mídia.' });
        }
    }

    // Caminho de fallback: WhatsApp Web desconectado — tenta via Evolution API
    if (!evolutionConfigurada()) {
        return res.status(503).json({ erro: 'WhatsApp Web desconectado e Evolution API não configurada.' });
    }

    try {
        // Passa chatId (não numero) — chatId pode ser grupo (XXXX@g.us) ou contato (XXXX@c.us)
        await enviarMidiaEvolution(chatId, base64, mimetype, nomeArquivo, legenda);
        return res.status(200).json({
            sucesso: true,
            via: 'evolution',
            mensagem: 'Mídia enviada via Evolution API (fallback).',
            aviso: 'WhatsApp Web está desconectado. Reconecte reiniciando o servidor e lendo o QR Code.'
        });
    } catch (erro) {
        console.error('Erro ao enviar mídia via Evolution API (fallback):', erro);
        return res.status(500).json({ sucesso: false, erro: 'Erro interno ao enviar mídia (fallback também falhou).' });
    }
});

client.initialize();
const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
    console.log(`🚀 Servidor rodando na porta ${PORT}`);
});