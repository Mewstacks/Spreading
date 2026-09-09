"""Macro-categoria a partir do NOME, para quem não tem `categoria` do marketplace.

`scraper_mercadolivre/cateorize.py` deriva a macro do `domain_id` do Mercado Livre
(`MLB-CELLPHONES` -> "Celulares, Telefonia e Wearables"). Funciona para o que veio da
raspagem de catálogo, e não funciona para nada mais: produto criado pelo pipeline de
cupom, a partir de página de container ou campanha, nasce com `categoria` em
`DESCONHECIDO`.

Medido em produção em 04/09/2026: dos 300 candidatos que tinham par cupom+produto
confirmado, cupom ativo e ficha completa, **249 estavam sem macro-categoria**. Toda
`ConfiguracaoEnvio` filtra por macro. Ou seja, 83% do material que sustenta o produto
— produto do nicho com cupom que se aplica a ele — era invisível para qualquer regra
de envio. As três regras da conta enxergavam 2, 10 e 5 candidatos.

O nome, esse eles têm, e é descritivo: "Airtag Para Coleira De Cachorro",
"Anéis De Vedação Para Processador", "Toalha Mesa Tnt 70x70".

**Precisão acima de cobertura, de propósito.** Classificar errado é pior do que não
classificar: manda cápsula de gelatina para a regra de Ferramentas e devolve ao grupo
exatamente a "promoção de merda" que o filtro de nicho existe para evitar. Então:

- palavra inteira, nunca substring — "cama" não casa em "câmara", "tv" não casa em
  "tvs" por acidente de acento;
- empate entre duas macros com a mesma pontuação deixa o produto SEM macro. Dúvida
  não vira palpite;
- só grava quando o campo está vazio. Categoria vinda do marketplace é autoridade
  maior e nunca é sobrescrita aqui.

Os nomes das macros são exatamente os de `cateorize.macro_dict` — é o valor que as
regras já guardam em `ConfiguracaoEnvio.macro_categoria`, e divergir aqui criaria uma
segunda taxonomia que não casa com nada.
"""
from __future__ import annotations

import logging
import re
import unicodedata

logger = logging.getLogger(__name__)


