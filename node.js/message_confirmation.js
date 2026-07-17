'use strict';

const crypto = require('crypto');

// whatsapp-web.js normalmente entrega `Message.id` como Wid (`_serialized` +
// `id`), mas o modelo devolvido pelo WhatsApp Web muda com frequência. Em
// algumas versões chega uma string direta ou o id só no `_data` bruto.
// Centralizar a leitura evita marcar um envio confirmado como falho por uma
// simples mudança de formato.
const extrairMensagemId = (mensagem) => {
    if (!mensagem || typeof mensagem !== 'object') return null;

    const candidatos = [mensagem.id, mensagem._data?.id];
    for (const candidato of candidatos) {
        if (typeof candidato === 'string' && candidato.trim()) return candidato;
        if (!candidato || typeof candidato !== 'object') continue;
        for (const campo of ['_serialized', 'id', 'key']) {
            if (typeof candidato[campo] === 'string' && candidato[campo].trim()) {
                return candidato[campo];
            }
        }
    }
    return null;
};

// Não use `waitUntilMsgSent` aqui. Na versão atual do WhatsApp Web, esperar o
// ACK mantém o evaluate aberto durante uma recarga silenciosa da página e pode
// terminar em "detached Frame" mesmo depois que a mensagem já foi entregue.
// A resolução normal do sendMessage já significa que o Web aceitou o envio.
const opcoesDeEnvio = (legenda = null) => ({
    ...(legenda != null ? { caption: legenda } : {}),
});

// A página do WhatsApp foi substituída enquanto um envio estava em voo. Depois
// que sendMessage foi chamado, o resultado é intrinsecamente ambíguo: repetir
// pode duplicar uma mensagem que já chegou ao grupo. O chamador registra isso
// como sucesso protegido e recicla o Chromium antes do próximo envio.
const erroFrameDestacado = (erro) => /detached\s+frame|frame\s+was\s+detached/i.test(
    String(erro?.message || erro || '')
);

// Mesma causa raiz do frame destacado, outra assinatura: quando o WA Web recarrega
// a página no meio de um `pupPage.evaluate` (o sendMessage do whatsapp-web.js roda
// getChat+sendMessage num único evaluate/CDP Runtime.callFunctionOn), o contexto JS
// é destruído e o puppeteer lança "Execution context was destroyed" — ou, se o
// alvo/sessão do CDP caiu junto, "Target/Session closed". Nada disso casava com o
// regex de frame destacado, então caía em 'desconhecido' (contava falha) e, durante
// o envio, perdia a proteção contra duplicata. Ver index.js (catch do envio).
const erroContextoDestruido = (erro) => /execution context (was destroyed|is not available)|cannot find context|target closed|session closed|protocol error \(runtime\.callfunctionon\)/i.test(
    String(erro?.message || erro || '')
);

// "Recarga em voo": a página do WA Web foi trocada/destruída sob nossos pés,
// qualquer que seja a assinatura. É o predicado que os pontos de decisão do envio
// devem consultar (retry pré-envio, envio protegido pós-envio, classificação).
const erroReloadEmVoo = (erro) => erroFrameDestacado(erro) || erroContextoDestruido(erro);

// Antes de sendMessage é seguro repetir: nada foi enviado ainda. O WA Web troca
// seu frame principal em recargas silenciosas; a primeira evaluate pode cair
// nesse instante e a segunda, poucos ms depois, já encontra a página estável.
const repetirSeFrameDestacado = async (operacao, {
    tentativas = 2,
    esperar = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    atrasoMs = 750,
} = {}) => {
    let ultimoErro;
    for (let tentativa = 1; tentativa <= tentativas; tentativa += 1) {
        try {
            return await operacao();
        } catch (erro) {
            ultimoErro = erro;
            if (!erroReloadEmVoo(erro) || tentativa === tentativas) throw erro;
            await esperar(atrasoMs);
        }
    }
    throw ultimoErro;
};

// `sendMessage` só resolve depois que o WA Web aceitou o envio. A versão atual
// pode, porém, devolver undefined em vez do modelo da mensagem — comportamento
// observado no worker real, embora a mensagem tenha chegado ao grupo. Nesse
// caso o ID local preserva o contrato HTTP (e impede o Django de repetir o
// envio), sem fingir que ele é o ID canônico do WhatsApp.
const confirmarMensagem = (mensagem, instancia, {
    agora = () => Date.now(),
    uuid = () => crypto.randomUUID(),
} = {}) => {
    const idNativo = extrairMensagemId(mensagem);
    if (idNativo) return { mensagemId: idNativo, confirmacao: 'nativa' };
    return {
        mensagemId: `local-${instancia}-${agora()}-${uuid()}`,
        confirmacao: 'aceita_sem_id',
    };
};

module.exports = {
    extrairMensagemId, opcoesDeEnvio, erroFrameDestacado, erroContextoDestruido,
    erroReloadEmVoo, confirmarMensagem, repetirSeFrameDestacado,
};
