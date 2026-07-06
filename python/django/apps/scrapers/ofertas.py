import random
import requests
from datetime import timedelta
from django.utils import timezone
from django.db.models import F, FloatField, ExpressionWrapper
from apps.scrapers.models import Produto, Cupom, HistoricoEnvio
from apps.scrapers.precos import stats as _stats_preco

def esta_vivo(produto):
    """
    Estado da oferta no ML, em TRÊS valores (A1 — seleção não destrutiva):
      True  -> página 200 e sem texto de "pausado/esgotado".
      False -> CONFIRMADO morto (HTTP 404/410 ou texto de pausa/inexistente).
      None  -> DESCONHECIDO (timeout, erro de conexão, status estranho). NÃO apagar:
               uma instabilidade de rede não pode apagar um produto bom do banco.
    Só o chamador apaga, e somente quando recebe False.
    """
    from apps.scrapers.auxiliar import ua_aleatorio
    headers = {'User-Agent': ua_aleatorio()}
    termos_inativos = [
        "Anúncio pausado",
        "Este anúncio foi pausado",
        "Estoque indisponível",
        "Este item não está mais",
        "Página não encontrada",
    ]
    try:
        r = requests.get(produto.link_produto, headers=headers, timeout=5)
        if r.status_code in (404, 410):
            return False                      # confirmado: não existe mais
        if r.status_code != 200:
            return None                       # 5xx/redirect estranho -> incerto, mantém
        for termo in termos_inativos:
            if termo in r.text:
                return False                  # confirmado: pausado/esgotado
        return True
    except Exception:
        return None                           # timeout/conexão -> incerto, mantém

def selecionar_item_para_grupo(macros_selecionadas=None, categorias_selecionadas=None, limite_envio=1, horas_cooldown=24, min_desconto_percent=15.0, termo=None, marketplace=None, usuario=None):
    """
    Seleciona produtos da base usando a lógica de 'Roleta Viciada' (Weighted Random Choice).
    Leva em conta: O Desconto percentual, desconto absoluto, preço e novidade do cupom.
    Evita reenviar itens enviados há menos de `horas_cooldown`.
    `termo`: sub-nicho opcional — string com termos separados por vírgula; mantém só
    produtos cujo nome casa com ALGUM termo (ex: "aspirador robo, robot vacuum").
    """
    from django.db.models import Q
    from apps.scrapers.marketplaces.registry import get_marketplace

    # 1. Filtro inicial - tudo menos os cupons de RESGATE legados (origem='cupom'),
    # que não aplicam via link. Entram: 'oferta' (feed), 'busca' (termo), 'cupom_codigo'.
    qs = Produto.objects.exclude(origem="cupom")

    # Multi-tenant: pool COMPARTILHADO (owner=None, ex: ML) + itens PRIVADOS do usuário
    # (owner=usuario, ex: Amazon raspada com a conta dele). Sem usuario -> só o compartilhado.
    if usuario is not None:
        qs = qs.filter(Q(owner__isnull=True) | Q(owner=usuario))
    else:
        qs = qs.filter(owner__isnull=True)

    if marketplace:
        qs = qs.filter(marketplace=marketplace)
    if macros_selecionadas:
        qs = qs.filter(macro_categoria__in=macros_selecionadas)
    if categorias_selecionadas:
        qs = qs.filter(categoria__in=categorias_selecionadas)

    # Sub-nicho: filtra pelo nome (OR entre os termos)
    if termo:
        termos = [t.strip() for t in termo.split(",") if t.strip()]
        if termos:
            cond = Q()
            for t in termos:
                cond |= Q(nome__icontains=t)
            qs = qs.filter(cond)

    # 2. NUNCA repetir oferta: exclui produto já enviado alguma vez POR ESTE usuário.
    # (horas_cooldown ignorado de propósito — dedup é permanente, não janela.)
    # usuario=None (chamadas legadas) -> dedup global, como antes.
    hist = HistoricoEnvio.objects.all()
    if usuario is not None:
        hist = hist.filter(usuario=usuario)
    qs = qs.exclude(id__in=hist.values_list('produto_id', flat=True))

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

        # B1 — HISTÓRICO DE PREÇOS: o "de/por" do ML é frequentemente inflado.
        # Com histórico suficiente (>=3 pontos/30d), comparamos com o próprio preço
        # típico do item: preço "de sempre" -> desconto fictício, NÃO anuncia;
        # perto da mínima de 30 dias -> queda REAL, ganha boost forte.
        h = _stats_preco(prod, dias=30)
        if h and h["n"] >= 3:
            if prod.preco_com_cupom >= h["mediana"] * 0.98:
                continue  # não é oferta de verdade vs. o histórico do item
            if prod.preco_com_cupom <= h["minimo"] * 1.02:
                score *= 1.6  # perto da mínima histórica — oferta genuína

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

        # Checa o estado do anúncio (tri-state, A1) pela loja do produto:
        # ML faz GET na PDP; Amazon usa getItems. Agnóstico de marketplace.
        estado = get_marketplace(getattr(escolhido, "marketplace", "mercadolivre")).is_alive(escolhido)
        if estado is True:
            vencedores.append(escolhido)
        elif estado is False:
            # CONFIRMADO morto (404/pausado): aí sim pode limpar o banco.
            print(f"⚠️ Oferta morta confirmada: '{escolhido.nome}' (apagando do DB).")
            escolhido.delete()
        else:
            # None = incerto (timeout/erro). NÃO apaga; só pula nesta rodada.
            print(f"… estado incerto de '{escolhido.nome}' (mantém no DB, pula).")

        # Retira o escolhido da lista de sorteio atual
        idx = opcoes_sorteio.index(escolhido)
        opcoes_sorteio.pop(idx)
        pesos_sorteio.pop(idx)

    # NÃO grava HistoricoEnvio aqui! Só após o envio bem-sucedido (ver
    # management/commands/enviar_oferta.py). Gravar antes congelaria o produto
    # no cooldown mesmo se o link/envio falhasse.
    return vencedores