# Palavra -> macro. Só termo que identifica a categoria sozinho, no vocabulário de
# anúncio brasileiro. Termo genérico ("kit", "conjunto", "premium", "original") fica
# de fora: aparece em tudo e só produziria empate ou erro. Marca que atravessa
# categoria também fica de fora — "xiaomi" vende celular, aspirador, patinete e TV,
# então casar por ela é sorteio, não classificação.
#
# `("termo", peso)` quando o peso natural (número de palavras) classifica errado.
# São poucos e cada um veio de um caso real:
#   "Relógio Smartwatch Forestory"      -> relogio(Joias) empatava com smartwatch
#   "Robô Aspirador ... Alexa"          -> aspirador precisa ganhar do resto
#   "Compressor ... Calibrador De Pneu" -> é peça automotiva, não ferramenta
PALAVRAS_POR_MACRO: dict[str, tuple] = {
    "Celulares, Telefonia e Wearables": (
        "celular", "celulares", "smartphone", "smartphones", "iphone", "galaxy",
        "redmi", "motorola", ("smartwatch", 3), ("smartband", 3), "chip",
        "capinha", "capa de celular", "pelicula", "carregador", "powerbank", "power bank", "poco",
        "fone de ouvido", "airpods", "watch",
    ),
    "Eletrônicos e Informática": (
        "notebook", "laptop", "computador", "desktop", "monitor", "teclado",
        "mouse", "mousepad", "impressora", "roteador", "modem", "pendrive",
        "ssd", "hd externo", "memoria ram", "placa de video", "placa-mae", "fonte cooler master", "processador",
        "webcam", "tablet", "estabilizador", "nobreak", "cabo hdmi", "starlink", "gpu", "radeon", "adaptador hub usb",
    ),
    "Áudio, Vídeo e Fotografia": (
        "caixa de som", "soundbar", "fone bluetooth", "headset", "headphone",
        "microfone", "camera", "cameras", "gopro", "drone", "projetor",
        "televisao", "smart tv", "lente", "tripe", "ring light", "fones de ouvido",
        "caixa som", "projetores",
        # Títulos recentes de marketplace usam o substantivo curto antes de
        # qualificadores: “Fone tradutor ...”, “Fone sem fio ...”. É inequívoco
        # no catálogo de produto (não confundir com texto livre de atendimento).
        "fone",
    ),
    "Eletrodomésticos": (
        "geladeira", "refrigerador", "fogao", "cooktop", "microondas",
        "lava louca", "lava roupas", "lavadora", "secadora", "freezer",
        "airfryer", "air fryer", "fritadeira", "liquidificador", "batedeira",
        "lava e seca", "maquina de lavar", "extratora",
        ("aspirador", 3),
        "cafeteira", "sanduicheira", "forno eletrico", "purificador", "bebedouro", "ferro de passar",
        "panificadora", "tanquinho", "sorveteira", "mixer", "dreame",
        "frigobar", "ferro a vapor", "fritadeiras", "micro ondas", "micro-ondas", "depurador", "filtro de agua",
    ),
    "Climatização e Aquecimento": (
        "ar condicionado", "ventilador", "climatizador", "aquecedor",
        "umidificador", "circulador de ar", "exaustor", "ar cond",
    ),
    "Casa, Móveis e Decoração": (
        "sofa", "poltrona", "cadeira", "mesa", "estante", "armario", "guarda roupa",
        "colchao", "travesseiro", "edredom", "lencol", "cortina", "tapete",
        "toalha", "almofada", "quadro decorativo", "espelho", "luminaria",
        "abajur", "criado mudo", "rack", "painel de tv", "cabideiro", "banqueta",
        "mesa de cabeceira", "lixeira", "manta", "paneleiro", "difusor de ambiente",
        "aromatizador", "home spray",
        "penteadeira", "comoda", "roupeiro", "quarto modulado", "cama box", "painel decorativo", "figura decorativa", "vaso decorativo", "letreiro decorativo",
        "porta chaves", "porta-retrato", "porta retrato", "cachepot", "ornamento decorativo", "placa decorativa", "caixa decorativa", "puff", "costela de adao artificial", "caixa plastica", "cesto de lixo", "elefante decorativo", "plantas artificiais", "painel 3d",
    ),
    "Cozinha, Mesa e Bar": (
        "panela", "panelas", "frigideira", "talher", "talheres", "prato", "copo", "taca",
        "caneca", "garrafa termica", "jogo de jantar", "faqueiro", "assadeira",
        "escorredor", "pote hermetico", "marmita", "churrasqueira", "espetinho",
        "galheteiro", "saleiro", "bandeja", "garrafa isotermica", "espatula",
        "faca de carne", "pinca culinaria", "luva termica", "tabua de corte",
        "balanca de cozinha", "organizador de esponja", "pote organizador",
        "cuscuzeira", "mandolin", "multifatiador", "utensilios de cozinha", "pote de vidro",
    ),
    "Limpeza e Lavanderia": (
        "detergente", "sabao", ("amaciante", 2), "desinfetante", "agua sanitaria",
        "vassoura", "rodo", "balde", "esfregao", "pano de chao", "cesto de roupa",
        "varal", "cabide", "pano limpa vidros", "neutralizador de ar",
        "kit banheiro", "cesto para roupas", "cesto dobravel", "cesto multiuso", "sacos de lavar", "escova eletrica de limpeza", "neutralizadores", "inseticida",
    ),
    "Casa e Construção": (
        "torneira", "chuveiro", "registro", "sifao", "vaso sanitario", "pia",
        "azulejo", "porcelanato", "argamassa", "cimento", "tinta", "verniz",
        "fechadura", "dobradica", "cadeado", "mangueira", "caixa dagua", "puxador",
        "cremalheira", "rodape", "prendedor de porta",
        "ducha higienica", "cuba de embutir", "piso vinilico", "spray impermeabilizante", "sanitario", "vaso convencional",
    ),
    "Ferramentas e Manutenção": (
        "furadeira", "parafusadeira", "esmerilhadeira", "serra", "martelo",
        "alicate", "chave de fenda", "chave philips", "chave inglesa",
        "jogo de chaves", "trena", "nivel a laser", "lixadeira", "soldador",
        "compressor", "macaco hidraulico", "morsa", "broca", "parafuso", "machado",
        "arruela", "porca", "vedacao", "multimetro", "paquimetro", "maquina de solda",
        "arame solda", "nivelador a laser", "pistola de pintura", "abracadeira",
        "chave combinada", "maleta anti impacto",
        "maquina inversora", "maquina solda", "chave de impacto", "perfurador de solo", "bomba pressurizadora", "desobstruidora de alta pressao", "multi ferramentas", "escada multifuncional", "fio de solda", "fita dupla face",
    ),
    "Materiais Elétricos e Componentes": (
        "lampada", "reator", "disjuntor", "tomada", "interruptor", "fita led",
        "refletor", "extensao eletrica", "filtro de linha", "pilha", "bateria",
        "soquete", "e27", "plafon", "spot led", "rolo de fio", "cabo flexivel",
        "placa solar", "inversor", "fio eletrico",
        "protetor de surto", "conector wago", "cabo de rede", "cabo ethernet", "refletores", "conector compacto", "conectores compactos", "iclamper",
    ),
    "Jardim, Piscina e Área Externa": (
        "piscina", "mangueira de jardim", "regador", "vaso de planta", "adubo",
        "substrato", "semente", "cortador de grama", "aparador de cerca",
        "pergolado", "churrasqueira de jardim", "rede de descanso", "tesoura de poda", "tesoura para poda", "pa de jardim", "pa de jardinagem", "pazinha", "pulverizador", "kit jardinagem", "ferramentas de jardinagem", "soprador", "ancinho", "luvas para jardinagem", "esguicho", "para jardim", "para grama", "vassoura para grama", "vasoura para grama", "foice de jardinagem", "irrigacao gotejamento", "tesoura para podar", "tesoura de jardinagem", "horta em vasos", "bonsai", "formifita", "antiformigas",
    ),
    "Automotivo": (
        ("pneu", 2), ("calibrador de pneu", 4), "roda automotiva", "amortecedor", "pastilha de freio", "oleo motor",
        "bateria automotiva", "farol", "lanterna automotiva", "retrovisor",
        "capa de banco", "tapete automotivo", "som automotivo", "cera automotiva",
        "aditivo", "limpador de para brisa", "capacete", "moto peca", "parachoque",
        "grade radiador", "sensor abs", "manopla cambio", "comando de valvula",
        "correia tensor", "eletrovalvula", "capa de chuva moto", "capa chuva motoqueiro",
        "lixeira automotiva", "troca oleo", "oleo 5w30", "central multimidia", "capa de chuva motoqueiro", "michelin city extra", "rallybar", "parabrisa", "comando admissao", "painel secagem pintura",
    ),
    "Pets e Animais": (
        "cachorro", "gato", "pet", "racao", "coleira", "guia para cachorro",
        "arranhador", "aquario", "petisco", "areia higienica", "caixa de transporte",
        "comedouro", "bebedouro pet", "tapete higienico", "zenrelia",
    ),
    "Bebês e Maternidade": (
        "bebe", "fralda", "mamadeira", "chupeta", "carrinho de bebe",
        "bebe conforto", "berco", "papinha", "lenco umedecido", "babador", "cadeirinha infantil",
    ),
    "Beleza e Cuidados Pessoais": (
        "shampoo", "condicionador", "hidratante", "perfume", "batom", "esmalte",
        "maquiagem", "base facial", "protetor solar", "secador de cabelo", "secador de cabelos", "prancha lizze", "dyson airstrait",
        "chapinha", "prancha de cabelo", "barbeador", "depilador", "creme facial",
        "sabonete", "desodorante", "escova de cabelo", "leave in", "oleo capilar",
        "matizador", "bioplastia", "babyliss", "cabelo e corpo", "hair care", "lowell",
        "po descolorante", "progressiva", "modelador de cachos", "mascara condicionador",
        "reparador de pontas", "protetor termico", "cronograma capilar", "cronograma",
        "gel de limpeza", "clareador corporal", "clareador para axilas", "primer iluminador", "oneblade", "shaver", "maquina acabamento",
        "aparador de pelos", "aparador oneblade", "serum capilar", "serum", "secador multifuncional", "alisador", "base coat", "oleo creme", "oleo reparador", "principia", "eico pro",
    ),
    "Saúde, Ortopedia e Equipamentos Médicos": (
        "termometro", "oximetro", "medidor de pressao", "nebulizador", "mascara", "balanca digital corporal", "alwaysfit",
        "atadura", "colar cervical", "joelheira", "tornozeleira", "muleta",
        "cadeira de rodas", "escova de dente", "creme dental", "fio dental",
        "suplemento", "whey", "creatina", "colageno", "vitamina",
        "magnesio", "pre treino", "cinta hernia", "multivitaminico", "omega 3",
        "andador", "barra de apoio", "corretor de postura", "medidor de febre", "kinesio",
        "proteina isolada", "isolate protein", "beef protein", "hipercalorico", "cpap",
        "balanca bioimpedancia", "corretores de postura", "fotopolimerizador odontologico", "cunha para uso",
    ),
    "Alimentos e Bebidas": (
        "cafe", "cha", "chocolate", "biscoito", "bolacha", "cerveja", "vinho", "supercoffee",
        "whisky", "vodka", "refrigerante", "suco", "azeite", "arroz", "feijao",
        "macarrao", "farinha", "acucar", "leite", "achocolatado", "castanha",
        "amendoim", "macadamia", "noz", "mel", "tempero", "gelatina", "creme de avela",
        "molho", "curry", "chiclete",
    ),
    "Moda, Calçados e Acessórios": (
        "camiseta", "camisa", "calca", "bermuda", "bermudas dry fit", "short", "vestido", "saia", "regata",
        "jaqueta", "moletom", "blusa", "tenis", "sapato", "sandalia", "chinelo",
        "bota", "meia", "cueca", "calcinha", "sutia", "oculos de sol", "cinto",
        "bone", "pijama", "cropped", "headband", "chapeu de palha", "meias", "cuecas",
        "polo masculina", "polo enxuto", "tech t shirt", "daily t shirt", "cinta modeladora", "guarda chuva",
    ),
    "Bolsas, Malas e Viagem": (
        "mochila", "bolsa", "mala de viagem", "carteira", "necessaire",
        "pochete", "mala de bordo", "chaveiro", "bolsa masculina",
    ),
    "Joias, Relógios e Bijuterias": (
        "relogio", "colar", "pulseira", "brinco", "anel", "corrente de prata",
        "bijuteria", "piercing", "alianca", "aliancas", "gargantilha",
    ),
    "Esportes e Fitness": (
        "halter", "haltere", "anilha", "barra de supino", "esteira ergometrica",
        "bicicleta ergometrica", "bicicleta spinning", "corda de pular", "colchonete", "tapete de yoga",
        "bola de futebol", "chuteira", "luva de boxe", "skate", "patins",
        "prancha de equilibrio", "elastico de exercicio", "natacao", "mergulho",
        "oculos de natacao", "garrafa de pulso", "treino funcional",
    ),
    "Camping, Pesca e Outdoor": (
        "barraca", "saco de dormir", "lanterna de cabeca", "canivete",
        "vara de pesca", "molinete", "anzol", "isca", "cantil", "cadeira de camping",
    ),
    "Games, Brinquedos e Hobbies": (
        "playstation", "xbox", "nintendo", "controle de video game", "joystick",
        "boneca", "boneco", "lego", "quebra cabeca", "jogo de tabuleiro",
        "carrinho de brinquedo", "pelucia", "cubo magico", "video game",
        "figurinhas", "cartas pokemon", "switch fisico",
    ),
    "Papelaria, Escritório e Escola": (
        "caderno", "caneta", "lapis", "borracha escolar", "mochila escolar",
        "estojo", "agenda", "papel sulfite", "grampeador", "pasta arquivo",
        "marca texto", "cartucho", "toner", "fita adesiva",
        # Vocabulário observado no feed sem domain_id em 09/09/2026. São nomes de
        # produto, não marcas, e evitam que a maior parte dos itens escolares
        # recentes fique invisível só por abrir com “kit” ou “caderneta”.
        "caderneta", "papelaria", "canetas", "caneta esferografica", "regua",
        "bloco de anotacoes", "lapiseira", "ecolapis", "fragmentadora de papel",
        "prendedor de papel", "papel reciclado", "nota autoadesiva", "cola em fita",
        "tilibra", "pentel", "tris",
        "planner", "borracha", "papel a4", "sulfite", "fita corretiva",
        "prancheta", "marca-texto", "ficha pautada", "adesivo decorado",
        "papel colorido", "ficha", "marcador de linhas", "adesivos", "lettering", "caligrafia", "faber castell", "faber-castell", "canson", "trilux",
    ),
    "Livros, Mídia e Conteúdo": (
        "livro", "livros", "coloring book",
    ),
    "Música e Instrumentos": (
        "violao", "guitarra", "baixo eletrico", "teclado musical", "bateria musical",
        "pedaleira", "ukulele", "cavaquinho", "amplificador", "palheta",
    ),
    "Arte, Artesanato e Costura": (
        "linha de costura", "agulha", "maquina de costura", "tecido", "feltro",
        "tinta acrilica", "pincel", "tela para pintura", "cola quente", "biscuit", "pastel oleoso",
    ),
    "Festas, Eventos e Presentes": (
        "balao", "bexiga", "confete", "vela de aniversario", "topo de bolo",
        "lembrancinha", "painel de festa", "descartavel para festa", "bandeira do brasil",
        "bandeirola", "festa junina",
    ),
    "Embalagens e Descartáveis": (
        "saco plastico", "sacola", "sacolas", "embalagem", "caixa de papelao",
        "plastico bolha",
        "copo descartavel", "pote descartavel",
    ),
}

