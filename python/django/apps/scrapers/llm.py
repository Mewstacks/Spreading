import json
import logging
import re

from django.conf import settings

logger = logging.getLogger(__name__)

_MODELO_PADRAO = "claude-sonnet-5"

_PROMPT = """Você escreve a chamada de um achado para grupo de WhatsApp no Brasil.
Tom: assertivo, concreto, curto. Alguém que achou um desconto de verdade, não um influencer.

REGRAS OBRIGATÓRIAS:
1. "titulo": TUDO EM CAIXA ALTA, 3 a 6 palavras, sem aspas, emoji, ponto, preço, %, R$ ou a palavra cupom.
2. O título nomeia o ganho (o que a pessoa leva ou o que muda). Proibido: IMPERDÍVEL, OPORTUNIDADE, CORRE, BORA, CLIQUE, OFERTA, PROMOÇÃO, DESCONTO, OFF, PROMO.
3. Sem piada sobre o comprador. Sem "pra tu que". Sem "se sentir".
4. "nome_curto": tipo + marca + modelo + no máximo 2 características. Máximo 70 caracteres. Não invente.
5. Sem Markdown, asterisco ou formatação.
6. Responda SOMENTE JSON: {{"titulo":"...","nome_curto":"..."}}.

Exemplos:
Produto: Multivitamínico 120 Cáps. Growth Supplements
Resposta: {{"titulo":"VITAMINA SEM ENROLAÇÃO","nome_curto":"Multivitamínico Growth 120 cápsulas"}}

Produto: Cadeira Gamer Wells Preta Healer
Resposta: {{"titulo":"CADEIRA QUE SEGURA PESO","nome_curto":"Cadeira Gamer Healer Wells Preta"}}

Produto: Monitor Gamer Samsung Odyssey G5 27, Resolução QHD, Taxa de atualização de 165Hz & 1ms de tempo de resposta (MPRT), Curvatura com 1000R, HDR 10, AMD FreeSync, Eye Saver Mode & Flicker Free Mode
Resposta: {{"titulo":"TELA QUE NÃO ATRASA","nome_curto":"Monitor Gamer Samsung Odyssey G5 27 QHD 165Hz"}}

Agora:
{contexto}
Resposta:"""

_PROMPT_AVALIACAO = """Você decide se um cupom vale publicar num grupo de WhatsApp de achados
de desconto no Brasil. O piso automático já bloqueou cupons cujo benefício em reais é
irrisório (teto ou valor fixo abaixo de um mínimo configurado) — isso NÃO chega até você.
Sua função é a leitura que um número sozinho não pega: condição confusa, escopo ilegível,
ou cheiro de isca (percentual chamativo com pegadinha na letra miúda).

Rejeite só quando houver um motivo concreto e citável. Na dúvida, aceite — cupom real e
comum não é "ruim" por não ser excepcional.

Dados do cupom:
{contexto}

Responda SOMENTE JSON:
{{"vale_a_pena": true ou false, "motivo": "...", "escopo_legivel": "..."}}
- "motivo": até 12 palavras, em português, direto ao ponto.
- "escopo_legivel": reescreva o campo Escopo de forma humana e curta para aparecer numa
  mensagem de WhatsApp (ex.: "produtos de Glamour.div" vira "loja Glamour"; um handle cru
  de loja vira nome legível). Se já estiver claro, repita como veio. Nunca invente marca,
  loja ou categoria que não esteja no escopo original — só reescreva o que já existe.
"""

_PROMPT_DEAL = """Você escreve o texto de um achado de desconto para grupo de WhatsApp
no Brasil. Quem lê está no celular, passando o dedo, e decide em dois segundos.

Você recebe fatos JÁ VERIFICADOS pelo sistema. Seu trabalho é só o texto humano em
cima deles.

REGRAS OBRIGATÓRIAS:
1. Nunca escreva algarismo, preço, percentual, "R$", "%" ou data. O sistema imprime os
   números; se você escrever, a mensagem passa a mentir assim que o preço mudar.
2. Não invente característica, marca, medida ou uso que não esteja nos dados.
3. Português do Brasil, de pessoa real conversando. Sem jargão de anúncio: nada de
   imperdível, corre, não perca, última chance, clique aqui, aproveite já.
4. Sem emoji (o sistema põe os dele) e sem markdown.
5. "gancho": 3 a 6 palavras, TUDO EM CAIXA ALTA, nomeia o ganho concreto de quem compra.
6. "produto": UMA frase de até 18 palavras dizendo o que é e para quem serve.
7. "porque_vale": UMA frase de até 18 palavras explicando por que o preço de hoje é bom,
   em cima do motivo fornecido. Se o motivo citar cupom, diga que o abatimento sai no
   checkout.
8. Responda SOMENTE JSON: {{"gancho":"...","produto":"...","porque_vale":"..."}}

Exemplo:
Dados: Produto: Air Fryer Mondial Family 4L Preta | Categoria: Eletrodomésticos | Motivo: preço no fundo do histórico de 90 dias
Resposta: {{"gancho":"FRITURA SEM ÓLEO EM CASA","produto":"Air fryer de quatro litros, tamanho que dá conta da janta de duas ou três pessoas.","porque_vale":"É o preço mais baixo que a gente viu nessa air fryer nos últimos meses."}}

Exemplo:
Dados: Produto: Fone Bluetooth JBL Tune 510BT | Categoria: Eletrônicos | Motivo: cupom abate um valor relevante neste item
Resposta: {{"gancho":"FONE QUE AGUENTA O DIA","produto":"Fone bluetooth de ouvir o dia inteiro, com bateria longa e dobrável pra mochila.","porque_vale":"O cupom desconta na hora de pagar, e é ele que derruba o preço aqui."}}

Agora:
{contexto}
Resposta:"""

