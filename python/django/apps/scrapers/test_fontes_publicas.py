"""Promobit e Telegram público: radares comunitários sujeitos a validação.

O eixo de todos estes testes é o mesmo: **alegação de terceiro não pode virar preço**.
Um "de R$ 500 por R$ 99" escrito por um canal desconhecido, se entrasse como preço de
referência, produziria um desconto falso com a assinatura de quem publica. Por isso
várias asserções aqui verificam o que a fonte NÃO faz.
"""
from unittest.mock import patch

import requests
from django.test import TestCase

from apps.scrapers.sources.promobit import PromobitSource
from apps.scrapers.sources.registry import SOURCES
from apps.scrapers.sources.telegram_publico import TelegramPublicoSource

TG_HTML = """
<div class="tgme_widget_message" data-post="canalteste/376">
  <div class="tgme_widget_message_text js-message_text">
    🔥 Camiseta Tommy (Cupom MELIMODA)<br/>💰 R$ 113,00<br/>
    🔗 https://meli.la/21M8WPs?matt_word=ABC
  </div>
</div>
<div class="tgme_widget_message" data-post="canalteste/377">
  <div class="tgme_widget_message_text js-message_text">
    Fone bom<br/>R$ 89,90<br/>https://www.amazon.com.br/dp/B0ABCDEFGH
  </div>
</div>
<div class="tgme_widget_message" data-post="canalteste/378">
  <div class="tgme_widget_message_text js-message_text">
    Bom dia, pessoal! Sem link nenhum aqui.
  </div>
</div>
<div class="tgme_widget_message" data-post="canalteste/379">
  <div class="tgme_widget_message_text js-message_text">
    Confira minha vitrine<br/>https://www.mercadolivre.com.br/social/lojinha
  </div>
</div>
"""

# O encurtador do exemplo aponta para um anúncio de verdade. Nos testes ele é
# resolvido sem rede — o que se mede aqui é a REGRA, não a internet.
DESTINO_CURTO = "https://produto.mercadolivre.com.br/MLB-123456-camiseta"

PROMOBIT_HTML = """
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"ItemList","itemListElement":[
 {"@type":"Offer","name":"Cupom LISTA25 - Amazon",
  "description":"Cupom Amazon concede desconto de 25% em mercado",
  "discountCode":"LISTA25",
  "url":"https://www.promobit.com.br/Redirect/cupom/68473"},
 {"@type":"Offer","name":"Cupom sem codigo",
  "description":"desconto de 10%","url":"https://www.promobit.com.br/x"},
 {"@type":"Offer","name":"Cupom SEMVALOR - Amazon",
  "description":"Cupom para usar hoje","discountCode":"SEMVALOR"},
 {"@type":"Offer","name":"Cupom TUDO100","description":"desconto de 100%",
  "discountCode":"TUDO100"},
 {"@type":"Offer","name":"Cupom Resgate no produto - Amazon",
  "description":"desconto de 30%","discountCode":"Resgate no produto"}
]}
</script>
"""

PROMOBIT_NEXT = """
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"serverCoupons":{"coupons":[
  {"couponCode":"EXTRA15","couponTitle":"Extra Amazon",
   "couponDiscountValue":"15% de Desconto","couponDiscountOn":"selecao",
   "couponStatusName":"APPROVED","couponUntil":"2028-01-01T20:59:59-0300",
   "couponUrl":"https://www.promobit.com.br/Redirect/cupom/1"},
  {"couponCode":"VELHO10","couponTitle":"Expirado",
   "couponDiscountValue":"10% de Desconto","couponStatusName":"APPROVED",
   "couponUntil":"2020-01-01T20:59:59-0300"},
  {"couponCode":"NOVO20","couponTitle":"Pendente",
   "couponDiscountValue":"20% de Desconto","couponStatusName":"PENDING"},
  {"couponCode":"","couponTitle":"Sem codigo",
   "couponDiscountValue":"30% de Desconto","couponStatusName":"APPROVED"}
],"couponsRelated":[
  {"couponCode":"AUDIO400","couponTitle":"Magalu",
   "couponDiscountValue":"R$ 400 de Desconto","couponStatusName":"APPROVED"}
]}}}}
</script>
"""


