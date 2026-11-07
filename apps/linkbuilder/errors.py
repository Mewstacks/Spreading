"""Por que o Link Builder não entregou um link. Cada classe pede uma ação diferente."""


class BuilderError(Exception):
    pass


class LoginRequired(BuilderError):
    """O portal mostrou a tela de login: a sessão salva não vale mais."""


class NoSession(LoginRequired):
    """Não há sessão salva (rode tools/ml_login.py). Nada foi aberto."""


class Blocked(BuilderError):
    """O ML interpôs verificação/anti-bot. A conta está boa: esperar, não reconectar."""


class Unavailable(BuilderError):
    """Portal fora do ar ou controles ausentes. Tentar de novo mais tarde."""


class Rejected(BuilderError):
    """O Programa recusou esta URL/produto. Insistir não muda o resultado."""
