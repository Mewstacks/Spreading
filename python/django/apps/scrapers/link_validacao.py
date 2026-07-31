"""Fonte ÚNICA de normalização e decisão de aprovação de links de oferta.

Motivo (regressão que originou este módulo): havia dois portões divergentes de
"esta oferta pode ser enviada?".

  - A listagem marcava `afiliado_pronto` só porque existia um link cacheado
    (`estado='pronto'`), sem nunca conferir o destino.
  - O envio, e SÓ ele, abria o link ao vivo (`verify_link`) e podia reprovar.

Um link de afiliado que redirecionava para a vitrine `/social/.../lists` do
afiliado (e não para o anúncio) passava como "pronto" na tela e só era reprovado
depois do clique em enviar — "[ERRO] link reprovado na verificação".

A correção centraliza AQUI:
  1. a classificação do destino (é a página do produto? é a vitrine social?);
  2. a decisão de aprovação a partir do relatório de verificação.

`verify_link` (Playwright), a geração de link (aprovação) e o envio passam a usar
EXATAMENTE esta mesma decisão, de modo que um item só é exibido como enviável
quando o mesmo critério que o envio aplicaria já o aprovou.
"""
import re

# Página de anúncio individual afiliável (não catálogo /up/MLBU nem vitrine).
_RE_ITEM_PRODUTO = re.compile(r"/MLB-?\d{6,}")


def normalizar_url(url: str) -> str:
    """Forma canônica estável de uma URL, para comparação e armazenamento.

    Conservadora de propósito: não reescreve host nem querystring (um link de
    afiliado depende dos parâmetros de atribuição). Só apara espaços e a barra
    final redundante, que são as divergências que geram falso "link diferente".
    """
    u = (url or "").strip()
    if not u:
        return ""
    # Remove só a barra final do path raiz/sem query, preservando o resto intacto.
    if u.endswith("/") and "?" not in u and "#" not in u:
        u = u[:-1]
    return u


def eh_pagina_produto(url: str) -> bool:
    """True se a URL é uma página de anúncio individual do Mercado Livre."""
    u = url or ""
    return (
        "produto.mercadolivre" in u
        or "/p/MLB" in u
        or bool(_RE_ITEM_PRODUTO.search(u))
    )


def eh_vitrine_social(url: str) -> bool:
    """True se a URL é uma vitrine/storefront `/social/` do afiliado.

    A vitrine é uma página de coleção do afiliado (ex.: `/social/<slug>/lists`),
    não o anúncio. Um link que cai aqui — e não na página do produto — não pode
    ser tratado como enviável.
    """
    return "/social/" in (url or "")


def aprovado_por_relatorio(relatorio: dict, confiar_desconto: bool) -> bool:
    """Decisão ÚNICA de aprovação a partir de um relatório de `verify_link`.

    Extraída de dentro de `verificar_link_afiliado` para ser reutilizada tanto na
    aprovação (geração do link) quanto na conferência do envio — sem uma segunda
    implementação que possa divergir.

    - `confiar_desconto=True` (ofertas com de/por confirmado na raspagem): basta
      destino afiliado válido (página do produto OU vitrine que destaca o item),
      nome batendo e anúncio ativo.
    - `confiar_desconto=False` (cupom): exige confirmar o desconto NA página do
      produto (não na vitrine).
    """
    erros = relatorio.get("erros") or []
    inativo = any("inativo" in e or "inexistente" in e for e in erros)
    nome_ok = relatorio.get("nome_confere") is not False  # None (não checado) ou True
    destino_valido = bool(
        relatorio.get("is_pagina_produto") or relatorio.get("is_landing_afiliado"))
    desconto_real = bool(
        relatorio.get("cupom_detectado") or relatorio.get("preco_riscado"))

    if confiar_desconto:
        return bool(destino_valido and nome_ok and not inativo)
    return bool(
        relatorio.get("is_pagina_produto")
        and relatorio.get("preco_visivel")
        and nome_ok
        and desconto_real
        and not inativo
    )


def motivo_reprovacao(relatorio: dict, confiar_desconto: bool) -> str:
    """Motivo técnico legível de por que o link NÃO foi aprovado.

    Serve para gravar em `LinkAfiliadoUsuario.verificacao_motivo` e mostrar na
    tela — o usuário vê "link inválido: <motivo>" em vez de descobrir só no envio.
    """
    if aprovado_por_relatorio(relatorio, confiar_desconto):
        return ""
    erros = relatorio.get("erros") or []
    if any("inativo" in e or "inexistente" in e for e in erros):
        return "O anúncio está pausado ou não existe mais."
    if relatorio.get("nome_confere") is False:
        if eh_vitrine_social(relatorio.get("url_final") or "") and not relatorio.get("is_pagina_produto"):
            return ("O link abre a vitrine do afiliado, não o anúncio do produto. "
                    "Gere o link novamente a partir da página do produto.")
        return "A página aberta pelo link não corresponde ao produto anunciado."
    if not (relatorio.get("is_pagina_produto") or relatorio.get("is_landing_afiliado")):
        return "O link não abre uma página de produto do Mercado Livre."
    if not confiar_desconto and not relatorio.get("preco_visivel"):
        return "Não foi possível confirmar o preço na página do produto."
    if not confiar_desconto and not (relatorio.get("cupom_detectado") or relatorio.get("preco_riscado")):
        return "Não foi possível confirmar o desconto/cupom na página do produto."
    if erros:
        return str(erros[0])[:280]
    return "O link não passou na verificação de destino."