class TelegramPublicoTests(TestCase):
    def _coletar(self, corpo=TG_HTML, canais=("canalteste",), destino=DESTINO_CURTO):
        fonte = TelegramPublicoSource()
        with patch.object(TelegramPublicoSource, "_baixar", return_value=corpo), \
                patch("apps.scrapers.sources.telegram_publico.resolver",
                      return_value=destino):
            return fonte, list(fonte.discover_offers(canais=list(canais)))

    def test_extrai_link_e_reconhece_a_loja(self):
        _, itens = self._coletar()
        lojas = sorted(i.marketplace for i in itens)
        self.assertEqual(lojas, ["amazon", "mercadolivre"])

    def test_preco_do_texto_nunca_vira_preco_do_produto(self):
        """O ponto central: R$ 113,00 escrito pelo canal é alegação, não preço."""
        _, itens = self._coletar()
        for item in itens:
            self.assertEqual(item.current_price, 0.0)
            self.assertEqual(item.reference_price, 0.0)
        alegados = sorted(i.evidence["preco_alegado"] for i in itens)
        self.assertEqual(alegados, [89.90, 113.00])

    def test_cupom_citado_entra_como_evidencia(self):
        _, itens = self._coletar()
        ml = next(i for i in itens if i.marketplace == "mercadolivre")
        self.assertEqual(ml.evidence["cupom_citado"], "MELIMODA")

    def test_mensagem_sem_link_de_loja_e_ignorada(self):
        _, itens = self._coletar()
        self.assertEqual(len(itens), 2)

    def test_vitrine_de_afiliado_e_descartada(self):
        """O achado que mais importa desta fonte.

        Medido em 18/08/2026 nos canais reais: de 44 links, ZERO era página de
        produto. Os canais publicam `meli.la` que resolve para `/social/<perfil>` —
        a vitrine de afiliado de quem postou, com a atribuição DELES. Sem este
        portão a fonte enchia o catálogo de linhas que o Programa de Afiliados
        recusa e que nunca virariam envio.
        """
        fonte, itens = self._coletar()
        urls = [i.canonical_url for i in itens]
        self.assertFalse([u for u in urls if "/social/" in u])
        self.assertGreaterEqual(fonte.last_metrics["descartados"]["nao_e_produto"], 1)

    def test_encurtador_que_cai_em_vitrine_e_descartado(self):
        fonte, itens = self._coletar(
            destino="https://www.mercadolivre.com.br/social/achadosoriginais")
        # Sobra só o link direto da Amazon, que já é página de produto.
        self.assertEqual([i.marketplace for i in itens], ["amazon"])
        self.assertGreaterEqual(fonte.last_metrics["descartados"]["nao_e_produto"], 2)

    def test_encurtador_que_nao_resolve_e_descartado(self):
        """Não conferiu, não publica. Perder oferta é melhor que publicar às cegas."""
        fonte, itens = self._coletar(destino="")
        self.assertEqual([i.marketplace for i in itens], ["amazon"])
        self.assertEqual(fonte.last_metrics["descartados"]["nao_resolveu"], 1)

    def test_encurtador_resolvido_entra_com_a_url_final(self):
        _, itens = self._coletar()
        ml = next(i for i in itens if i.marketplace == "mercadolivre")
        self.assertEqual(ml.canonical_url, DESTINO_CURTO)

    def test_coleta_nunca_se_declara_completa(self):
        """A prévia só mostra as recentes; ausência aqui não expira catálogo."""
        fonte, _ = self._coletar()
        self.assertFalse(fonte.last_metrics["complete"])

    def test_handle_invalido_e_recusado_sem_requisicao(self):
        fonte = TelegramPublicoSource()
        with patch("apps.scrapers.sources.telegram_publico.requests.get") as get:
            self.assertEqual(fonte._baixar("../etc/passwd"), "")
        get.assert_not_called()

    def test_canal_fora_do_ar_nao_derruba_a_coleta(self):
        fonte = TelegramPublicoSource()
        with patch.object(TelegramPublicoSource, "_baixar",
                          side_effect=requests.ConnectionError("offline")):
            itens = list(fonte.discover_offers(canais=["canalteste"]))
        self.assertEqual(itens, [])
        self.assertEqual(fonte.last_metrics["canais_falhos"], 1)
        self.assertEqual(fonte.last_health_status, "degraded")

    def test_nao_pede_navegador(self):
        self.assertFalse(getattr(TelegramPublicoSource, "requires_chromium", False))


