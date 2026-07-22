"""
Scraper das OFERTAS do Mercado Livre (https://www.mercadolivre.com.br/ofertas).

Diferente dos cupons: aqui o desconto JÁ está no preço (de/por), visível na PDP,
sem necessidade de resgate. Toda oferta raspada tem desconto real.

Cada oferta vira um Produto com origem='oferta'. preco_sem_desconto = "de",
preco_com_cupom = "por" (mantemos o nome do campo p/ reaproveitar a seleção/envio).
"""
import os
import re
import logging

from django.db import OperationalError, connections

from apps.scrapers.auxiliar import iniciar_browser, pausa_humana
from apps.scrapers.models import Produto
from apps.scrapers.progresso import emitir_progresso
from apps.scrapers.session_paths import ml_auth_path

caminho_atual = os.path.dirname(os.path.abspath(__file__))
logger = logging.getLogger(__name__)


def _reconectar_db():
    """Descarta conexões possivelmente mortas; o Django reabre na próxima query.

    A raspagem coleta primeiro (minutos na fase de browser) e só depois salva. Nesse
    intervalo a conexão aberta no início do ciclo fica ociosa, e o Postgres/proxy da
    Fly derruba o socket sem o Django saber. Sem isto, a 1ª query do save reusa o
    socket morto e estoura OperationalError("server closed the connection
    unexpectedly"). Chamado no começo de cada fase de save.
    """
    connections.close_all()


def _upsert_resiliente(**kwargs):
    """`Produto.update_or_create` tolerante a socket morto: numa OperationalError,
    reconecta e tenta de novo uma vez (cobre a queda no meio do save)."""
    try:
        return Produto.objects.update_or_create(**kwargs)
    except OperationalError:
        logger.warning("Conexão do banco caiu no save; reconectando e tentando de novo.")
        _reconectar_db()
        return Produto.objects.update_or_create(**kwargs)

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


# Catálogo de SUB-NICHOS: macro -> [(rótulo, termos separados por vírgula)].
# O 'value' do option é a própria string de termos (vai pro termo_busca).
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


def _preco_float(texto_frac, texto_cents="0"):
    frac = (texto_frac or "0").replace(".", "").strip()
    cents = (texto_cents or "0").strip() or "0"
    try:
        return float(f"{frac}.{cents.zfill(2)}")
    except ValueError:
        return 0.0


def _coletar_cards(page):
    """Extrai todos os cards com desconto (de/por) da página atual. Lista de dicts.

    Conta POR QUE cada card foi descartado. Os motivos moravam em `continue` mudos e
    num logger.debug — que o LOGGING em INFO apaga em produção. Um seletor renomeado
    pelo ML zerava a coleta e o único sinal era o total, que só cai quando TUDO
    quebra: enquanto uma parte funcionasse, ninguém via nada.
    """
    out = []
    descartes = {"sem_nome_ou_link": 0, "sem_desconto": 0, "preco_invalido": 0,
                 "erro_no_card": 0}
    cards = page.locator(".poly-card")
    total = cards.count()
    for i in range(total):
        card = cards.nth(i)
        try:
            nome = card.locator(".poly-component__title").first.inner_text(timeout=2000).strip()
            link = card.locator("a.poly-component__title, a[href*='/MLB'], a[href*='mercadolivre']").first.get_attribute("href", timeout=2000)
            if not link or not nome:
                descartes["sem_nome_ou_link"] += 1
                continue

            por = _preco_float(
                card.locator(".poly-price__current .andes-money-amount__fraction").first.inner_text(timeout=2000),
                (card.locator(".poly-price__current .andes-money-amount__cents").first.inner_text(timeout=500)
                 if card.locator(".poly-price__current .andes-money-amount__cents").count() else "0"),
            )
            de_loc = card.locator("s.andes-money-amount--previous .andes-money-amount__fraction")
            if de_loc.count() == 0:
                descartes["sem_desconto"] += 1
                continue  # sem desconto visível
            de = _preco_float(
                de_loc.first.inner_text(timeout=2000),
                (card.locator("s.andes-money-amount--previous .andes-money-amount__cents").first.inner_text(timeout=500)
                 if card.locator("s.andes-money-amount--previous .andes-money-amount__cents").count() else "0"),
            )
            if de <= 0 or por <= 0 or por >= de:
                descartes["preco_invalido"] += 1
                continue

            imagem = ""
            try:
                img = card.locator("img").first
                imagem = (img.get_attribute("data-src", timeout=500)
                          or img.get_attribute("src", timeout=500) or "")
                if imagem.startswith("data:"):
                    imagem = img.get_attribute("data-src", timeout=500) or ""
            except Exception:
                pass

            full = False
            try:
                full = card.locator("svg[aria-label*='Full' i], img[alt*='Full' i]").count() > 0
            except Exception:
                pass

            relampago = False
            try:
                relampago = card.get_by_text(re.compile(r"rel[âa]mpago", re.I)).count() > 0
            except Exception:
                pass

            out.append({
                "nome": nome[:255],
                "link_produto": link.split("#")[0],
                "preco_sem_desconto": de,
                "preco_com_cupom": por,
                "imagem_url": imagem[:1000],
                "frete_full": full,
                "relampago": relampago,
            })
        except Exception as e:
            descartes["erro_no_card"] += 1
            logger.debug("Erro num card de oferta ML: %s", e)
    _logar_descartes(total, len(out), descartes)
    return out


