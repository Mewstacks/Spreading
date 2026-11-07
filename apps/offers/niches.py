"""Taxonomia de nichos (portada do v1): 17 macros por palavra-chave + sub-nichos por termos.

Chave de nicho: "m:<Macro>" (a macro inteira) ou "s:<Macro>|<Rótulo>" (um sub-nicho).
"""
import unicodedata

# Ordem importa: o mais específico primeiro.
MACROS = [
    ("Celulares, Telefonia e Wearables", ["celular", "smartphone", "iphone", "galaxy", "moto g", "xiaomi", "redmi", "smartwatch", "smart watch", "fone bluetooth", "chip ", "capa de celular", "capinha", "pelicula"]),
    ("Eletrônicos e Informática", ["notebook", "laptop", "computador", "pc gamer", "monitor", "teclado", "mouse", "ssd", "hd ", "pen drive", "pendrive", "placa de video", "placa mae", "processador", "memoria ram", "roteador", "impressora", "tablet", "webcam", "cooler", "gabinete"]),
    ("Áudio, Vídeo e Fotografia", ["smart tv", " tv ", "televisor", "caixa de som", "soundbar", "fone de ouvido", "headset", "headphone", "microfone", "camera", "câmera", "drone", "projetor", "echo dot", "alexa"]),
    ("Eletrodomésticos", ["geladeira", "refrigerador", "fogao", "fogão", "microondas", "micro-ondas", "lava roupas", "lava louças", "lava loucas", "lava-louças", "lavadora", "secadora", "cooktop", "coifa", "depurador", "air fryer", "fritadeira", "liquidificador", "batedeira", "cafeteira", "aspirador", "ventilador", "ar condicionado", "climatizador", "purificador", "forno eletrico"]),
    ("Cozinha, Mesa e Bar", ["panela", "frigideira", "talher", "faqueiro", "copo", "taca", "taça", "jogo de pratos", "garrafa termica", "potes", "assadeira", "cafeteira italiana", "tabua de corte", "escorredor"]),
    ("Casa, Móveis e Decoração", ["sofa", "sofá", "cama", "colchao", "colchão", "guarda roupa", "guarda-roupa", "mesa", "cadeira", "estante", "armario", "armário", "cortina", "tapete", "luminaria", "luminária", "rack", "criado mudo", "escrivaninha", "lencol", "lençol", "edredom", "travesseiro", "toalha", "colcha", "cobre leito", "cobre-leito", "lixeira", "organizador", "cabide", "varal"]),
    ("Beleza e Cuidados Pessoais", ["perfume", "eau de parfum", "eau de toilette", "eau de cologne", "edp ", "edt ", "deo parfum", "deo colonia", "colonia ", "body splash", "desodorante", "maquiagem", "blush", "batom", "shampoo", "condicionador", "creme facial", "hidratante", "secador de cabelo", "chapinha", "barbeador", "depilador", "esmalte", "protetor solar", "skincare"]),
    ("Moda, Calçados e Acessórios", ["tenis", "tênis", "sapato", "sandalia", "sandália", "chinelo", "camiseta", "camisa", "calca", "calça", "vestido", "blusa", "jaqueta", "bermuda", "short", "bone", "boné", "oculos", "óculos", "relogio", "relógio", "bolsa", "mochila", "carteira", "cinto", "meia"]),
    ("Esportes e Fitness", ["bicicleta", "halter", "anilha", "esteira", "academia", "musculacao", "musculação", "whey", "creatina", "suplemento", "barra fixa", "corda de pular", "bola ", "patins", "skate", "caneleira"]),
    ("Games, Brinquedos e Hobbies", ["playstation", "ps5", "ps4", "xbox", "nintendo", "controle ", "joystick", "jogo ", "brinquedo", "boneca", "lego", "quebra cabeca", "carrinho de brinquedo", "pelucia", "pelúcia"]),
    ("Ferramentas e Manutenção", ["furadeira", "parafusadeira", "serra ", "chave de fenda", "kit ferramentas", "esmerilhadeira", "lixadeira", "soldador", "trena", "alicate", "martelo", "compressor", "plaina", "tomada", "extensao 5", "interruptor", "cabo pp", "disjuntor", "fita isolante"]),
    ("Automotivo", ["pneu", "oleo motor", "óleo motor", "bateria automotiva", "farol", "retrovisor", "limpador para-brisa", "som automotivo", "capa banco", "tapete carro", "terminal direcao", "terminal direção", "amortecedor", "pastilha de freio", "moto ", "capacete"]),
    ("Pets e Animais", ["racao", "ração", "petisco", "coleira", "aquario", "aquário", "arranhador", "casinha cachorro", "comedouro", "areia gato"]),
    ("Bebês e Maternidade", ["fralda", "carrinho de bebe", "carrinho de bebê", "bercco", "berço", "mamadeira", "chupeta", "cadeira para auto", "body bebe"]),
    ("Alimentos e Bebidas", ["cafe ", "café ", "chocolate", "whisky", "vinho", "cerveja", "azeite", "biscoito", "achocolatado", "leite ", "energetico", "energético"]),
    ("Saúde, Ortopedia e Equipamentos Médicos", ["termometro", "termômetro", "oximetro", "medidor de pressao", "massageador", "cadeira de rodas", "fralda geriatrica", "vitamina", "colageno", "colágeno", "capsulas", "cápsulas", "caps)", "colagen"]),
    ("Papelaria, Escritório e Escola", ["caderno", "caneta", "mochila escolar", "estojo", "lapis", "lápis", "papel sulfite", "agenda"]),
]