# Índice invertido, com o número de palavras do termo — termo mais específico
# ("fone de ouvido") pesa mais do que termo de uma palavra ("fone").
_INDICE: list[tuple[re.Pattern, str, int]] = []


def _dobrar(texto: str) -> str:
    """Minúsculo, sem acento e sem variação de hífen/espaço entre palavras."""
    normalizado = unicodedata.normalize("NFKD", str(texto or ""))
    sem_acento = "".join(c for c in normalizado if not unicodedata.combining(c))
    sem_hifen = re.sub(r"[-‐‑‒–—―]", " ", sem_acento)
    return re.sub(r"\s+", " ", sem_hifen).casefold().strip()


def _construir_indice() -> None:
    if _INDICE:
        return
    for macro, palavras in PALAVRAS_POR_MACRO.items():
        for entrada in palavras:
            palavra, peso_fixo = entrada if isinstance(entrada, tuple) else (entrada, None)
            termo = _dobrar(palavra)
            # `\b` nas duas pontas: palavra inteira. Sem isso "cama" casaria dentro
            # de "camarao" e "tv" dentro de "tvs" — erro silencioso e caro, porque
            # manda o item para o nicho errado em vez de deixá-lo de fora.
            # Cards de marketplace alternam livremente singular e plural: ``toalha``
            # e ``toalhas``, ``projetor`` e ``projetores``. A regra anterior usava
            # fronteira de palavra (correto), mas tratava cada plural como uma palavra
            # totalmente diferente (inútil). Aceitar só o ``s`` final preserva a
            # fronteira e não transforma uma busca em substring: ``casa`` casa em
            # ``casas``, nunca em ``casamento``. Palavras que já terminam em ``s``
            # ficam literais para não gerar formas artificiais como ``ss``.
            partes = []
            for parte in termo.split(" "):
                sufixo_plural = r"s?" if parte.isalpha() and not parte.endswith("s") else ""
                partes.append(re.escape(parte) + sufixo_plural)
            padrao = re.compile(r"\b" + r"\s+".join(partes) + r"\b")
            _INDICE.append((padrao, macro, peso_fixo or len(termo.split(" "))))


