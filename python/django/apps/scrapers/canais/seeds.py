"""Canais públicos do Telegram usados como SINAL DE DESCOBERTA de ofertas.

Leia isto antes de ligar qualquer um: **canal-fonte não é fonte de verdade.** O que
um canal publica é a alegação de um terceiro sobre preço, estoque e desconto. Quem
opera o Spreading são influenciadores colocando a própria reputação em cada
mensagem — então nada daqui pode chegar ao grupo sem passar pela mesma verificação
que uma oferta raspada por nós (destino vivo, preço conferido no momento do envio,
desconto provado contra o histórico). O canal encurta a DESCOBERTA; ele não
substitui a prova.

Como esta lista foi montada (18/08/2026): abri a prévia pública `t.me/s/<canal>` de
cada candidato, contei quantos posts das 20 mensagens mais recentes carregavam link
de loja e classifiquei por marketplace. Canal sem prévia pública, sem link de loja
ou com nome que imita uma marca conhecida ficou de fora — dois casos concretos
recusados foram `@meliuz` e `@cuponomia`, que **não pertencem** às empresas de mesmo
nome e enganariam quem lesse a lista.

A densidade abaixo é do momento da medição e serve para ordenar, não como promessa.
Canal do Telegram muda de dono, vira privado e morre sem aviso — por isso o comando
`semear_canais` só sugere, nunca liga sozinho, e cada usuário decide o que monitorar.
"""

# (handle, rótulo, marketplaces observados, links de loja em 20 posts)
CANAIS_SUGERIDOS = [
    {
        "handle": "achadinhosdomercadolivre",
        "nome": "Achadinhos do Mercado Livre e da Shopee",
        "marketplaces": ["mercadolivre", "shopee"],
        "densidade": 40,
        "nota": "Maior densidade medida de link do ML; começa por aqui.",
    },
    {
        "handle": "ofertaslivre",
        "nome": "Ofertas / Mercado Livre",
        "marketplaces": ["mercadolivre"],
        "densidade": 36,
        "nota": "Só Mercado Livre, volume alto e constante.",
    },
    {
        "handle": "cupomdedesconto",
        "nome": "Clube de Ofertas",
        "marketplaces": ["mercadolivre"],
        "densidade": 34,
        "nota": "Fala de cupom no título; a densidade real medida é de oferta do ML.",
    },
    {
        "handle": "promocao",
        "nome": "Santostecpromo — Ofertas e cupom",
        "marketplaces": ["mercadolivre", "shopee"],
        "densidade": 36,
        "nota": "Mistura ML e Shopee; útil para cobrir as duas lojas de uma vez.",
    },
    {
        "handle": "promocoesdodia",
        "nome": "PromoTop",
        "marketplaces": ["mercadolivre", "amazon", "shopee"],
        "densidade": 34,
        "nota": "O mais diverso: única fonte medida com as três lojas juntas.",
    },
    {
        "handle": "Postou_Achou",
        "nome": "Postou Achou — Achadinhos e Promoções",
        "marketplaces": ["mercadolivre"],
        "densidade": 20,
        "nota": "Citado na imprensa de nicho como canal de achadinhos da Amazon; "
                "na medição os links eram de ML.",
    },
]

# Recusados de propósito, para que ninguém os re-adicione sem saber por quê.
RECUSADOS = {
    "meliuz": "Nome imita a marca Méliuz e o canal não é dela.",
    "cuponomia": "Nome imita a marca Cuponomia e o canal não é dela.",
    "promobit": "No Telegram este handle é um canal de hardware, não o site Promobit.",
    "hotpromo": "Praticamente sem post e sem link de loja na medição.",
    "promoup": "Sem prévia pública.",
}


def sugestoes_para(marketplace=""):
    """Sugestões, opcionalmente filtradas por loja, das mais densas para as menos."""
    alvo = str(marketplace or "").strip().lower()
    itens = [
        c for c in CANAIS_SUGERIDOS
        if not alvo or alvo in c["marketplaces"]
    ]
    return sorted(itens, key=lambda c: c["densidade"], reverse=True)