def _frase_marketing(produto):
    """Frase de marketing: usa o cache (frase_llm); só chama o Ollama ao vivo como
    último recurso, com timeout curto (10s, não os 120s antigos que travavam o envio),
    e GRAVA no cache p/ o próximo envio ser instantâneo."""
    cache = getattr(produto, "frase_llm", "") or ""
    if cache:
        return cache
    from apps.scrapers.llm import gerar_descricao
    frase = gerar_descricao(produto.nome, timeout=10)
    if frase and hasattr(produto, "save") and getattr(produto, "pk", None):
        try:
            produto.frase_llm = frase
            produto.save(update_fields=["frase_llm"])
        except Exception:
            pass
    return frase


def montar_mensagem(produto, link_afiliado: str, cupom_pai, markup=None) -> str:
    """
    Monta o texto da oferta usando o `Markup` do canal (WhatsApp *neg*, Telegram <b>).
    Conteúdo dinâmico passa por markup.escape p/ não quebrar HTML do Telegram.
    """
    from apps.scrapers.senders.base import WhatsAppMarkup
    m = markup or WhatsAppMarkup()
    esc = m.escape

    economia_rs = produto.preco_sem_desconto - produto.preco_com_cupom
    desconto_percent = (economia_rs / produto.preco_sem_desconto) * 100 if produto.preco_sem_desconto else 0

    linhas = [
        m.bold("🚨 OFERTAS DA RUDI 🚨"),
        f"📱 {esc(produto.nome.strip())}",
    ]

    frase = _frase_marketing(produto)
    if frase:
        linhas += ["", m.italic(esc(frase))]

    linhas += [
        "",
        f"❌ De: {m.strike(f'R$ {produto.preco_sem_desconto:.2f}')}",
        f"✅ {m.bold(f'Por: R$ {produto.preco_com_cupom:.2f}')} ({desconto_percent:.0f}% OFF)",
    ]

    # REGRA: cupons NÃO acumulam no ML. Cada item anuncia no máximo UM cupom.
    # Prioridade: cupom do link (cupom_pai) > código do próprio item (codigo_checkout)
    # > melhor código genérico VÁLIDO para este item. Nunca os três juntos.
    cod_item = getattr(produto, "codigo_checkout", "")
    if cupom_pai is not None:
        linhas += [
            m.italic(f"(Cupom: {esc(cupom_pai.titulo)})"),
            "",
            f"⚠️ {m.bold('ATIVE O CUPOM:')} abra o link, toque em {m.bold('Ativar cupom')} e o desconto entra no checkout.",
        ]
    elif cod_item:
        linhas += ["", f"🎟️ Use o cupom {m.bold(esc(cod_item))} no checkout"]
    else:
        # Códigos genéricos (CupomCodigo) são de checkout do ML — NÃO valem na Amazon
        # (lá cupom é de clipar, sem código). Só aplica p/ ML/legado.
        mkt = getattr(produto, "marketplace", "mercadolivre")
        codigo = _melhor_codigo(produto) if mkt in ("mercadolivre", "") else None
        if codigo:
            linhas += ["", f"🎟️ Use o cupom {m.code(esc(codigo))} no checkout"]
        else:
            linhas += ["", "✅ Desconto já aplicado no preço."]

    if getattr(produto, "frete_full", False):
        linhas.append(f"🚚 {m.bold('Full')} — frete grátis e entrega rápida")

    linhas += [
        "",
        m.bold("🛒 Compre aqui:"),
        f"👉 {esc(link_afiliado)}",
    ]
    return "\n".join(linhas)


