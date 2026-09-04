from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings


def _resposta(texto):
    return SimpleNamespace(content=[
        SimpleNamespace(type="text", text=texto),
    ])


@override_settings(
    LLM_ATIVO=True,
    ANTHROPIC_API_KEY="chave-de-teste",
    LLM_MODELO="modelo-de-teste",
)
class LLMContentTests(SimpleTestCase):
    @patch("apps.scrapers.llm._cliente")
    def test_gera_titulo_e_nome_curto_sem_markdown(self, cliente):
        from apps.scrapers.llm import gerar_conteudo

        messages = Mock()
        messages.create.return_value = _resposta(
            '```json\n{"titulo":"*tela braba pra jogar bonito*",'
            '"nome_curto":"Monitor Gamer Samsung Odyssey G5 27 QHD 165Hz"}\n```'
        )
        cliente.return_value = SimpleNamespace(messages=messages)

        resultado = gerar_conteudo(
            "Monitor Gamer Samsung Odyssey G5 27 com muitas especificações"
        )

        self.assertEqual(resultado["titulo"], "TELA BRABA PRA JOGAR BONITO")
        self.assertEqual(
            resultado["nome_curto"],
            "Monitor Gamer Samsung Odyssey G5 27 QHD 165Hz",
        )

    @patch("apps.scrapers.llm._cliente")
    def test_rejeita_chamada_de_influencer(self, cliente):
        from apps.scrapers.llm import gerar_conteudo

        messages = Mock()
        messages.create.return_value = _resposta(
            '{"titulo":"IMPERDÍVEL BORA CORRE","nome_curto":"Monitor"}'
        )
        cliente.return_value = SimpleNamespace(messages=messages)
        resultado = gerar_conteudo("Monitor Gamer")
        self.assertEqual(resultado["titulo"], "")
        self.assertEqual(resultado["nome_curto"], "Monitor")
        self.assertEqual(messages.create.call_args.kwargs["model"], "modelo-de-teste")
        self.assertEqual(
            messages.create.call_args.kwargs["thinking"],
            {"type": "disabled"},
        )

    @patch("apps.scrapers.llm._cliente")
    def test_lote_preserva_ordem_e_remove_formatacao(self, cliente):
        from apps.scrapers.llm import gerar_nomes_curtos

        messages = Mock()
        messages.create.return_value = _resposta(
            '["*Monitor Samsung Odyssey G5 27 QHD*", '
            '"Cadeira Gamer Healer Wells Preta"]'
        )
        cliente.return_value = SimpleNamespace(messages=messages)

        resultado = gerar_nomes_curtos(["Monitor muito longo", "Cadeira muito longa"])

        self.assertEqual(resultado, [
            "Monitor Samsung Odyssey G5 27 QHD",
            "Cadeira Gamer Healer Wells Preta",
        ])
        self.assertEqual(messages.create.call_args.kwargs["model"], "modelo-de-teste")
        self.assertEqual(
            messages.create.call_args.kwargs["thinking"],
            {"type": "disabled"},
        )


@override_settings(
    LLM_ATIVO=True,
    ANTHROPIC_API_KEY="chave-de-teste",
    LLM_MODELO="modelo-de-teste",
)
class AvaliacaoDeCupomIATests(SimpleTestCase):
    """Segunda opinião sobre um cupom que já passou pelo piso monetário fixo."""

    @patch("apps.scrapers.llm._cliente")
    def test_ia_rejeita_e_devolve_motivo(self, cliente):
        from apps.scrapers.llm import avaliar_cupom_ia

        messages = Mock()
        messages.create.return_value = _resposta(
            '{"vale_a_pena": false, "motivo": "Condição confusa, parece isca",'
            '"escopo_legivel": "produtos selecionados"}'
        )
        cliente.return_value = SimpleNamespace(messages=messages)

        resultado = avaliar_cupom_ia(
            escopo="produtos selecionados", tipo_desconto="porcentagem",
            valor_desconto=80, desconto_maximo=15,
        )

        self.assertFalse(resultado["vale_a_pena"])
        self.assertEqual(resultado["motivo"], "Condição confusa, parece isca")

    @patch("apps.scrapers.llm._cliente")
    def test_ia_aprova_e_humaniza_escopo(self, cliente):
        from apps.scrapers.llm import avaliar_cupom_ia

        messages = Mock()
        messages.create.return_value = _resposta(
            '{"vale_a_pena": true, "motivo": "",'
            '"escopo_legivel": "loja Glamour"}'
        )
        cliente.return_value = SimpleNamespace(messages=messages)

        resultado = avaliar_cupom_ia(
            escopo="produtos de Glamour.div", tipo_desconto="porcentagem",
            valor_desconto=30, desconto_maximo=80,
        )

        self.assertTrue(resultado["vale_a_pena"])
        self.assertEqual(resultado["escopo_legivel"], "loja Glamour")

    def test_llm_desligada_falha_aberta_sem_chamar_api(self):
        """Fail-open: nunca bloqueia sozinha quando a IA está fora."""
        from apps.scrapers.llm import avaliar_cupom_ia

        with override_settings(LLM_ATIVO=False):
            resultado = avaliar_cupom_ia(escopo="produtos de Glamour.div")

        self.assertTrue(resultado["vale_a_pena"])
        self.assertEqual(resultado["escopo_legivel"], "produtos de Glamour.div")

    @patch("apps.scrapers.llm._cliente")
    def test_falha_da_api_falha_aberta(self, cliente):
        """Erro de rede/timeout nunca vira rejeição — degrada para aprovar."""
        from apps.scrapers.llm import avaliar_cupom_ia

        cliente.side_effect = RuntimeError("timeout")

        resultado = avaliar_cupom_ia(escopo="loja X", tipo_desconto="porcentagem")

        self.assertTrue(resultado["vale_a_pena"])
        self.assertEqual(resultado["escopo_legivel"], "loja X")

    @patch("apps.scrapers.llm._cliente")
    def test_resposta_sem_booleano_falha_aberta(self, cliente):
        """JSON malformado (sem vale_a_pena booleano) não pode virar bloqueio."""
        from apps.scrapers.llm import avaliar_cupom_ia

        messages = Mock()
        messages.create.return_value = _resposta('{"motivo": "sei lá"}')
        cliente.return_value = SimpleNamespace(messages=messages)

        resultado = avaliar_cupom_ia(escopo="loja X")

        self.assertTrue(resultado["vale_a_pena"])