# Quantas palavras do começo do nome contam. Título de anúncio brasileiro abre pelo
# substantivo-cabeça e o resto é qualificador, acessório ou público-alvo. Ler o nome
# inteiro fazia o qualificador vencer a cabeça, e a auditoria de 25 nomes reais em
# 06/09/2026 mostrou 1 em cada 4 errados por isso:
#
#   "Lanterna Tática ... Ultra Potente Bateria"   -> bateria  -> Elétricos (é lanterna)
#   "Óculos Natação ... Mergulho Piscina"         -> piscina  -> Jardim    (é esporte)
#   "Kit 2 Colmeia Organizador Gaveta Calcinha"   -> calcinha -> Moda      (é organizador)
#
# Nos três, a palavra que decidiu está no fim. Errar assim é pior que não classificar:
# põe o item na regra errada e devolve ao grupo exatamente a oferta fora do nicho.
JANELA_CABECA = 4


def _cabeca(alvo: str) -> tuple[str, int]:
    """As primeiras palavras úteis, e até onde a cabeça vai.

    Sem a quantidade que abre tanto anúncio: "50 Sacolas Plástica Premium" tem a
    cabeça na segunda palavra, e contar o "50" gastaria um quarto da janela com
    ruído.

    Devolve uma string mais longa que a janela junto com o limite dela, porque um
    termo de várias palavras que COMEÇA na cabeça continua valendo mesmo terminando
    fora — "Compressor Portátil Digital Calibrador De Pneu" tem "calibrador de
    pneu" começando na quarta palavra, e cortar no limite seco deixava só
    "compressor" e mandava peça automotiva para Ferramentas.
    """
    palavras = [p for p in alvo.split(" ") if p and not p.isdigit()]
    cabeca = " ".join(palavras[:JANELA_CABECA])
    return " ".join(palavras[:JANELA_CABECA + 3]), len(cabeca)


