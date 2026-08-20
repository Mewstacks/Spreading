"""Envio "incerto" é falta de resposta, não veredito — e o ledger costuma ter a resposta.

O worker WhatsApp tem orçamento próprio de 55s para o `sendMessage`. Estourou,
devolve "incerto". Medido em produção em 20/08/2026: 13 de 129 tentativas em dois
dias, todas com `duration_ms` colado em 55.000 — e `uncertain` era estado terminal,
então a linha ficava em limbo para sempre. A hipótese que o próprio código sugeria
(foto grande estourando o upload) não se sustentou: a mediana de `foto_bytes` dos
incertos era MENOR que a dos enviados com sucesso.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.scrapers.models import Produto, Publicacao
from apps.scrapers.send_pipeline import reconciliar_incertos, transition


class ReconciliacaoTests(TestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_user("dono", password="x")
        self.produto = Produto.objects.create(
            marketplace="mercadolivre", nome="Air Fryer Philco 15l",
            link_produto="https://www.mercadolivre.com.br/af/p/MLB7",
            imagem_url="https://http2.mlstatic.com/a.jpg",
            preco_sem_desconto=699.0, preco_com_cupom=399.0, preco_efetivo=399.0,
            estado="ativo", origem="oferta", fonte="mercadolivre-web",
            ultima_verificacao=timezone.now())

    def _incerta(self):
        pub = Publicacao.objects.create(
            usuario=self.usuario, origem="produto", produto=self.produto,
            canal="whatsapp", destino_id="g@g.us", destino_nome="Grupo",
            preco_original=699.0, preco_final=399.0, status="pendente",
        )
        Publicacao.objects.filter(pk=pub.pk).update(
            stage="uncertain", status="incerto")
        pub.refresh_from_db()
        return pub

    def test_ledger_confirmado_vira_enviado(self):
        pub = self._incerta()

        resumo = reconciliar_incertos(consulta=lambda s, k: {
            "encontrado": True, "fase": "confirmed",
            "resultado": {"sucesso": True, "mensagem_id": "true_123"},
        })

        pub.refresh_from_db()
        self.assertEqual(resumo["confirmadas"], 1)
        self.assertEqual(pub.status, "enviado")
        self.assertEqual(pub.stage, "confirmed")
        self.assertIsNotNone(pub.enviada_em)

    def test_ledger_sem_prova_nao_mexe(self):
        pub = self._incerta()

        for resposta in (
            {"encontrado": False},
            {"encontrado": True, "fase": "transport_started"},
            {"encontrado": True, "fase": "confirmed", "resultado": {"sucesso": True}},
            {"encontrado": True, "fase": "confirmed",
             "resultado": {"sucesso": False, "mensagem_id": "x"}},
        ):
            resumo = reconciliar_incertos(consulta=lambda s, k, r=resposta: r)
            pub.refresh_from_db()
            self.assertEqual(resumo["confirmadas"], 0, resposta)
            self.assertEqual(pub.status, "incerto", resposta)

    def test_ledger_indisponivel_nao_quebra(self):
        pub = self._incerta()

        def explode(session, key):
            raise RuntimeError("ledger fora do ar")

        resumo = reconciliar_incertos(consulta=explode)

        pub.refresh_from_db()
        self.assertEqual(resumo, {"consultadas": 1, "confirmadas": 0})
        self.assertEqual(pub.status, "incerto")

    def test_fora_da_janela_nao_e_reaberto(self):
        from apps.scrapers.send_pipeline import RECONCILIACAO_JANELA
        pub = self._incerta()
        antiga = timezone.now() - RECONCILIACAO_JANELA - timezone.timedelta(hours=1)
        Publicacao.objects.filter(pk=pub.pk).update(criada_em=antiga)

        resumo = reconciliar_incertos(consulta=lambda s, k: {
            "encontrado": True, "fase": "confirmed",
            "resultado": {"sucesso": True, "mensagem_id": "x"}})

        pub.refresh_from_db()
        self.assertEqual(resumo["consultadas"], 0)
        self.assertEqual(pub.status, "incerto")

    def test_confirmado_continua_final(self):
        pub = self._incerta()
        transition(pub, "confirmed", status="enviado",
                   enviada_em=timezone.now(), transport_state="confirmed")
        pub.refresh_from_db()

        with self.assertRaises(ValueError):
            transition(pub, "uncertain", status="incerto")

    def test_incerto_nao_vira_falha_permanente(self):
        pub = self._incerta()

        with self.assertRaises(ValueError):
            transition(pub, "permanent_failed", status="falhou")