SUBNICHOS = {
    "Celulares, Telefonia e Wearables": [
        ("Smartphones", "celular, smartphone, iphone, galaxy, moto g, xiaomi, redmi"),
        ("Smartwatch", "smartwatch, smart watch, relogio inteligente"),
        ("Fones bluetooth", "fone bluetooth, earbuds, tws, airpods"),
        ("Capas e películas", "capinha, capa de celular, pelicula"),
    ],
    "Eletrônicos e Informática": [
        ("Notebooks", "notebook, laptop"),
        ("Monitores", "monitor"),
        ("Teclado/Mouse/Headset", "teclado, mouse, headset"),
        ("Armazenamento (SSD/HD)", "ssd, hd externo, pen drive, pendrive, cartao de memoria"),
        ("Componentes PC", "placa de video, placa mae, processador, memoria ram, fonte atx, gabinete"),
        ("Tablets", "tablet, ipad"),
        ("Roteador/Rede", "roteador, repetidor, mesh"),
    ],
    "Áudio, Vídeo e Fotografia": [
        ("Smart TVs", "smart tv, televisor, tv 4k, tv led"),
        ("Caixas de som/Soundbar", "caixa de som, soundbar, jbl"),
        ("Câmeras", "camera, câmera, gopro, action cam"),
        ("Drones", "drone"),
        ("Projetores", "projetor"),
        ("Alexa/Echo", "echo dot, alexa"),
    ],
    "Eletrodomésticos": [
        ("Robô aspirador", "aspirador robo, robô aspirador, robot vacuum, robo aspirador"),
        ("Aspirador de pó", "aspirador de po, aspirador vertical"),
        ("Air fryer", "air fryer, fritadeira eletrica, fritadeira sem oleo"),
        ("Geladeira", "geladeira, refrigerador, frigobar"),
        ("Fogão/Cooktop", "fogao, cooktop, forno eletrico"),
        ("Micro-ondas", "microondas, micro-ondas"),
        ("Lava-roupas", "lava roupas, lavadora, maquina de lavar"),
        ("Ar-condicionado", "ar condicionado, climatizador"),
        ("Ventilador", "ventilador"),
        ("Liquidificador/Mixer", "liquidificador, batedeira, mixer"),
        ("Cafeteira", "cafeteira, nespresso, dolce gusto"),
    ],
    "Cozinha, Mesa e Bar": [
        ("Panelas", "panela, frigideira, jogo de panelas"),
        ("Garrafa térmica", "garrafa termica, stanley"),
    ],
    "Casa, Móveis e Decoração": [
        ("Colchões", "colchao, colchão"),
        ("Sofá", "sofa, sofá"),
        ("Cama/Guarda-roupa", "cama, guarda roupa, guarda-roupa, beliche"),
        ("Cadeira escritório/gamer", "cadeira de escritorio, cadeira gamer"),
        ("Cama, mesa e banho", "lencol, lençol, edredom, toalha, jogo de cama"),
    ],
    "Beleza e Cuidados Pessoais": [
        ("Perfumes", "perfume"),
        ("Secador/Chapinha", "secador de cabelo, chapinha, prancha"),
        ("Barbeador/Aparador", "barbeador, aparador, maquina de cortar cabelo"),
        ("Maquiagem", "maquiagem, batom, base, paleta"),
    ],
    "Moda, Calçados e Acessórios": [
        ("Tênis", "tenis, tênis"),
        ("Relógios", "relogio, relógio"),
        ("Óculos", "oculos, óculos"),
        ("Mochilas/Bolsas", "mochila, bolsa, carteira"),
    ],
    "Esportes e Fitness": [
        ("Suplementos", "whey, creatina, suplemento"),
        ("Bicicletas", "bicicleta, bike"),
        ("Musculação", "halter, anilha, barra fixa, kettlebell"),
        ("Esteira/Elíptico", "esteira, eliptico"),
    ],
    "Games, Brinquedos e Hobbies": [
        ("Consoles", "playstation, ps5, ps4, xbox, nintendo switch"),
        ("Controles", "controle, joystick, dualsense"),
        ("Lego/Blocos", "lego, blocos de montar"),
    ],
    "Ferramentas e Manutenção": [
        ("Furadeira/Parafusadeira", "furadeira, parafusadeira"),
        ("Kit ferramentas", "kit ferramentas, jogo de ferramentas"),
        ("Serra/Lixadeira", "serra, esmerilhadeira, lixadeira"),
    ],
    "Automotivo": [
        ("Pneus", "pneu"),
        ("Som automotivo", "som automotivo, multimidia, central multimidia"),
        ("Acessórios carro", "tapete carro, capa banco, suporte celular carro"),
        ("Capacete/Moto", "capacete, moto"),
    ],
    "Pets e Animais": [
        ("Ração", "racao, ração"),
        ("Acessórios pet", "coleira, comedouro, arranhador, casinha"),
    ],
    "Bebês e Maternidade": [
        ("Fraldas", "fralda"),
        ("Carrinho de bebê", "carrinho de bebe, carrinho de bebê"),
        ("Cadeirinha auto", "cadeira para auto, bebe conforto"),
    ],
    "Alimentos e Bebidas": [
        ("Café", "cafe, café"),
        ("Bebidas", "whisky, vinho, cerveja, gin, energetico"),
        ("Chocolate", "chocolate, achocolatado"),
    ],
    "Saúde, Ortopedia e Equipamentos Médicos": [
        ("Massageador", "massageador"),
        ("Medidor pressão/Termômetro", "medidor de pressao, oximetro, termometro"),
        ("Vitaminas", "vitamina, colageno, colágeno, omega"),
    ],
    "Papelaria, Escritório e Escola": [
        ("Mochila escolar", "mochila escolar"),
        ("Material escolar", "caderno, caneta, estojo, lapis"),
    ],
}