def macro_do_nome(nome: str) -> str:
    """Macro-categoria do nome, ou "" quando não dá para afirmar.

    Só a cabeça do nome pontua (ver `JANELA_CABECA`). Dentro dela, pontua por
    especificidade: um termo de duas palavras vale mais que um de uma, porque
    "fone de ouvido" identifica melhor que "fone". Empate no topo devolve vazio —
    duas categorias igualmente plausíveis é ausência de resposta, não escolha
    entre elas.
    """
    _construir_indice()
    alvo = _dobrar(nome)
    if not alvo:
        return ""
    trecho, limite = _cabeca(alvo)
    if not trecho:
        return ""
    pontos: dict[str, int] = {}
    for padrao, macro, peso in _INDICE:
        achado = padrao.search(trecho)
        # O termo tem de COMEÇAR dentro da cabeça. Terminar fora é permitido; nascer
        # fora, não — é aí que mora o qualificador que classificava errado.
        if achado and achado.start() < limite:
            pontos[macro] = pontos.get(macro, 0) + peso
    if not pontos:
        return ""
    ordenado = sorted(pontos.items(), key=lambda kv: -kv[1])
    if len(ordenado) > 1 and ordenado[0][1] == ordenado[1][1]:
        return ""
    return ordenado[0][0]


