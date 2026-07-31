"""Neutraliza os dados de cupom gravados pelos parsers antigos.

Roda no deploy (entrypoint.sh aplica as migrações), então não depende de ninguém
abrir um terminal. Três defeitos deixaram lastro no banco:

1. compra mínima com separador de milhar lida como unidade (R$ 2.000 -> R$ 2,00);
2. teto de desconto (`capAmount`) nunca gravado -> preço final anunciado menor
   que o real;
3. produtos de `origem='cupom'` gravados com o preço JÁ descontado, o que fazia o
   cupom ser aplicado duas vezes.

Nada disso é recuperável a partir do que está gravado: os valores certos só voltam
relendo a página do ML. Então aqui a gente NÃO tenta adivinhar — desliga o que é
comprovadamente suspeito, e o primeiro scrape bem-sucedido repõe a verdade
(`mapear_cupons` reescreve estado, validade, compra mínima e teto).
"""
from django.db import migrations
from django.utils import timezone


def neutralizar(apps, schema_editor):
    Cupom = apps.get_model("scrapers", "Cupom")
    CupomNormalizado = apps.get_model("scrapers", "CupomNormalizado")
    CupomPreparacao = apps.get_model("scrapers", "CupomPreparacao")
    Produto = apps.get_model("scrapers", "Produto")
    ProdutoCupom = apps.get_model("scrapers", "ProdutoCupom")
    agora = timezone.now()

    # Campanhas: compra mínima errada e sem teto. Ficam inativas até o próximo
    # scrape confirmar status/validade/valores direto da fonte.
    Cupom.objects.filter(estado="ativo").update(estado="inativo",
                                                ultima_verificacao=agora)
    CupomNormalizado.objects.filter(
        external_id__startswith="campanha:", estado="ativo",
    ).update(estado="inativo")

    # Produtos da lane de cupom: preço na semântica antiga. Stale tira do envio;
    # eles voltam com o contrato novo (tabela | vitrine) quando o cupom for
    # preparado — coupon_products._coletar_ml_remoto regrava e reativa a linha.
    Produto.objects.filter(
        marketplace="mercadolivre", owner__isnull=True, origem="cupom",
    ).exclude(estado="stale").update(
        estado="stale",
        falha_verificacao="Preço regravado: semântica de cupom corrigida",
        ultima_verificacao=agora)

    # Preços por cupom e caches de preparo saíram do cálculo sem teto e com
    # mínimo errado: expirar força o recálculo em Decimal.
    ProdutoCupom.objects.filter(status="confirmado").update(status="expirado")
    CupomPreparacao.objects.exclude(produtos_chave="").update(
        produtos_chave="", status="vazio", erro="Invalidado: regras de cupom corrigidas")


class Migration(migrations.Migration):

    dependencies = [
        ("scrapers", "0053_cupom_regras_reais"),
    ]

    # Sem reverso: os valores antigos estavam errados, restaurá-los seria voltar a
    # anunciar preço que não bate no checkout.
    operations = [migrations.RunPython(neutralizar, migrations.RunPython.noop)]
