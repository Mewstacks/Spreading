"""
Mercado Livre — wrapper sobre o código existente (scrapers + link.py).
Sem mudança de comportamento: só expõe o que já existe pelo contrato Marketplace.
"""
import logging

from apps.scrapers.marketplaces.base import Marketplace

logger = logging.getLogger(__name__)


class MercadoLivre(Marketplace):
    slug = "mercadolivre"

    def scrape_all(self, termos=None) -> None:
        from apps.scrapers.scraper_mercadolivre.ofertas_scraper import (
            mapear_ofertas, buscar_por_termo,
        )
        from apps.scrapers.scraper_mercadolivre.cupons_codigo_scraper import mapear_cupons_codigo
        from apps.scrapers.scraper_mercadolivre.scraper import mapear_cupons, projetar_catalogo_cupons

        from django.utils import timezone
        from apps.scrapers.eventos import log_event
        from apps.scrapers.models import FonteIngestao, ExecucaoIngestao
        fonte, _ = FonteIngestao.objects.get_or_create(
            slug="mercadolivre-web", defaults={
                "marketplace": "mercadolivre", "nome": "Mercado Livre — páginas públicas"})
        run = ExecucaoIngestao.objects.create(fonte=fonte)
        fonte.ultima_tentativa = timezone.now()
        fonte.save(update_fields=["ultima_tentativa"])
        try:
            ofertas = mapear_ofertas(max_paginas=40)
            cupons_codigo = mapear_cupons_codigo()
            # Cupons de campanha (tabela Cupom). Estava fora do loop automático: só
            # rodava no clique manual da tela de Scraper, então em produção a tabela
            # ficava vazia -- e link.py aborta a geração de link quando o produto tem
            # campanha_id sem Cupom correspondente. Cupom faltando virava link pendente.
            #
            # Isolado do resto: o parser depende de um JSON embutido num bundle do ML
            # (__NORDIC_RENDERING_CTX__), que é a peça mais frágil daqui. Se ele
            # quebrar, as ofertas e os códigos ainda entram.
            try:
                cupons_campanha = mapear_cupons()
            except Exception as e:
                logger.warning("Raspagem de cupons de campanha ML falhou: %s", e)
                log_event("scraper", "cupons_campanha_erro",
                          f"Não foi possível raspar os cupons de campanha do ML: {e}",
                          level="warning", contexto={"marketplace": "mercadolivre"}, exc=e)
                cupons_campanha = 0
            # A aba "Cupons" do site lê só o CupomNormalizado. Projeta a tabela Cupom
            # (campanhas) para o catálogo, lendo o banco — vale mesmo quando a raspagem
            # deste ciclo veio vazia (anti-wipe preserva as campanhas anteriores).
            try:
                projetar_catalogo_cupons()
            except Exception as e:
                logger.warning("Projeção do catálogo de cupons ML falhou: %s", e)
                log_event("scraper", "cupons_projecao_erro",
                          "Não foi possível publicar os cupons no catálogo.",
                          level="warning", contexto={"marketplace": "mercadolivre"}, exc=e)
            # Códigos realmente divulgáveis vêm da fonte oficial de afiliados.
            # Campanhas personalizadas/tokenizadas permanecem internas e não entram
            # no total exibido como "cupons encontrados".
            from apps.scrapers.sources import run_source
            from apps.scrapers.sources.persistence import persist_items
            oficiais = run_source("ml-cupons-afiliados")
            persistidos = persist_items(oficiais.get("coupons", []))
            cupons_oficiais = persistidos["coupons"]
            try:
                from apps.scrapers.coupon_products import preparar_lote
                preparar_lote(limite=max(12, cupons_oficiais))
            except Exception:
                logger.exception("Preparação dos cupons oficiais ML falhou")
            cupons = cupons_oficiais
            for t in (termos or []):
                try:
                    buscar_por_termo(t)
                except Exception as e:
                    logger.warning("Busca ML '%s' falhou: %s", t, e)
        except Exception:
            now = timezone.now()
            run.status, run.finalizada_em = "error", now
            run.erro_publico = "Falha temporária na coleta; dados anteriores preservados."
            run.save(update_fields=["status", "finalizada_em", "erro_publico"])
            fonte.status, fonte.erro_publico = "degraded", run.erro_publico
            fonte.falhas_consecutivas += 1
            fonte.save(update_fields=["status", "erro_publico", "falhas_consecutivas"])
            raise
        total = ofertas + cupons
        now = timezone.now()
        run.status = "ok" if total else "empty"
        run.total_ofertas, run.total_cupons = ofertas, cupons
        run.finalizada_em = now
        run.save()
        fonte.ultimo_total = total
        fonte.status = "ok" if total else "degraded"
        fonte.erro_publico = "" if total else "Coleta vazia; catálogo anterior preservado."
        if total:
            fonte.ultimo_sucesso, fonte.falhas_consecutivas = now, 0
        fonte.save()
        logger.info(
            "Raspagem ML: %s oferta(s), %s produto(s) na vitrine de cupons, "
            "%s código(s) público(s), %s campanha(s) personalizada(s) interna(s)",
            ofertas, cupons_codigo, cupons, cupons_campanha)
        # Trazer 800 ofertas e ZERO cupons era reportado como sucesso: o único sinal
        # era o total zerado, e as ofertas sozinhas o mantinham positivo. Foi assim
        # que os cupons puderam sumir sem ninguém notar.
        if ofertas and not cupons:
            log_event("scraper", "cupons_vazios",
                      f"A raspagem trouxe {ofertas} oferta(s) e nenhum cupom.",
                      level="warning", contexto={"marketplace": "mercadolivre",
                                                 "ofertas": ofertas})

    def build_affiliate_link(self, produto, usuario=None):
        from apps.scrapers.scraper_mercadolivre.link import gerar_link_afiliado_para_produto
        return gerar_link_afiliado_para_produto(produto, usuario=usuario)

    def verify_affiliate_tag(self, link, usuario=None):
        from apps.scrapers.scraper_mercadolivre.link import link_tem_tag_afiliado
        return link_tem_tag_afiliado(link, usuario=usuario)

    def can_affiliate(self, produto, usuario=None) -> bool:
        # A identidade vem da conta autenticada no Link Builder, não de uma tag: ter o
        # link pré-gerado é a única evidência de atribuição disponível sem rede.
        #
        # O link mora em LinkAfiliadoUsuario (cada usuário afilia com a conta dele).
        # Este predicado lia só o Produto.link_afiliado global e por isso mostrava
        # "pendente" em item que o usuário já tinha afiliado. O campo global segue
        # como fallback pelos itens gerados antes do multi-tenant.
        from apps.scrapers.afiliado import link_cacheado
        cacheado = link_cacheado(usuario, produto)
        if cacheado and cacheado.link_afiliado:
            return True
        return bool(getattr(produto, "link_afiliado", ""))

    def preparar_exibicao(self, produtos, usuario=None) -> None:
        from apps.scrapers.afiliado import situacao_dos_links

        # UMA query para a página toda, e dela sai tudo: quem tem link e, para quem
        # não tem, POR QUE não tem. Marcar tudo de "pendente" era desonesto — um item
        # que o Programa de Afiliados nunca vai aceitar não está numa fila, e o
        # usuário ficava olhando uma pilha esperando link que jamais viria.
        situacao = situacao_dos_links(usuario, produtos)
        for p in produtos:
            info = situacao.get(p.id) or {}
            tem_link_usuario = bool(info.get("link_afiliado"))
            verificado = info.get("verificado_ok") if info else None
            # O link_afiliado do Produto é o fallback legado (pré-multi-tenant): não
            # há linha por usuário, então mantém o comportamento antigo (enviável).
            legado = (not info) and bool(getattr(p, "link_afiliado", ""))

            # ENVIÁVEL só quando o destino já foi aprovado (verificado_ok is True).
            # Ter um link cacheado NÃO basta: era exatamente isso que deixava um link
            # que caía na vitrine /social/ aparecer como enviável e só reprovar no
            # clique de enviar.
            p.afiliado_pronto = bool(
                (tem_link_usuario and verificado is True) or legado)

            if p.afiliado_pronto:
                p.afiliado_estado, p.afiliado_motivo = "pronto", ""
            elif tem_link_usuario and verificado is False:
                # Link existe mas o destino foi reprovado: mostra o motivo técnico
                # e NÃO oferece envio.
                p.afiliado_estado = "link_invalido"
                p.afiliado_motivo = info.get("verificacao_motivo") or info.get("ultimo_erro") or ""
            elif tem_link_usuario:
                # Link gerado, aguardando a conferência de destino (verificado_ok=None).
                p.afiliado_estado, p.afiliado_motivo = "verificando", ""
            elif info:
                p.afiliado_estado = info["estado"]
                p.afiliado_motivo = info["ultimo_erro"]
            else:
                # Nunca tentado ainda — está mesmo na fila.
                p.afiliado_estado, p.afiliado_motivo = "pendente", ""

    def verify_link(self, link, nome_esperado=None, confiar_desconto=False, usuario=None):
        from apps.scrapers.scraper_mercadolivre.link import verificar_link_afiliado
        return verificar_link_afiliado(link, nome_esperado=nome_esperado,
                                       confiar_desconto=confiar_desconto,
                                       usuario=usuario)

    def is_alive(self, produto):
        from apps.scrapers.ofertas import esta_vivo
        return esta_vivo(produto)

    def buscar_por_termo(self, termo_busca, min_desconto=15, macro=None, usuario=None):
        # ML = pool COMPARTILHADO (owner=None p/ todos). Ignora usuario de propósito.
        from apps.scrapers.scraper_mercadolivre.ofertas_scraper import buscar_por_termo
        return buscar_por_termo(termo_busca, min_desconto=min_desconto, macro=macro)

    def prefetch_links(self, produtos, usuario=None, faixa=None):
        """Pré-gera links em lote (uma sessão Playwright). Retorna (gerados, falhas).

        O pool ML é compartilhado, mas a SESSÃO é por usuário: `usuario` diz qual
        auth_{id}.json abrir. Sem ele, link.py resolve a sessão disponível.
        """
        from apps.scrapers.scraper_mercadolivre.link import gerar_links_em_lote
        return gerar_links_em_lote(produtos, usuario=usuario, faixa=faixa)
