"""
Scraper das OFERTAS do Mercado Livre (https://www.mercadolivre.com.br/ofertas).

Diferente dos cupons: aqui o desconto JÁ está no preço (de/por), visível na PDP,
sem necessidade de resgate. Toda oferta raspada tem desconto real.

Cada oferta vira um Produto com origem='oferta'. preco_sem_desconto = "de",
preco_com_cupom = "por" (mantemos o nome do campo p/ reaproveitar a seleção/envio).
"""
import os
import re

from apps.scrapers.auxiliar import iniciar_browser
from apps.scrapers.models import Produto

caminho_atual = os.path.dirname(os.path.abspath(__file__))

# Classificação de OFERTAS por palavra-chave no nome (PT), mapeando para os mesmos
# nomes de macro de cateorize.py. Ordem importa: mais específico primeiro.
_PT_MACRO = [
    ("Celulares, Telefonia e Wearables", ["celular", "smartphone", "iphone", "galaxy", "moto g", "xiaomi", "redmi", "smartwatch", "smart watch", "fone bluetooth", "chip ", "capa de celular", "capinha", "pelicula"]),
    ("Eletrônicos e Informática", ["notebook", "laptop", "computador", "pc gamer", "monitor", "teclado", "mouse", "ssd", "hd ", "pen drive", "pendrive", "placa de video", "placa mae", "processador", "memoria ram", "roteador", "impressora", "tablet", "webcam", "cooler", "gabinete"]),
    ("Áudio, Vídeo e Fotografia", ["smart tv", " tv ", "televisor", "caixa de som", "soundbar", "fone de ouvido", "headset", "headphone", "microfone", "camera", "câmera", "drone", "projetor", "echo dot", "alexa"]),
    ("Eletrodomésticos", ["geladeira", "refrigerador", "fogao", "fogão", "microondas", "micro-ondas", "lava roupas", "lavadora", "secadora", "air fryer", "fritadeira", "liquidificador", "batedeira", "cafeteira", "aspirador", "ventilador", "ar condicionado", "climatizador", "purificador", "forno eletrico"]),
    ("Cozinha, Mesa e Bar", ["panela", "frigideira", "talher", "faqueiro", "copo", "taca", "taça", "jogo de pratos", "garrafa termica", "potes", "assadeira"]),
    ("Casa, Móveis e Decoração", ["sofa", "sofá", "cama", "colchao", "colchão", "guarda roupa", "guarda-roupa", "mesa", "cadeira", "estante", "armario", "armário", "cortina", "tapete", "luminaria", "luminária", "rack", "criado mudo", "escrivaninha", "lencol", "lençol", "edredom", "travesseiro", "toalha"]),
    ("Beleza e Cuidados Pessoais", ["perfume", "maquiagem", "batom", "shampoo", "condicionador", "creme facial", "hidratante", "secador de cabelo", "chapinha", "barbeador", "depilador", "esmalte", "protetor solar", "skincare"]),
    ("Moda, Calçados e Acessórios", ["tenis", "tênis", "sapato", "sandalia", "sandália", "chinelo", "camiseta", "camisa", "calca", "calça", "vestido", "blusa", "jaqueta", "bermuda", "short", "bone", "boné", "oculos", "óculos", "relogio", "relógio", "bolsa", "mochila", "carteira", "cinto", "meia"]),
    ("Esportes e Fitness", ["bicicleta", "halter", "anilha", "esteira", "academia", "musculacao", "musculação", "whey", "creatina", "suplemento", "barra fixa", "corda de pular", "bola ", "patins", "skate", "caneleira"]),
    ("Games, Brinquedos e Hobbies", ["playstation", "ps5", "ps4", "xbox", "nintendo", "controle ", "joystick", "jogo ", "brinquedo", "boneca", "lego", "quebra cabeca", "carrinho de brinquedo", "pelucia", "pelúcia"]),
    ("Ferramentas e Manutenção", ["furadeira", "parafusadeira", "serra ", "chave de fenda", "kit ferramentas", "esmerilhadeira", "lixadeira", "soldador", "trena", "alicate", "martelo", "compressor"]),
    ("Automotivo", ["pneu", "oleo motor", "óleo motor", "bateria automotiva", "farol", "retrovisor", "limpador para-brisa", "som automotivo", "capa banco", "tapete carro", "terminal direcao", "terminal direção", "amortecedor", "pastilha de freio", "moto ", "capacete"]),
    ("Pets e Animais", ["racao", "ração", "petisco", "coleira", "aquario", "aquário", "arranhador", "casinha cachorro", "comedouro", "areia gato"]),
    ("Bebês e Maternidade", ["fralda", "carrinho de bebe", "carrinho de bebê", "bercco", "berço", "mamadeira", "chupeta", "cadeira para auto", "body bebe"]),
    ("Alimentos e Bebidas", ["cafe ", "café ", "chocolate", "whisky", "vinho", "cerveja", "azeite", "biscoito", "achocolatado", "leite ", "energetico", "energético"]),
    ("Saúde, Ortopedia e Equipamentos Médicos", ["termometro", "termômetro", "oximetro", "medidor de pressao", "massageador", "cadeira de rodas", "fralda geriatrica", "vitamina", "colageno", "colágeno"]),
    ("Papelaria, Escritório e Escola", ["caderno", "caneta", "mochila escolar", "estojo", "lapis", "lápis", "papel sulfite", "agenda"]),
]


