"""O alerta que faltava: incidente que abre precisa chegar numa pessoa.

Cada teste aqui corresponde a uma forma conhecida de o alerta virar inútil — ou por
não tocar quando devia, ou por tocar tanto que se aprende a ignorá-lo.
"""
from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.scrapers.alertas import notificar_incidente
from apps.scrapers.models import IncidenteSaude


def _incidente(**extra):
    dados = {
        "chave": "chave-teste",
        "causa": "envio_parado",
        "pipeline": "publicacao",
        "escopo": "sistema",
        "level": "error",
        "ultima_mensagem": "Ciclo de envio falhou.",
        "primeira_ocorrencia": timezone.now(),
        "ultima_ocorrencia": timezone.now(),
    }
    dados.update(extra)
    return IncidenteSaude.objects.create(**dados)


@override_settings(ALERTA_TELEGRAM_CHAT_ID="", ALERTA_EMAILS="operacao@example.com",
                   ALERTA_SILENCIO_MIN=60,
                   EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class AlertaDeIncidenteTests(TestCase):
    def setUp(self):
        cache.clear()
        mail.outbox.clear()

    def test_incidente_novo_de_erro_avisa(self):
        self.assertTrue(notificar_incidente(_incidente(), criado=True))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("envio_parado", mail.outbox[0].body)

    def test_ocorrencia_repetida_nao_avisa_de_novo(self):
        """400 ocorrências do mesmo problema são UM alerta, não 400."""
        incidente = _incidente()
        notificar_incidente(incidente, criado=True)
        mail.outbox.clear()
        notificar_incidente(incidente, criado=False, reaberto=False)
        self.assertEqual(mail.outbox, [])

    def test_mesma_chave_fica_em_silencio_dentro_da_janela(self):
        incidente = _incidente()
        notificar_incidente(incidente, criado=True)
        mail.outbox.clear()
        # Reabertura logo em seguida: é evento novo, mas a chave ainda está calada.
        notificar_incidente(incidente, criado=False, reaberto=True)
        self.assertEqual(mail.outbox, [])

    def test_reabertura_depois_do_silencio_avisa(self):
        incidente = _incidente()
        notificar_incidente(incidente, criado=True)
        mail.outbox.clear()
        cache.clear()  # simula a janela de silêncio vencendo
        self.assertTrue(notificar_incidente(incidente, criado=False, reaberto=True))
        self.assertEqual(len(mail.outbox), 1)

    def test_warning_nao_acorda_ninguem(self):
        """`warning` vive na tela de Saúde; só `error` interrompe alguém."""
        self.assertFalse(
            notificar_incidente(_incidente(level="warning"), criado=True))
        self.assertEqual(mail.outbox, [])

    def test_chaves_diferentes_alertam_separadamente(self):
        notificar_incidente(_incidente(chave="a"), criado=True)
        notificar_incidente(_incidente(chave="b", causa="sessao_wa"), criado=True)
        self.assertEqual(len(mail.outbox), 2)

    def test_falha_no_transporte_nao_derruba_o_fluxo(self):
        """Isto roda dentro do caminho de log de erro; não pode levantar nada."""
        with patch("apps.scrapers.alertas._enviar_email",
                   side_effect=RuntimeError("smtp fora")):
            self.assertFalse(notificar_incidente(_incidente(), criado=True))

    def test_falha_no_transporte_libera_o_silencio_para_a_proxima(self):
        """Telegram fora do ar não pode calar o incidente pela janela inteira."""
        incidente = _incidente()
        with patch("apps.scrapers.alertas._enviar_email",
                   side_effect=RuntimeError("smtp fora")):
            notificar_incidente(incidente, criado=True)
        # Sem transporte na primeira tentativa, a segunda ainda deve tentar.
        self.assertTrue(notificar_incidente(incidente, criado=True))
        self.assertEqual(len(mail.outbox), 1)


@override_settings(ALERTA_TELEGRAM_CHAT_ID="", ALERTA_EMAILS="",
                   EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class CanalDesligadoTests(TestCase):
    def setUp(self):
        cache.clear()
        mail.outbox.clear()

    def test_sem_canal_configurado_nao_quebra(self):
        self.assertFalse(notificar_incidente(_incidente(), criado=True))
        self.assertEqual(mail.outbox, [])


@override_settings(ALERTA_TELEGRAM_CHAT_ID="123", TELEGRAM_BOT_TOKEN="token",
                   ALERTA_EMAILS="", ALERTA_SILENCIO_MIN=60)
class TelegramTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_usa_telegram_quando_configurado(self):
        with patch("apps.scrapers.alertas._enviar_telegram",
                   return_value=True) as envio:
            self.assertTrue(notificar_incidente(_incidente(), criado=True))
        self.assertEqual(envio.call_count, 1)
        texto = envio.call_args[0][1]
        self.assertIn("incidente aberto", texto)
        self.assertIn("envio_parado", texto)


class ProjecaoDispararAlertaTests(TestCase):
    """O gancho está no lugar certo: projetar um evento de erro alerta."""

    def setUp(self):
        cache.clear()

    @override_settings(ALERTA_EMAILS="operacao@example.com",
                       ALERTA_TELEGRAM_CHAT_ID="",
                       EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_evento_de_erro_projeta_incidente_e_alerta(self):
        from apps.scrapers.eventos import log_event

        mail.outbox.clear()
        log_event("publicacao", "tick_erro", "Ciclo de envio falhou.", level="error")
        self.assertTrue(IncidenteSaude.objects.filter(status="aberto").exists())
        self.assertEqual(len(mail.outbox), 1)
