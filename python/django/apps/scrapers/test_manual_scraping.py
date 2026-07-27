import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import OperationalError, connection
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone

from apps.accounts.models import ensure_personal_organization
from apps.accounts.tenant import system_context
from apps.scrapers.manual_scraping import (
    JobReporter, _db_retry, claim_next_job, criar_execucao, executar_job,
)
from apps.scrapers.models import EventoOperacional, ExecucaoRaspagem


class ManualScrapingApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "staff-scrape", password="x", is_staff=True,
        )
        self.organization = ensure_personal_organization(self.user)
        self.user.perfil.marcar_verificado()
        self.client.force_login(self.user)

    @patch("apps.scrapers.automacao_state.spawn_worker")
    def test_post_enfileira_e_get_antigo_nao_executa(self, spawn):
        response = self.client.post("/scrapers/ofertas/")

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["tipo"], "ofertas")
        self.assertEqual(payload["status"], "queued")
        spawn.assert_called_once_with("scrape")
        self.assertEqual(self.client.get("/scrapers/ofertas/").status_code, 405)

    def test_disparo_exige_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)

        self.assertEqual(csrf_client.post("/scrapers/ofertas/").status_code, 403)

    @patch("apps.scrapers.automacao_state.spawn_worker")
    def test_segundo_clique_reutiliza_job_ativo(self, _spawn):
        first = self.client.post("/scrapers/cupons-codigo/").json()
        second = self.client.post("/scrapers/cupons-codigo/")

        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["id"], first["id"])
        self.assertTrue(second.json()["reutilizada"])
        self.assertEqual(ExecucaoRaspagem.objects.count(), 1)

    @patch("apps.scrapers.automacao_state.spawn_worker")
    def test_outro_tipo_retorna_conflito_e_job_existente(self, _spawn):
        first = self.client.post("/scrapers/ofertas/").json()
        second = self.client.post("/scrapers/cupons-codigo/")

        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["id"], first["id"])

    @patch("apps.scrapers.automacao_state.spawn_worker")
    def test_status_e_isolado_por_organizacao(self, _spawn):
        job_id = self.client.post("/scrapers/ofertas/").json()["id"]
        other = get_user_model().objects.create_user(
            "other-staff", password="x", is_staff=True,
        )
        ensure_personal_organization(other)
        other.perfil.marcar_verificado()
        self.client.force_login(other)

        response = self.client.get(f"/scrapers/raspagens/{job_id}/status/")

        self.assertEqual(response.status_code, 404)


class ManualScrapingQueueTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "queue-owner", password="x", is_staff=True,
        )
        self.organization = ensure_personal_organization(self.user)

    def _job(self, **overrides):
        values = {
            "organization": self.organization,
            "solicitada_por": self.user,
            "tipo": "ofertas",
        }
        values.update(overrides)
        return ExecucaoRaspagem.objects.create(**values)

    def test_retry_de_banco_reabre_fora_de_atomic(self):
        calls = []

        def operation():
            calls.append(connection.in_atomic_block)
            if len(calls) == 1:
                raise OperationalError("the connection is closed")
            return "ok"

        with patch("apps.scrapers.manual_scraping.connections.close_all") as close:
            self.assertEqual(_db_retry(operation), "ok")

        self.assertEqual(calls, [False, False])
        self.assertGreaterEqual(close.call_count, 3)

    def test_reporter_persiste_quando_chamado_dentro_de_event_loop(self):
        job = self._job(status="running", tentativas=1)
        reporter = JobReporter(job.pk)

        async def emitir_dentro_do_loop():
            with system_context():
                reporter.step("Coletando promoções", 12)

        asyncio.run(emitir_dentro_do_loop())
        job.refresh_from_db()

        self.assertEqual(job.etapa, "Coletando promoções")
        self.assertEqual(job.progresso, 12)
        self.assertTrue(
            job.eventos.filter(mensagem="Coletando promoções").exists()
        )

    def test_job_interrompido_e_retomado_uma_vez(self):
        job = self._job(
            status="running", tentativas=1,
            heartbeat_em=timezone.now() - timedelta(minutes=3),
        )

        claimed = claim_next_job()

        self.assertEqual(claimed.pk, job.pk)
        self.assertEqual(claimed.status, "running")
        self.assertEqual(claimed.tentativas, 2)
        self.assertTrue(claimed.eventos.filter(nivel="warning").exists())

    def test_segunda_interrupcao_falha_definitivamente(self):
        job = self._job(
            status="running", tentativas=2,
            heartbeat_em=timezone.now() - timedelta(minutes=3),
        )

        self.assertIsNone(claim_next_job())
        job.refresh_from_db()

        self.assertEqual(job.status, "failed")
        self.assertEqual(job.codigo_erro, "worker_interrupted")
        self.assertEqual(job.etapa, "Retomada após interrupção")
        self.assertTrue(job.erro_publico)

    @patch("apps.scrapers.manual_scraping._gerar_links", return_value=(3, 0))
    @patch(
        "apps.scrapers.scraper_mercadolivre.ofertas_scraper.mapear_ofertas",
        return_value=7,
    )
    def test_ofertas_concluem_com_contagens(self, _mapear, _links):
        job = self._job(status="running", tentativas=1)

        executar_job(job)
        job.refresh_from_db()

        self.assertEqual(job.status, "succeeded")
        self.assertEqual(job.progresso, 100)
        self.assertEqual(job.contagens["ofertas"], 7)
        self.assertEqual(job.contagens["links_gerados"], 3)

    @patch("apps.scrapers.manual_scraping._gerar_links", return_value=(2, 1))
    @patch(
        "apps.scrapers.scraper_mercadolivre.ofertas_scraper.mapear_ofertas",
        return_value=5,
    )
    def test_falha_de_enriquecimento_resulta_em_parcial(self, _mapear, _links):
        job = self._job(status="running", tentativas=1)

        executar_job(job)
        job.refresh_from_db()

        self.assertEqual(job.status, "partial")
        self.assertEqual(job.contagens["ofertas"], 5)
        self.assertEqual(job.contagens["links_falhos"], 1)

    @patch(
        "apps.scrapers.manual_scraping._gerar_links",
        side_effect=RuntimeError("browser de afiliação indisponível"),
    )
    @patch(
        "apps.scrapers.scraper_mercadolivre.ofertas_scraper.mapear_ofertas",
        return_value=5,
    )
    def test_excecao_em_links_preserva_ofertas_como_parcial(
        self, _mapear, _links,
    ):
        job = self._job(status="running", tentativas=1)

        executar_job(job)
        job.refresh_from_db()

        self.assertEqual(job.status, "partial")
        self.assertEqual(job.contagens["ofertas"], 5)
        self.assertTrue(job.eventos.filter(nivel="warning").exists())

    @patch(
        "apps.scrapers.scraper_mercadolivre.ofertas_scraper.mapear_ofertas",
        side_effect=RuntimeError("falha técnica controlada"),
    )
    def test_falha_total_aparece_na_saude_da_conta(self, _mapear):
        job = self._job(status="running", tentativas=1)

        executar_job(job)
        job.refresh_from_db()

        evento = EventoOperacional.objects.get(
            evento="scrape_erro", usuario=self.user,
        )
        self.assertEqual(job.status, "failed")
        self.assertEqual(evento.contexto["tipo_raspagem"], "ofertas")
        self.assertEqual(evento.contexto["codigo"], "unknown")

    @patch("apps.scrapers.manual_scraping._gerar_links", return_value=(0, 0))
    @patch("apps.scrapers.sources.persistence.persist_items",
           return_value={"offers": 0, "coupons": 4})
    @patch("apps.scrapers.sources.run_source",
           return_value={"status": "ok", "coupons": [object()]})
    @patch("apps.scrapers.coupon_products.preparar_lote",
           return_value={"processados": 4, "prontos": 3})
    @patch(
        "apps.scrapers.scraper_mercadolivre.scraper.projetar_catalogo_cupons",
        return_value=2,
    )
    @patch(
        "apps.scrapers.scraper_mercadolivre.cupons_codigo_scraper.mapear_cupons_codigo",
        side_effect=RuntimeError("selector mudou"),
    )
    @patch(
        "apps.scrapers.scraper_mercadolivre.scraper.mapear_cupons",
        return_value=6,
    )
    @patch(
        "apps.scrapers.conexoes.estado_ml",
        return_value=SimpleNamespace(conectado=True),
    )
    def test_cupons_preservam_sucessos_quando_uma_etapa_falha(
        self, *_patches,
    ):
        job = self._job(tipo="cupons", status="running", tentativas=1)

        executar_job(job)
        job.refresh_from_db()

        self.assertEqual(job.status, "partial")
        self.assertEqual(job.contagens["campanhas"], 6)
        self.assertEqual(job.contagens["cupons_oficiais"], 4)
        self.assertTrue(job.eventos.filter(nivel="warning").exists())

    @patch("apps.scrapers.manual_scraping._gerar_links", return_value=(1, 0))
    @patch(
        "apps.scrapers.sources.run_source",
        side_effect=TimeoutError("fonte oficial demorou"),
    )
    @patch(
        "apps.scrapers.coupon_products.preparar_lote",
        return_value={"processados": 3, "prontos": 2},
    )
    @patch(
        "apps.scrapers.scraper_mercadolivre.scraper.projetar_catalogo_cupons",
        return_value=2,
    )
    @patch(
        "apps.scrapers.scraper_mercadolivre.cupons_codigo_scraper.mapear_cupons_codigo",
        return_value=3,
    )
    @patch(
        "apps.scrapers.scraper_mercadolivre.scraper.mapear_cupons",
        return_value=6,
    )
    @patch(
        "apps.scrapers.conexoes.estado_ml",
        return_value=SimpleNamespace(conectado=True),
    )
    def test_fonte_oficial_isolada_preserva_outros_cupons(self, *_patches):
        job = self._job(tipo="cupons", status="running", tentativas=1)

        executar_job(job)
        job.refresh_from_db()

        self.assertEqual(job.status, "partial")
        self.assertEqual(job.contagens["campanhas"], 6)
        self.assertEqual(job.contagens["produtos_checkout"], 3)
        self.assertTrue(
            job.eventos.filter(
                nivel="warning", mensagem__startswith="Fonte oficial:",
            ).exists()
        )

    @patch(
        "apps.scrapers.conexoes.estado_ml",
        return_value=SimpleNamespace(conectado=False),
    )
    def test_cupom_sem_sessao_retorna_acao_especifica(self, _estado):
        job = self._job(tipo="cupons", status="running", tentativas=1)

        executar_job(job)
        job.refresh_from_db()

        self.assertEqual(job.status, "failed")
        self.assertEqual(job.codigo_erro, "session_expired")
        self.assertEqual(job.etapa, "Validando conexão")
        self.assertIn("Reconecte", job.acao_recomendada)