def _logar_descartes(total, aproveitados, descartes):
    """Resumo por etapa. É o sinal que teria mostrado um seletor quebrado no dia."""
    perdidos = total - aproveitados
    if not total or not perdidos:
        return
    detalhe = ", ".join(f"{n} {motivo.replace('_', ' ')}"
                        for motivo, n in descartes.items() if n)
    # Descartar card sem desconto é o trabalho normal desta função; o que merece
    # atenção é perder card por erro ou por não achar nome/link — aí o seletor mudou.
    quebrados = descartes["erro_no_card"] + descartes["sem_nome_ou_link"]
    nivel = logger.warning if quebrados else logger.info
    nivel("Cards ML: %s lidos, %s aproveitados, %s descartados (%s)",
          total, aproveitados, perdidos, detalhe)


def _salvar(coletados, origem, codigo_checkout="", macro_fixa=None):
    """Upsert não destrutivo. Uma coleta parcial nunca apaga o catálogo anterior."""
    _reconectar_db()  # conexão fresca: a fase de browser pode ter matado o socket
    vistos = set()
    salvos = []
    for o in coletados:
        if o["link_produto"] in vistos:
            continue
        vistos.add(o["link_produto"])
        from apps.scrapers.scraper_mercadolivre.link import e_catalogo_universal
        catalogo = e_catalogo_universal(o["link_produto"])
        produto, _ = _upsert_resiliente(
            marketplace="mercadolivre", owner=None, link_produto=o["link_produto"],
            defaults={"campanha_id": "", "origem": origem,
                      "fonte": "mercadolivre-web", "codigo_checkout": codigo_checkout,
                      "nome": o["nome"], "preco_sem_desconto": o["preco_sem_desconto"],
                      "preco_com_cupom": o["preco_com_cupom"],
                      "preco_fonte": o["preco_sem_desconto"],
                      "preco_efetivo": o["preco_com_cupom"],
                      "estado": "invalido" if catalogo else "ativo",
                      "falha_verificacao": (
                          "Catálogo universal sem anúncio individual afiliável."
                          if catalogo else ""), "falhas_consecutivas": 0,
                      "confianca": "media", "evidencia": {"transport": "public-web"},
                      "categoria": "DESCONHECIDO",
                      "macro_categoria": macro_fixa or classificar_oferta_por_nome(o["nome"]),
                      "imagem_url": o["imagem_url"], "frete_full": o["frete_full"],
                      "relampago": o.get("relampago", False)})
        if catalogo:
            # Falhas terminais antigas não devem continuar ocupando a tela nem a
            # fila quando a regra agora é global: catálogo universal não publica.
            produto.links_usuario.all().delete()
        else:
            salvos.append(produto)
    # Histórico de preços (B1): 1 observação por item p/ detectar queda real depois.
    from apps.scrapers.precos import registrar_varios
    registrar_varios(salvos)
    return len(salvos)


