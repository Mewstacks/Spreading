"""Promobit e Telegram público: radares comunitários sujeitos a validação.

O eixo de todos estes testes é o mesmo: **alegação de terceiro não pode virar preço**.
Um "de R$ 500 por R$ 99" escrito por um canal desconhecido, se entrasse como preço de
referência, produziria um desconto falso com a assinatura de quem publica. Por isso
várias asserções aqui verificam o que a fonte NÃO faz.
"""
from unittest.mock import Mock, patch

import requests
from django.test import TestCase

from apps.scrapers.sources.promobit import PromobitSource
from apps.scrapers.sources.meliuz_coupons import MeliuzCouponsSource
from apps.scrapers.sources.registry import SOURCES
from apps.scrapers.sources.telegram_publico import TelegramPublicoSource
from apps.scrapers.sources.shopee_public_coupons import (
    ShopeePublicCouponsSource, _parse_rendered_card, _snapshot_state,
)

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

MELIUZ_HTML = """
<div class="cpn-layout offer-cpn" data-offer-id="308750"
 data-offer-code="JARDIM30" data-offer-title="Jardim com 30% OFF">
 <span class="offer-cpn__offer-summary"><strong>30%</strong></span>
 <div class="cpn-layout__rules" hidden>Em compras acima de R$200, limitado a R$80.</div>
</div>
<div class="cpn-layout offer-cpn" data-offer-id="308751"
 data-offer-code="CUPOMNOLINK" data-offer-title="Veja o cupom no link">
 <span class="offer-cpn__offer-summary"><strong>20%</strong></span>
 <div class="cpn-layout__rules" hidden>Confira os produtos.</div>
</div>
<h2>Cupons expirados de Amazon</h2>
<div class="cpn-layout offer-cpn" data-offer-id="antigo"
 data-offer-code="VELHO50" data-offer-title="Cupom antigo">
 <span class="offer-cpn__offer-summary"><strong>50%</strong></span>
</div>
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

    @patch("apps.scrapers.sources.telegram_publico.time.monotonic",
           side_effect=[100, 110, 221])
    @patch("apps.scrapers.sources.telegram_publico.requests.get")
    def test_cache_expira_e_novas_mensagens_voltam_a_ser_lidas(self, get, _clock):
        get.side_effect = [
            Mock(status_code=200, text="primeira"),
            Mock(status_code=200, text="segunda"),
        ]
        fonte = TelegramPublicoSource()

        self.assertEqual(fonte._baixar("cupombr"), "primeira")
        self.assertEqual(fonte._baixar("cupombr"), "primeira")
        self.assertEqual(fonte._baixar("cupombr"), "segunda")
        self.assertEqual(get.call_count, 2)

    def test_redirect_repetido_usa_cache_do_worker(self):
        fonte = TelegramPublicoSource()
        with patch(
                "apps.scrapers.sources.telegram_publico.resolver",
                return_value="https://www.amazon.com.br/dp/B012345678") as resolve:
            primeira = fonte._resolver_lote(["https://amzn.to/exemplo"])
            segunda = fonte._resolver_lote(["https://amzn.to/exemplo"])

        self.assertEqual(primeira, segunda)
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(fonte._last_redirect_cache_hits, 1)

    def test_ciclo_de_cupons_nao_resolve_ofertas_sem_uso(self):
        fonte = TelegramPublicoSource()
        with patch.object(fonte, "_carregar_canais") as carregar:
            self.assertEqual(list(fonte.discover_offers(include_offers=False)), [])
        carregar.assert_not_called()

    def test_handle_do_canal_nunca_vira_codigo_de_cupom(self):
        fonte = TelegramPublicoSource()
        cupons = [
            {"codigo": "TVCASASBAHIA", "loja": "mercadolivre",
             "tipo": "porcentagem", "valor": 10, "minimo": 0, "teto": 0,
             "escopo": ""},
            {"codigo": "REAL10", "loja": "mercadolivre",
             "tipo": "porcentagem", "valor": 10, "minimo": 0, "teto": 0,
             "escopo": ""},
        ]
        with patch.object(fonte, "_carregar_canais",
                          return_value=[("TVCASASBAHIA", "html", "")]), \
                patch.object(fonte, "_mensagens",
                             return_value=[("post-1", "10% OFF")]), \
                patch("apps.scrapers.sources.telegram_publico.extrair",
                      return_value=cupons):
            itens = list(fonte.discover_coupons(canais=["TVCASASBAHIA"]))

        self.assertEqual([item.coupon_code for item in itens], ["REAL10"])
        self.assertEqual(fonte.last_metrics["codigos_ruidosos_descartados"], 1)

    def test_cupom_guarda_asin_da_mesma_mensagem_sem_tracking(self):
        fonte = TelegramPublicoSource()
        mensagem = (
            "20% OFF com cupom REAL20 "
            "https://www.amazon.com.br/dp/B012345678?tag=canal-20&ref_=x"
        )
        cupom = [{
            "codigo": "REAL20", "loja": "amazon", "tipo": "porcentagem",
            "valor": 20, "minimo": 0, "teto": 0, "escopo": "",
        }]
        with patch.object(
                fonte, "_carregar_canais",
                return_value=[("canalteste", "html", "")]), patch.object(
                fonte, "_mensagens", return_value=[("canalteste/1", mensagem)]), patch(
                "apps.scrapers.sources.telegram_publico.extrair", return_value=cupom):
            itens = list(fonte.discover_coupons(canais=["canalteste"]))

        self.assertEqual(len(itens), 1)
        self.assertEqual(itens[0].canonical_url,
                         "https://www.amazon.com.br/dp/B012345678")
        self.assertEqual(itens[0].evidence["asins"], ["B012345678"])
        self.assertEqual(
            itens[0].evidence["association"], "same_public_telegram_message",
        )

    def test_href_oculto_atras_de_comprar_tambem_associa_produto(self):
        fonte = TelegramPublicoSource()
        corpo = """
        <div class="tgme_widget_message" data-post="canalteste/7">
          <div class="tgme_widget_message_text js-message_text">
            15% OFF cupom LINK15
            <a href="https://www.amazon.com.br/dp/B012345679?tag=canal-20">COMPRAR</a>
          </div>
        </div>
        """
        cupom = [{
            "codigo": "LINK15", "loja": "amazon", "tipo": "porcentagem",
            "valor": 15, "minimo": 0, "teto": 0, "escopo": "",
        }]
        with patch.object(
                fonte, "_carregar_canais",
                return_value=[("canalteste", corpo, "")]), patch(
                "apps.scrapers.sources.telegram_publico.extrair", return_value=cupom):
            itens = list(fonte.discover_coupons(canais=["canalteste"]))

        self.assertEqual(itens[0].evidence["asins"], ["B012345679"])
        self.assertEqual(
            itens[0].canonical_url, "https://www.amazon.com.br/dp/B012345679",
        )

    def test_cupom_mescla_produto_de_ocorrencia_posterior(self):
        fonte = TelegramPublicoSource()
        cupom = [{
            "codigo": "REAL20", "loja": "mercadolivre",
            "tipo": "porcentagem", "valor": 20, "minimo": 0,
            "teto": 0, "escopo": "",
        }]
        mensagens = [
            ("canalteste/1", "20% OFF cupom REAL20"),
            ("canalteste/2", "20% OFF cupom REAL20 https://meli.la/abc123"),
        ]
        with patch.object(
                fonte, "_carregar_canais",
                return_value=[("canalteste", "html", "")]), patch.object(
                fonte, "_mensagens", return_value=mensagens), patch(
                "apps.scrapers.sources.telegram_publico.extrair", return_value=cupom), patch(
                "apps.scrapers.sources.telegram_publico.resolver",
                return_value="https://produto.mercadolivre.com.br/MLB-123456-produto?matt_tool=x"):
            itens = list(fonte.discover_coupons(canais=["canalteste"]))

        self.assertEqual(len(itens), 1)
        self.assertEqual(itens[0].evidence["item_ids"], ["MLB123456"])
        self.assertNotIn("?", itens[0].canonical_url)

    def test_host_parecido_com_marketplace_nao_e_aceito(self):
        corpo = TG_HTML.replace(
            "https://www.amazon.com.br/dp/B0ABCDEFGH",
            "https://amazon.com.br.evil.test/dp/B0ABCDEFGH",
        )
        _, itens = self._coletar(corpo=corpo)
        self.assertEqual([item.marketplace for item in itens], ["mercadolivre"])


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

    def test_campo_codigo_nao_vence_regra_que_diz_aplicacao_automatica(self):
        corpo = """
        <script type="application/ld+json">
        {"@type":"ItemList","itemListElement":[{"@type":"Offer",
         "name":"50% de desconto","description":"O benefício entra automaticamente, sem precisar de código",
         "discountCode":"50NOW"}]}
        </script>
        """
        _, itens = self._coletar(corpo=corpo)
        self.assertEqual(itens, [])

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


class MeliuzCouponsTests(TestCase):
    def _collect(self, body=MELIUZ_HTML):
        source = MeliuzCouponsSource()
        with patch.object(MeliuzCouponsSource, "_download", return_value=body):
            items = list(source.discover_coupons(marketplaces=["amazon"]))
        return source, items

    def test_extracts_only_real_active_code(self):
        source, items = self._collect()

        self.assertEqual([item.coupon_code for item in items], ["JARDIM30"])
        self.assertEqual(items[0].coupon_rules["valor_desconto"], 30)
        self.assertEqual(items[0].coupon_rules["valor_minimo"], 200)
        self.assertEqual(items[0].coupon_rules["desconto_maximo"], 80)
        self.assertEqual(source.last_metrics["cards_seen"], 2)

    def test_does_not_reuse_aggregator_affiliate_redirect(self):
        _, items = self._collect()

        self.assertEqual(items[0].canonical_url, "")
        self.assertEqual(items[0].evidence["confianca_origem"], "comunidade")

    def test_placeholder_and_expired_section_are_rejected(self):
        source, _ = self._collect()

        self.assertEqual(source.last_metrics["rejected_by_reason"]["placeholder_code"], 1)
        self.assertNotIn("VELHO50", str(source.last_metrics))

    def test_placeholders_de_resgate_nao_viram_codigo(self):
        body = MELIUZ_HTML.replace("JARDIM30", "RESGATENOLINK")
        source, items = self._collect(body=body)
        self.assertEqual(items, [])
        self.assertGreaterEqual(
            source.last_metrics["rejected_by_reason"]["placeholder_code"], 2,
        )

    def test_is_always_a_partial_radar(self):
        source, _ = self._collect()

        self.assertFalse(source.last_metrics["complete"])
        self.assertEqual(source.last_health_status, "healthy")


class RegistroDeFontesTests(TestCase):
    def test_as_duas_fontes_estao_registradas(self):
        self.assertIn("promobit-cupons", SOURCES)
        self.assertIn("meliuz-cupons", SOURCES)
        self.assertIn("telegram-publico", SOURCES)
        self.assertIn("shopee-public-coupons", SOURCES)
        self.assertIn("ml-lightning-coupons", SOURCES)

    def test_precedencia_baixa_para_alegacao_de_terceiro(self):
        """Comunidade corrobora; não decide sozinha contra uma fonte oficial."""
        from apps.scrapers.sources.persistence import _SOURCE_PRECEDENCE

        oficial = _SOURCE_PRECEDENCE["ml-cupons-afiliados"]
        self.assertEqual(_SOURCE_PRECEDENCE["ml-lightning-coupons"], oficial)
        for slug in ("promobit-cupons", "meliuz-cupons", "telegram-publico"):
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
        self.assertFalse(SOURCES["meliuz-cupons"].inventario_completo)
        self.assertFalse(SOURCES["telegram-publico"].inventario_completo)

    def test_fonte_oficial_continua_cobrada_por_completude(self):
        """A página oficial ou lista tudo, ou está quebrada — e isso tem de aparecer."""
        self.assertTrue(SOURCES["ml-cupons-afiliados"].inventario_completo)


class ShopeePublicCouponsTests(TestCase):
    def _card(self, text, promotion="1496364873691136"):
        return {
            "text": text,
            "href": (
                "/voucher/details?evcode=MTIz&promotionId="
                f"{promotion}&signature=publica"
            ),
        }

    def test_parseia_voucher_fixo_disponivel(self):
        row, reason = _parse_rendered_card(self._card(
            "TODAS AS LOJAS\nR$20 OFF\nNas compras acima de R$159\n"
            "Limitado a R$20\nCondicoes\nEu quero"
        ))

        self.assertEqual(reason, "")
        self.assertEqual(row["discount_type"], "fixo")
        self.assertEqual(row["discount"], 20)
        self.assertEqual(row["minimum"], 159)
        self.assertEqual(row["maximum"], 20)
        self.assertIn("promotionId=1496364873691136", row["url"])

    def test_parseia_percentual_e_milhar_brasileiro(self):
        row, _ = _parse_rendered_card(self._card(
            "MOVEIS\n12,5% OFF\nNas compras acima de R$1,6mil "
            "Limitado a R$200\nCondicoes\nEu quero"
        ))

        self.assertEqual(row["discount_type"], "porcentagem")
        self.assertEqual(row["discount"], 12.5)
        self.assertEqual(row["minimum"], 1600)

    def test_nao_confunde_cashback_com_desconto(self):
        row, reason = _parse_rendered_card(self._card(
            "CUPOM\n20% DE CASHBACK\nLimitado a R$20\nCondicoes\nEu quero"
        ))

        self.assertIsNone(row)
        self.assertEqual(reason, "cashback_not_discount")

    def test_esgotado_nao_entra(self):
        row, reason = _parse_rendered_card(self._card(
            "TODAS AS LOJAS\nR$20 OFF\nCondicoes\nEsgotado"
        ))

        self.assertIsNone(row)
        self.assertEqual(reason, "unavailable")

    def test_fonte_oficial_consume_slot_de_chromium(self):
        self.assertTrue(ShopeePublicCouponsSource.requires_chromium)

    def test_snapshot_esgotado_e_cashback_pode_ser_vazio_saudavel(self):
        complete, health, schema_errors = _snapshot_state(
            4, 0, {"unavailable": 3, "cashback_not_discount": 1},
        )

        self.assertTrue(complete)
        self.assertEqual(health, "healthy_empty")
        self.assertEqual(schema_errors, 0)

    def test_quebra_de_schema_preserva_catalogo_anterior(self):
        complete, health, schema_errors = _snapshot_state(
            5, 2, {"unavailable": 2, "missing_discount": 1},
        )

        self.assertFalse(complete)
        self.assertEqual(health, "partial")
        self.assertEqual(schema_errors, 1)

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
