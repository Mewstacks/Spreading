"""Regras de APRESENTAÇÃO da vitrine de promoções.

Separado do ranking de envio (`content_ranking`) de propósito: aqui não se decide o
que é uma boa oferta, só como o resultado já ranqueado é distribuído na tela.
"""
from collections import Counter


def contar_por_marketplace(itens) -> dict:
    """{marketplace: quantidade} sobre exatamente a lista que a tela vai paginar.

    Os contadores precisam sair do MESMO universo da listagem — janela de frescor,
    filtros e corte de afiliação já aplicados. Contar direto no banco produzia
    números que não reconciliavam com a tela e transformavam cada indicador numa
    pergunta ("por que diz 300 se só vejo 40?").
    """
    return dict(Counter(
        str(getattr(item, "marketplace", "") or "desconhecido") for item in itens
    ))


def equilibrar_primeira_pagina(itens, por_pagina, reserva=0.25):
    """Garante lugar na 1ª página para cada loja que tem item pronto.

    A ordenação é global (maior desconto primeiro) e o catálogo do Mercado Livre é
    muito maior que o da Amazon: bastava o ML ter os 20 maiores descontos para a
    Amazon só aparecer páginas adiante, mesmo com centenas de ofertas frescas — na
    prática, uma loja inteira invisível para quem não fuça a paginação.

    A reserva é um PISO, não uma cota fixa: quem já entrou por mérito não é
    removido, e a loja ausente entra com até `reserva` da página. Dentro de cada
    loja a ordem escolhida pelo usuário é preservada, e nenhum item é descartado —
    os deslocados vão para o começo da página seguinte.
    """
    itens = list(itens)
    por_pagina = max(1, int(por_pagina))
    if len(itens) <= por_pagina:
        return itens

    primeira = itens[:por_pagina]
    presentes = {getattr(item, "marketplace", "") for item in primeira}
    # Ordem de chegada no ranking global: a loja mais bem colocada entre as
    # ausentes é promovida primeiro.
    ausentes = []
    for item in itens[por_pagina:]:
        loja = getattr(item, "marketplace", "")
        if loja not in presentes and loja not in ausentes:
            ausentes.append(loja)
    if not ausentes:
        return itens

    cota = max(1, int(por_pagina * reserva))
    promovidos, ids_promovidos = [], set()
    for loja in ausentes:
        for item in itens:
            if getattr(item, "marketplace", "") != loja:
                continue
            promovidos.append(item)
            ids_promovidos.add(id(item))
            if len([p for p in promovidos
                    if getattr(p, "marketplace", "") == loja]) >= cota:
                break
    if not promovidos or len(promovidos) >= por_pagina:
        return itens

    restante = [item for item in itens if id(item) not in ids_promovidos]
    corte = por_pagina - len(promovidos)
    return restante[:corte] + promovidos + restante[corte:]
