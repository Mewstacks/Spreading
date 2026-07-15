'use strict';

// Leitura da lista de grupos direto da collection do WhatsApp Web.
//
// Por que nao usamos client.getChats(): ele desce para WWebJS.getChats, que roda
// Promise.all(getChatModel) sobre TODOS os chats da conta. getChatModel faz, por
// chat, um serialize() completo, um groupMetadata.update() (round-trip de rede),
// toPn() por participante (migracao LID) e um getMessagesById() para a ultima
// mensagem. Um unico chat que lance derruba o Promise.all inteiro — e o erro
// chega minificado ("r"), sem dizer qual chat nem qual passo. Era exatamente
// isso que deixava a lista de grupos permanentemente vazia com o WhatsApp
// conectado.
//
// Aqui so precisamos de { id, nome }. Entao filtramos os grupos ANTES de tocar
// em qualquer coisa cara e lemos so os dois campos, com try/catch POR GRUPO:
// um grupo problematico vira uma entrada em `ignorados`, nunca uma lista vazia.
//
// coletarGrupos roda DENTRO do Chromium (via pupPage.evaluate). Por isso e
// auto-contida: sem require, sem closure sobre o escopo do modulo. Recebe `win`
// como parametro justamente para ser testavel em Node com um window falso
// (test/group_reader.test.js), sem subir navegador.
//
// CONTRATO: NUNCA lanca. Sempre devolve um envelope:
//   { ok: true,  grupos, ignorados, totalChats }
//   { ok: false, passo, erro }   <- `passo` diz onde quebrou, em vez de "r"
const coletarGrupos = (win) => {
    // Todo acesso ao modelo passa por aqui: sao getters mobx que podem lancar
    // sozinhos, mesmo quando o objeto existe.
    const seguro = (fn, padrao = null) => {
        try {
            const valor = fn();
            return valor === undefined ? padrao : valor;
        } catch (err) {
            return padrao;
        }
    };

    // Passo 'collections': o nome do modulo muda entre versoes do WA Web.
    // Reportar as chaves disponiveis e o que permite descobrir o novo nome sem
    // precisar depurar dentro do Chromium.
    let colecoes = null;
    try {
        colecoes = win.require('WAWebCollections');
    } catch (err) {
        return { ok: false, passo: 'collections', erro: String(err && err.message || err) };
    }
    const Chat = seguro(() => colecoes.Chat) || seguro(() => colecoes.WAWebChatCollection);
    if (!Chat) {
        return {
            ok: false,
            passo: 'collections',
            erro: 'WAWebCollections sem .Chat',
            modulos: seguro(() => Object.keys(colecoes || {}), []),
        };
    }

    // Passo 'models': getModelsArray e a API atual; models/_models sao o que
    // existia antes e seguem presentes como propriedade do collection mobx.
    const chats = seguro(() => Chat.getModelsArray && Chat.getModelsArray())
        || seguro(() => Chat.models)
        || seguro(() => Chat._models);
    if (!Array.isArray(chats)) {
        return { ok: false, passo: 'models', erro: 'collection sem array de chats' };
    }

    const grupos = [];
    const ignorados = [];
    for (const chat of chats) {
        const id = seguro(() => chat.id._serialized);
        try {
            // Filtra ANTES de ler qualquer campo caro. Deixa de fora @c.us,
            // @newsletter, @broadcast e @lid.
            const ehGrupo = seguro(() => chat.id.server === 'g.us', false)
                || Boolean(seguro(() => chat.groupMetadata));
            if (!ehGrupo) continue;
            if (!id) {
                ignorados.push({ id: null, erro: 'chat sem id._serialized' });
                continue;
            }

            // formattedTitle primeiro: Chat.js:28 (`this.name = data.formattedTitle`)
            // mostra que e exatamente o que o getChats() antigo entregava, entao
            // o nome nao muda no caminho feliz. Os demais cobrem o caso do grupo
            // cujo titulo ainda nao foi computado no cache local.
            const nome = seguro(() => chat.formattedTitle)
                || seguro(() => chat.name)
                || seguro(() => chat.subject)
                || seguro(() => chat.groupMetadata.subject)
                || null;

            // Nome nunca vazio: um item sem rotulo seria inutil no seletor.
            // `nomeAusente` separa "grupo sem nome" de "nome nao lido" no log.
            grupos.push({ id, nome: nome || id.split('@')[0], nomeAusente: !nome });
        } catch (err) {
            // O coracao da correcao: um grupo ruim nao pode zerar a lista.
            ignorados.push({ id, erro: String(err && err.message || err) });
        }
    }

    return { ok: true, grupos, ignorados, totalChats: chats.length };
};

// O throw que motivou tudo isto tinha message "r": o bundle minificado do WA Web
// lanca objetos que nao sao Error, e `err.message` sozinho vira ruido. Aqui
// preservamos o que der: stack quando ha, forma serializada quando nao ha.
const descreverErro = (err) => {
    if (err instanceof Error) {
        const origem = err.stack ? ` | ${err.stack.split('\n').slice(1, 3).join(' ').trim()}` : '';
        return `${err.name}: ${err.message}${origem}`;
    }
    try {
        return `${typeof err} ${JSON.stringify(err)}`;
    } catch (erroSerializacao) {
        return `${typeof err} ${String(err)}`;
    }
};

module.exports = { coletarGrupos, descreverErro };