class PromobitTests(TestCase):
    def _coletar(self, corpo=PROMOBIT_HTML, lojas=("amazon",)):
        fonte = PromobitSource()
        with patch.object(PromobitSource, "_baixar", return_value=corpo):
            return fonte, list(fonte.discover_coupons(lojas=list(lojas)))

    def test_le_o_cupom_do_schema_org(self):
        _, itens = self._coletar()
        codigos = sorted(i.coupon_code for i in itens)
        self.assertEqual(codigos, ["LISTA25"])
        item = itens[0]
        self.assertEqual(item.marketplace, "amazon")
        self.assertEqual(item.coupon_rules["valor_desconto"], 25.0)
        self.assertEqual(item.coupon_rules["modo_resgate"], "codigo")

    def test_cupom_sem_codigo_e_descartado(self):
        """Sem código digitável, o clique (e a comissão) iria para o Promobit."""
        _, itens = self._coletar()
        self.assertNotIn("", [i.coupon_code for i in itens])

    def test_cupom_sem_valor_comprovado_e_descartado(self):
        _, itens = self._coletar()
        self.assertNotIn("SEMVALOR", [i.coupon_code for i in itens])

    def test_desconto_de_cem_por_cento_e_recusado(self):
        """100% não existe em varejo; é erro de parse ou promessa falsa."""
        _, itens = self._coletar()
        self.assertNotIn("TUDO100", [i.coupon_code for i in itens])

    def test_frase_no_lugar_do_codigo_e_recusada(self):
        """Caso real da fonte: `discountCode` vinha como "Resgate no produto".

        Publicar isso manda o grupo digitar uma frase no checkout e não funcionar —
        exatamente o tipo de cupom que queima a confiança de quem assina a mensagem.
        """
        _, itens = self._coletar()
        codigos = [i.coupon_code for i in itens]
        self.assertNotIn("RESGATE NO PRODUTO", codigos)
        for codigo in codigos:
            self.assertNotIn(" ", codigo)

    def test_nao_publica_a_url_de_redirect_do_promobit(self):
        _, itens = self._coletar()
        for item in itens:
            self.assertNotIn("promobit.com.br", item.canonical_url)

    def test_next_data_traz_codigo_que_o_schema_org_nao_lista(self):
        _, itens = self._coletar(corpo=PROMOBIT_HTML + PROMOBIT_NEXT)
        codigos = sorted(i.coupon_code for i in itens)
        self.assertEqual(codigos, ["EXTRA15", "LISTA25"])
        extra = next(i for i in itens if i.coupon_code == "EXTRA15")
        self.assertEqual(extra.evidence["transport"], "promobit-next-data")
        self.assertIsNotNone(extra.valid_until)
        self.assertNotIn("promobit.com.br", extra.canonical_url)

    def test_next_data_ignora_expirado_pendente_e_loja_relacionada(self):
        _, itens = self._coletar(corpo=PROMOBIT_NEXT)
        self.assertEqual([i.coupon_code for i in itens], ["EXTRA15"])

    def test_corrige_percentual_impossivel_sem_perder_condicoes_reais(self):
        corpo = """
        <script id="__NEXT_DATA__" type="application/json">
        {"props":{"pageProps":{"serverCoupons":{"coupons":[{
          "couponCode":"MELHORPROMO",
          "couponTitle":"Economize 20000% no Mercado Livre",
          "couponDiscountValue":"20000% de Desconto",
          "couponDiscount":"Desconto de R$200 para compras a partir de R$4.999, limitado a R$200",
          "couponDiscountOn":"https://lista.mercadolivre.com.br/_Container_200-smf",
          "couponStatusName":"APPROVED"
        }]}}}}
        </script>
        """
        _, itens = self._coletar(corpo=corpo, lojas=("mercado-livre",))

        self.assertEqual(len(itens), 1)
        item = itens[0]
        self.assertEqual(item.title, "Cupom MELHORPROMO — R$ 200,00 OFF")
        self.assertNotIn("20000%", item.coupon_rules["escopo"])
        self.assertEqual(item.coupon_rules["tipo_desconto"], "fixo")
        self.assertEqual(item.coupon_rules["valor_desconto"], 200.0)
        self.assertEqual(item.coupon_rules["valor_minimo"], 4999.0)
        self.assertEqual(item.coupon_rules["desconto_maximo"], 200.0)
        self.assertEqual(
            item.coupon_rules["container_url"],
            "https://lista.mercadolivre.com.br/_Container_200-smf",
        )

    def test_preserva_percentual_decimal_valido_na_descricao(self):
        corpo = PROMOBIT_NEXT.replace("15%", "12,5%").replace("EXTRA15", "EXTRA125")
        _, itens = self._coletar(corpo=corpo)

        self.assertEqual(len(itens), 1)
        self.assertEqual(itens[0].coupon_rules["valor_desconto"], 12.5)
        self.assertIn("12,5%", itens[0].coupon_rules["escopo"])

    def test_marca_a_origem_como_comunidade(self):
        _, itens = self._coletar()
        self.assertEqual(itens[0].evidence["confianca_origem"], "comunidade")

    def test_slug_invalido_e_recusado_sem_requisicao(self):
        fonte = PromobitSource()
        with patch("apps.scrapers.sources.promobit.requests.get") as get:
            self.assertEqual(fonte._baixar("../admin"), "")
        get.assert_not_called()

    def test_coleta_nunca_se_declara_completa(self):
        fonte, _ = self._coletar()
        self.assertFalse(fonte.last_metrics["complete"])

    def test_loja_fora_do_conjunto_afiliavel_nao_e_consultada(self):
        fonte = PromobitSource()
        with patch.object(PromobitSource, "_baixar", return_value="") as baixar:
            list(fonte.discover_coupons(lojas=["magazine-luiza"]))
        baixar.assert_not_called()


