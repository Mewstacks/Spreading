"""O portão que impede a mensagem de um estranho de sair com a assinatura do usuário.

Estes testes existem por um motivo de produto, não de código: quem opera o Spreading
são influenciadores, e uma oferta esgotada ou um desconto inventado chegam ao grupo
com a cara deles. Cada caso abaixo é uma forma de isso acontecer.
"""
from unittest.mock import patch

from django.test import TestCase

from apps.scrapers.canais import validacao
from apps.scrapers.canais.seeds import CANAIS_SUGERIDOS, RECUSADOS, sugestoes_para

ML = "https://produto.mercadolivre.com.br/MLB-123456789-fone"
ML_AF = "https://mercadolivre.com/sec/abc123"
AMZ = "https://www.amazon.com.br/dp/B0ABCDEFGH"
AMZ_AF = "https://www.amazon.com.br/dp/B0ABCDEFGH?tag=meutag-20"


class VeredictoDeLinkTests(TestCase):
    def test_loja_desconhecida_nao_passa(self):
        """Loja que não sabemos conferir não pode escapar por omissão."""
        veredito, motivo = validacao.verificar_link(
            "https://loja-qualquer.com/produto", url_origem="https://loja-qualquer.com/p",
        )
        self.assertEqual(veredito, validacao.REPROVADO)
        self.assertIn("não reconhecida", motivo)

    def test_destino_aprovado_libera(self):
        with patch("apps.scrapers.marketplaces.registry.get_marketplace") as loja:
            loja.return_value.verify_link.return_value = {"ok": True}
            veredito, _ = validacao.verificar_link(ML_AF, url_origem=ML)
        self.assertEqual(veredito, validacao.APROVADO)

    def test_destino_reprovado_bloqueia_com_motivo(self):
        with patch("apps.scrapers.marketplaces.registry.get_marketplace") as loja:
            loja.return_value.verify_link.return_value = {
                "ok": False, "motivo": "Anúncio pausado",
            }
            veredito, motivo = validacao.verificar_link(ML_AF, url_origem=ML)
        self.assertEqual(veredito, validacao.REPROVADO)
        self.assertEqual(motivo, "Anúncio pausado")

    def test_falha_de_transporte_e_incerto_nao_reprovacao(self):
        """Anti-bot e timeout não podem queimar oferta boa em lote."""
        with patch("apps.scrapers.marketplaces.registry.get_marketplace") as loja:
            loja.return_value.verify_link.side_effect = TimeoutError("sem resposta")
            veredito, _ = validacao.verificar_link(ML_AF, url_origem=ML)
        self.assertEqual(veredito, validacao.INCERTO)

    def test_veredito_marcado_transitorio_e_incerto(self):
        with patch("apps.scrapers.marketplaces.registry.get_marketplace") as loja:
            loja.return_value.verify_link.return_value = {
                "ok": False, "transitorio": True, "motivo": "challenge",
            }
            veredito, _ = validacao.verificar_link(ML_AF, url_origem=ML)
        self.assertEqual(veredito, validacao.INCERTO)


class MensagemLiberadaTests(TestCase):
    def test_mensagem_sem_link_nao_sai(self):
        liberada, veredito, _ = validacao.mensagem_liberada([])
        self.assertFalse(liberada)
        self.assertEqual(veredito, validacao.REPROVADO)

    def test_todos_aprovados_libera(self):
        with patch("apps.scrapers.marketplaces.registry.get_marketplace") as loja:
            loja.return_value.verify_link.return_value = {"ok": True}
            liberada, veredito, _ = validacao.mensagem_liberada(
                [(ML, ML_AF), (AMZ, AMZ_AF)],
            )
        self.assertTrue(liberada)
        self.assertEqual(veredito, validacao.APROVADO)

    def test_um_link_reprovado_derruba_a_mensagem_inteira(self):
        """Não se reescreve o texto de terceiro para tirar só o item ruim.

        O resultado seria uma mensagem que ninguém escreveu, com preço solto sem o
        produto correspondente — pior para quem lê do que não receber nada.
        """
        vereditos = [{"ok": True}, {"ok": False, "motivo": "Produto indisponível"}]
        with patch("apps.scrapers.marketplaces.registry.get_marketplace") as loja:
            loja.return_value.verify_link.side_effect = vereditos
            liberada, veredito, motivo = validacao.mensagem_liberada(
                [(ML, ML_AF), (AMZ, AMZ_AF)],
            )
        self.assertFalse(liberada)
        self.assertEqual(veredito, validacao.REPROVADO)
        self.assertEqual(motivo, "Produto indisponível")

    def test_incerto_segura_sem_reprovar(self):
        with patch("apps.scrapers.marketplaces.registry.get_marketplace") as loja:
            loja.return_value.verify_link.side_effect = [
                {"ok": True}, {"ok": False, "incerto": True, "motivo": "sem resposta"},
            ]
            liberada, veredito, _ = validacao.mensagem_liberada(
                [(ML, ML_AF), (AMZ, AMZ_AF)],
            )
        self.assertFalse(liberada)
        self.assertEqual(veredito, validacao.INCERTO)

    def test_reprovacao_encerra_a_checagem(self):
        """Reprovado é definitivo: não gasta verificação no resto da mensagem."""
        with patch("apps.scrapers.marketplaces.registry.get_marketplace") as loja:
            loja.return_value.verify_link.side_effect = [
                {"ok": False, "motivo": "Anúncio pausado"}, {"ok": True},
            ]
            validacao.mensagem_liberada([(ML, ML_AF), (AMZ, AMZ_AF)])
            self.assertEqual(loja.return_value.verify_link.call_count, 1)


class SementeDeCanaisTests(TestCase):
    def test_sugestoes_ordenadas_por_densidade(self):
        densidades = [c["densidade"] for c in sugestoes_para()]
        self.assertEqual(densidades, sorted(densidades, reverse=True))

    def test_filtro_por_marketplace(self):
        for canal in sugestoes_para("shopee"):
            self.assertIn("shopee", canal["marketplaces"])

    def test_nenhum_canal_recusado_entra_na_lista(self):
        """Handles que imitam marca conhecida não podem voltar por descuido."""
        sugeridos = {c["handle"].lower() for c in CANAIS_SUGERIDOS}
        self.assertEqual(sugeridos & set(RECUSADOS), set())

    def test_todo_canal_declara_loja_e_motivo(self):
        for canal in CANAIS_SUGERIDOS:
            self.assertTrue(canal["marketplaces"], canal["handle"])
            self.assertTrue(canal["nota"].strip(), canal["handle"])


class RelinkDetalhadoTests(TestCase):
    def test_pares_acompanham_as_chaves(self):
        from apps.scrapers.canais import relink

        texto = f"Oferta boa {ML} corre"
        with patch.object(relink, "gerar_link_afiliado", return_value=ML_AF):
            novo, chaves, pares = relink.reescrever_mensagem_detalhada(texto, None)
        self.assertIn(ML_AF, novo)
        self.assertEqual(len(chaves), 1)
        self.assertEqual(pares, [(ML, ML_AF)])

    def test_assinatura_antiga_continua_valendo(self):
        """`reescrever_mensagem` tem chamadores antigos; não pode mudar de forma."""
        from apps.scrapers.canais import relink

        with patch.object(relink, "gerar_link_afiliado", return_value=ML_AF):
            resultado = relink.reescrever_mensagem(f"veja {ML}", None)
        self.assertEqual(len(resultado), 2)
