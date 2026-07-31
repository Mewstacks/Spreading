import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar


logger = logging.getLogger(__name__)
_reporter = ContextVar("scraper_progress_reporter", default=None)
_PCT_RE = re.compile(r"\((\d{1,3})%\)")


@contextmanager
def usar_reporter(reporter):
    """Direciona progresso para o job atual sem trocar ``sys.stdout`` global."""
    token = _reporter.set(reporter)
    try:
        yield
    finally:
        _reporter.reset(token)


def emitir_progresso(mensagem: str) -> None:
    """Emite progresso estruturado; fora de job, usa o logger do worker."""
    reporter = _reporter.get()
    if reporter is not None:
        match = _PCT_RE.search(mensagem or "")
        reporter(
            mensagem,
            progresso=min(100, int(match.group(1))) if match else None,
        )
        return
    logger.info("%s", mensagem)


def emitir_fase(rotulo: str, fracao: float = 0.0, faixa=None) -> None:
    """Emite uma linha de progresso com o % já mapeado na faixa da etapa.

    Um pipeline com várias etapas (cupons: campanhas → códigos → projeção → links)
    não tem um contador único; cada etapa recebe uma FAIXA do total (ex.: 0–45%) e
    reporta `fracao` (0..1) do próprio trabalho. Sem isso, a barra ou voltava a zero
    a cada etapa ou (o que acontecia) nunca aparecia.

    Sem `faixa` a linha sai só com o rótulo — a UI usa como legenda e mantém a barra
    indeterminada, que é o certo para etapa de duração desconhecida.
    """
    if not faixa:
        emitir_progresso(f"[PROGRESSO] {rotulo}")
        return
    ini, fim = faixa
    pct = int(ini + (fim - ini) * max(0.0, min(1.0, fracao)))
    emitir_progresso(f"[PROGRESSO] {rotulo} ({pct}%)")