class RegistroDeFontesTests(TestCase):
    def test_as_duas_fontes_estao_registradas(self):
        self.assertIn("promobit-cupons", SOURCES)
        self.assertIn("telegram-publico", SOURCES)

    def test_precedencia_baixa_para_alegacao_de_terceiro(self):
        """Comunidade corrobora; não decide sozinha contra uma fonte oficial."""
        from apps.scrapers.sources.persistence import _SOURCE_PRECEDENCE

        oficial = _SOURCE_PRECEDENCE["ml-cupons-afiliados"]
        for slug in ("promobit-cupons", "telegram-publico"):
            self.assertGreater(_SOURCE_PRECEDENCE[slug], oficial, slug)

    def test_esqueleto_antigo_do_promobit_continua_desabilitado(self):
        """O stub histórico não pode voltar a rodar por engano junto do novo."""
        self.assertFalse(SOURCES["promobit-community"].healthcheck()["ok"])


class InventarioParcialTests(TestCase):
    """Fonte que nunca vê o inventário inteiro não pode ser cobrada por isso.

    `maintenance.diagnosticar_alertas_pipeline_cupons` acusa fonte que passa dois
    ciclos sem se declarar completa. Promobit e Telegram são recortes por
    construção — sem esta marcação eles disparavam o alerta para sempre, que é o
    mesmo defeito de `projection_stale` contando cupom saudável: ruído permanente no
    lugar onde o operador precisa ver problema de verdade. Medido em produção como
    `source_without_complete_two_cycles: 2` logo após o deploy das duas fontes.
    """

    def test_fontes_de_recorte_se_declaram_parciais(self):
        self.assertFalse(SOURCES["promobit-cupons"].inventario_completo)
        self.assertFalse(SOURCES["telegram-publico"].inventario_completo)

    def test_fonte_oficial_continua_cobrada_por_completude(self):
        """A página oficial ou lista tudo, ou está quebrada — e isso tem de aparecer."""
        self.assertTrue(SOURCES["ml-cupons-afiliados"].inventario_completo)

    def test_alerta_ignora_as_parciais(self):
        from apps.scrapers.maintenance import diagnosticar_alertas_pipeline_cupons
        from apps.scrapers.models import ExecucaoIngestao, FonteIngestao

        fonte = FonteIngestao.objects.create(
            slug="promobit-cupons", marketplace="multiloja",
            nome="Promobit", status="ok",
        )
        for _ in range(2):
            ExecucaoIngestao.objects.create(
                fonte=fonte, status="ok", health_status="healthy",
                metricas={"complete": False},
            )
        contas = diagnosticar_alertas_pipeline_cupons()
        self.assertEqual(contas["source_without_complete_two_cycles"], 0)
