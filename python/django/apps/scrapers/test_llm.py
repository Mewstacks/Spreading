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
