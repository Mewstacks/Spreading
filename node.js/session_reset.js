'use strict';

// Motivo estruturado de uma falha_reset, gravado em session.motivoFalhaReset e
// impresso numa linha só no `fly logs`. Sem isto, os seis caminhos que levam a
// "Não foi possível gerar um QR novo" eram indistinguíveis em produção: não dava
// para saber se o Chromium não morreu, se o purge falhou, ou se o QR só não veio
// a tempo.
const MOTIVO_FALHA_RESET = {
    CHROMIUM_NAO_ENCERROU: 'chromium_nao_encerrou',
    PURGE_FALHOU: 'purge_falhou',
    INIT_FALHOU: 'init_falhou',
    LIMPEZA_RETRY_FALHOU: 'limpeza_retry_falhou',
    QR_NAO_GERADO: 'qr_nao_gerado',
    RETRY_FALHOU: 'retry_falhou',
    DESCONHECIDO: 'desconhecido',
};

const RESET_TIMER_FIELDS = [
    'initTimer',
    'qrIdleTimer',
    'reconnectTimer',
    'qrBootstrapTimer',
    'preparationTimer',
    'gruposRetryTimer',
];

const beginQrReset = (session, clearTimer = clearTimeout) => {
    for (const field of RESET_TIMER_FIELDS) {
        if (session[field]) clearTimer(session[field]);
        session[field] = null;
    }
    session.encerrandoManual = true;
    session.isConnected = false;
    session.preparando = false;
    session.ultimoQR = null;
    session.progresso = 0;
    session.fase = 'reiniciando_qr';
    session.faseMsg = 'Cancelando sessão e gerando um novo QR…';
};

const markResetFailure = (session, message, motivo = MOTIVO_FALHA_RESET.DESCONHECIDO) => {
    for (const field of ['initTimer', 'qrIdleTimer', 'reconnectTimer', 'qrBootstrapTimer']) {
        if (session[field]) clearTimeout(session[field]);
        session[field] = null;
    }
    session.client = null;
    session.initialized = false;
    session.isConnected = false;
    session.preparando = false;
    session.ultimoQR = null;
    session.progresso = 0;
    session.fase = 'falha_reset';
    session.faseMsg = message;
    session.motivoFalhaReset = motivo;
    session.qrBootstrapAtivo = false;
    // Uma falha ao apagar o auth nunca pode cair de volta na recuperação
    // automática da credencial que o usuário acabou de rejeitar.
    session.encerrandoManual = true;
};

const markQrBootstrap = (session) => {
    session.qrBootstrapAtivo = true;
    session.qrBootstrapAttempts = 1;
    session.qrBootstrapTimer = null;
    session.encerrandoManual = false;
    session.fase = 'reiniciando_qr';
    session.faseMsg = 'Sessão anterior descartada. Gerando um novo QR…';
    session.progresso = 0;
    session.ultimoQR = null;
    return session;
};

const resetSessionForQr = (session, {
    destroyRuntime,
    cleanupProfile = async () => true,
    purgeAuth,
    createState,
    replaceSession,
    initialize,
    hasCapacity = () => true,
    createCapacityState = createState,
    clearTimer = clearTimeout,
}) => {
    if (session.resetPromise) return session.resetPromise;

    beginQrReset(session, clearTimer);

    const execution = (async () => {
        await destroyRuntime(session);

        const runtimeClean = await cleanupProfile(session);
        if (!runtimeClean) {
            const mensagem = 'Não foi possível encerrar o leitor anterior. Tente novamente.';
            markResetFailure(session, mensagem, MOTIVO_FALHA_RESET.CHROMIUM_NAO_ENCERROU);
            return {
                sucesso: false,
                auth_removido: false,
                instancia: session.id,
                mensagem,
                status: session,
            };
        }

        const authRemoved = purgeAuth(session);
        if (!authRemoved) {
            const mensagem = 'Não foi possível descartar a sessão antiga. Tente novamente.';
            markResetFailure(session, mensagem, MOTIVO_FALHA_RESET.PURGE_FALHOU);
            return {
                sucesso: false,
                auth_removido: false,
                instancia: session.id,
                mensagem,
                status: session,
            };
        }

        if (!hasCapacity()) {
            const capacity = createCapacityState(session.id);
            replaceSession(capacity);
            return {
                sucesso: false,
                auth_removido: true,
                instancia: session.id,
                mensagem: capacity.faseMsg,
                status: capacity,
            };
        }

        const fresh = markQrBootstrap(createState(session.id));
        replaceSession(fresh);
        try {
            initialize(fresh);
        } catch (error) {
            const mensagem = 'A sessão antiga foi descartada, mas o novo QR não pôde ser iniciado.';
            markResetFailure(fresh, mensagem, MOTIVO_FALHA_RESET.INIT_FALHOU);
            return {
                sucesso: false,
                auth_removido: true,
                instancia: session.id,
                mensagem,
                status: fresh,
            };
        }
        return {
            sucesso: true,
            auth_removido: true,
            instancia: session.id,
            status: fresh,
        };
    })();

    const shared = execution.finally(() => {
        if (session.resetPromise === shared) session.resetPromise = null;
    });
    session.resetPromise = shared;
    return shared;
};

// Decide o destino de um diretório de sessão achado no volume durante o boot.
// Módulo puro: recebe três booleanos (lidos do disco pelo index.js), devolve a
// ação. Existe para o restart no meio de um "novo QR" não perder a sessão.
//
//   'restaurar' -> credencial pareada intacta: sobe o Chromium e reconecta.
//   'rearmar'   -> um QR novo estava sendo gerado quando o worker caiu. O reset
//                  já apagou o `.paired`, então sem isto o restore ignorava a
//                  pasta e a tela ficava presa em 'inativo', sem QR. Recomeça o
//                  bootstrap de QR do zero.
//   'ignorar'   -> desabilitada pelo usuário (logout), ou lixo sem credencial
//                  nem QR em voo.
//
// `desabilitado` vence tudo: um logout explícito nunca deve ressuscitar sozinho,
// mesmo que um marcador de QR tenha sobrado.
const decidirRestauracao = ({ pareado, desabilitado, qrEmPreparo }) => {
    if (desabilitado) return 'ignorar';
    if (pareado) return 'restaurar';
    if (qrEmPreparo) return 'rearmar';
    return 'ignorar';
};

module.exports = {
    beginQrReset,
    markResetFailure,
    markQrBootstrap,
    resetSessionForQr,
    decidirRestauracao,
    MOTIVO_FALHA_RESET,
};