def _upsert_ofertas(coletados):
    """Insere/atualiza ofertas por link SEM apagar o feed (usado pela LANE RÁPIDA/flash,
    B3, que roda com poucas páginas e não pode zerar o feed completo da lane lenta)."""
    from apps.scrapers.precos import registrar
    _reconectar_db()  # conexão fresca: a fase de browser pode ter matado o socket
    vistos, n = set(), 0
    for o in coletados:
        if o["link_produto"] in vistos:
            continue
        vistos.add(o["link_produto"])
        from apps.scrapers.scraper_mercadolivre.link import e_catalogo_universal
        catalogo = e_catalogo_universal(o["link_produto"])
        produto, _ = _upsert_resiliente(
            origem="oferta", link_produto=o["link_produto"], owner=None,
            defaults={
                "nome": o["nome"],
                "preco_sem_desconto": o["preco_sem_desconto"],
                "preco_com_cupom": o["preco_com_cupom"],
                "preco_fonte": o["preco_sem_desconto"],
                "preco_efetivo": o["preco_com_cupom"],
                "fonte": "mercadolivre-web",
                "estado": "invalido" if catalogo else "ativo",
                "falha_verificacao": (
                    "Catálogo universal sem anúncio individual afiliável."
                    if catalogo else ""),
                "categoria": "DESCONHECIDO",
                "macro_categoria": classificar_oferta_por_nome(o["nome"]),
                "imagem_url": o["imagem_url"],
                "frete_full": o["frete_full"],
                "relampago": o.get("relampago", False),
            },
        )
        if catalogo:
            produto.links_usuario.all().delete()
        else:
            registrar("mercadolivre", "", o["link_produto"], o["preco_com_cupom"])
            n += 1
    return n


def mapear_ofertas(max_paginas=40, substituir=True):
    """Raspa N páginas de /ofertas. substituir=True (lane LENTA): regrava todo o feed.
    substituir=False (lane RÁPIDA/flash, B3): upsert por link, sem zerar o feed."""
    logger.info("Iniciando raspagem de ofertas ML (%s)", "full" if substituir else "flash")
    coletados = []
    caminho_auth = ml_auth_path()

    with iniciar_browser(auth_path=caminho_auth, headless=True,
                         validar_sessao=False) as (page, context):
        for n in range(1, max_paginas + 1):
            emitir_progresso(f"[PROGRESSO] Ofertas página {n}/{max_paginas} ({n*100//max_paginas}%)")
            try:
                page.goto(f"https://www.mercadolivre.com.br/ofertas?page={n}",
                          wait_until="domcontentloaded", timeout=45000)
                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass
            except Exception as e:
                logger.warning("Erro ao carregar pagina de ofertas ML %s: %s", n, e)
                continue
            cards = _coletar_cards(page)
            if not cards:
                logger.info("Pagina %s sem ofertas; parando", n)
                break
            coletados.extend(cards)
            pausa_humana()  # ritmo humano entre páginas (anti-bloqueio)

    if not coletados:
        logger.warning("Raspagem de ofertas ML vazia; feed existente preservado")
        return 0
    n = _salvar(coletados, origem="oferta")
    logger.info("Ofertas ML salvas/atualizadas: %s", n)
    return n


def _slug_busca(termo):
    """Converte 'robô aspirador' -> 'robo-aspirador' para a URL de busca do ML."""
    import unicodedata
    t = unicodedata.normalize("NFKD", termo).encode("ascii", "ignore").decode().lower().strip()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def buscar_por_termo(termo_busca, min_desconto=15, max_paginas=3, macro=None):
    """
    Para cada termo (lista separada por vírgula) raspa a BUSCA do ML com filtro de
    desconto e salva como origem='busca'. Atualiza só os itens 'busca' que casam com
    estes termos (não mexe no feed nem nos cupons-código).
    """
    termos = [t.strip() for t in (termo_busca or "").split(",") if t.strip()]
    if not termos:
        return 0
    caminho_auth = ml_auth_path()
    coletados = []

    with iniciar_browser(auth_path=caminho_auth, headless=True,
                         validar_sessao=False) as (page, context):
        for termo in termos:
            slug = _slug_busca(termo)
            if not slug:
                continue
            for p in range(max_paginas):
                desde = p * 50 + 1
                url = f"https://lista.mercadolivre.com.br/{slug}_Discount_{int(min_desconto)}-100"
                if desde > 1:
                    url += f"_Desde_{desde}"
                emitir_progresso(f"[PROGRESSO] Busca '{termo}' pág {p+1}/{max_paginas}")
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
                except Exception as e:
                    logger.warning("Erro na busca ML por termo '%s': %s", termo, e)
                    break
                cards = _coletar_cards(page)
                if not cards:
                    break
                coletados.extend(cards)
                pausa_humana()  # ritmo humano entre páginas (anti-bloqueio)

    # Refresh escopado: remove itens 'busca' que casam com algum termo, recria.
    # Reconecta antes: o delete é a 1ª query após a longa fase de browser.
    _reconectar_db()
    from django.db.models import Q
    cond = Q()
    for t in termos:
        cond |= Q(nome__icontains=t)
    Produto.objects.filter(
        marketplace="mercadolivre", owner__isnull=True, origem="busca"
    ).filter(cond).delete()
    n = _salvar(coletados, origem="busca", macro_fixa=macro)
    logger.info("Busca ML '%s': %s produtos salvos", termo_busca, n)
    return n
