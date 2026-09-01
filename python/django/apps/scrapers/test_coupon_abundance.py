"""Meta de abundância por loja: contagem distinta, déficit e prova de exaustão.

A meta do contrato tem duas metades e a segunda é a que costuma sumir: 100 cupons
distintos prontos POR LOJA, e — quando não houver — a prova de que as fontes
daquela loja foram até o fim. Um déficit com fonte parada em `max_pages` não é
"a loja não tem inventário", é "nós não terminamos de olhar".
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import ensure_personal_organization, organization_for_user
from apps.scrapers.coupon_abundance import (
    exaustao_das_fontes, prontos_distintos, relatorio_abundancia,
)
from apps.scrapers.models import (
    CupomDisponibilidade, CupomFonteObservacao, CupomNormalizado,
    ExecucaoIngestao, FonteIngestao,
)


class AbundanciaTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("abundancia", password="x")
        ensure_personal_organization(cls.user)
        cls.org = organization_for_user(cls.user)
        cls.outro = get_user_model().objects.create_user("abundancia2", password="x")
        ensure_personal_organization(cls.outro)
        cls.org_outro = organization_for_user(cls.outro)
        cls.fontes = {
            marketplace: FonteIngestao.objects.create(
                slug=f"fonte-{marketplace}", marketplace=marketplace,
                nome=f"Fonte {marketplace}", status="ok", habilitada=True,
            )
            for marketplace in ("mercadolivre", "amazon", "shopee")
        }

    def _cupom(self, marketplace, sufixo):
        return CupomNormalizado.objects.create(
            fonte=self.fontes[marketplace], external_id=f"{marketplace}-{sufixo}",
            marketplace=marketplace, titulo=f"Cupom {sufixo}",
            codigo=f"COD{sufixo}", estado="ativo", redemption_mode="code",
            ultima_observacao=timezone.now(),
            regras={"tipo_desconto": "porcentagem", "valor_desconto": 15,
                    "modo_resgate": "codigo"},
        )

    def _projecao(self, cupom, *, stage="ready", usuario=None, organization=None,
                  use_mode="code_notice", reason_code=""):
        return CupomDisponibilidade.objects.create(
            organization=organization or self.org, usuario=usuario or self.user,
            cupom=cupom, channel="whatsapp", use_mode=use_mode, stage=stage,
            category="" if stage == "ready" else "waiting", reason_code=reason_code,
        )

    def _execucao(self, marketplace, *, metricas=None, health="ok", status="ok"):
        return ExecucaoIngestao.objects.create(
            fonte=self.fontes[marketplace], status=status, health_status=health,
            metricas=metricas or {}, finalizada_em=timezone.now(),
        )

    def test_conta_cupom_distinto_e_nao_projecao_por_usuario(self):
        """Dois usuários prontos com o MESMO cupom são um cupom, não dois.

        Contar linhas de projeção faria a meta ser atingida multiplicando
        usuários — exatamente o "reciclar duplicatas" que o contrato proíbe.
        """
        cupom = self._cupom("amazon", "UNICO")
        self._projecao(cupom)
        self._projecao(cupom, usuario=self.outro, organization=self.org_outro)
        self.assertEqual(prontos_distintos()["amazon"]["total"], 1)

    def test_modos_nao_somam_duas_vezes_o_mesmo_cupom(self):
        cupom = self._cupom("mercadolivre", "DOISMODOS")
        self._projecao(cupom, use_mode="code_notice")
        self._projecao(cupom, use_mode="product_activation")
        prontos = prontos_distintos()["mercadolivre"]
        self.assertEqual(prontos["total"], 1)
        self.assertEqual(prontos["por_modo"],
                         {"code_notice": 1, "product_activation": 1})

    def test_cupom_expirado_nao_conta(self):
        cupom = self._cupom("shopee", "VELHO")
        CupomNormalizado.objects.filter(pk=cupom.pk).update(
            ultima_observacao=timezone.now() - timedelta(hours=72),
        )
        self._projecao(cupom)
        self.assertEqual(prontos_distintos()["shopee"]["total"], 0)

    def test_deficit_com_fonte_parada_por_orcamento_e_coleta_incompleta(self):
        """`max_pages` é orçamento nosso; não prova ausência de inventário."""
        self._execucao("amazon", metricas={"stop_reason": "max_pages",
                                           "complete": False})
        relatorio = relatorio_abundancia(meta=10)
        loja = relatorio["lojas"]["amazon"]
        self.assertEqual(loja["veredito"], "coleta_incompleta")
        self.assertFalse(loja["deficit_provado"])
        self.assertIn("fonte-amazon", loja["fontes_nao_exauridas"])

    def test_deficit_provado_exige_todas_as_fontes_exauridas(self):
        # A prova é global para a loja: desabilita as fontes multiloja sem
        # execução que o catálogo de teste já traz, deixando uma única fonte.
        FonteIngestao.objects.exclude(
            pk=self.fontes["shopee"].pk,
        ).update(habilitada=False)
        self._execucao("shopee", metricas={"stop_reason": "no_new_items",
                                           "complete": True})
        relatorio = relatorio_abundancia(meta=10)
        loja = relatorio["lojas"]["shopee"]
        self.assertEqual(loja["veredito"], "deficit_provado")
        self.assertTrue(loja["deficit_provado"])
        self.assertEqual(loja["deficit"], 10)

    def test_fonte_bloqueada_nunca_prova_deficit(self):
        self._execucao("shopee", metricas={"stop_reason": "captcha_or_block"},
                       health="blocked", status="error")
        loja = relatorio_abundancia(meta=10)["lojas"]["shopee"]
        self.assertEqual(loja["veredito"], "coleta_incompleta")
        self.assertEqual(
            next(
                item for item in exaustao_das_fontes()["shopee"]
                if item["fonte"] == "fonte-shopee"
            )["exaustao"],
            "bloqueada",
        )

    def test_exaustao_consulta_somente_a_execucao_mais_recente(self):
        antiga = self._execucao(
            "amazon", metricas={"stop_reason": "no_new_items", "complete": True},
            health="ok", status="ok",
        )
        ExecucaoIngestao.objects.filter(pk=antiga.pk).update(
            iniciada_em=timezone.now() - timedelta(days=1),
        )
        recente = self._execucao(
            "amazon", metricas={"stop_reason": "max_pages", "complete": False},
            health="ok", status="ok",
        )

        item = next(
            row for row in exaustao_das_fontes()["amazon"]
            if row["fonte"] == self.fontes["amazon"].slug
        )

        self.assertEqual(item["exaustao"], "incompleta")
        self.assertEqual(item["stop_reason"], "max_pages")
        self.assertEqual(item["quando"], recente.finalizada_em)

    def test_uma_loja_abaixo_reprova_o_conjunto(self):
        for indice in range(2):
            self._projecao(self._cupom("mercadolivre", f"ML{indice}"))
            self._projecao(self._cupom("amazon", f"AZ{indice}"))
        relatorio = relatorio_abundancia(meta=2)
        self.assertEqual(relatorio["lojas"]["mercadolivre"]["veredito"],
                         "meta_atingida")
        self.assertEqual(relatorio["lojas"]["amazon"]["veredito"], "meta_atingida")
        self.assertFalse(relatorio["aprovado"])

    def test_bloqueios_explicam_o_deficit(self):
        cupom = self._cupom("amazon", "RETIDO")
        self._projecao(cupom, stage="collected",
                       reason_code="community_uncorroborated")
        loja = relatorio_abundancia(meta=10)["lojas"]["amazon"]
        self.assertEqual(loja["bloqueios"], [{
            "stage": "collected", "category": "waiting",
            "reason_code": "community_uncorroborated", "cupons": 1,
        }])

    def test_fonte_sem_execucao_nao_e_exaurida(self):
        loja = relatorio_abundancia(meta=10)["lojas"]["mercadolivre"]
        self.assertEqual(loja["veredito"], "coleta_incompleta")
        self.assertEqual(loja["fontes"][0]["exaustao"], "nunca_executada")

    def test_fonte_multiloja_participa_da_exaustao_das_tres_lojas(self):
        fonte = FonteIngestao.objects.create(
            slug="telegram-publico", marketplace="multiloja",
            nome="Telegram", status="ok", habilitada=True,
        )
        ExecucaoIngestao.objects.create(
            fonte=fonte, status="ok", health_status="healthy",
            metricas={"complete": True, "stop_reason": "no_new_items"},
            finalizada_em=timezone.now(),
        )
        fontes = exaustao_das_fontes()
        for marketplace in ("mercadolivre", "amazon", "shopee"):
            item = next(x for x in fontes[marketplace]
                        if x["fonte"] == "telegram-publico")
            self.assertEqual(item["exaustao"], "exaurida")

    @override_settings(COUPON_DAILY_DISCOVERY_GOAL=1)
    def test_descoberta_multiloja_usa_marketplace_do_cupom_e_tres_classes(self):
        marketplace = "amazon"
        cupom = self._cupom(marketplace, "FONTES")
        for slug in ("amazon-coupons", "meliuz-cupons", "telegram-publico"):
            fonte = FonteIngestao.objects.create(
                slug=slug, marketplace=(
                    marketplace if slug == "amazon-coupons" else "multiloja"
                ), nome=slug, status="ok",
            )
            CupomFonteObservacao.objects.create(
                fonte=fonte, cupom=cupom, canonical_key=f"{slug}:CODFONTES",
                source_external_id=slug, observed_at=timezone.now(),
            )
        loja = relatorio_abundancia(meta=0)["lojas"][marketplace]
        self.assertEqual(loja["descoberta_24h"], 3)
        self.assertEqual(
            loja["classes_descoberta"], ["agregador", "comunidade", "oficial"],
        )
        self.assertTrue(loja["descoberta_atingida"])
