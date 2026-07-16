"""Classificação, ciclo de vida e reteste seguro dos incidentes de Saúde."""
from __future__ import annotations

import hashlib
from django.utils import timezone


def _texto(evento) -> str:
    return " ".join([evento.evento or "", evento.mensagem or "", evento.erro or ""]).lower()


def causa_do_evento(evento) -> str:
    contexto = evento.contexto or {}
    if contexto.get("causa"):
        return str(contexto["causa"])[:80]
    texto = _texto(evento)
    if evento.evento == "send_failed":
        if "getstate timeout" in texto:
            return "whatsapp_preflight_timeout"
        if "detached frame" in texto or "recarregando" in texto:
            return "whatsapp_frame_recarregado"
        if "confirma" in texto or "ack" in texto:
            return "whatsapp_confirmacao"
        if "link de afiliado" in texto or "link builder" in texto:
            return "link_afiliado_recusado"
        if texto.strip().endswith(" r") or "\nr" in texto:
            return "whatsapp_erro_minificado"
        return "publicacao_falhou"
    if evento.evento == "send_timeout":
        return "whatsapp_timeout_entrega"
    return evento.evento


def escopo_do_evento(evento) -> str:
    c = evento.contexto or {}
    if evento.evento.startswith("send_") or evento.pipeline in {"whatsapp", "telegram"}:
        canal = c.get("canal") or ("whatsapp" if evento.pipeline == "whatsapp" else "")
        destino = c.get("destino") or c.get("grupo_id") or "destino desconhecido"
        return f"{canal}:{destino}"[:255]
    for campo in ("marketplace", "fonte", "servico", "config_id", "view"):
        if c.get(campo):
            return f"{campo}:{c[campo]}"[:255]
    return "sistema"


def _chave(evento, causa: str, escopo: str) -> str:
    bruto = f"{evento.pipeline}|{causa}|{evento.usuario_id or 0}|{escopo}".encode()
    return hashlib.sha256(bruto).hexdigest()


def _concluir_envios(evento):
    from apps.scrapers.models import IncidenteSaude
    escopo = escopo_do_evento(evento)
    IncidenteSaude.objects.filter(
        status="aberto", usuario_id=evento.usuario_id, escopo=escopo,
        pipeline__in=("publicacao", "whatsapp", "telegram"),
    ).update(status="concluido", confirmado_em=evento.criado_em,
             confirmacao="Envio real posterior concluído com sucesso.")


def _concluir_relatorio(evento):
    from apps.scrapers.models import IncidenteSaude
    IncidenteSaude.objects.filter(
        status="aberto", usuario_id=evento.usuario_id, pipeline="relatorios",
        escopo=escopo_do_evento(evento),
    ).update(status="concluido", confirmado_em=evento.criado_em,
             confirmacao="Sincronização posterior concluída com sucesso.")


def processar_evento(evento):
    """Atualiza a projeção de incidentes a partir de um EventoOperacional."""
    if evento.evento == "send_ok":
        _concluir_envios(evento)
        return None
    if evento.evento == "sync_ok":
        _concluir_relatorio(evento)
        return None
    if evento.evento == "conexao_voltou":
        from apps.scrapers.models import IncidenteSaude
        IncidenteSaude.objects.filter(status="aberto", usuario_id=evento.usuario_id,
            pipeline="conexao", escopo=escopo_do_evento(evento)).update(
                status="concluido", confirmado_em=evento.criado_em,
                confirmacao="Conexão posterior restabelecida.")
        return None
    if evento.level not in {"warning", "error"}:
        return None
    from apps.scrapers.models import IncidenteSaude
    causa, escopo = causa_do_evento(evento), escopo_do_evento(evento)
    chave = _chave(evento, causa, escopo)
    incidente, criado = IncidenteSaude.objects.get_or_create(
        chave=chave,
        defaults={"causa": causa, "pipeline": evento.pipeline, "escopo": escopo,
                  "usuario_id": evento.usuario_id, "level": evento.level,
                  "primeira_ocorrencia": evento.criado_em, "ultima_ocorrencia": evento.criado_em,
                  "ultima_mensagem": evento.mensagem, "contexto": evento.contexto or {},
                  "evento_origem": evento},
    )
    if not criado:
        incidente.ocorrencias += 1
        incidente.ultima_ocorrencia = evento.criado_em
        incidente.ultima_mensagem = evento.mensagem
        incidente.contexto = evento.contexto or {}
        incidente.evento_origem = evento
        incidente.level = "error" if evento.level == "error" else incidente.level
        incidente.status = "aberto"
        incidente.confirmado_em = None
        incidente.confirmacao = ""
        incidente.save(update_fields=["ocorrencias", "ultima_ocorrencia", "ultima_mensagem", "contexto",
            "evento_origem", "level", "status", "confirmado_em", "confirmacao"])
    return incidente


def reconciliar_eventos(queryset):
    """Compatibilidade para logs gravados antes da projeção de incidentes."""
    for evento in queryset.order_by("criado_em"):
        processar_evento(evento)


def confirmar(incidente, mensagem: str):
    incidente.status = "concluido"
    incidente.confirmado_em = timezone.now()
    incidente.confirmacao = mensagem[:255]
    incidente.save(update_fields=["status", "confirmado_em", "confirmacao"])
    return incidente
