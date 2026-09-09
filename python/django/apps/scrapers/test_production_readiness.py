from contextlib import nullcontext
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from apps.scrapers.models import ConfiguracaoEnvio, Produto
from apps.scrapers.production_readiness import _taxonomia_catalogo, avaliar


class ProducaoReadinessTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("lules", password="x")
        self.config = ConfiguracaoEnvio.objects.create(
            owner=self.user, grupo_id="1203630@g.us", grupo_nome="Achados da Lu",
            macro_categoria="Eletrodomésticos", ativo=True,
        )
        Produto.objects.create(
            owner=None, marketplace="mercadolivre", nome="Aspirador robô",
            origem="oferta", estado="ativo", link_produto="https://example.com/MLB1",
            preco_sem_desconto=100, preco_com_cupom=80,
            macro_categoria="Eletrodomésticos", ultima_observacao=timezone.now(),
        )

    def _cobertura(self, *, cupom=True, aprovado=True):
        return {
            "aprovado": aprovado,
            "regras": [{
                "config_id": self.config.pk, "com_cupom": int(cupom),
                "deficit_cupom": 0 if cupom else 3,
            }],
        }

    def test_taxonomia_mede_pool_elegivel_nao_catalogo_bruto(self):
        """Produto fora do mínimo não pode reprovar o gate de candidatos."""
        Produto.objects.create(
            owner=None, marketplace="mercadolivre", nome="Produto sem desconto",
            origem="oferta", estado="ativo", link_produto="https://example.com/MLB2",
            preco_sem_desconto=100, preco_com_cupom=96, macro_categoria="",
            ultima_observacao=timezone.now(),
        )

        metrica = _taxonomia_catalogo(self.user)

        self.assertEqual(metrica["total"], 1)
        self.assertEqual(metrica["classificados"], 1)
        self.assertEqual(metrica["percentual"], 100.0)

    @patch("apps.scrapers.automacao_state.worker_alive", return_value=True)
    @patch("apps.scrapers.production_readiness._destinos", return_value=("-100123", []))
    @patch("apps.scrapers.deal_abundance.relatorio_cobertura")
    def test_aprova_quando_todos_os_gates_tem_evidencia(self, cobertura, _destinos, _lane):
        cobertura.return_value = self._cobertura()

        resultado = avaliar(self.user)

        self.assertTrue(resultado["aprovado"])
        self.assertTrue(all(resultado["gates"].values()))

    @patch("apps.scrapers.automacao_state.worker_alive", return_value=True)
    @patch("apps.scrapers.production_readiness._destinos", return_value=("", []))
    @patch("apps.scrapers.deal_abundance.relatorio_cobertura")
    def test_reprova_alerta_ausente_e_cupom_faltante(self, cobertura, _destinos, _lane):
        cobertura.return_value = self._cobertura(cupom=False, aprovado=False)

        resultado = avaliar(self.user)

        self.assertFalse(resultado["aprovado"])
        self.assertFalse(resultado["gates"]["alerta_configurado"])
        self.assertFalse(resultado["gates"]["nichos_lu_com_cupom"])

    @patch("apps.scrapers.management.commands.prontidao_producao.system_context",
           return_value=nullcontext())
    @patch("apps.scrapers.management.commands.prontidao_producao.avaliar")
    def test_comando_retorna_erro_quando_algum_gate_reprova(
        self, avaliar_mock, _contexto
    ):
        avaliar_mock.return_value = {
            "aprovado": False, "gates": {"alerta_configurado": False},
            "taxonomia": {"classificados": 0, "total": 0, "percentual": 0},
            "minimo_taxonomia": 95.0, "alerta": {"telegram": False, "email": False},
            "esteiras": {}, "config_ids_prioritarios": [],
        }

        with self.assertRaises(CommandError):
            call_command("prontidao_producao", username="lules", stdout=StringIO())