_PROMPT_NOMES = """Resuma nomes de produtos para mensagens de promoções.

REGRAS:
1. Preserve tipo do produto, marca, modelo e no máximo 2 características essenciais.
2. Remova listas técnicas, recursos secundários, texto publicitário, frete e repetições.
3. Cada nome deve ter no máximo 70 caracteres.
4. Não invente informação e não use Markdown, emoji ou preço.
5. Responda SOMENTE com um array JSON de strings, na mesma ordem da entrada.

Produtos:
{produtos}

Resposta:"""


def _bloco_contexto(nome, preco=None, desconto_percent=None, categoria=None) -> str:
    """Linhas de contexto do produto p/ o prompt; só entra o que existir."""
    linhas = [f"Produto: {nome.strip()}"]
    if preco:
        linhas.append(f"Preço atual: R$ {float(preco):.2f}")
    # Desconto minúsculo não é gancho de venda; só entra quando impressiona.
    if desconto_percent and 5 <= float(desconto_percent) < 90:
        linhas.append(f"Desconto: {float(desconto_percent):.0f}%")
    if categoria:
        linhas.append(f"Categoria: {str(categoria).strip()}")
    return "\n".join(linhas)


def _texto_resposta(resposta) -> str:
    return "".join(
        bloco.text for bloco in resposta.content if getattr(bloco, "type", "") == "text"
    ).strip()


def _json_resposta(texto: str):
    """Aceita JSON puro ou cercado por ```json, sem tolerar prosa adicional."""
    limpo = str(texto or "").strip()
    limpo = re.sub(r"^```(?:json)?\s*", "", limpo, flags=re.I)
    limpo = re.sub(r"\s*```$", "", limpo)
    return json.loads(limpo)


def _sem_formatacao(texto, limite=80) -> str:
    limpo = re.sub(r"[*_`~]+", "", str(texto or ""))
    limpo = re.sub(r"\s+", " ", limpo).strip().strip("\"'")
    if len(limpo) <= limite:
        return limpo.rstrip(" -–—,;|/")
    cortado = limpo[:limite + 1].rsplit(" ", 1)[0]
    return (cortado or limpo[:limite]).rstrip(" -–—,;|/")


_TITULO_PROIBIDO = re.compile(
    r"\b(?:CUPOM|PROMOÇÃO|PROMOCAO|OFERTA|IMPERD[IÍ]VEL|OPORTUNIDADE|"
    r"CORRE|BORA|CLIQUE|DESCONTO|OFF|PROMO)\b",
    re.I,
)


def _titulo_chamada(texto) -> str:
    limpo = _sem_formatacao(texto, 80).upper()
    palavras = [p for p in re.split(r"\s+", limpo) if p]
    if not 2 <= len(palavras) <= 6:
        return ""
    if _TITULO_PROIBIDO.search(limpo):
        return ""
    if re.search(r"\d+\s*%|R\$", limpo):
        return ""
    return limpo


def _cliente(timeout):
    import anthropic

    return anthropic.Anthropic(
        api_key=getattr(settings, "ANTHROPIC_API_KEY", ""),
        timeout=float(timeout),
    )


