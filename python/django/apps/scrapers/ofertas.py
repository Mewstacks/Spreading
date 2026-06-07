import random
import requests
from datetime import timedelta
from django.utils import timezone
from django.db.models import F, FloatField, ExpressionWrapper
from apps.scrapers.models import Produto, Cupom, HistoricoEnvio

def validar_oferta_ativa(produto):
    """
    Tenta acessar a página do produto pelo Requests rapidamente para verificar 
    se ele não foi pausado, esgotou ou saiu do ar.
    Retorna True se estiver ativo, False se estiver inválido.
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    try:
        r = requests.get(produto.link_produto, headers=headers, timeout=5)
        # Se deu página quebrada/redirecionamento bizarro
        if r.status_code != 200:
            return False
            
        texto_html = r.text
        # Termos comuns que o ML exibe quando a oferta esgota ou pausa:
        termos_inativos = [
            "Anúncio pausado",
            "Este anúncio foi pausado",
            "Estoque indisponível",
            "Este item não está mais",
            "Página não encontrada"
        ]
        
        for termo in termos_inativos:
            if termo in texto_html:
                return False
                
        return True
    except Exception:
        # Em caso de timeout/erro de conexão, assumimos falso para não arriscar
        return False

def selecionar_item_para_grupo(macros_selecionadas=None, categorias_selecionadas=None, limite_envio=1, horas_cooldown=24, min_desconto_percent=15.0):
    """
    Seleciona produtos da base usando a lógica de 'Roleta Viciada' (Weighted Random Choice).
    Leva em conta: O Desconto percentual, desconto absoluto, preço e novidade do cupom.
    Evita reenviar itens enviados há menos de `horas_cooldown`.
    """
    
    # 1. Filtro inicial - só OFERTAS (desconto já no preço, verificável).
    # Itens origem='cupom' não entram: cupons de resgate não aplicam via link.
    qs = Produto.objects.filter(origem="oferta")

    if macros_selecionadas:
        qs = qs.filter(macro_categoria__in=macros_selecionadas)
    if categorias_selecionadas:
        qs = qs.filter(categoria__in=categorias_selecionadas)

    # 2. Excluir da lista quem tá na "Geladeira" (Cooldown)
    tempo_limite = timezone.now() - timedelta(hours=horas_cooldown)
    itens_recentes_ids = HistoricoEnvio.objects.filter(
        data_envio__gte=tempo_limite
    ).values_list('produto_id', flat=True)
    
    qs = qs.exclude(id__in=itens_recentes_ids)

    # 3. Calcula economia e desconto (%) - Mantém apenas os válidos (> 10% e < 90%)
    # Desconto >= 90% indica dado corrompido (ex: cupom fixo maior que o preço do produto)
    produtos_elegiveis = qs.annotate(
        economia_rs=ExpressionWrapper(F('preco_sem_desconto') - F('preco_com_cupom'), output_field=FloatField()),
        desconto_percent=ExpressionWrapper(((F('preco_sem_desconto') - F('preco_com_cupom')) / F('preco_sem_desconto')) * 100, output_field=FloatField())
    ).filter(desconto_percent__gte=min_desconto_percent, desconto_percent__lt=90.0, preco_com_cupom__gt=0)

    # Buscamos cupons numa tacada só para checar 'data_criacao' depois
    campanhas_ids = list(produtos_elegiveis.values_list('campanha_id', flat=True))
    cupons_map = {c.campanha_id: c for c in Cupom.objects.filter(campanha_id__in=campanhas_ids)}

    opcoes_sorteio = []
    pesos_sorteio = []

    for prod in produtos_elegiveis:
        cupom = cupons_map.get(prod.campanha_id)

        # Descarta produto que não atinge o valor mínimo de compra do cupom
        if cupom and cupom.valor_minimo > 0 and prod.preco_sem_desconto < cupom.valor_minimo:
            continue

        # PONTUAÇÃO BASE: O peso foca bastante no Desconto Percentual
        score = prod.desconto_percent * 2.0 
        
        # BÔNUS ECONOMIA (R$): Ajuda produtos caros com bom desconto em R$
        score += (prod.economia_rs / 20.0)
        
        # BÔNUS TICKET BAIXO: Produtos baratos (<R$30) recebem mais chance
        if prod.preco_com_cupom < 30.0:
            score += 20.0
            
        # BÔNUS URGÊNCIA: Cupom novo (criado nas últimas 12h) recebe Boost de 50%
        if cupom and cupom.data_criacao >= timezone.now() - timedelta(hours=12):
            score *= 1.5
            
        opcoes_sorteio.append(prod)
        pesos_sorteio.append(score)

    if not opcoes_sorteio:
        return []

    # 4. Sorteio (A 'Roleta Viciada') com VALIDAÇÃO Just-in-Time!
    vencedores = []
    tentativas = 0
    max_tentativas = limite_envio * 10 # proteção contra loop infinito
    
    while len(vencedores) < limite_envio and opcoes_sorteio and tentativas < max_tentativas:
        tentativas += 1
        escolhido = random.choices(population=opcoes_sorteio, weights=pesos_sorteio, k=1)[0]
        
        # Checa em milissegundos se o anúncio ainda tá vivo no ML
        if validar_oferta_ativa(escolhido):
            vencedores.append(escolhido)
        else:
            print(f"⚠️ Oferta morta: '{escolhido.nome}' (apagando do DB e sorteando outro...)")
            escolhido.delete() # Limpa nosso banco na hora!
            
        # Retira o escolhido da lista de sorteio atual
        idx = opcoes_sorteio.index(escolhido)
        opcoes_sorteio.pop(idx)
        pesos_sorteio.pop(idx)

    # NÃO grava HistoricoEnvio aqui! Só após o envio bem-sucedido (ver
    # management/commands/enviar_oferta.py). Gravar antes congelaria o produto
    # no cooldown mesmo se o link/envio falhasse.
    return vencedores


def montar_mensagem_whatsapp(produto_isca, link_afiliado: str, cupom_pai) -> str:
    """
    Monta o texto para o WhatsApp/Telegram no novo formato:
    - SEM código de cupom (tokens longos não são códigos legíveis)
    - COM instruções de como clicar em 'Aplicar Cupom' na landing page
    - Link aponta para a URL do Container (já afiliado)
    """
    economia_rs = produto_isca.preco_sem_desconto - produto_isca.preco_com_cupom
    desconto_percent = (economia_rs / produto_isca.preco_sem_desconto) * 100 if produto_isca.preco_sem_desconto else 0

    linhas = [
        f"🚨 *OFERTA DETECTADA* 🚨",
        f"📱 {produto_isca.nome.strip()}",
        "",
        f"❌ De: ~R$ {produto_isca.preco_sem_desconto:.2f}~",
        f"✅ *Por: R$ {produto_isca.preco_com_cupom:.2f}* ({desconto_percent:.0f}% OFF)",
    ]

    if cupom_pai is not None:
        # Cupom de resgate: o desconto entra ao ativar o cupom / no checkout
        linhas += [
            f"_(Cupom: {cupom_pai.titulo})_",
            "",
            "⚠️ *ATIVE O CUPOM:* abra o link, toque em *Ativar cupom* e o desconto entra no checkout.",
        ]
    else:
        # Oferta direta: desconto já no preço
        linhas += ["", "✅ Desconto já aplicado no preço."]

    if getattr(produto_isca, "frete_full", False):
        linhas.append("🚚 *Full* — frete grátis e entrega rápida")

    # Cupons de CÓDIGO ativos aplicáveis (digitar no checkout)
    codigos = _codigos_aplicaveis(produto_isca.preco_com_cupom)
    if codigos:
        linhas += ["", "🏷️ *Cupons p/ usar no checkout:*"]
        linhas += [f"• `{c}`" for c in codigos]

    linhas += [
        "",
        "🛒 *Compre aqui:*",
        f"👉 {link_afiliado}",
        "",
        "#publicidade",
    ]
    return "\n".join(linhas)


def _codigos_aplicaveis(preco):
    """Lista de strings 'CODIGO — descrição' dos CupomCodigo ativos cujo mínimo cabe no preço."""
    from apps.scrapers.models import CupomCodigo
    hoje = timezone.now().date()
    out = []
    for c in CupomCodigo.objects.filter(ativo=True):
        if c.validade and c.validade < hoje:
            continue
        if c.valor_minimo and preco < c.valor_minimo:
            continue
        desc = f" — {c.descricao}" if c.descricao else ""
        out.append(f"{c.codigo}{desc}")
    return out


def _baixar_imagem_b64(url):
    """
    Baixa a imagem e converte p/ JPEG -> (base64, 'image/jpeg').
    Converte porque o whatsapp-web.js falha ao enviar webp (formato padrão do ML).
    ('', '') se falhar/sem url.
    """
    if not url or not url.startswith("http"):
        return "", ""
    import base64
    from io import BytesIO
    try:
        r = requests.get(url, timeout=8)
        if r.status_code != 200 or not r.content:
            return "", ""
        from PIL import Image
        img = Image.open(BytesIO(r.content)).convert("RGB")
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"
    except Exception as e:
        print(f"[img] falha ao processar imagem: {e}")
        return "", ""


def enviar_oferta_de_produto(produto, grupo_id, verificar=True, dry_run=False):
    """
    Núcleo de envio reutilizável (comando manual + tasks Celery):
      garante link afiliado -> (opcional) verifica no browser -> monta msg -> envia.
    Grava HistoricoEnvio SOMENTE em envio bem-sucedido.

    Retorna dict: {sucesso, motivo?, link?, mensagem?, verificacao?, via?}
    """
    from apps.scrapers.scraper_mercadolivre.link import (
        gerar_link_afiliado_para_produto, verificar_link_afiliado,
    )
    from apps.scrapers import whatsapp_client

    info = gerar_link_afiliado_para_produto(produto)
    if not info:
        return {"sucesso": False, "motivo": "falha ao gerar link de afiliado"}
    link = info["link_afiliado"]

    verificacao = None
    if verificar:
        # Ofertas (de/por) já têm desconto confirmado na raspagem; cupom precisa confirmar na página.
        eh_oferta = getattr(produto, "origem", "cupom") == "oferta"
        verificacao = verificar_link_afiliado(link, nome_esperado=produto.nome, confiar_desconto=eh_oferta)
        if not verificacao.get("ok"):
            return {"sucesso": False, "motivo": "link reprovado na verificação",
                    "link": link, "verificacao": verificacao}

    # Ofertas (origem='oferta') não têm Cupom; só busca quando há campanha_id
    cupom = None
    if produto.campanha_id:
        cupom = Cupom.objects.filter(campanha_id=produto.campanha_id).first()
    mensagem = montar_mensagem_whatsapp(produto, link, cupom)

    if dry_run:
        return {"sucesso": True, "dry_run": True, "link": link,
                "mensagem": mensagem, "verificacao": verificacao}

    # Tenta enviar com imagem (mídia); cai p/ texto se baixar falhar
    imagem_b64, img_mime = _baixar_imagem_b64(getattr(produto, "imagem_url", ""))
    if imagem_b64:
        resultado = whatsapp_client.enviar_oferta(
            grupo_id, mensagem, imagem_base64=imagem_b64,
            mimetype=img_mime or "image/jpeg", legenda=mensagem)
    else:
        resultado = whatsapp_client.enviar_oferta(grupo_id, mensagem)
    if resultado.get("sucesso"):
        HistoricoEnvio.objects.create(produto=produto)  # só após sucesso
        return {"sucesso": True, "link": link, "mensagem": mensagem,
                "via": resultado.get("via"), "verificacao": verificacao}
    return {"sucesso": False, "motivo": resultado.get("erro") or "falha no envio",
            "link": link, "verificacao": verificacao}


def selecionar_e_enviar(macros, grupo_id, min_desconto_percent=15.0,
                        horas_cooldown=24, max_tentativas=8, verificar=True, dry_run=False):
    """
    Seleciona um POOL de candidatos do nicho e tenta enviar um por um até o primeiro
    que passa na verificação. Devolve o resultado do envio bem-sucedido, ou o último
    erro / 'sem item elegível'. Evita abortar por causa de um único item que reprova.
    """
    pool = selecionar_item_para_grupo(
        macros_selecionadas=macros,
        limite_envio=max_tentativas,
        horas_cooldown=horas_cooldown,
        min_desconto_percent=min_desconto_percent,
    )
    if not pool:
        return {"sucesso": False, "motivo": "sem item elegível"}

    ultimo = None
    for prod in pool:
        print(f"Tentando: {prod.nome[:60]} (origem={getattr(prod,'origem','cupom')})")
        r = enviar_oferta_de_produto(prod, grupo_id, verificar=verificar, dry_run=dry_run)
        if r.get("sucesso"):
            return r
        print(f"  reprovado: {r.get('motivo')}")
        ultimo = r
    return ultimo or {"sucesso": False, "motivo": "nenhum candidato passou"}


def processar_configs_de_envio():
    """
    Percorre ConfiguracaoEnvio ativas; para cada uma vencida (now - ultimo_envio >=
    intervalo), seleciona 1 item do nicho e envia. Chamado pelo tick do Celery.
    Retorna lista de resultados por config.
    """
    from apps.scrapers.models import ConfiguracaoEnvio

    agora = timezone.now()
    resultados = []
    for cfg in ConfiguracaoEnvio.objects.filter(ativo=True):
        vencido = (cfg.ultimo_envio is None or
                   agora - cfg.ultimo_envio >= timedelta(minutes=cfg.intervalo_minutos))
        if not vencido:
            continue

        macros = [cfg.macro_categoria] if cfg.macro_categoria else None  # vazio = qualquer (inclui ofertas)
        r = selecionar_e_enviar(
            macros, cfg.grupo_id,
            min_desconto_percent=cfg.min_desconto_percent,
            horas_cooldown=cfg.horas_cooldown,
            verificar=True,
        )
        if r.get("sucesso"):
            cfg.ultimo_envio = agora
            cfg.save(update_fields=["ultimo_envio"])
        resultados.append({"config": cfg.id, **r})
    return resultados