#: `categoria` que o marketplace não soube dizer. Macro apoiada nela é palpite de
#: alguém, não autoridade — ver `popular_macro_por_nome`.
CATEGORIA_SEM_AUTORIDADE = ("", "DESCONHECIDO")


def popular_macro_por_nome(*, limite=None, apenas_com_cupom=False,
                           corrigir_sem_categoria=True, produto_ids=None) -> int:
    """Preenche `macro_categoria` a partir do nome. Idempotente.

    Onde o marketplace informou `categoria`, ele manda:
    `cateorize.popular_macro_categorias` deriva a macro dali e nada aqui a toca.

    Onde `categoria` é `DESCONHECIDO`, porém, a macro gravada não tem autoridade
    nenhuma — ela também saiu de um palpite. Encontrado em produção em 06/09/2026:
    "Robô Aspirador Xiaomi S40 Lds 10.000pa Alexa Wi-fi" com `categoria` desconhecida
    e macro "Celulares, Telefonia e Wearables", o que colocava um aspirador na regra
    de celulares e mandava para o grupo exatamente a oferta fora do nicho. Nesse
    caso, e só nele, um veredito confiante do nome corrige o palpite anterior.

    `apenas_com_cupom` restringe ao que realmente muda o funil hoje — produto com
    par confirmado e cupom ativo —, para o ciclo de 15 minutos não varrer o
    catálogo inteiro atrás de linha que ninguém vai publicar.
    """
    from django.db.models import Q

    from apps.scrapers.models import Produto

    vazio = Q(macro_categoria__isnull=True) | Q(macro_categoria="")
    if corrigir_sem_categoria:
        # Macro preenchida sobre categoria desconhecida também entra: pode estar
        # errada, e o nome é a única evidência disponível para conferir.
        vazio |= (Q(categoria__in=CATEGORIA_SEM_AUTORIDADE)
                  | Q(categoria__isnull=True))
    qs = Produto.objects.filter(vazio).exclude(nome="")
    if produto_ids is not None:
        qs = qs.filter(pk__in=list(produto_ids))
    if apenas_com_cupom:
        qs = qs.filter(
            cupons_normalizados__status="confirmado",
            cupons_normalizados__cupom__estado="ativo",
        ).distinct()
    if limite:
        qs = qs.order_by("-ultima_observacao", "-id")[:limite]

    lote, atualizados = [], 0
    for produto in qs.iterator(chunk_size=500) if not limite else list(qs):
        macro = macro_do_nome(produto.nome)
        if not macro or macro == (produto.macro_categoria or ""):
            continue
        # Só sobrescreve macro já preenchida quando a categoria não tem autoridade.
        # Com categoria conhecida, o marketplace continua sendo a fonte da verdade
        # mesmo que o nome sugira outra coisa.
        if (produto.macro_categoria or "").strip():
            categoria = (produto.categoria or "").strip().upper()
            if categoria not in CATEGORIA_SEM_AUTORIDADE:
                continue
        produto.macro_categoria = macro
        lote.append(produto)
        if len(lote) >= 500:
            Produto.objects.bulk_update(lote, ["macro_categoria"])
            atualizados += len(lote)
            lote = []
    if lote:
        Produto.objects.bulk_update(lote, ["macro_categoria"])
        atualizados += len(lote)
    if atualizados:
        logger.info("Macro por nome: %s produto(s) classificado(s)", atualizados)
    return atualizados