# Back-compat: chamadas antigas continuam funcionando (markup WhatsApp default).
def montar_mensagem_whatsapp(produto, link_afiliado: str, cupom_pai) -> str:
    return montar_mensagem(produto, link_afiliado, cupom_pai)


def _melhor_codigo(produto):
    """
    Devolve o ÚNICO melhor código de checkout VÁLIDO para este item (ou None).

    Cupons não acumulam: escolhemos um só. Filtra por categoria/mínimo/validade
    via CupomCodigo.aplica_em e prioriza o de maior desconto percentual estimado.
    """
    from apps.scrapers.models import CupomCodigo
    candidatos = [c for c in CupomCodigo.objects.filter(ativo=True) if c.aplica_em(produto)]
    if not candidatos:
        return None

    def desconto_est(c):
        if c.tipo_desconto == "porcentagem":
            return produto.preco_com_cupom * (c.valor_desconto / 100.0)
        return c.valor_desconto

    melhor = max(candidatos, key=desconto_est)
    return f"{melhor.codigo} — {melhor.descricao}" if melhor.descricao else melhor.codigo


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


def enviar_oferta_de_produto(produto, grupo_id, verificar=True, dry_run=False, canal="whatsapp", usuario=None):
    """
    Núcleo de envio reutilizável e AGNÓSTICO de loja/canal:
      resolve marketplace (link afiliado + verificação) e sender (transporte) via registry.
      garante link -> checa tag afiliado (A3) -> (opcional) verifica destino -> monta msg
      no markup do canal -> envia. Grava HistoricoEnvio SOMENTE em envio bem-sucedido.

    Retorna dict: {sucesso, motivo?, link?, mensagem?, verificacao?, via?}
    """
    from django.conf import settings
    from apps.scrapers.marketplaces.registry import get_marketplace
    from apps.scrapers.senders.registry import get_sender

    mp = get_marketplace(getattr(produto, "marketplace", "mercadolivre"))
    sender = get_sender(canal)

    from apps.scrapers.auxiliar import BrowserError, SessaoExpirada
    from apps.scrapers.scraper_mercadolivre.link import LoginError, AuthError

    try:
        info = mp.build_affiliate_link(produto, usuario=usuario)
    except (LoginError, AuthError, SessaoExpirada) as e:
        # Sessão do ML caída: sem link de afiliado NENHUM produto sai. Motivo claro
        # + flag p/ a UI oferecer a reconexão e o chamador parar de retentar.
        return {"sucesso": False, "motivo": str(e), "precisa_login_ml": True}
    except BrowserError as e:
        texto = str(e)
        return {"sucesso": False, "motivo": texto.replace("LOGIN_REQUIRED: ", ""),
                "precisa_login_ml": "LOGIN_REQUIRED" in texto}
    if not info:
        return {"sucesso": False, "motivo": "falha ao gerar link de afiliado "
                "(URL não afiliável ou o Link Builder recusou — veja o log acima)"}
    link = info["link_afiliado"]

    # A3 — sem tag de afiliado o clique não gera comissão. Recusa (ou avisa).
    afiliado_ok = info.get("afiliado_ok")
    if afiliado_ok is None:
        afiliado_ok = mp.verify_affiliate_tag(link, usuario=usuario)
    if not afiliado_ok:
        if getattr(settings, "AFILIADO_EXIGIR", True):
            return {"sucesso": False, "motivo": "link sem tag de afiliado (A3) — não enviado",
                    "link": link}
        print(f"⚠️ AVISO: link sem tag de afiliado, enviando assim mesmo: {link}")

    verificacao = None
    if verificar:
        # 'oferta'/'busca' têm de/por confirmado na raspagem; 'cupom_codigo' precisa
        # confirmar o desconto/badge na PDP (confiar_desconto=False).
        origem = getattr(produto, "origem", "cupom")
        confiar = origem in ("oferta", "busca")
        verificacao = mp.verify_link(link, nome_esperado=produto.nome, confiar_desconto=confiar)
        if not verificacao.get("ok"):
            return {"sucesso": False, "motivo": "link reprovado na verificação",
                    "link": link, "verificacao": verificacao}

    # Ofertas (origem='oferta') não têm Cupom; só busca quando há campanha_id
    cupom = None
    if produto.campanha_id:
        cupom = Cupom.objects.filter(campanha_id=produto.campanha_id).first()
    mensagem = montar_mensagem(produto, link, cupom, markup=sender.markup)

    if dry_run:
        return {"sucesso": True, "dry_run": True, "link": link,
                "mensagem": mensagem, "verificacao": verificacao}

    # Sessão WhatsApp do DONO (multi-tenant): envia pela conexão dele, não pela default.
    wa_session = None
    if usuario is not None:
        perfil = getattr(usuario, "perfil", None)
        wa_session = perfil.sessao_whatsapp() if perfil else (str(getattr(usuario, "id", "")) or None)

    # Imagem conforme o canal: Telegram aceita URL direto; WhatsApp precisa de base64.
    if sender.prefers_image == "url":
        resultado = sender.enviar_oferta(grupo_id, mensagem,
                                         imagem_url=getattr(produto, "imagem_url", "") or None,
                                         legenda=mensagem, usuario=usuario, session=wa_session)
    else:
        imagem_b64, img_mime = _baixar_imagem_b64(getattr(produto, "imagem_url", ""))
        resultado = sender.enviar_oferta(grupo_id, mensagem, imagem_b64=imagem_b64 or None,
                                         mimetype=img_mime or "image/jpeg", legenda=mensagem,
                                         usuario=usuario, session=wa_session)

    if resultado.get("sucesso"):
        HistoricoEnvio.objects.create(produto=produto, usuario=usuario)  # só após sucesso
        return {"sucesso": True, "link": link, "mensagem": mensagem,
                "via": resultado.get("via"), "verificacao": verificacao}
    return {"sucesso": False, "motivo": resultado.get("erro") or "falha no envio",
            "link": link, "verificacao": verificacao}


