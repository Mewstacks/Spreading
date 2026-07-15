"""Contrato de marketplace. ML é a 1ª impl; Amazon/Shopee plugam depois sem mexer aqui."""
from abc import ABC, abstractmethod


class Marketplace(ABC):
    slug: str = ""

    @abstractmethod
    def scrape_all(self, termos=None) -> None:
        """
        Raspa TODAS as fontes de ofertas desta loja e persiste Produtos com
        marketplace=self.slug. `termos`: lista de strings de busca (sub-nichos ativos).
        Cada loja decide internamente quantas fontes/páginas usar.
        """

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
        de LEITURA: sem rede e sem escrita — a listagem chama isto por item, num GET.
        Cada loja resolve a atribuição de um jeito (ML: sessão do Link Builder;
        Amazon: tag do Perfil), por isso a regra mora aqui e não na view.
        """
        return bool(getattr(produto, "afiliado_ok", False))

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

    def prefetch_links(self, produtos, usuario=None):
        """Pré-gera links em lote. Default: loop sobre build_affiliate_link."""
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