def fold(text: str) -> str:
    """Minúsculas e sem acento (dos dois lados da comparação)."""
    decomposed = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def classify(title: str) -> str:
    """Macro-categoria pelo nome do produto; '' se nada bater."""
    low = f" {fold(title)} "
    best, best_len = "", 0
    for macro, keywords in MACROS:
        longest = max((len(fold(k).strip()) for k in keywords if fold(k) in low), default=0)
        if longest > best_len:  # a palavra mais específica vence (aspirador > xiaomi)
            best, best_len = macro, longest
    return best


def macro_key(macro: str) -> str:
    return f"m:{macro}"


def sub_key(macro: str, label: str) -> str:
    return f"s:{macro}|{label}"


def catalog():
    """Para a tela: [(macro, macro_key, [(rótulo, chave)])] na ordem da taxonomia."""
    out = []
    for macro, _ in MACROS:
        subs = [(label, sub_key(macro, label)) for label, _ in SUBNICHOS.get(macro, [])]
        out.append((macro, macro_key(macro), subs))
    return out


def valid_keys() -> set[str]:
    keys = set()
    for _macro, mkey, subs in catalog():
        keys.add(mkey)
        keys.update(k for _label, k in subs)
    return keys


def _sub_terms(key: str) -> list[str]:
    _, rest = key.split(":", 1)
    macro, label = rest.split("|", 1)
    for lab, terms in SUBNICHOS.get(macro, []):
        if lab == label:
            return [fold(t.strip()) for t in terms.split(",") if t.strip()]
    return []


def match(selected: list[str], title: str, macro: str) -> str:
    """'' = não casa; 'sub' = casou sub-nicho; 'macro' = casou a macro inteira."""
    if not selected:
        return "macro"  # sem nicho escolhido = tudo
    low = f" {fold(title)} "
    best = ""
    for key in selected:
        if key.startswith("m:") and macro and key == macro_key(macro):
            best = best or "macro"
        elif key.startswith("s:") and any(t in low for t in _sub_terms(key)):
            return "sub"
    return best
