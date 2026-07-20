'use strict';

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

const markResetFailure = (session, message) => {
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
            markResetFailure(session, mensagem);
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
            markResetFailure(session, mensagem);
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
            markResetFailure(fresh, mensagem);
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

module.exports = {
    beginQrReset,
    markResetFailure,
    markQrBootstrap,
    resetSessionForQr,
};
