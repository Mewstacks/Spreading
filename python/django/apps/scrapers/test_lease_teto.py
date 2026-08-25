"""O navegador tem de voltar para a fila mesmo quando o dono trava.

Incidente de 25/08/2026: o worker `scrape` pendurou dentro do Playwright às 10:14
segurando `django_chromium`. A thread de heartbeat do lease continuou renovando
`expires_at` por oito horas, o TTL de 90s nunca disparou e links, cupons,
verificação e envio passaram o dia lendo "navegador ocupado por outra tarefa".

O que estes testes fixam: heartbeat prova que o processo respira, não que o
trabalho anda — então quem PEDE o recurso é que aplica o teto.
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.scrapers import resource_control as rc
from apps.scrapers.models import ResourceLease

CHAVE = "django_chromium"


def _lease_ocupado(*, owner_kind="scrape", ha_segundos=60, esperando_ha=None,
                   lane="scheduled"):
    """Cria um lease com heartbeat VIVO (expires_at no futuro), como em produção."""
    agora = timezone.now()
    campos = {
        "owner_token": "token-do-dono",
        "owner_kind": owner_kind,
        "acquired_at": agora - timedelta(seconds=ha_segundos),
        "heartbeat_at": agora,
        "expires_at": agora + timedelta(seconds=rc.LEASE_TTL_SECONDS),
    }
    if esperando_ha is not None:
        campos[f"{lane}_waiting_since"] = agora - timedelta(seconds=esperando_ha)
    return ResourceLease.objects.create(resource_key=CHAVE, **campos)


class TetoDePosseDoLeaseTests(TestCase):
    def test_heartbeat_vivo_nao_basta_para_segurar_o_recurso_para_sempre(self):
        # O caso do incidente: dono vivo, renovando, parado há horas.
        _lease_ocupado(owner_kind="scrape",
                       ha_segundos=rc.TETO_DE_POSSE_LOTE_LONGO_SEGUNDOS + 60)

        token, detalhe = rc.acquire(CHAVE, owner_kind="scheduled")

        self.assertTrue(token, f"o recurso continuou preso: {detalhe}")
        lease = ResourceLease.objects.get(resource_key=CHAVE)
        self.assertEqual(lease.owner_kind, "scheduled")

    def test_lote_longo_saudavel_nao_e_interrompido(self):
        # 40 páginas do ML levam ~62min; expropriar antes disso mata trabalho bom.
        _lease_ocupado(owner_kind="scrape", ha_segundos=30 * 60)

        token, detalhe = rc.acquire(CHAVE, owner_kind="scheduled")

        self.assertIsNone(token)
        self.assertEqual(detalhe["owner_kind"], "scrape")

    def test_esteira_curta_tem_teto_menor_que_o_lote_longo(self):
        _lease_ocupado(owner_kind="links",
                       ha_segundos=rc.TETO_DE_POSSE_SEGUNDOS + 60)

        token, _ = rc.acquire(CHAVE, owner_kind="scheduled")

        self.assertTrue(token)

    def test_dono_que_nao_cede_com_fila_esperando_perde_a_vez(self):
        # Um dono saudável cede em ~1 página quando alguém entra na fila. Dez
        # minutos de fila contra o MESMO dono significam que ele não cede: travou.
        _lease_ocupado(owner_kind="scrape", ha_segundos=20 * 60,
                       esperando_ha=rc.CESSAO_MAX_SEGUNDOS + 30)

        token, _ = rc.acquire(CHAVE, owner_kind="scheduled")

        self.assertTrue(token)

    def test_fila_curta_nao_derruba_dono_que_ainda_esta_trabalhando(self):
        _lease_ocupado(owner_kind="scrape", ha_segundos=20 * 60, esperando_ha=60)

        token, _ = rc.acquire(CHAVE, owner_kind="scheduled")

        self.assertIsNone(token)

    def test_marcador_de_espera_herdado_nao_derruba_dono_recente(self):
        # O marcador só é limpo quando a lane dele consegue o recurso. Sem esta
        # guarda, uma espera antiga expropriaria quem acabou de entrar.
        _lease_ocupado(owner_kind="scrape", ha_segundos=30,
                       esperando_ha=6 * 60 * 60)

        token, _ = rc.acquire(CHAVE, owner_kind="scheduled")

        self.assertIsNone(token)

    def test_dono_expropriado_nao_reassume_o_lease_ao_terminar(self):
        # O zombie pode "acordar" horas depois. `release` confere o token, então
        # ele não pode apagar o dono novo.
        _lease_ocupado(owner_kind="scrape",
                       ha_segundos=rc.TETO_DE_POSSE_LOTE_LONGO_SEGUNDOS + 60)
        novo_token, _ = rc.acquire(CHAVE, owner_kind="scheduled")

        self.assertFalse(rc.release(CHAVE, "token-do-dono"))
        lease = ResourceLease.objects.get(resource_key=CHAVE)
        self.assertEqual(lease.owner_token, novo_token)

    def test_lease_livre_continua_sendo_entregue_normalmente(self):
        token, detalhe = rc.acquire(CHAVE, owner_kind="links")

        self.assertTrue(token)
        self.assertEqual(detalhe["owner_kind"], "links")
        self.assertTrue(rc.release(CHAVE, token))

    def test_raspagem_manual_longa_conserva_o_teto_generoso(self):
        # Raspagem manual de cupons já levou 56min em produção.
        _lease_ocupado(owner_kind="manual", ha_segundos=50 * 60)

        token, _ = rc.acquire(CHAVE, owner_kind="scheduled")

        self.assertIsNone(token)


class DerrubadaDoNavegadorTravadoTests(TestCase):
    """Expropriar o lease no Postgres não basta: o flock só cai com o processo.

    `machine_resource_slot` é um flock em /tmp e o sistema só o libera quando o
    dono MORRE. Em produção o worker travado seguia VIVO — `/proc/locks` apontava
    o FLOCK para o PID dele. Derrubar só o navegador desbloqueia a chamada síncrona
    sem matar o worker (e sem o honcho levar a VM inteira junto).
    """

    def test_so_o_recurso_de_navegador_derruba_processo(self):
        # Lease de sessão é exclusão por credencial, não o Chromium da máquina.
        self.assertEqual(rc._derrubar_navegador_travado("ml_site_session:abc"), 0)

    def test_a_varredura_de_proc_ignora_o_que_nao_e_navegador(self):
        # O próprio processo de teste não tem Playwright/Chromium abaixo dele.
        self.assertEqual(rc._descendentes_de_navegador(-1), [])

    def test_encerrar_por_teto_nunca_levanta(self):
        # É chamado de dentro da thread de heartbeat: levantar ali mataria o
        # pulso e deixaria o lease vivo justamente no caso que ele deve resolver.
        rc._encerrar_por_teto("django_chromium", "teste")
        rc._encerrar_por_teto("ml_site_session:abc", "teste")
