"""Cupom de código não espera um link que a mensagem nunca vai usar.

`enviar_aviso_cupons` monta a mensagem com UM link para a lista inteira de códigos —
é o formato ("Ative em algum produto do link") e é o que o código faz: resolve o
link do primeiro cupom do lote e anuncia todos sob ele.

A readiness, porém, exigia um `LinkAfiliadoCupomUsuario` verificado POR CUPOM. Cada
um desses custa uma vaga do único Chromium da máquina, para produzir um link que
nunca é lido. Em produção isso apareceu como `code_not_ready_20m` na casa das
centenas com `browser_wait_over_60m` acumulando.

Estes testes fixam o novo contrato e, principalmente, o que ele NÃO afrouxa.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import ensure_personal_organization
from apps.scrapers.coupon_readiness import _tem_link_de_aviso
from apps.scrapers.models import (
    CupomNormalizado, FonteIngestao, LinkAfiliadoCupomUsuario,
)


class LinkDeAvisoTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("gargalo", password="x")
        ensure_personal_organization(cls.user)
        cls.fonte = FonteIngestao.objects.create(
            slug="ml-cupons-afiliados", marketplace="mercadolivre",
            nome="ML cupons", status="ok",
        )

    def _cupom(self, codigo):
        return CupomNormalizado.objects.create(
            fonte=self.fonte, external_id=f"ext-{codigo}", marketplace="mercadolivre",
            titulo=f"Cupom {codigo}", codigo=codigo, estado="ativo",
            redemption_mode="code",
            regras={"tipo_desconto": "porcentagem", "valor_desconto": 20,
                    "modo_resgate": "codigo"},
        )

    def _link(self, cupom, *, ok=True, idade_horas=1):
        return LinkAfiliadoCupomUsuario.objects.create(
            usuario=self.user, cupom=cupom,
            url_origem="https://www.mercadolivre.com.br/cupons",
            link_afiliado="https://mercadolivre.com/sec/abc",
            url_canonica="https://mercadolivre.com/sec/abc",
            afiliado_ok=True, verificado_ok=ok,
            verificado_em=timezone.now() - timedelta(hours=idade_horas),
        )

    def test_sem_nenhum_link_nao_libera(self):
        self._cupom("PRIMEIRO")
        self.assertFalse(_tem_link_de_aviso(self.user, "mercadolivre"))

    def test_link_de_outro_cupom_serve_para_o_aviso(self):
        """O ponto da mudança: um link por usuário basta, não um por cupom."""
        self._link(self._cupom("PRIMEIRO"))
        self._cupom("SEGUNDO")  # sem link próprio
        self.assertTrue(_tem_link_de_aviso(self.user, "mercadolivre"))

    def test_link_reprovado_nao_serve(self):
        self._link(self._cupom("PRIMEIRO"), ok=False)
        self.assertFalse(_tem_link_de_aviso(self.user, "mercadolivre"))

    def test_link_vencido_nao_serve(self):
        """Relaxar QUAL cupom não relaxa o TTL: link velho continua não valendo."""
        self._link(self._cupom("PRIMEIRO"), idade_horas=24 * 30)
        self.assertFalse(_tem_link_de_aviso(self.user, "mercadolivre"))

    def test_link_de_outra_loja_nao_serve(self):
        """Link do ML não autoriza anunciar código da Amazon."""
        self._link(self._cupom("PRIMEIRO"))
        self.assertFalse(_tem_link_de_aviso(self.user, "amazon"))

    def test_link_de_outro_usuario_nao_serve(self):
        outro = get_user_model().objects.create_user("alheio", password="x")
        ensure_personal_organization(outro)
        self._link(self._cupom("PRIMEIRO"))
        self.assertFalse(_tem_link_de_aviso(outro, "mercadolivre"))


class ProntidaoDeCodigoTests(TestCase):
    """O efeito no funil: o segundo cupom deixa de esperar o Link Builder."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("funil", password="x")
        ensure_personal_organization(cls.user)
        cls.fonte = FonteIngestao.objects.create(
            slug="ml-cupons-afiliados", marketplace="mercadolivre",
            nome="ML cupons", status="ok",
        )

    def _cupom(self, codigo):
        return CupomNormalizado.objects.create(
            fonte=self.fonte, external_id=f"ext-{codigo}", marketplace="mercadolivre",
            titulo=f"Cupom {codigo}", codigo=codigo, estado="ativo",
            redemption_mode="code",
            regras={"tipo_desconto": "porcentagem", "valor_desconto": 20,
                    "modo_resgate": "codigo"},
        )

    def test_segundo_cupom_fica_pronto_sem_link_proprio(self):
        from apps.scrapers.coupon_readiness import _codigo, conexao_ml

        primeiro = self._cupom("PRIMEIRO")
        LinkAfiliadoCupomUsuario.objects.create(
            usuario=self.user, cupom=primeiro,
            url_origem="https://www.mercadolivre.com.br/cupons",
            link_afiliado="https://mercadolivre.com/sec/abc",
            url_canonica="https://mercadolivre.com/sec/abc",
            afiliado_ok=True, verificado_ok=True, verificado_em=timezone.now(),
        )
        segundo = self._cupom("SEGUNDO")

        resultado = _codigo(segundo, self.user, conexao_ml(self.user))
        self.assertEqual(resultado["stage"], "ready",
                         "Sem link próprio, mas com link de aviso do usuário: pronto.")

    def test_sem_link_algum_continua_aguardando(self):
        from apps.scrapers.coupon_readiness import _codigo, conexao_ml

        cupom = self._cupom("SOZINHO")
        resultado = _codigo(cupom, self.user, conexao_ml(self.user))
        self.assertNotEqual(resultado["stage"], "ready")
