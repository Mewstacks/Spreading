"""Consolida incidentes que a classificação duplicada partiu em dois.

A migração 0037 duplicou a lógica de classificação em vez de importar
incidentes_saude — decisão certa (importar modelo de produção numa migração puxava
campos de migrações futuras para o SELECT). Mas a cópia ficou defasada: ela não tem
a regra `"getchat"/"módulos internos" -> whatsapp_store_recarregado` que
incidentes_saude.causa_do_evento:20-21 tem.

Consequência: o backfill classificou esses eventos como `publicacao_falhou`, e todo
evento novo (que passa por log_event -> processar_evento) vira
`whatsapp_store_recarregado`. Como a chave é sha256(pipeline|causa|usuario|escopo),
as duas causas geram DOIS incidentes separados para o mesmo problema, cada um com a
própria contagem — e resolver um deixa o outro aberto para sempre. É parte da pilha
de erros da tela de Saúde que ninguém conseguia baixar.

Aqui reclassificamos os incidentes backfilled cujo evento de origem indica store
recarregado, fundindo-os no incidente correto quando ele já existe.
"""
import hashlib

from django.db import migrations


def _texto(evento):
    return " ".join([evento.evento or "", evento.mensagem or "", evento.erro or ""]).lower()


def _chave(pipeline, causa, usuario_id, escopo):
    bruto = f"{pipeline}|{causa}|{usuario_id or 0}|{escopo}".encode()
    return hashlib.sha256(bruto).hexdigest()


def consolidar(apps, schema_editor):
    IncidenteSaude = apps.get_model("scrapers", "IncidenteSaude")

    candidatos = (IncidenteSaude.objects
                  .filter(causa="publicacao_falhou", evento_origem__isnull=False)
                  .select_related("evento_origem"))
    for incidente in candidatos:
        texto = _texto(incidente.evento_origem)
        if "getchat" not in texto and "módulos internos" not in texto:
            continue

        causa = "whatsapp_store_recarregado"
        chave = _chave(incidente.pipeline, causa, incidente.usuario_id, incidente.escopo)
        gemeo = IncidenteSaude.objects.filter(chave=chave).exclude(pk=incidente.pk).first()
        if gemeo is None:
            incidente.causa, incidente.chave = causa, chave
            incidente.save(update_fields=["causa", "chave"])
            continue

        # Já existe o incidente com a causa certa (criado pelos eventos novos): funde
        # as contagens nele e descarta a duplicata, em vez de deixar os dois na tela.
        gemeo.ocorrencias += incidente.ocorrencias
        gemeo.primeira_ocorrencia = min(gemeo.primeira_ocorrencia,
                                        incidente.primeira_ocorrencia)
        if incidente.ultima_ocorrencia > gemeo.ultima_ocorrencia:
            gemeo.ultima_ocorrencia = incidente.ultima_ocorrencia
            gemeo.ultima_mensagem = incidente.ultima_mensagem
        if incidente.level == "error":
            gemeo.level = "error"
        # Aberto vence concluído: se uma das metades segue aberta, o problema segue.
        if incidente.status == "aberto":
            gemeo.status, gemeo.confirmado_em, gemeo.confirmacao = "aberto", None, ""
        gemeo.save()
        incidente.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("scrapers", "0041_linkafiliadousuario_estado_and_more"),
    ]

    operations = [
        # Irreversível de propósito: desfazer recriaria a duplicata que a 0037
        # causou. A migração é idempotente — rodar de novo não acha mais candidatos.
        migrations.RunPython(consolidar, migrations.RunPython.noop),
    ]
