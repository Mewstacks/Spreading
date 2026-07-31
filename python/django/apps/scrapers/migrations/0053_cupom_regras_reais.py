"""Campos que o payload do ML já entregava e o scraper descartava.

`desconto_maximo` (teto) e `restrito` (condição de público) no Cupom, e a marca
`automatico` no CupomCodigo, que separa código curado de código descoberto por
regex. O `codigo` também vira único: o scraper já fazia `update_or_create` por ele,
o que estourava MultipleObjectsReturned assim que houvesse duplicata.
"""
from django.db import migrations, models


def marcar_automaticos(apps, schema_editor):
    CupomCodigo = apps.get_model("scrapers", "CupomCodigo")
    CupomCodigo.objects.filter(descricao="cupom ML (checkout)").update(automatico=True)


def desfazer_marcacao(apps, schema_editor):
    CupomCodigo = apps.get_model("scrapers", "CupomCodigo")
    CupomCodigo.objects.update(automatico=False)


def deduplicar_codigos(apps, schema_editor):
    """Mantém uma linha por código antes da constraint. Prioriza o curado à mão."""
    CupomCodigo = apps.get_model("scrapers", "CupomCodigo")
    vistos = {}
    remover = []
    # Curados primeiro: se houver um manual e um automático com o mesmo código, o
    # manual é o que a cliente editou e deve sobreviver.
    for cupom in CupomCodigo.objects.order_by("automatico", "-ativo", "id"):
        chave = (cupom.codigo or "").strip().upper()
        if chave in vistos:
            remover.append(cupom.pk)
        else:
            vistos[chave] = cupom.pk
    if remover:
        CupomCodigo.objects.filter(pk__in=remover).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("scrapers", "0052_backfill_organizations"),
    ]

    operations = [
        migrations.AddField(
            model_name="cupom",
            name="desconto_maximo",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="cupom",
            name="restrito",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="cupomcodigo",
            name="automatico",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.RunPython(marcar_automaticos, desfazer_marcacao),
        migrations.RunPython(deduplicar_codigos, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="cupomcodigo",
            name="codigo",
            field=models.CharField(max_length=60, unique=True),
        ),
    ]
