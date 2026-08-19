"""A regra do cupom viaja junto do código, no aviso em lote.

Um aviso lista vários cupons de uma vez e cada um tem o seu limite: "Tecnologia",
"entregas Full", "Lojas Oficiais", "todo site". Sem essa linha, quem lê tenta o
cupom de Tecnologia numa camiseta, não funciona, e a culpa cai em quem publicou —
não no canal, não na loja: no influenciador que assinou a mensagem.

A mensagem de cupom ÚNICO já trazia "Válido para:". O aviso em lote mostrava só
desconto e código.
"""
from django.test import TestCase

from apps.scrapers.models import CupomNormalizado, FonteIngestao
from apps.scrapers.ofertas import _escopo_curto, montar_mensagem_aviso_cupons


class EscopoCurtoTests(TestCase):
    def test_texto_curto_passa_inteiro(self):
        self.assertEqual(_escopo_curto("Tecnologia"), "Tecnologia")

    def test_espacos_sao_normalizados(self):
        self.assertEqual(_escopo_curto("  entregas   Full \n"), "entregas Full")

    def test_texto_longo_corta_na_palavra_e_avisa(self):
        """Cortar no meio da palavra inventa condição que não existe."""
        longo = ("Válido em produtos das Lojas Oficiais participantes exceto "
                 "eletrodomésticos e itens de mercado")
        curto = _escopo_curto(longo, limite=40)
        self.assertLessEqual(len(curto), 42)
        self.assertTrue(curto.endswith("…"))
        self.assertNotIn("  ", curto)
        # Não corta no meio de uma palavra.
        self.assertTrue(longo.startswith(curto[:-1].rstrip("…")))


class AvisoComRegraTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.fonte = FonteIngestao.objects.create(
            slug="ml-cupons-afiliados", marketplace="mercadolivre",
            nome="ML cupons", status="ok",
        )

    def _cupom(self, codigo, valor, escopo="", minimo=0):
        return CupomNormalizado.objects.create(
            fonte=self.fonte, external_id=f"ext-{codigo}", marketplace="mercadolivre",
            titulo=f"Cupom {codigo}", codigo=codigo, estado="ativo",
            redemption_mode="code", categoria=escopo,
            regras={
                "tipo_desconto": "porcentagem", "valor_desconto": valor,
                "valor_minimo": minimo, "modo_resgate": "codigo", "escopo": escopo,
            },
        )

    def test_escopo_aparece_junto_do_codigo(self):
        cupons = [
            self._cupom("PROMOCERTAML", 10, escopo="Tecnologia"),
            self._cupom("FULLMIX", 10, escopo="entregas Full"),
        ]
        texto = montar_mensagem_aviso_cupons(cupons, "mercadolivre", link="https://x")
        self.assertIn("PROMOCERTAML", texto)
        self.assertIn("Tecnologia", texto)
        self.assertIn("FULLMIX", texto)
        self.assertIn("entregas Full", texto)

    def test_cada_cupom_leva_a_propria_regra(self):
        """Duas regras diferentes não podem se misturar na mesma lista."""
        cupons = [
            self._cupom("TECH10", 10, escopo="Tecnologia"),
            self._cupom("MODA20", 20, escopo="Moda"),
        ]
        texto = montar_mensagem_aviso_cupons(cupons, "mercadolivre", link="https://x")
        bloco_tech = texto.split("TECH10")[0]
        self.assertIn("Tecnologia", bloco_tech)
        self.assertNotIn("Moda", bloco_tech)

    def test_cupom_sem_escopo_nao_ganha_linha_vazia(self):
        texto = montar_mensagem_aviso_cupons(
            [self._cupom("TODOOSITE", 10)], "mercadolivre", link="https://x",
        )
        self.assertIn("TODOOSITE", texto)
        self.assertNotIn("🏷️", texto)

    def test_minimo_continua_aparecendo(self):
        texto = montar_mensagem_aviso_cupons(
            [self._cupom("CASA1508", 50, minimo=399)], "mercadolivre", link="https://x",
        )
        self.assertIn("399", texto)
