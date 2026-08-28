"""A leitura é do modelo; a decisão de aceitar é nossa.

O extrator existe porque a expressão regular falhou de forma medida: 14 canais
varridos, zero cupons — não por falta de cupom, mas porque cada canal escreve de um
jeito. Uma mensagem real do `@cupombr` carrega sete cupons de Mercado Livre com
desconto, mínimo, teto e escopo, e nenhum casava com o padrão que o regex esperava.

Estes testes cobrem exatamente a parte que NÃO é o modelo: `_limpar`, que decide o
que entra. Ela roda sem rede e sem chave de API de propósito — a regra de negócio
não pode depender de uma chamada externa para ser verificável.
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from apps.scrapers.cupom_extractor import (
    _limpar, extrair, extrair_deterministico, parece_ter_cupom,
)

# Transcrição fiel de uma mensagem real do @cupombr, medida em 19/08/2026.
MENSAGEM_REAL = """LISTÃO de Cupom Mercado Livre

10% OFF, Limite de R$ 20 OFF em todo site: TODOOSITE1308
15%OFF Limite de R$ 189: TVS1208CELULAR
R$50 OFF em R$399: CASA1508
25% OFF Acessórios para veículos: OMELHOR
✅ Ative aqui: https://mercadolivre.com.br/sec/31qRqvp"""

SAIDA_MODELO = {"cupons": [
    {"codigo": "TODOOSITE1308", "loja": "mercadolivre", "tipo": "porcentagem",
     "valor": 10, "minimo": 0, "teto": 20, "escopo": "todo site"},
    {"codigo": "CASA1508", "loja": "mercadolivre", "tipo": "fixo",
     "valor": 50, "minimo": 399, "teto": 0, "escopo": ""},
]}


class SinalDeCupomTests(TestCase):
    def test_mensagem_com_cupom_e_marcada(self):
        self.assertTrue(parece_ter_cupom(MENSAGEM_REAL))
        self.assertTrue(parece_ter_cupom("🎟️ AMIG4ASPROM0 30%"))
        self.assertTrue(parece_ter_cupom("20% OFF hoje"))

    def test_mensagem_sem_sinal_nao_vai_para_o_modelo(self):
        """Filtro de custo: mensagem sem cara de cupom não é lida."""
        self.assertFalse(parece_ter_cupom("Bom dia, pessoal!"))
        self.assertFalse(parece_ter_cupom("Monitor LG por R$ 569 à vista"))