def gerar_conteudo(nome: str, timeout: int = 30, preco=None,
                   desconto_percent=None, categoria=None) -> dict:
    """Gera chamada e nome curto em uma única chamada ao Claude.

    Retorna sempre ``{"titulo": str, "nome_curto": str}``; qualquer falha
    degrada para strings vazias e nunca impede o envio.

    Gate: settings.LLM_ATIVO e uma ANTHROPIC_API_KEY presente. Motor trocado do
    Ollama local (que não roda no Fly) para a API do Claude (anthropic SDK).
    Preço/desconto/categoria são opcionais e afiam o gancho da frase; o prompt
    proíbe citar o preço em números porque a frase fica em cache (frase_llm) e
    é reaproveitada em envios com preço já atualizado.
    """
    vazio = {"titulo": "", "nome_curto": ""}
    if not getattr(settings, "LLM_ATIVO", False) or not nome:
        return vazio
    api_key = getattr(settings, "ANTHROPIC_API_KEY", "")
    if not api_key:
        # Sem título por IA na mensagem = quase sempre isto. Loga uma vez p/ o
        # painel de saúde mostrar o motivo em vez de "sumiu o título".
        logger.warning("LLM sem ANTHROPIC_API_KEY: título por IA não será gerado")
        return vazio

    try:
        contexto = _bloco_contexto(nome, preco, desconto_percent, categoria)
        resposta = _cliente(timeout).messages.create(
            model=getattr(settings, "LLM_MODELO", _MODELO_PADRAO),
            max_tokens=180,
            # Sonnet 5 habilita pensamento adaptativo por padrão. Estas respostas
            # são JSON curto e determinístico; desativá-lo preserva latência e
            # deixa todo o orçamento de saída disponível para o conteúdo.
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": _PROMPT.format(contexto=contexto)}],
        )
        dados = _json_resposta(_texto_resposta(resposta))
        if not isinstance(dados, dict):
            return vazio
        return {
            "titulo": _titulo_chamada(dados.get("titulo")),
            "nome_curto": _sem_formatacao(dados.get("nome_curto"), 70),
        }
    except Exception as exc:
        logger.warning("Falha ao gerar conteúdo por IA: %s: %s", type(exc).__name__, exc)
        return vazio


_FRASE_PROIBIDA = re.compile(
    r"\b(?:imperd[ií]vel|oportunidade [úu]nica|corre|bora|clique aqui|"
    r"n[ãa]o perca|[úu]ltima chance|aproveite j[áa]|garanta j[áa])\b",
    re.I,
)


def _frase_humana(texto, limite=140) -> str:
    """Frase de IA aceitável numa mensagem: sem número, sem jargão, sem markdown.

    O veto a algarismo/`R$`/`%` não é estilo, é veracidade. O texto é escrito uma
    vez e a mensagem é montada depois, com preço revalidado ao vivo: qualquer
    número escrito pelo modelo vira uma afirmação que o sistema não consegue mais
    garantir. Os números da mensagem saem SEMPRE do código.
    """
    limpo = _sem_formatacao(texto, limite)
    if not limpo:
        return ""
    if re.search(r"\d|R\$|%", limpo):
        return ""
    if _FRASE_PROIBIDA.search(limpo):
        return ""
    if len(limpo.split()) > 24:
        return ""
    return limpo


def gerar_texto_deal(*, nome, categoria="", motivo="", tem_cupom=False,
                     timeout: int = 20) -> dict:
    """Texto humanizado de um deal: gancho, o que é o produto e por que vale.

    Devolve sempre ``{"gancho","produto","porque_vale"}``; qualquer falha degrada
    para strings vazias e NUNCA impede o envio — a mensagem sem estes campos
    continua completa, porque preço, economia, cupom e prova são impressos pelo
    código.

    Uma chamada por tentativa real de envio, como `avaliar_cupom_ia`. Não há cache
    porque `porque_vale` depende do motivo daquele momento; o custo é uma chamada
    curta por publicação, não por item de catálogo.
    """
    vazio = {"gancho": "", "produto": "", "porque_vale": ""}
    if not getattr(settings, "LLM_ATIVO", False) or not nome:
        return vazio
    if not getattr(settings, "ANTHROPIC_API_KEY", ""):
        logger.warning("LLM sem ANTHROPIC_API_KEY: texto do deal não será gerado")
        return vazio
    partes = [f"Produto: {nome}"]
    if categoria:
        partes.append(f"Categoria: {categoria}")
    if motivo:
        partes.append(f"Motivo: {motivo}")
    partes.append(
        "Desconto sai por cupom no checkout." if tem_cupom
        else "Sem cupom: o preço já está aplicado na página."
    )
    try:
        resposta = _cliente(timeout).messages.create(
            model=getattr(settings, "LLM_MODELO", _MODELO_PADRAO),
            max_tokens=300,
            thinking={"type": "disabled"},
            messages=[{
                "role": "user",
                "content": _PROMPT_DEAL.format(contexto=" | ".join(partes)),
            }],
        )
        dados = _json_resposta(_texto_resposta(resposta))
        if not isinstance(dados, dict):
            return vazio
        return {
            "gancho": _titulo_chamada(dados.get("gancho")),
            "produto": _frase_humana(dados.get("produto")),
            "porque_vale": _frase_humana(dados.get("porque_vale")),
        }
    except Exception as exc:
        logger.warning("Falha ao gerar texto do deal: %s: %s",
                       type(exc).__name__, exc)
        return vazio


