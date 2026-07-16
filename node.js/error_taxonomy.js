'use strict';

const { erroFrameDestacado } = require('./message_confirmation');

// Classificacao de falha de envio, consumida pelo Django em
// whatsapp_client.enviar_oferta -> ofertas.processar_configs_de_envio.
//
// Por que isto existe: o orquestrador do Django conta falhas_consecutivas e
// desliga a ConfiguracaoEnvio ao bater o teto. Sem classe, uma pagina do WA Web
// ainda hidratando conta igual a um grupo apagado — e ~5h de worker indisponivel
// desligavam a automacao sozinhas, sem nada para religa-la.
//
// Tres valores, nao um booleano `retryable`: o erro mais comum aqui e um throw
// minificado do bundle do WA Web (ver descreverErro), e para ele a resposta
// honesta e "nao sei". Booleano obrigaria a chutar, e o chute seguro
// (nao-retentavel) desligaria a automacao pelo mesmo motivo de hoje.
//
//   'transitorio'  -> some sozinho: nao conta falha, nao pausa a config
//   'permanente'   -> so acao do usuario resolve (grupo apagado, id invalido)
//   'desconhecido' -> conta falha, como era antes desta taxonomia existir
const TRANSITORIO = 'transitorio';
const PERMANENTE = 'permanente';
const DESCONHECIDO = 'desconhecido';

const CLASSES = new Set([TRANSITORIO, PERMANENTE, DESCONHECIDO]);

// Anexa a classe ao proprio Error nos pontos onde o motivo ja e conhecido.
// Carregar a classe no erro (em vez de re-inferir no catch por texto) mantem a
// decisao onde esta o contexto: quem lanca sabe se o grupo sumiu ou se a pagina
// estava recarregando; o catch, olhando so a string, nao saberia.
const erroClassificado = (mensagem, classe) => {
    const erro = new Error(mensagem);
    erro.classe = CLASSES.has(classe) ? classe : DESCONHECIDO;
    return erro;
};

// CONTRATO: nunca lanca e sempre devolve uma das tres classes. Roda dentro do
// catch do envio — uma excecao aqui trocaria uma falha classificavel por um 500.
//
// O try/catch abraca TUDO de proposito, em vez de proteger cada leitura: o
// argumento e, por definicao, o que o bundle minificado do WA Web resolveu
// lancar. Ler `.message` dele ja e executar codigo de terceiro (pode ser um
// getter, um Proxy), e nenhuma dessas leituras vale o risco de derrubar o envio.
const classificarErro = (erro) => {
    try {
        if (erro && typeof erro === 'object' && CLASSES.has(erro.classe)) return erro.classe;
        if (erroFrameDestacado(erro)) return TRANSITORIO;

        // withTimeout lanca `${label} timeout` (index.js). Todo timeout daqui e
        // de uma operacao no Chromium (getState, inspecionarGrupo, sendMessage):
        // a conta do usuario esta intacta, foi a pagina que nao respondeu a tempo.
        const mensagem = String((erro && erro.message) || erro || '');
        if (/\btimeout\b/i.test(mensagem)) return TRANSITORIO;
    } catch (erroDeLeitura) {
        return DESCONHECIDO;
    }
    return DESCONHECIDO;
};

module.exports = {
    TRANSITORIO, PERMANENTE, DESCONHECIDO, CLASSES,
    erroClassificado, classificarErro,
};
