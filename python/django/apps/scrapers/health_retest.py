"""Reteste global de saúde, restrito ao processo com papel de sistema."""

import logging

from apps.accounts.tenant import system_job


logger = logging.getLogger(__name__)


@system_job
def retest_incident_group(incident_id: int) -> dict:
    """Retesta um grupo cross-tenant fora do processo web.

    A role runtime não pode enxergar ou alterar tenants diferentes. Este serviço
    é deliberadamente acessível apenas por um management command usando a role
    de sistema e deixa uma linha estruturada no log operacional.
    """
    from apps.scrapers.incidentes_saude import confirmar
    from apps.scrapers.models import IncidenteSaude
    from apps.scrapers.views_admin import _retestar_incidente

    base = IncidenteSaude.objects.select_related("usuario").get(pk=incident_id)
    group = list(
        IncidenteSaude.objects.select_related("usuario").filter(
            pipeline=base.pipeline,
            causa=base.causa,
            escopo=base.escopo,
            status="aberto",
        )
    )
    if not group:
        group = [base]

    completed = failed = 0
    last_message = ""
    for incident in group:
        try:
            result = _retestar_incidente(incident)
        except Exception as exc:
            logger.warning(
                "phase0_health_retest_probe_failed incident_id=%s error_type=%s",
                incident.pk,
                type(exc).__name__,
            )
            result = {
                "sucesso": False,
                "mensagem": "O reteste encontrou uma falha interna.",
            }
        last_message = result.get("mensagem") or last_message
        if result.get("sucesso"):
            confirmar(incident, result["mensagem"])
            completed += 1
        else:
            failed += 1

    logger.info(
        "phase0_health_retest incident_id=%s group_size=%s completed=%s failed=%s",
        incident_id,
        len(group),
        completed,
        failed,
    )
    return {
        "completed": completed,
        "failed": failed,
        "message": last_message,
    }