class RegraDeAceitacaoTests(TestCase):
    """O que o modelo devolve passa por aqui antes de virar cupom."""

    def test_aceita_os_cupons_da_mensagem_real(self):
        aceitos = _limpar(SAIDA_MODELO)
        self.assertEqual([c["codigo"] for c in aceitos],
                         ["TODOOSITE1308", "CASA1508"])
        primeiro = aceitos[0]
        self.assertEqual(primeiro["tipo"], "porcentagem")
        self.assertEqual(primeiro["valor"], 10.0)
        self.assertEqual(primeiro["teto"], 20.0)
        self.assertEqual(primeiro["escopo"], "todo site")
        self.assertEqual(aceitos[1]["minimo"], 399.0)

    def test_codigo_com_espaco_e_recusado(self):
        """"MERCADO LIVRE" não é código; ninguém digita isso no checkout."""
        aceitos = _limpar({"cupons": [
            {"codigo": "MERCADO LIVRE", "loja": "mercadolivre",
             "tipo": "porcentagem", "valor": 10},
        ]})
        self.assertEqual(aceitos, [])

    def test_palavra_operacional_nao_vira_codigo_via_modelo(self):
        aceitos = _limpar({"cupons": [
            {"codigo": "ESTOQUE", "loja": "mercadolivre",
             "tipo": "porcentagem", "valor": 10},
        ]})
        self.assertEqual(aceitos, [])

    def test_cem_por_cento_e_recusado(self):
        aceitos = _limpar({"cupons": [
            {"codigo": "TUDO100", "loja": "mercadolivre",
             "tipo": "porcentagem", "valor": 100},
        ]})
        self.assertEqual(aceitos, [])

    def test_loja_que_nao_sabemos_afiliar_e_recusada(self):
        """Cupom de loja não afiliável é trabalho para o usuário e comissão de outro."""
        aceitos = _limpar({"cupons": [
            {"codigo": "MAGALU10", "loja": "magazineluiza",
             "tipo": "porcentagem", "valor": 10},
        ]})
        self.assertEqual(aceitos, [])

    def test_valor_zero_e_recusado(self):
        aceitos = _limpar({"cupons": [
            {"codigo": "SEMVALOR", "loja": "amazon",
             "tipo": "porcentagem", "valor": 0},
        ]})
        self.assertEqual(aceitos, [])

    def test_loja_ausente_cai_no_padrao_do_link(self):
        aceitos = _limpar({"cupons": [
            {"codigo": "SEMLOJA10", "loja": "", "tipo": "porcentagem", "valor": 10},
        ]}, loja_padrao="shopee")
        self.assertEqual(aceitos[0]["loja"], "shopee")

    def test_codigo_repetido_entra_uma_vez(self):
        aceitos = _limpar({"cupons": [
            {"codigo": "IGUAL10", "loja": "amazon", "tipo": "porcentagem", "valor": 10},
            {"codigo": "IGUAL10", "loja": "amazon", "tipo": "porcentagem", "valor": 15},
        ]})
        self.assertEqual(len(aceitos), 1)

    def test_resposta_ilegivel_do_modelo_nao_quebra(self):
        self.assertEqual(_limpar(None), [])
        self.assertEqual(_limpar({"cupons": "nao e lista"}), [])
        self.assertEqual(_limpar({"cupons": ["texto solto"]}), [])

    def test_fallback_local_le_lista_real_sem_chave(self):
        achados = extrair_deterministico(MENSAGEM_REAL)
        self.assertEqual(
            [c["codigo"] for c in achados],
            ["TODOOSITE1308", "TVS1208CELULAR", "CASA1508", "OMELHOR"],
        )
        self.assertEqual(achados[0]["teto"], 20.0)
        self.assertEqual(achados[2]["minimo"], 399.0)

    def test_fallback_nao_confunde_nome_de_produto_com_codigo(self):
        texto = (
            "🔥 Smartphone Motorola Moto G17 4G 128GB (Cupom 10% OFF)\n"
            "💰 R$ 728,19\nhttps://meli.la/abc"
        )
        self.assertEqual(extrair_deterministico(texto), [])

    def test_fallback_associa_codigo_na_linha_seguinte(self):
        texto = (
            "Cupom Shopee\nR$15 OFF nas compras acima de R$79\n"
            "Usem o Cupom: F1MD03SQU3NT4\nhttps://s.shopee.com.br/abc"
        )
        achados = extrair_deterministico(texto)
        self.assertEqual([c["codigo"] for c in achados], ["F1MD03SQU3NT4"])
        self.assertEqual(achados[0]["minimo"], 79.0)


