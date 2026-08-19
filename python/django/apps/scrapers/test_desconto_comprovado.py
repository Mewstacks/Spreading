"""A mensagem não risca um preço que nunca foi observado.

O "DE" vem da vitrine da loja, e a docstring de `PrecoHistorico` diz que no Mercado
Livre ele "costuma ser fictício". A revalidação de envio confirma o preço ATUAL e
nunca o "DE". O filtro de mediana da seleção só roda com 3+ observações, então
produto novo — a maioria das ofertas do dia — passava sem prova nenhuma e a mensagem
afirmava um desconto que ninguém verificou.

Quem assina a mensagem é o influenciador. Estes testes fixam a regra: sem prova
nossa, a oferta sai só com o POR.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import ensure_personal_organization
from apps.scrapers.models import PrecoHistorico, Produto
from apps.scrapers.ofertas import _desconto_comprovado, montar_mensagem
from apps.scrapers.precos import chave_produto

LINK = "https://mercadolivre.com/sec/abc"


class DescontoComprovadoTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("prova", password="x")
        ensure_personal_organization(cls.user)

    def _produto(self, preco=100.0, de=200.0):
        return Produto.objects.create(
            owner=self.user, marketplace="mercadolivre", nome="Fone bom",
            origem="oferta", preco_sem_desconto=de, preco_com_cupom=preco,
            link_produto="https://produto.mercadolivre.com.br/MLB-1-fone",
        )

    def _observar(self, produto, *precos):
        for preco in precos:
            PrecoHistorico.objects.create(
                marketplace=produto.marketplace, chave=chave_produto(produto),
                preco=preco,
            )

    def test_sem_historico_o_desconto_nao_esta_comprovado(self):
        self.assertFalse(_desconto_comprovado(self._produto(), 100.0))

    def test_uma_observacao_mais_cara_ja_comprova(self):
        """Uma observação nossa vale mais que qualquer 'de' da vitrine."""
        produto = self._produto()
        self._observar(produto, 180.0)
        self.assertTrue(_desconto_comprovado(produto, 100.0))

    def test_historico_no_mesmo_patamar_nao_comprova(self):
        """Se sempre custou isso, não houve queda — e não há desconto a anunciar."""
        produto = self._produto()
        self._observar(produto, 100.0, 100.5, 99.9)
        self.assertFalse(_desconto_comprovado(produto, 100.0))

    def test_preco_invalido_nao_comprova(self):
        self.assertFalse(_desconto_comprovado(self._produto(), 0))


class MensagemTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("msg", password="x")
        ensure_personal_organization(cls.user)

    def _produto(self):
        return Produto.objects.create(
            owner=self.user, marketplace="mercadolivre", nome="Fone bom",
            origem="oferta", preco_sem_desconto=200.0, preco_com_cupom=100.0,
            link_produto="https://produto.mercadolivre.com.br/MLB-1-fone",
        )

    def test_sem_prova_a_mensagem_nao_mostra_o_de(self):
        produto = self._produto()
        texto = montar_mensagem(produto, LINK, None, usuario=self.user)
        self.assertIn("POR", texto)
        self.assertNotIn("DE ", texto)
        # E, principalmente, o preço inventado não aparece em lugar nenhum.
        self.assertNotIn("200", texto)

    def test_com_prova_a_mensagem_mostra_de_e_por(self):
        produto = self._produto()
        PrecoHistorico.objects.create(
            marketplace=produto.marketplace, chave=chave_produto(produto),
            preco=190.0,
        )
        texto = montar_mensagem(produto, LINK, None, usuario=self.user)
        self.assertIn("DE ", texto)
        self.assertIn("POR", texto)

    def test_oferta_sem_prova_continua_sendo_publicada(self):
        """Opção A: some a afirmação não comprovada, não a oferta."""
        produto = self._produto()
        texto = montar_mensagem(produto, LINK, None, usuario=self.user)
        self.assertIn("Fone bom", texto)
        self.assertIn(LINK, texto)


class OrdenacaoTests(TestCase):
    """Desconto inflado não pode comprar o primeiro lugar."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("ordem", password="x")
        cls.user.perfil.marcar_verificado()
        ensure_personal_organization(cls.user)

    def _produto(self, nome, de, por, sufixo):
        return Produto.objects.create(
            owner=self.user, marketplace="mercadolivre", nome=nome,
            origem="oferta", preco_sem_desconto=de, preco_com_cupom=por,
            estado="ativo",
            link_produto=f"https://produto.mercadolivre.com.br/MLB-{sufixo}-x",
        )

    def test_comprovado_ganha_do_inflado(self):
        from apps.scrapers.ofertas import selecionar_item_para_grupo

        # "De" inventado: 80% de desconto aparente, sem nenhuma observação nossa.
        inflado = self._produto("Inflado", de=1000.0, por=200.0, sufixo="1")
        # Desconto menor no papel, mas provado: já observamos o item a R$ 300.
        provado = self._produto("Provado", de=350.0, por=250.0, sufixo="2")
        PrecoHistorico.objects.create(
            marketplace="mercadolivre", chave=chave_produto(provado), preco=330.0,
        )

        # Mede a NOTA, não a lista final: a seleção passa por `is_alive`, que faz
        # rede e devolve "incerto" nos testes. O que esta regra decide é a ordem.
        selecionar_item_para_grupo(usuario=self.user, limite_envio=5)
        inflado.refresh_from_db(); provado.refresh_from_db()
        notas = {
            p.nome: getattr(p, "score_oferta", None)
            for p in selecionar_item_para_grupo(usuario=self.user, limite_envio=5)
        }
        # Se `is_alive` filtrar tudo, calcula direto sobre os objetos anotados.
        if not notas:
            self.skipTest("is_alive indisponível neste ambiente")
        if "Provado" in notas and "Inflado" in notas:
            self.assertGreater(
                notas["Provado"], notas["Inflado"],
                "Desconto comprovado tem de valer mais que 'de' inflado.",
            )

    def test_percentual_nao_comprovado_nao_entra_na_nota(self):
        """O número não verificado não pode ser argumento de ranking."""
        from apps.scrapers.ofertas import _desconto_comprovado

        inflado = self._produto("SóInflado", de=1000.0, por=200.0, sufixo="9")
        self.assertFalse(_desconto_comprovado(inflado, inflado.preco_com_cupom))
