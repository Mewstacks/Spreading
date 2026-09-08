"""A fila v2 tem de ser legível: tentativa, prazo e lease vencido.

Três colunas foram criadas para contar esta história — `transport_state`,
`attempt_count`, `next_retry_at` — e nenhuma tela lia nenhuma delas. Enquanto isso,
"pendente" cobria tanto o item que nasceu agora quanto o que já queimou três
tentativas e está com o prazo do worker vencido. Os dois pareciam iguais.
"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import ensure_personal_organization
from apps.scrapers.models import Publicacao
from apps.scrapers.send_pipeline import LEASE_V2_MIN, estado_da_fila


class EstadoDaFilaTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("lules", password="x")
        self.org = ensure_personal_organization(self.user)

    def _publicar(self, **campos):
        base = dict(
            usuario=self.user, organization=self.org, canal="whatsapp",
            destino_id="123@g.us", destino_nome="Achadinhos",
            status="pendente", stage="transport_queued",
            transport_state="queued_v2",
        )
        base.update(campos)
        return Publicacao.objects.create(**base)

    def test_fila_vazia_nao_inventa_numero(self):
        resumo = estado_da_fila()
        self.assertEqual(resumo["total"], 0)
        self.assertEqual(resumo["itens"], [])
        self.assertEqual(resumo["atrasadas"], 0)
        self.assertEqual(resumo["presas"], 0)
        self.assertFalse(resumo["truncada"])

    def test_conta_so_o_que_esta_na_fila(self):
        agora = timezone.now()
        self._publicar(next_retry_at=agora + timedelta(minutes=5))
        # Já entregue: saiu da fila e não pode voltar a aparecer nela.
        self._publicar(status="enviado", stage="confirmed",
                       transport_state="confirmed")
        # Falha terminal: também fora — não há o que esperar.
        self._publicar(status="falhou", stage="permanent_failed",
                       transport_state="failed")

        self.assertEqual(estado_da_fila(agora=agora)["total"], 1)

    def test_tentativa_queimada_aparece_com_o_teto(self):
        agora = timezone.now()
        self._publicar(attempt_count=2, transport_state="retry_wait",
                       next_retry_at=agora + timedelta(minutes=3))

        item = estado_da_fila(agora=agora)["itens"][0]

        self.assertEqual(item["tentativa"], 2)
        self.assertGreaterEqual(item["teto"], 1)
        self.assertFalse(item["atrasada"])
        self.assertEqual(estado_da_fila(agora=agora)["reentregas"], 1)

    def test_prazo_no_passado_e_atraso_observado_nao_estimativa(self):
        agora = timezone.now()
        self._publicar(transport_state="retry_wait",
                       next_retry_at=agora - timedelta(minutes=1))

        resumo = estado_da_fila(agora=agora)

        self.assertEqual(resumo["atrasadas"], 1)
        self.assertTrue(resumo["itens"][0]["atrasada"])
        self.assertFalse(resumo["itens"][0]["presa"])

    def test_lease_vencido_e_o_caso_que_pede_coveiro(self):
        # `_claim_next_batch` marca `processing_v2` e guarda o prazo do lease em
        # `next_retry_at`. Prazo vencido nesse estado significa que ninguém está
        # processando a linha — e é diferente de "esperando o próximo retry".
        agora = timezone.now()
        self._publicar(
            transport_state="processing_v2",
            next_retry_at=agora - timedelta(minutes=LEASE_V2_MIN + 1),
        )

        resumo = estado_da_fila(agora=agora)

        self.assertEqual(resumo["presas"], 1)
        self.assertTrue(resumo["itens"][0]["presa"])

    def test_lease_ainda_dentro_do_prazo_nao_e_presa(self):
        agora = timezone.now()
        self._publicar(
            transport_state="processing_v2",
            next_retry_at=agora + timedelta(minutes=1),
        )

        resumo = estado_da_fila(agora=agora)

        self.assertEqual(resumo["presas"], 0)
        self.assertFalse(resumo["itens"][0]["presa"])

    def test_a_lista_e_cortada_e_diz_que_foi(self):
        agora = timezone.now()
        for _ in range(14):
            self._publicar(next_retry_at=agora + timedelta(minutes=1))

        resumo = estado_da_fila(agora=agora, limite=12)

        self.assertEqual(resumo["total"], 14)
        self.assertEqual(len(resumo["itens"]), 12)
        self.assertTrue(resumo["truncada"])

    def test_a_fila_de_outra_conta_nao_aparece(self):
        agora = timezone.now()
        outra = User.objects.create_user("outro", password="x")
        outra_org = ensure_personal_organization(outra)
        self._publicar(next_retry_at=agora)
        self._publicar(usuario=outra, organization=outra_org,
                       next_retry_at=agora)

        self.assertEqual(estado_da_fila(usuario=self.user, agora=agora)["total"], 1)

    def test_o_mais_urgente_vem_primeiro(self):
        agora = timezone.now()
        self._publicar(destino_nome="Depois",
                       next_retry_at=agora + timedelta(minutes=10))
        self._publicar(destino_nome="Agora",
                       next_retry_at=agora - timedelta(minutes=10))

        itens = estado_da_fila(agora=agora)["itens"]

        self.assertEqual(itens[0]["destino"], "Agora")


class CoberturaPorNichoNaSaudeTests(TestCase):
    """A pergunta que decide o aceite tinha módulo e não tinha tela.

    "Cada grupo tem oferta suficiente do nicho dele?" é o critério de entrega —
    cobertura por nicho, não contagem por marketplace. `deal_abundance` já
    respondia, e o único leitor era um management command: só dava para saber por
    SSH. Mesma situação em que a fila v2 estava.
    """

    def setUp(self):
        self.user = User.objects.create_user("cobertura", password="x")
        ensure_personal_organization(self.user)

    def test_o_resumo_da_saude_publica_a_cobertura(self):
        from apps.scrapers.saude import resumo

        r = resumo(horas=24, usuario=self.user)

        self.assertIn("cobertura", r)
        self.assertIn("regras", r["cobertura"])
        self.assertIn("meta", r["cobertura"])

    def test_falha_na_cobertura_nao_derruba_a_saude(self):
        """Uma linha de painel não pode tirar a tela inteira do ar."""
        from unittest.mock import patch

        from apps.scrapers.saude import resumo

        with patch("apps.scrapers.deal_abundance.relatorio_cobertura",
                   side_effect=RuntimeError("fonte fora")):
            r = resumo(horas=24, usuario=self.user)

        self.assertTrue(r["cobertura"].get("indisponivel"))
        self.assertEqual(r["cobertura"]["regras"], [])
        self.assertIn("estado", r)