def selecionar_e_enviar(macros, grupo_id, min_desconto_percent=15.0,
                        horas_cooldown=24, max_tentativas=8, verificar=True, dry_run=False,
                        termo=None, canal="whatsapp", marketplace=None, usuario=None):
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
        termo=termo,
        marketplace=marketplace,
        usuario=usuario,
    )
    if not pool:
        return {"sucesso": False, "motivo": "sem item elegível"}

    ultimo = None
    for prod in pool:
        print(f"Tentando: {prod.nome[:60]} (origem={getattr(prod,'origem','cupom')}, mkt={getattr(prod,'marketplace','?')})")
        r = enviar_oferta_de_produto(prod, grupo_id, verificar=verificar, dry_run=dry_run, canal=canal, usuario=usuario)
        if r.get("sucesso"):
            return r
        print(f"  reprovado: {r.get('motivo')}")
        ultimo = r
        if r.get("precisa_login_ml"):
            # Sessão do ML caiu: os demais candidatos falhariam igual (cada tentativa
            # abre um browser e leva ~30s). Aborta e devolve o motivo real.
            return r
    return ultimo or {"sucesso": False, "motivo": "nenhum candidato passou"}


def processar_configs_de_envio():
    """
    Percorre ConfiguracaoEnvio ativas; para cada uma vencida (now - ultimo_envio >=
    intervalo), seleciona 1 item do nicho e envia. Chamado pelo tick do Celery.
    Retorna lista de resultados por config.
    """
    from apps.scrapers.models import ConfiguracaoEnvio, HistoricoEnvio

    agora = timezone.now()
    hoje = timezone.localtime(agora).date()
    resultados = []
    # Cache por-owner dentro do tick: quantos envios já saíram hoje (cota diária).
    _envios_hoje: dict = {}

    def _cota_estourada(owner) -> bool:
        """True se o dono está suspenso ou já bateu a cota diária de envios.
        owner=None (pool legado/compartilhado) não tem dono → sem cota/bloqueio."""
        if owner is None:
            return False
        perfil = getattr(owner, "perfil", None)
        if perfil and perfil.bloqueado:
            return True
        if owner.id not in _envios_hoje:
            _envios_hoje[owner.id] = HistoricoEnvio.objects.filter(
                usuario=owner, data_envio__date=hoje).count()
        limite = perfil.cota_max_envios_dia() if perfil else 0
        return bool(limite) and _envios_hoje[owner.id] >= limite

    for cfg in ConfiguracaoEnvio.objects.filter(ativo=True).select_related("owner__perfil"):
        # 0. Dono suspenso ou cota diária estourada → nunca envia.
        if _cota_estourada(cfg.owner):
            continue
        # 1. Respeita a janela de horário (ex: 8h-20h). Fora dela, nunca envia.
        if not cfg.dentro_da_janela(agora):
            continue
        # 2. Vencido = sem agendamento ainda OU passou do proximo_envio (intervalo + jitter).
        vencido = cfg.proximo_envio is None or agora >= cfg.proximo_envio
        if not vencido:
            continue

        macros = [cfg.macro_categoria] if cfg.macro_categoria else None  # vazio = qualquer (inclui ofertas)
        r = selecionar_e_enviar(
            macros, cfg.grupo_id,
            min_desconto_percent=cfg.min_desconto_percent,
            horas_cooldown=cfg.horas_cooldown,
            verificar=True,
            termo=cfg.termo_busca,
            canal=getattr(cfg, "canal", "whatsapp"),
            marketplace=getattr(cfg, "marketplace", "") or None,
            usuario=cfg.owner,
        )
        # Reagenda sempre (sucesso ou não) p/ não ficar martelando o mesmo tick;
        # jitter ±1-10min deixa o ritmo humano. ultimo_envio só em sucesso (display).
        cfg.agendar_proximo(agora)
        if r.get("sucesso"):
            cfg.ultimo_envio = agora
            if cfg.owner_id is not None:
                _envios_hoje[cfg.owner_id] = _envios_hoje.get(cfg.owner_id, 0) + 1
        cfg.save(update_fields=["proximo_envio", "ultimo_envio"])
        resultados.append({"config": cfg.id, **r})
    return resultados
