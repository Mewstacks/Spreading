"""Persistência das regras que determinam se um cupom pode ser publicado."""

from django.db import migrations, models


DESCRICAO_AUTOMATICA = "cupom ML (checkout)"


def preparar_dados(apps, schema_editor):
    Cupom = apps.get_model("scrapers", "Cupom")
    CupomCodigo = apps.get_model("scrapers", "CupomCodigo")

    # Os registros legados não carregam teto/status confiáveis. Falhar fechado
    # evita anunciar uma campanha antiga até a próxima coleta oficial revalidá-la.
    Cupom.objects.filter(estado="ativo").update(estado="inativo")
    CupomCodigo.objects.filter(descricao=DESCRICAO_AUTOMATICA).update(
        automatico=True,
    )

    # update_or_create(codigo=...) exige uma linha por código. Preserva primeiro o
    # cupom curado manualmente e, entre equivalentes, o ativo mais antigo.
    vistos = set()
    remover = []
    for cupom in CupomCodigo.objects.order_by("automatico", "-ativo", "id"):
        chave = (cupom.codigo or "").strip().casefold()
        if chave in vistos:
            remover.append(cupom.pk)
        else:
            vistos.add(chave)
    if remover:
        CupomCodigo.objects.filter(pk__in=remover).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("scrapers", "0054_backfill_preco_cupom_duplicado"),
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
        migrations.RunPython(preparar_dados, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="cupomcodigo",
            name="codigo",
            field=models.CharField(max_length=60, unique=True),
        ),
    ]
