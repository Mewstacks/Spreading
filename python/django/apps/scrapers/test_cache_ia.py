"""O cache de título por IA precisa realmente ser gravado.

Medido em produção em 20/08/2026: 148 de 47.554 produtos tinham `nome_llm`. A
política `tenant_update` de `scrapers_produto` é
``USING ((system) OR organization_id = <org>)`` — o catálogo compartilhado
(`organization_id IS NULL`) só é gravável em contexto de sistema. O envio roda no
contexto da organização, então o UPDATE casava zero linhas, o Django levantava
`Produto.NotUpdated` e o `except` engolia: cada mensagem pagava uma chamada nova
ao modelo para reescrever um título que o produto já tinha.

SQLite não tem RLS, então aqui o que dá para travar é o contrato: produto do
catálogo compartilhado é gravado DENTRO de `system_context`; produto de uma
organização não sai do escopo dela.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import organization_for_user
from apps.scrapers.models import Produto


class CacheIaTests(TestCase):
    def _produto(self, **extra):
        return Produto.objects.create(
            marketplace="mercadolivre", nome="Monitor Gamer TCL 25",
            link_produto="https://www.mercadolivre.com.br/monitor/p/MLB9",
            imagem_url="https://http2.mlstatic.com/m.jpg",
            preco_sem_desconto=2949.0, preco_com_cupom=1130.0,
            preco_efetivo=1130.0, estado="ativo", origem="oferta",
            fonte="mercadolivre-web", ultima_verificacao=timezone.now(), **extra)

    def test_catalogo_compartilhado_grava_e_persiste(self):
        from apps.scrapers.ofertas import _salvar_cache_ia
        produto = self._produto()
        self.assertIsNone(produto.organization_id)

        _salvar_cache_ia(produto, titulo="MONITOR PODEROSO",
                         nome_curto="Monitor Gamer TCL 25")

        produto.refresh_from_db()
        self.assertEqual(produto.frase_llm, "MONITOR PODEROSO")
        self.assertEqual(produto.nome_llm, "Monitor Gamer TCL 25")

    def test_catalogo_compartilhado_grava_dentro_do_contexto_de_sistema(self):
        from apps.scrapers.ofertas import _salvar_cache_ia
        produto = self._produto()
        dentro = []

        original = Produto.save

        def espiao(self, *args, **kwargs):
            from apps.accounts.tenant import in_system_context
            dentro.append(in_system_context())
            return original(self, *args, **kwargs)

        with patch.object(Produto, "save", espiao):
            _salvar_cache_ia(produto, titulo="T", nome_curto="N")

        self.assertEqual(dentro, [True],
                         "o catálogo público precisa ser gravado em system_context")

    def test_produto_de_organizacao_nao_abre_contexto_de_sistema(self):
        from apps.scrapers.ofertas import _salvar_cache_ia
        usuario = get_user_model().objects.create_user("dono", password="x")
        produto = self._produto(organization=organization_for_user(usuario))
        dentro = []

        original = Produto.save

        def espiao(self, *args, **kwargs):
            from apps.accounts.tenant import in_system_context
            dentro.append(in_system_context())
            return original(self, *args, **kwargs)

        with patch.object(Produto, "save", espiao):
            _salvar_cache_ia(produto, titulo="T", nome_curto="N")

        self.assertEqual(dentro, [False],
                         "produto de tenant não deve escapar do escopo dele")
        produto.refresh_from_db()
        self.assertEqual(produto.nome_llm, "N")

    def test_falha_de_escrita_nao_derruba_o_envio(self):
        from apps.scrapers.ofertas import _salvar_cache_ia
        produto = self._produto()

        with patch.object(Produto, "save", side_effect=Produto.NotUpdated("x")):
            _salvar_cache_ia(produto, titulo="T", nome_curto="N")

        produto.refresh_from_db()
        self.assertEqual(produto.nome_llm, "")

    def test_sem_mudanca_nao_escreve(self):
        from apps.scrapers.ofertas import _salvar_cache_ia
        produto = self._produto(frase_llm="T", nome_llm="N")

        with patch.object(Produto, "save") as save:
            _salvar_cache_ia(produto, titulo="T", nome_curto="N")

        save.assert_not_called()
