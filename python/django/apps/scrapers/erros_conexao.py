"""Tradução de falhas dos live views de conexão em mensagem acionável.

Os três workers de login (Mercado Livre, portal de afiliados ML e Amazon) tinham o
mesmo `except Exception as exc: ... erro=f"Falha na conexão: {exc}"`. Isso é ruim
das duas pontas: o usuário lia texto interno que não diz o que fazer — o caso real
foi "Falha na conexão: You cannot call this from an async context - use a thread or
sync_to_async" — e o traceback de verdade nunca chegava ao log.

Aqui a exceção vira (mensagem para a tela, código curto para o log). O código liga a
mensagem genérica à linha de log correspondente sem expor nada ao usuário.
"""
import logging
import uuid

logger = logging.getLogger(__name__)


def novo_codigo() -> str:
    """Código curto que aparece na tela e no log da mesma falha."""
    return uuid.uuid4().hex[:8]


def mensagem_de_erro(exc: Exception, codigo: str, *, servico: str = "o serviço") -> str:
    """Traduz a falha em ação para o usuário. O traceback vai só para o log."""
    from django.core.exceptions import SynchronousOnlyOperation
    from django.db import InterfaceError, OperationalError

    from apps.accounts.ml_session_crypto import MLSessionCryptoError

    # RuntimeError aqui é sempre nosso (_ir_para_login levanta um texto já acionável).
    if isinstance(exc, RuntimeError) and str(exc):
        return str(exc)

    if isinstance(exc, SynchronousOnlyOperation):
        # Esta é exatamente a classe de bug que a refatoração da ponte de ORM
        # eliminou. Se voltar, tem de ser barulhento no log — não pode virar de novo
        # uma mensagem enigmática na tela do usuário.
        logger.error(
            "REGRESSÃO: ORM chamado dentro do event loop do Playwright (código %s). "
            "Use apps.accounts.tenant.executar_no_tenant ou mova a query para fora "
            "do bloco `with sync_playwright()`.", codigo,
        )
        return (f"Erro interno ao gravar a sessão (código {codigo}). "
                "A equipe já foi notificada; tente novamente em alguns minutos.")

    if isinstance(exc, MLSessionCryptoError):
        return (f"Não foi possível cifrar a sessão para guardá-la (código {codigo}). "
                "Avise o suporte com esse código.")

    if isinstance(exc, (OperationalError, InterfaceError)):
        return ("Falha temporária no banco ao salvar a sessão. "
                "Tente conectar novamente.")

    nome = type(exc).__name__
    texto = str(exc)
    if nome == "TimeoutError" or "Timeout" in nome:
        return f"{servico} não respondeu a tempo. Tente novamente."
    if "Target closed" in texto or "Browser closed" in texto or "closed" in texto.lower():
        return ("O navegador do servidor encerrou antes de concluir o login. "
                "Tente de novo.")
    if isinstance(exc, ValueError) and "organização" in texto:
        return "Sua conta não tem uma organização ativa. Fale com o suporte."

    return (f"Não foi possível concluir a conexão (código {codigo}). "
            "Tente novamente; se persistir, informe esse código ao suporte.")
