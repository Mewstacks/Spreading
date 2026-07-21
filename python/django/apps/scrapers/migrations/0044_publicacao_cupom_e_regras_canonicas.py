import re

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


CODIGO_HUMANO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{2,39}$")


def _texto(valor):
    return "" if valor is None else str(valor).strip()


def _numero(valor):
    if valor is None or valor == "" or isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    texto = _texto(valor).replace("R$", "").replace("%", "").replace(" ", "")
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    try:
        return float(texto)
    except (TypeError, ValueError):
        match = re.search(r"\d+(?:[.,]\d+)?", texto)
        return float(match.group().replace(",", ".")) if match else None


def normalizar_catalogo(apps, schema_editor):
    CupomNormalizado = apps.get_model("scrapers", "CupomNormalizado")
    for cupom in CupomNormalizado.objects.all().iterator(chunk_size=500):
        raw = cupom.regras if isinstance(cupom.regras, dict) else {}
        codigo = _texto(cupom.codigo)
        evidencia = dict(cupom.evidencia) if isinstance(cupom.evidencia, dict) else {}
        campanha = _texto(cupom.external_id).startswith("campanha:")
        humano = bool(CODIGO_HUMANO.fullmatch(codigo)) and not campanha
        if codigo and not humano:
            evidencia.setdefault("token_ativacao", codigo)
            codigo = ""

        tipo = _texto(raw.get("tipo_desconto")).lower()
        if tipo == "percentual":
            tipo = "porcentagem"
        valor_texto = _texto(raw.get("valor_desconto"))
        if tipo not in {"porcentagem", "fixo"}:
            if "%" in valor_texto or raw.get("discount_num") not in (None, ""):
                tipo = "porcentagem"
            elif "R$" in valor_texto:
                tipo = "fixo"
            else:
                tipo = ""
        valor = _numero(raw.get("discount_num"))
        if valor is None:
            valor = _numero(raw.get("valor_desconto"))
        minimo = _numero(raw.get("valor_minimo"))
        if minimo is None:
            minimo = _numero(raw.get("min_compra"))
        maximo = _numero(raw.get("desconto_maximo"))
        if maximo is None:
            maximo = _numero(raw.get("desconto_max"))
        modo = _texto(raw.get("modo_resgate"))
        if modo not in {"codigo", "ativacao"}:
            modo = "codigo" if humano else "ativacao"

        cupom.codigo = codigo
        cupom.regras = {
            "tipo_desconto": tipo,
            "valor_desconto": valor,
            "valor_minimo": minimo,
            "desconto_maximo": maximo,
            "modo_resgate": modo,
            "escopo": _texto(raw.get("escopo") or raw.get("acao")),
            "container_url": _texto(raw.get("container_url")),
            "container_name": _texto(raw.get("container_name")),
            "is_mar_aberto": bool(raw.get("is_mar_aberto")),
            "dia_inicio": _texto(raw.get("dia_inicio")),
            "dia_fim": _texto(raw.get("dia_fim")),
        }
        cupom.evidencia = evidencia
        cupom.save(update_fields=["codigo", "regras", "evidencia"])


class Migration(migrations.Migration):
    dependencies = [
        ("scrapers", "0043_invalida_snapshots_legados"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="publicacao",
            name="origem",
            field=models.CharField(db_index=True, default="produto", max_length=30),
        ),
        migrations.AddField(
            model_name="publicacao",
            name="cupom_normalizado",
            field=models.ForeignKey(blank=True, null=True,
                                    on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="publicacoes",
                                    to="scrapers.cupomnormalizado"),
        ),
        migrations.CreateModel(
            name="LinkAfiliadoCupomUsuario",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("url_origem", models.URLField(max_length=1000)),
                ("link_afiliado", models.URLField(max_length=1500)),
                ("afiliado_ok", models.BooleanField(default=False)),
                ("atualizado_em", models.DateTimeField(auto_now=True)),
                ("cupom", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                             related_name="links_usuarios",
                                             to="scrapers.cupomnormalizado")),
                ("usuario", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                               related_name="links_cupons",
                                               to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddConstraint(
            model_name="linkafiliadocupomusuario",
            constraint=models.UniqueConstraint(fields=("usuario", "cupom"),
                                               name="uniq_link_cupom_usuario"),
        ),
        migrations.RunPython(normalizar_catalogo, migrations.RunPython.noop),
    ]
