from datetime import datetime

from django.test import SimpleTestCase
from django.utils import timezone

from apps.scrapers.sources.ml_official_promotions import extrair_cupons_promocoes


HTML = """
<h3>Cupom ATIVO20</h3><p>Cupom válido de 26/08/26 a 28/08/26.
Desconto de até 20% em compra a partir de R$ 199, com desconto máximo de R$ 80.
Cupom pessoal e intransferível.</p>
<h3>Cupom FIXO50</h3><p>Cupom disponível apenas em 27/08/26.
Desconto de até R$50 em compra a partir de R$399.</p>
<h3>Cupom VELHO10</h3><p>Cupom válido de 02/04/26 até às 23h59.
Desconto de até 10% em compra a partir de R$99.</p>
"""


class MLOfficialPromotionsParserTests(SimpleTestCase):
    def test_aceita_ativos_e_descarta_expirado(self):
        agora = timezone.make_aware(datetime(2026, 8, 27, 12, 0))
        rows, metrics = extrair_cupons_promocoes(HTML, agora=agora)
        self.assertEqual([row["codigo"] for row in rows], ["ATIVO20", "FIXO50"])
        self.assertEqual(metrics, {"sections_seen": 3, "accepted": 2})

    def test_extrai_regras_financeiras(self):
        agora = timezone.make_aware(datetime(2026, 8, 27, 12, 0))
        rows, _ = extrair_cupons_promocoes(HTML, agora=agora)
        percentual, fixo = rows
        self.assertEqual(
            (percentual["tipo"], percentual["valor"], percentual["minimo"],
             percentual["maximo"]),
            ("porcentagem", 20.0, 199.0, 80.0),
        )
        self.assertEqual((fixo["tipo"], fixo["valor"]), ("fixo", 50.0))

    def test_schema_ausente_fica_observavel(self):
        rows, metrics = extrair_cupons_promocoes("<html>mudou tudo</html>")
        self.assertEqual(rows, [])
        self.assertEqual(metrics["sections_seen"], 0)