@override_settings(ANTHROPIC_API_KEY="chave-de-teste", CUPOM_LLM_ATIVO=True)
class ExtracaoTests(TestCase):
    def setUp(self):
        cache.clear()

    def _resposta(self, payload):
        def _fake(*args, **kwargs):
            return payload
        return _fake

    def test_le_a_mensagem_e_devolve_os_cupons(self):
        with patch("apps.scrapers.llm._cliente"), \
                patch("apps.scrapers.llm._texto_resposta", return_value="{}"), \
                patch("apps.scrapers.llm._json_resposta", return_value=SAIDA_MODELO):
            achados = extrair(MENSAGEM_REAL)
        self.assertEqual(len(achados), 2)

    def test_a_mesma_mensagem_e_lida_uma_vez_so(self):
        """Cache por hash: a mensagem do canal é imutável, pagar duas vezes é desperdício."""
        with patch("apps.scrapers.llm._cliente") as cliente, \
                patch("apps.scrapers.llm._texto_resposta", return_value="{}"), \
                patch("apps.scrapers.llm._json_resposta", return_value=SAIDA_MODELO):
            extrair(MENSAGEM_REAL)
            extrair(MENSAGEM_REAL)
        self.assertEqual(cliente.call_count, 1)

    def test_mensagem_sem_cupom_nem_chega_ao_modelo(self):
        with patch("apps.scrapers.llm._cliente") as cliente:
            self.assertEqual(extrair("Bom dia, pessoal!"), [])
        cliente.assert_not_called()

    def test_falha_do_modelo_devolve_vazio_e_nao_cacheia(self):
        """Erro não derruba a coleta nem congela a mensagem como vazia."""
        with patch("apps.scrapers.llm._cliente", side_effect=RuntimeError("api fora")):
            self.assertEqual(len(extrair(MENSAGEM_REAL)), 4)
        # Simula o TTL do circuito encerrado; a mensagem em si não foi cacheada.
        cache.delete("cupom-llm-circuit:anthropic")
        with patch("apps.scrapers.llm._cliente"), \
                patch("apps.scrapers.llm._texto_resposta", return_value="{}"), \
                patch("apps.scrapers.llm._json_resposta", return_value=SAIDA_MODELO):
            self.assertEqual(len(extrair(MENSAGEM_REAL)), 2)

    def test_erro_de_credito_abre_circuito_e_evita_tempestade_de_chamadas(self):
        outra = MENSAGEM_REAL.replace("CASA1508", "CASA1509")
        with patch(
            "apps.scrapers.llm._cliente",
            side_effect=RuntimeError("Your credit balance is too low; billing"),
        ) as cliente:
            self.assertEqual(len(extrair(MENSAGEM_REAL)), 4)
            self.assertEqual(len(extrair(outra)), 4)

        self.assertEqual(cliente.call_count, 1)
        self.assertEqual(cache.get("cupom-llm-circuit:anthropic"), "credential_or_credit")

    def test_resposta_truncada_salva_os_cupons_inteiros(self):
        """Erro real de produção: `Unterminated string` no meio da lista.

        A mensagem que estourou o orçamento foi justamente a mais valiosa — o
        "LISTÃO" com sete cupons. Perder a mensagem inteira por causa do último
        objeto cortado é o pior resultado possível.
        """
        truncada = (
            '{"cupons":[{"codigo":"TODOOSITE1308","loja":"mercadolivre",'
            '"tipo":"porcentagem","valor":10,"minimo":0,"teto":20,"escopo":"todo site"},'
            '{"codigo":"CASA1508","loja":"mercadolivre","tipo":"fixo","valor":50,'
            '"minimo":399,"teto":0,"escopo":""},'
            '{"codigo":"CORTADO","loja":"mercadoliv'
        )
        with patch("apps.scrapers.llm._cliente"), \
                patch("apps.scrapers.llm._texto_resposta", return_value=truncada), \
                patch("apps.scrapers.llm._json_resposta", return_value=None):
            achados = extrair(MENSAGEM_REAL)
        self.assertEqual([c["codigo"] for c in achados],
                         ["TODOOSITE1308", "CASA1508"])

    @override_settings(ANTHROPIC_API_KEY="")
    def test_sem_chave_nao_chama_nada(self):
        with patch("apps.scrapers.llm._cliente") as cliente:
            self.assertEqual(len(extrair(MENSAGEM_REAL)), 4)
        cliente.assert_not_called()

    @override_settings(CUPOM_LLM_ATIVO=False)
    def test_desligado_por_flag(self):
        with patch("apps.scrapers.llm._cliente") as cliente:
            self.assertEqual(len(extrair(MENSAGEM_REAL)), 4)
        cliente.assert_not_called()