def classificar_oferta_por_nome(nome: str):
    """Mapeia o nome (PT) da oferta para uma macro-categoria. None se não bater."""
    n = (nome or "").lower()
    for macro, kws in _PT_MACRO:
        if any(k in n for k in kws):
            return macro
    return None


def _preco_float(texto_frac, texto_cents="0"):
    frac = (texto_frac or "0").replace(".", "").strip()
    cents = (texto_cents or "0").strip() or "0"
    try:
        return float(f"{frac}.{cents.zfill(2)}")
    except ValueError:
        return 0.0


def mapear_ofertas(max_paginas=25):
    """
    Raspa N páginas de /ofertas e regrava os Produtos de origem='oferta'.
    Retorna quantidade de ofertas salvas.
    """
    print("Iniciando raspagem de OFERTAS (de/por)...")
    coletados = []
    caminho_auth = os.path.join(caminho_atual, "auth.json")

    with iniciar_browser(auth_path=caminho_auth, headless=True) as (page, context):
        for n in range(1, max_paginas + 1):
            print(f"[PROGRESSO] Ofertas página {n}/{max_paginas} ({n*100//max_paginas}%)")
            try:
                page.goto(f"https://www.mercadolivre.com.br/ofertas?page={n}",
                          wait_until="domcontentloaded", timeout=45000)
                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass
            except Exception as e:
                print(f"  Erro ao carregar página {n}: {e}")
                continue

            cards = page.locator(".poly-card")
            total = cards.count()
            if total == 0:
                print(f"  Página {n} sem ofertas — parando.")
                break

            for i in range(total):
                card = cards.nth(i)
                try:
                    nome = card.locator(".poly-component__title").first.inner_text(timeout=2000).strip()
                    link = card.locator("a.poly-component__title, a[href*='/MLB'], a[href*='mercadolivre']").first.get_attribute("href", timeout=2000)
                    if not link or not nome:
                        continue

                    # "por" (preço atual)
                    por = _preco_float(
                        card.locator(".poly-price__current .andes-money-amount__fraction").first.inner_text(timeout=2000),
                        (card.locator(".poly-price__current .andes-money-amount__cents").first.inner_text(timeout=500)
                         if card.locator(".poly-price__current .andes-money-amount__cents").count() else "0"),
                    )
                    # "de" (preço riscado)
                    de_loc = card.locator("s.andes-money-amount--previous .andes-money-amount__fraction")
                    if de_loc.count() == 0:
                        continue  # sem desconto visível -> ignora
                    de = _preco_float(
                        de_loc.first.inner_text(timeout=2000),
                        (card.locator("s.andes-money-amount--previous .andes-money-amount__cents").first.inner_text(timeout=500)
                         if card.locator("s.andes-money-amount--previous .andes-money-amount__cents").count() else "0"),
                    )
                    if de <= 0 or por <= 0 or por >= de:
                        continue

                    # Imagem do produto (src ou data-src do lazy-load)
                    imagem = ""
                    try:
                        img = card.locator("img").first
                        imagem = (img.get_attribute("data-src", timeout=500)
                                  or img.get_attribute("src", timeout=500) or "")
                        if imagem.startswith("data:"):  # placeholder base64 do lazy-load
                            imagem = img.get_attribute("data-src", timeout=500) or ""
                    except Exception:
                        pass

                    # Selo Full (logo verde do ML Full): img/svg com alt/aria "Full"
                    full = False
                    try:
                        full = card.locator("svg[aria-label*='Full' i], img[alt*='Full' i]").count() > 0
                    except Exception:
                        pass

                    coletados.append({
                        "nome": nome[:255],
                        "link_produto": link.split("#")[0],
                        "preco_sem_desconto": de,
                        "preco_com_cupom": por,
                        "imagem_url": imagem[:1000],
                        "frete_full": full,
                    })
                except Exception as e:
                    print(f"  Erro num card: {e}")

    # Regrava ofertas (refresh total da origem='oferta')
    Produto.objects.filter(origem="oferta").delete()
    novos = [
        Produto(
            campanha_id="",
            origem="oferta",
            nome=o["nome"],
            preco_sem_desconto=o["preco_sem_desconto"],
            preco_com_cupom=o["preco_com_cupom"],
            link_produto=o["link_produto"],
            categoria="DESCONHECIDO",
            macro_categoria=classificar_oferta_por_nome(o["nome"]),
            imagem_url=o["imagem_url"],
            frete_full=o["frete_full"],
        )
        for o in coletados
    ]
    Produto.objects.bulk_create(novos, batch_size=500)
    print(f"OFERTAS: {len(novos)} salvas.")
    return len(novos)
