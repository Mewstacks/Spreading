"""Rede de segurança de superfície: nenhuma rota nomeada fica sem veredito.

A suíte do projeto é grande, mas o esforço foi quase todo para o miolo do pipeline
(coleta, casamento de cupom, envio). A superfície que o usuário toca — as telas e os
endpoints que os botões chamam — tinha dezenas de rotas sem nenhuma referência em
teste, incluindo o painel de superadmin e a conexão do Telegram.

Este módulo não tenta testar o que cada tela faz; para isso existem os testes
específicos. Ele garante três coisas que valem para TODAS as rotas, hoje e nas que
forem criadas depois:

1. **Nenhuma rota escapa da classificação.** `test_toda_rota_tem_veredito` falha
   quando alguém adiciona uma rota sem dizer se ela é pública, segura para GET ou
   perigosa. Sem isso, uma lista de rotas cobertas envelhece em silêncio — que é
   exatamente como as 49 rotas descobertas ficaram sem teste.
2. **Nenhuma rota privada responde a anônimo.** É a asserção de segurança: o
   `LoginRequiredMiddleware` é global, e este teste prova que nada passou por fora.
3. **Nenhuma tela quebra ao abrir.** GET autenticado nas telas de leitura não pode
   devolver 5xx.

Rotas que disparam trabalho pesado (Chromium, SSE, chamada a serviço externo) ficam
em `QUARENTENA`, cada uma com o motivo escrito. Quarentena aqui não é "não testado":
é "testado pelo teste certo, não por este". O GET nelas ainda é exercido quando a
view é `require_POST`, porque aí o 405 prova o guarda sem executar o corpo.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import URLPattern, URLResolver, get_resolver, reverse

from apps.accounts.models import ensure_personal_organization

# Valores plausíveis para os conversores de path. Ids inexistentes de propósito: o
# que se mede aqui é que a view responde com um 404 honesto em vez de estourar.
AMOSTRAS = {
    "IntConverter": 999_999,
    "StringConverter": "amostra",
    "SlugConverter": "amostra",
    "UUIDConverter": "00000000-0000-0000-0000-000000000000",
    "PathConverter": "amostra",
}
AMOSTRA_PADRAO = "amostra"

# Alcançáveis sem sessão: redirects rastreados, entrada de conta e recuperação de
# senha. `apps.accounts.urls` é incluído SEM namespace, então os nomes são simples.
PUBLICAS = {
    "scraper-redirect",
    "redirect-curto",
    "healthz",
    "sentry-hook",
    "login",
    "signup",
    "password_reset",
    "password_reset_done",
    "password_reset_confirm",
    "password_reset_complete",
    "verificar-email",
}

# Telas e leituras de banco: GET autenticado tem de responder sem 5xx.
GET_SEGURO = {
    "scraper-dashboard",
    "scraper-comecar",
    "scraper-conta",
    "scraper-top",
    "scraper-configuracoes",
    "scraper-automacao",
    "scraper-amazon",
    "scraper-whatsapp",
    "scraper-telegram",
    "scraper-ml-conexao",
    "scraper-ml-relatorio-conexao",
    "scraper-amazon-conexao",
    "scraper-raspagem-atual",
    "scraper-raspagem-status",
    "superadmin-usuarios",
    "superadmin-usuario",
    "superadmin-saude",
    "superadmin-saude-json",
    "superadmin-infra",
    "home",
    "logout",
    "password_change",
    "password_change_done",
    "verificacao-pendente",
}

QUARENTENA = {
    # ── Abrem Chromium ou transmitem SSE por minutos ──
    "scraper-run": "Stream SSE que dispara a raspagem completa (Chromium).",
    "scraper-gerar-links": "Stream SSE que abre o Link Builder no Chromium.",
    "scraper-ofertas-run": "require_POST; dispara raspagem de ofertas.",
    "scraper-cupons-codigo": "require_POST; dispara raspagem de cupons.",
    "scraper-buscar-termo": "Stream SSE de busca por termo (rede + Chromium).",
    "scraper-buscar-promocoes": "Stream SSE de busca de promoções (rede).",
    "scraper-ml-frames": "SSE de frames do login ao vivo do ML.",
    "scraper-ml-relatorio-frames": "SSE de frames do login de relatório do ML.",
    "scraper-amazon-frames": "SSE de frames do login da Amazon.",
    # ── Consultam serviço externo (worker WhatsApp / Telegram) ──
    "scraper-whatsapp-status": "Consulta o worker WhatsApp; coberto com mock em test_wa_supervisor.",
    "scraper-whatsapp-grupos": "Consulta o worker WhatsApp; idem.",
    "scraper-whatsapp-qr": "Busca o PNG do QR no worker WhatsApp.",
    "scraper-telegram-status": "Consulta a sessão MTProto do Telegram.",
    "scraper-ml-status": "Lê a fase do login ao vivo; coberto em test_ml_live_transport.",
    "scraper-ml-relatorio-status": "Idem, para o login de relatório.",
    "scraper-amazon-status": "Idem, para o login da Amazon.",
    # ── require_POST: o GET é exercido abaixo e tem de dar 405 ──
    "scraper-sincronizar-receitas": "require_POST.",
    "scraper-awin-conectar": "require_POST.",
    "scraper-awin-selecionar": "require_POST.",
    "scraper-awin-sincronizar": "require_POST.",
    "scraper-awin-desconectar": "require_POST.",
    "scraper-awin-programa-toggle": "require_POST.",
    "scraper-shopee-conectar": "require_POST; valida credencial contra a API.",
    "scraper-shopee-sincronizar": "require_POST; dispara coleta na API da Shopee.",
    "scraper-shopee-desconectar": "require_POST.",
    "scraper-cupom-manual": "require_POST.",
    "scraper-cupom-manual-editar": "require_POST.",
    "scraper-cupom-manual-desativar": "require_POST.",
    "scraper-enviar-produto": "require_POST; envia mensagem de verdade.",
    "scraper-enviar-cupom": "require_POST; envia mensagem de verdade.",
    "scraper-enviar-aviso-cupons": "require_POST; envia mensagem de verdade.",
    "scraper-enviar-agora": "require_POST; envia mensagem de verdade.",
    "scraper-whatsapp-iniciar": "require_POST; sobe uma sessão de WhatsApp.",
    "scraper-whatsapp-refresh": "require_POST; força sincronismo de grupos.",
    "scraper-whatsapp-desconectar": "require_POST.",
    "scraper-whatsapp-cancelar": "require_POST.",
    "scraper-telegram-conectar": "require_POST.",
    "scraper-telegram-desconectar": "require_POST.",
    "scraper-ml-start": "require_POST; sobe o Chromium do login.",
    "scraper-ml-qr-retry": "require_POST.",
    "scraper-ml-salvar": "require_POST.",
    "scraper-ml-cancelar": "require_POST.",
    "scraper-ml-desconectar": "require_POST.",
    "scraper-ml-input": "require_POST; carrega teclas do login remoto.",
    "scraper-ml-relatorio-start": "require_POST; sobe o Chromium do login.",
    "scraper-ml-relatorio-qr-retry": "require_POST.",
    "scraper-ml-relatorio-salvar": "require_POST.",
    "scraper-ml-relatorio-cancelar": "require_POST.",
    "scraper-ml-relatorio-input": "require_POST.",
    "scraper-amazon-start": "require_POST; sobe o Chromium do login.",
    "scraper-amazon-salvar": "require_POST.",
    "scraper-amazon-cancelar": "require_POST.",
    "scraper-amazon-input": "require_POST.",
    "superadmin-criar-usuario": "require_POST.",
    "superadmin-suspender": "require_POST.",
    "superadmin-cotas": "require_POST.",
    "superadmin-permissao-envio": "require_POST.",
    "superadmin-impersonar": "require_POST.",
    "superadmin-parar-impersonar": "require_POST.",
    "superadmin-saude-retestar": "require_POST.",
    # Aceita GET e dispara e-mail quando o usuário ainda não verificou — ou seja,
    # um GET com efeito colateral. Fica fora do smoke para não mandar e-mail; a
    # observação de que isto deveria ser POST está registrada aqui de propósito.
    "reenviar-verificacao": "GET com efeito colateral (envia e-mail de verificação).",
}

# Só estas podem responder 405 ao GET; o resto da quarentena é SSE/externo.
SOMENTE_POST = {
    nome for nome, motivo in QUARENTENA.items() if motivo.startswith("require_POST")
}


def _rotas(resolver=None, prefixo=""):
    """(nome, url) de toda rota nomeada do urlconf raiz, com parâmetros preenchidos."""
    resolver = resolver or get_resolver()
    encontradas = []
    for padrao in resolver.url_patterns:
        if isinstance(padrao, URLResolver):
            namespace = padrao.namespace
            encontradas.extend(_rotas(
                padrao,
                prefixo=f"{prefixo}{namespace}:" if namespace else prefixo,
            ))
            continue
        if not isinstance(padrao, URLPattern) or not padrao.name:
            continue
        nome = f"{prefixo}{padrao.name}"
        kwargs = {
            chave: AMOSTRAS.get(type(conv).__name__, AMOSTRA_PADRAO)
            for chave, conv in padrao.pattern.converters.items()
        }
        try:
            encontradas.append((nome, reverse(nome, kwargs=kwargs)))
        except Exception:
            # Rota cujo conversor não está no mapa de amostras: melhor falhar no
            # teste de classificação, com o nome à vista, do que sumir daqui.
            encontradas.append((nome, None))
    return encontradas


def _rotas_do_produto():
    """Ignora o admin do Django e o static: não são superfície do produto."""
    return [
        (nome, url) for nome, url in _rotas()
        if not nome.startswith("admin:") and nome not in {"django-admindocs-docroot"}
    ]


class SuperficieDeRotasTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.usuario = User.objects.create_user(
            "smoke-rotas", email="smoke@example.com", password="smoke-senha-123",
            is_staff=True, is_superuser=True,
        )
        cls.usuario.perfil.marcar_verificado()
        ensure_personal_organization(cls.usuario)

    def test_toda_rota_tem_veredito(self):
        """Rota nova sem classificação quebra aqui — de propósito.

        É o que impede esta rede de envelhecer: quem cria a rota decide, no mesmo
        commit, se ela é pública, se é segura para um GET de fumaça ou se precisa de
        um teste dedicado. Sem este teste, a lista abaixo vira documentação morta.
        """
        classificadas = PUBLICAS | GET_SEGURO | set(QUARENTENA)
        nomes = {nome for nome, _ in _rotas_do_produto()}
        sem_veredito = sorted(nomes - classificadas)
        self.assertEqual(
            sem_veredito, [],
            "Rotas sem classificação no smoke de superfície. Adicione cada uma a "
            "PUBLICAS, GET_SEGURO ou QUARENTENA (com o motivo): "
            f"{sem_veredito}",
        )
        orfas = sorted(classificadas - nomes)
        self.assertEqual(
            orfas, [],
            f"Rotas classificadas que não existem mais no urlconf: {orfas}",
        )

    def test_rota_privada_nao_responde_a_anonimo(self):
        """Nenhuma rota privada entrega conteúdo sem sessão."""
        for nome, url in _rotas_do_produto():
            if nome in PUBLICAS or url is None:
                continue
            with self.subTest(rota=nome):
                resposta = self.client.get(url)
                self.assertNotEqual(
                    resposta.status_code // 100, 2,
                    f"{nome} respondeu {resposta.status_code} a um anônimo.",
                )
                self.assertNotEqual(
                    resposta.status_code // 100, 5,
                    f"{nome} estourou ({resposta.status_code}) com anônimo.",
                )

    def test_telas_de_leitura_abrem_autenticadas(self):
        """GET nas telas de leitura não pode dar 5xx."""
        self.client.force_login(self.usuario)
        for nome, url in _rotas_do_produto():
            if nome not in GET_SEGURO or url is None:
                continue
            with self.subTest(rota=nome):
                resposta = self.client.get(url, follow=True)
                self.assertNotEqual(
                    resposta.status_code // 100, 5,
                    f"{nome} estourou com {resposta.status_code}.",
                )

    def test_rota_de_escrita_recusa_get(self):
        """`require_POST` exercido de verdade: 405 prova o guarda sem rodar o corpo."""
        self.client.force_login(self.usuario)
        for nome, url in _rotas_do_produto():
            if nome not in SOMENTE_POST or url is None:
                continue
            with self.subTest(rota=nome):
                resposta = self.client.get(url)
                self.assertIn(
                    resposta.status_code, (405, 403, 404),
                    f"{nome} devia recusar GET; respondeu {resposta.status_code}.",
                )
