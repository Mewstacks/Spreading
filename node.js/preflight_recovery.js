'use strict';

// Operações ANTES de sendMessage. Se o Chromium não responde aqui, nenhuma
// mensagem foi criada e é seguro reciclar a sessão para a próxima tentativa.
const ETAPAS = new Set(['getState', 'verificar_grupo']);

const mensagemPreflight = (etapa) => (
    etapa === 'verificar_grupo'
        ? 'O WhatsApp não respondeu ao validar o grupo. A sessão será recuperada automaticamente; aguarde alguns segundos e tente novamente.'
        : 'O WhatsApp não respondeu ao testar a conexão. A sessão será recuperada automaticamente; aguarde alguns segundos e tente novamente.'
);

const timeoutPreflight = (etapa, erro) => {
    if (!ETAPAS.has(etapa)) return false;
    try {
        return /\btimeout\b/i.test(String(erro && erro.message || erro || ''));
    } catch (_) {
        return false;
    }
};

// Coalesce erros simultâneos do mesmo Chromium: o diagnóstico e um envio podem
// perceber o travamento quase juntos, mas só um recycle é necessário.
const iniciarRecuperacaoPreflight = (session, etapa, recycle, agendar = setTimeout) => {
    session.isConnected = false;
    session.fase = 'reconectando';
    session.faseMsg = 'WhatsApp não respondeu; recuperando sessão…';
    if (session._preflightRecoveryPending) return false;
    session._preflightRecoveryPending = true;
    const timer = agendar(() => {
        Promise.resolve(recycle(session, `timeout em ${etapa} antes do envio`))
            .catch(() => undefined)
            .finally(() => { session._preflightRecoveryPending = false; });
    }, 0);
    if (timer && typeof timer.unref === 'function') timer.unref();
    return true;
};

module.exports = { timeoutPreflight, mensagemPreflight, iniciarRecuperacaoPreflight };
