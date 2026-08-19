"""Shopee — a primeira loja do Spreading cujo link de afiliado não custa navegador.

Contraste com o Mercado Livre, que é o motivo desta classe existir do jeito que
existe: lá, cada link nasce do Link Builder dentro de um Chromium, o slot é único
por máquina e o custo cresce com ``produtos × usuários``. Aqui é uma chamada HTTP
assinada, então ``prefetch_links`` pode processar um lote inteiro sem pedir o
recurso mais disputado da VM.

Consequências diretas no contrato de ``Marketplace``:

  - ``verify_link`` fica no padrão (ok=True). A base já previa isso — "lojas com API
    confiável podem nem precisar". O destino vem da própria Shopee com a comissão
    embutida; não há o que reconferir abrindo a página.
  - ``verificar_links_pendentes`` não tem veredito próprio a dar, pelo mesmo motivo.
  - ``is_alive`` devolve None: sem consulta barata de estoque, "incerto" é honesto e
    nunca apaga produto do catálogo.
"""
import logging

from apps.scrapers.marketplaces.base import Marketplace, MarketplaceIndisponivel

logger = logging.getLogger(__name__)


def _integracao_do_usuario(usuario):
    from apps.scrapers.models import IntegracaoAfiliado

    if usuario is None:
        return None
    return IntegracaoAfiliado.objects.filter(
        owner=usuario, provedor=Shopee.slug, habilitada=True,
    ).first()


def _sub_ids(usuario, produto):
    """Rastreio por origem. Vai como ``subIds`` no encurtador da Shopee.

    Sem isso, todas as vendas de todos os tenants chegam ao relatório num balde só
    e a conciliação por cliente vira adivinhação. Os valores são identificadores
    internos, nunca e-mail ou nome.
    """
    sub = [f"u{getattr(usuario, 'id', '')}", "spreading"]
    marketplace_id = str(getattr(produto, "id", "") or "")
    if marketplace_id:
        sub.append(f"p{marketplace_id}")
    return [s for s in sub if s and s != "u"]


class Shopee(Marketplace):
    slug = "shopee"

    def scrape_all(self, termos=None):
        """A coleta é por adaptador de fonte (sources/shopee_*), não aqui.

        Os adaptadores rodam por organização, com a credencial daquela conta, e já
        gravam ``Produto``/``CupomNormalizado`` pelo caminho comum de persistência.
        Duplicar a coleta aqui criaria uma segunda porta de entrada com regras
        próprias — foi assim que o Mercado Livre acumulou três caminhos divergentes.
        """
        return None

    def scrape_para_usuario(self, usuario, termos=None):
        from apps.scrapers.shopee import ShopeeError
        from apps.scrapers.sources import run_source

        integracao = _integracao_do_usuario(usuario)
        if not integracao:
            raise MarketplaceIndisponivel(
                "Conecte sua conta de afiliado da Shopee para buscar ofertas."
            )
        try:
            payload = run_source(
                "shopee-offers", owner=usuario, termos=termos,
            )
        except ShopeeError as exc:
            raise MarketplaceIndisponivel(exc.public_message) from exc
        return len(payload.get("offers") or [])

    def build_affiliate_link(self, produto, usuario=None, activation_key=""):
        from apps.scrapers.shopee import (
            ShopeeError, credenciais_da_integracao, gerar_link,
        )

        destino = str(getattr(produto, "link_produto", "") or "").strip()
        if not destino:
            return None
        integracao = _integracao_do_usuario(usuario)
        if not integracao:
            logger.info(
                "Shopee sem integração conectada para o usuário %s.",
                getattr(usuario, "id", "?"),
            )
            return None
        try:
            app_id, secret = credenciais_da_integracao(integracao)
            link = gerar_link(
                destino, app_id=app_id, secret=secret,
                sub_ids=_sub_ids(usuario, produto),
            )
        except ShopeeError as exc:
            # Falha de rede/limite não é "produto não afiliável": devolver None faz
            # o chamador tratar como transitório e tentar de novo, sem marcar o item.
            logger.warning("Shopee não gerou link para o produto %s: %s",
                           getattr(produto, "pk", "?"), exc.public_message)
            return None
        return {"link_afiliado": link, "afiliado_ok": True, "url_isca": destino}

    def verify_affiliate_tag(self, link, usuario=None):
        """O encurtador oficial É a prova de atribuição.

        Só o domínio de link curto da Shopee carrega a comissão; um link de produto
        cru publicado por engano renderia zero, e é justamente esse o caso que este
        predicado tem de pegar.
        """
        texto = str(link or "").strip().lower()
        return texto.startswith(("https://s.shopee.com.br/", "https://shope.ee/"))

    def can_affiliate(self, produto, usuario=None):
        return bool(_integracao_do_usuario(usuario))

    def preparar_exibicao(self, produtos, usuario=None):
        """Resolve a integração UMA vez por página, não uma vez por item."""
        pronto = bool(_integracao_do_usuario(usuario))
        for produto in produtos:
            produto.afiliado_pronto = pronto

    def prefetch_links(self, produtos, usuario=None, faixa=None, activation_keys=None):
        """Lote inteiro sem pedir o navegador — a diferença que a API compra.

        O lote do Mercado Livre precisa ceder o Chromium no meio do caminho quando
        um login interativo aparece (ver `link.gerar_links_em_lote`). Aqui não há o
        que ceder, então o único motivo para parar é a Shopee pedir ritmo menor.
        """
        from apps.scrapers.afiliado import registrar_falha, salvar_cache
        from apps.scrapers.shopee import (
            ShopeeError, credenciais_da_integracao, gerar_link,
        )

        integracao = _integracao_do_usuario(usuario)
        if not integracao:
            return (0, 0)
        try:
            app_id, secret = credenciais_da_integracao(integracao)
        except ShopeeError:
            return (0, 0)

        gerados = falhas = 0
        for produto in produtos:
            destino = str(getattr(produto, "link_produto", "") or "").strip()
            if not destino:
                registrar_falha(usuario, produto,
                                "O produto não tem link de origem na Shopee.",
                                terminal=True)
                falhas += 1
                continue
            try:
                link = gerar_link(destino, app_id=app_id, secret=secret,
                                  sub_ids=_sub_ids(usuario, produto))
            except ShopeeError as exc:
                registrar_falha(usuario, produto, exc.public_message,
                                terminal=not exc.retryable)
                falhas += 1
                if exc.retryable:
                    # Limite de taxa: insistir nos itens seguintes só aprofunda o
                    # bloqueio. O restante do lote volta no próximo ciclo.
                    logger.info("Shopee pediu ritmo menor; lote interrompido em %s "
                                "gerado(s).", gerados)
                    break
                continue
            salvar_cache(
                usuario, produto, link, destino, True,
                verificado_ok=True, url_canonica=destino,
                verificacao_motivo="link curto oficial da Shopee",
            )
            gerados += 1
        return (gerados, falhas)
