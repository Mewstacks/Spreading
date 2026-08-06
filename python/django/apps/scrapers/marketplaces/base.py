"""Contrato de marketplace. ML é a 1ª impl; Amazon/Shopee plugam depois sem mexer aqui."""
from abc import ABC, abstractmethod


class MarketplaceIndisponivel(Exception):
    """A loja não pôde ser consultada, e o motivo é explicável ao usuário.

    Distingue "esta loja não tem o que você procura" (0 itens, sucesso) de "esta
    loja nem foi consultada" (conta desconectada, sem elegibilidade, fora do ar).
    A mensagem é escrita para aparecer na tela, então não carrega stack nem jargão.
    """


class Marketplace(ABC):
    slug: str = ""

    @abstractmethod
    def scrape_all(self, termos=None) -> None:
        """
        Raspa TODAS as fontes de ofertas desta loja e persiste Produtos com
        marketplace=self.slug. `termos`: lista de strings de busca (sub-nichos ativos).
        Cada loja decide internamente quantas fontes/páginas usar.
        """

    def scrape_para_usuario(self, usuario, termos=None) -> int:
        """Raspa as ofertas desta loja para UM usuário. Devolve quantos itens entraram.

        Existe separado de `scrape_all` por causa de quem dispara: `scrape_all` é do
        worker de fundo e percorre todos os tenants, o que uma raspagem pedida por
        uma pessoa na tela não pode fazer. Aqui a coleta é sempre no escopo de quem
        clicou.

        Default 0 = "esta loja não tem coleta por usuário" (Awin vive de feed). Quem
        não puder consultar agora levanta MarketplaceIndisponivel com um motivo
        legível: quem clicou precisa saber a diferença entre "não achei oferta" e
        "sua conta não está conectada".
        """
        return 0

    @abstractmethod
    def build_affiliate_link(self, produto, usuario=None) -> dict | None:
        """
        Gera (ou usa cache) o link de afiliado do produto. Retorna dict com pelo menos
        {'link_afiliado': str, 'afiliado_ok': bool, 'url_isca': str} ou None se falhar.
        `usuario` != None: link/tag DAQUELE usuário (multi-tenant); None = global (compat).
        """

    @abstractmethod
    def verify_affiliate_tag(self, link: str, usuario=None) -> bool:
        """True se o link final carrega a tag de afiliado (do usuário, ou global) — A3."""

    def can_affiliate(self, produto, usuario=None) -> bool:
        """
        Este item comissionaria para ESTE usuário se fosse publicado agora? Predicado
        de LEITURA: sem rede e sem escrita. Cada loja resolve a atribuição de um jeito
        (ML: sessão do Link Builder; Amazon: tag do Perfil), por isso a regra mora aqui
        e não na view. Para uma lista inteira use preparar_exibicao.
        """
        return bool(getattr(produto, "afiliado_ok", False))

    def preparar_exibicao(self, produtos, usuario=None) -> None:
        """Resolve em LOTE o que a listagem mostra por item (hoje: afiliado_pronto).

        A listagem chama isto UMA vez por página e por loja, não uma vez por item.
        Default: pergunta item a item. Loja cujo can_affiliate toca o banco deve
        sobrescrever, senão a página vira uma query por produto.

        `afiliado_pronto` é atributo só de exibição: `afiliado_ok` é campo persistido
        e não se escreve num GET.
        """
        for p in produtos:
            p.afiliado_pronto = self.can_affiliate(p, usuario)

    def verify_link(self, link: str, nome_esperado: str = None,
                    confiar_desconto: bool = False, usuario=None) -> dict:
        """
        Confere no destino que a oferta certa aparece, ativa, com desconto.
        Default: sem verificação de browser (ok=True) — lojas com API confiável
        (Amazon/Shopee) podem nem precisar. ML faz override com Playwright.
        """
        return {"ok": True}

    def is_alive(self, produto):
        """
        Estado do anúncio: True (vivo) | False (confirmado morto) | None (incerto).
        None NUNCA deve apagar o produto. Default None (loja sem checagem rápida).
        """
        return None

    def buscar_por_termo(self, termo_busca: str, min_desconto: int = 15, macro=None,
                         usuario=None) -> int:
        """
        Busca direcionada por sub-nicho (termos separados por vírgula) -> persiste
        Produtos origem='busca'. Usado pelo botão de busca por config. Default no-op
        (loja sem busca por termo).
        """
        return 0

    def prefetch_links(self, produtos, usuario=None, faixa=None):
        """Pré-gera links em lote. Default: loop sobre build_affiliate_link.

        `faixa` (ini, fim) é opcional e só interessa a quem reporta progresso numa
        barra compartilhada com outras etapas; lojas sem progresso ignoram.
        """
        gerados = falhas = 0
        for p in produtos:
            try:
                if self.build_affiliate_link(p, usuario=usuario):
                    gerados += 1
                else:
                    falhas += 1
            except Exception:
                falhas += 1
        return (gerados, falhas)
