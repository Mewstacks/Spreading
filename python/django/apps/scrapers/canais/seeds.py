"""Canais públicos do Telegram usados como SINAL DE DESCOBERTA de ofertas.

Leia isto antes de ligar qualquer um: **canal-fonte não é fonte de verdade.** O que
um canal publica é a alegação de um terceiro sobre preço, estoque e desconto. Quem
opera o Spreading são influenciadores colocando a própria reputação em cada
mensagem — então nada daqui pode chegar ao grupo sem passar pela mesma verificação
que uma oferta raspada por nós (destino vivo, preço conferido no momento do envio,
desconto provado contra o histórico). O canal encurta a DESCOBERTA; ele não
substitui a prova.

Como esta lista foi montada (28/08/2026): abri a prévia pública `t.me/s/<canal>` de
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
        "handle": "cupombr",
        "nome": "Cupom BR — Mercado Livre",
        "marketplaces": ["mercadolivre"],
        "densidade": 95,
        "nota": "31 códigos estruturados observados em 21 mensagens públicas.",
    },
    {
        "handle": "cupom_shopee",
        "nome": "Cupom Shopee",
        "marketplaces": ["shopee"],
        "densidade": 90,
        "nota": "Canal especializado; códigos digitáveis e regras de mínimo.",
    },
    {
        "handle": "fadadoscupons",
        "nome": "Fada dos Cupons",
        "marketplaces": ["mercadolivre", "amazon", "shopee"],
        "densidade": 85,
        "nota": "12 códigos estruturados observados na prévia pública.",
    },
    {
        "handle": "nerdcupons",
        "nome": "Nerd Cupons",
        "marketplaces": ["mercadolivre", "amazon", "shopee"],
        "densidade": 80,
        "nota": "Cobertura multiloja com códigos digitáveis recentes.",
    },
    {
        "handle": "LinksBrazil",
        "nome": "LinksBR — Promoções",
        "marketplaces": ["mercadolivre", "amazon", "shopee"],
        "densidade": 75,
        "nota": "Prévia pública ativa e alta frequência de sinais de cupom.",
    },
    {
        "handle": "bruxopromos",
        "nome": "Bruxo das Promoções",
        "marketplaces": ["mercadolivre", "amazon", "shopee"],
        "densidade": 65,
        "nota": "Fonte adicional para corroboração e cupons relâmpago.",
    },
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
    {
        "handle": "sddescontos",
        "nome": "SD Descontos — cupons",
        "marketplaces": ["mercadolivre", "amazon", "shopee"],
        "densidade": 88,
        "nota": "Prévia pública diária com códigos estruturados e cupons relâmpago.",
    },
    {
        "handle": "MercadoCuponsBR",
        "nome": "Mercado Cupons BR",
        "marketplaces": ["mercadolivre"],
        "densidade": 82,
        "nota": "Listas por categoria com código, mínimo, teto e link de produtos.",
    },
    {
        "handle": "ofertasportateis",
        "nome": "Ofertas Critical Hits",
        "marketplaces": ["mercadolivre", "amazon", "shopee"],
        "densidade": 70,
        "nota": "Cobertura multiloja recente, especialmente tecnologia e games.",
    },
    {
        "handle": "promoatualizado",
        "nome": "Promo Atualizado",
        "marketplaces": ["amazon", "shopee"],
        "densidade": 66,
        "nota": "Códigos recentes de Amazon e Shopee na prévia pública.",
    },
    {
        "handle": "ofertanasho",
        "nome": "Oferta na Sho — cupons",
        "marketplaces": ["amazon", "shopee"],
        "densidade": 65,
        "nota": "Canal focado em códigos Shopee, com sinais adicionais da Amazon.",
    },
    {
        "handle": "tecnanofertas",
        "nome": "Tecnan Ofertas",
        "marketplaces": ["mercadolivre", "amazon", "shopee"],
        "densidade": 55,
        "nota": "Fonte complementar de tecnologia e alertas de cupom ML.",
    },
    {
        "handle": "ofertasamazonbr",
        "nome": "Ofertas Amazon Brasil",
        "marketplaces": ["amazon"],
        "densidade": 78,
        "nota": "Canal especializado em promoções e códigos da Amazon Brasil.",
    },
    {
        "handle": "canalrodrigomoreira",
        "nome": "Ofertas Rodrigo Moreira",
        "marketplaces": ["amazon", "mercadolivre", "shopee"],
        "densidade": 76,
        "nota": "Alta frequência e códigos Amazon com mínimo e teto explícitos.",
    },
    {
        "handle": "TJGOFERTASs",
        "nome": "TJG Gaming — promoções",
        "marketplaces": ["amazon", "mercadolivre", "shopee"],
        "densidade": 74,
        "nota": "Canal grande de tecnologia com listas de códigos Amazon.",
    },
    {
        "handle": "escolhasegura",
        "nome": "Escolha Segura — ofertas",
        "marketplaces": ["amazon", "mercadolivre"],
        "densidade": 72,
        "nota": "Códigos Amazon frequentes e associação explícita a produtos.",
    },
    {
        "handle": "CuponsDaSho",
        "nome": "Cupons da Sho",
        "marketplaces": ["amazon", "shopee"],
        "densidade": 71,
        "nota": "Canal de códigos digitáveis Shopee e Amazon em tempo real.",
    },
    {
        "handle": "promotop",
        "nome": "PromoTop",
        "marketplaces": ["amazon", "mercadolivre", "shopee"],
        "densidade": 70,
        "nota": "Canal multiloja de grande volume, útil para cupons curtos.",
    },
    {
        "handle": "wolf_ofertas",
        "nome": "Wolf Ofertas",
        "marketplaces": ["amazon", "mercadolivre", "shopee"],
        "densidade": 68,
        "nota": "Previa ativa em 01/09/2026; codigos com desconto e minimo explicitos.",
    },
    {
        "handle": "ofertasbrasi",
        "nome": "Ofertas Brasil 2.0",
        "marketplaces": ["mercadolivre", "shopee"],
        "densidade": 64,
        "nota": "Previa ativa em 01/09/2026; codigos Shopee recentes e digitaveis.",
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