def gerar_nomes_curtos(nomes, timeout: int = 10) -> list[str]:
    """Resume vários nomes longos em uma chamada, preservando a ordem."""
    nomes = [str(nome or "").strip() for nome in nomes]
    if not nomes or not getattr(settings, "LLM_ATIVO", False):
        return [""] * len(nomes)
    if not getattr(settings, "ANTHROPIC_API_KEY", ""):
        return [""] * len(nomes)
    try:
        produtos = "\n".join(
            f"{indice + 1}. {nome}" for indice, nome in enumerate(nomes)
        )
        resposta = _cliente(timeout).messages.create(
            model=getattr(settings, "LLM_MODELO", _MODELO_PADRAO),
            max_tokens=max(180, len(nomes) * 60),
            thinking={"type": "disabled"},
            messages=[{
                "role": "user",
                "content": _PROMPT_NOMES.format(produtos=produtos),
            }],
        )
        dados = _json_resposta(_texto_resposta(resposta))
        if not isinstance(dados, list) or len(dados) != len(nomes):
            return [""] * len(nomes)
        return [_sem_formatacao(nome, 70) for nome in dados]
    except Exception as exc:
        logger.warning("Falha ao resumir nomes por IA: %s: %s", type(exc).__name__, exc)
        return [""] * len(nomes)


def gerar_descricao(nome: str, timeout: int = 30, preco=None,
                    desconto_percent=None, categoria=None) -> str:
    """Compatibilidade: consumidores antigos recebem somente a chamada."""
    return gerar_conteudo(
        nome, timeout=timeout, preco=preco,
        desconto_percent=desconto_percent, categoria=categoria,
    )["titulo"]


def avaliar_cupom_ia(*, escopo="", tipo_desconto="", valor_desconto=None,
                     desconto_maximo=None, valor_minimo=None, restrito=False,
                     timeout: int = 15) -> dict:
    """Segunda opinião sobre um cupom que JÁ passou pelo piso monetário fixo
    (``coupon_rules.cupom_e_lixo``). O piso pega valor irrisório; isto pega o
    que só leitura pega — condição confusa, escopo ilegível, cheiro de isca.

    Chamada UMA VEZ por tentativa real de envio (dentro de ``enviar_cupom``),
    nunca no funil de milhares de cupons — por isso pode ser um Sonnet completo
    sem custar escala.

    Fail-open por desenho: IA desligada ou fora do ar nunca bloqueia o envio
    sozinha — degrada para ``vale_a_pena=True``. O piso monetário fixo já é
    quem segura sozinho o caso claro; a IA é uma camada A MAIS, não a única
    porta antes do envio.
    """
    escopo_limpo = str(escopo or "").strip()
    vazio = {"vale_a_pena": True, "motivo": "", "escopo_legivel": escopo_limpo}
    if not getattr(settings, "LLM_ATIVO", False):
        return vazio
    api_key = getattr(settings, "ANTHROPIC_API_KEY", "")
    if not api_key:
        return vazio
    try:
        linhas = [f"Escopo: {escopo_limpo or '(não informado)'}"]
        if tipo_desconto:
            linhas.append(f"Tipo de desconto: {tipo_desconto}")
        if valor_desconto is not None:
            unidade = "%" if tipo_desconto == "porcentagem" else "R$"
            linhas.append(f"Valor anunciado: {unidade}{float(valor_desconto):.0f}")
        if desconto_maximo:
            linhas.append(f"Teto do desconto: R${float(desconto_maximo):.2f}")
        if valor_minimo:
            linhas.append(f"Compra mínima: R${float(valor_minimo):.2f}")
        if restrito:
            linhas.append("Público restrito: sim")
        contexto = "\n".join(linhas)
        resposta = _cliente(timeout).messages.create(
            model=getattr(settings, "LLM_MODELO", _MODELO_PADRAO),
            max_tokens=220,
            thinking={"type": "disabled"},
            messages=[{
                "role": "user",
                "content": _PROMPT_AVALIACAO.format(contexto=contexto),
            }],
        )
        dados = _json_resposta(_texto_resposta(resposta))
        if not isinstance(dados, dict):
            return vazio
        vale = dados.get("vale_a_pena")
        if not isinstance(vale, bool):
            return vazio
        escopo_legivel = _sem_formatacao(dados.get("escopo_legivel"), 120)
        return {
            "vale_a_pena": vale,
            "motivo": _sem_formatacao(dados.get("motivo"), 140),
            "escopo_legivel": escopo_legivel or escopo_limpo,
        }
    except Exception as exc:
        logger.warning("Falha ao avaliar cupom por IA: %s: %s", type(exc).__name__, exc)
        return vazio
