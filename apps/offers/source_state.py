"""Estado de cada fonte ou checagem do ciclo (`SourceRun`): Fontes, /status e alertas leem."""
import logging

from .models import SourceRun

logger = logging.getLogger(__name__)


def summary(value) -> str:
    if isinstance(value, dict):
        return " · ".join(f"{key} {count}" for key, count in value.items() if count)
    return str(value) if value else ""


def record(label, at, error=None, value=None):
    """Guarda o estado da fonte; nunca derruba o ciclo. Erro conta falha seguida; sucesso zera e
    guarda o último resultado não vazio."""
    try:
        run, _created = SourceRun.objects.get_or_create(label=label, defaults={"checked_at": at})
        run.checked_at, run.ok = at, error is None
        if error is not None:
            run.failures, run.error = run.failures + 1, error[:200]
        else:
            run.failures, run.error = 0, ""
            if summary(value):
                run.result, run.result_at = summary(value)[:200], at
        run.save()
    except Exception as exc:  # noqa: BLE001
        logger.warning("registro da fonte %s falhou: %s", label, exc)
