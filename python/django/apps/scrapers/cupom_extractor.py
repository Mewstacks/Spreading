"""Lê a mensagem de um canal como um humano leria e devolve os cupons dela.

Por que isto existe: a extração por expressão regular fracassou, e o motivo foi
medido. Uma varredura de 14 canais devolveu ZERO cupons — não porque os canais não
tenham, mas porque cada um escreve de um jeito. Uma amostra real do `@cupombr`:

    LISTÃO de Cupom Mercado Livre
    10% OFF, Limite de R$ 20 OFF em todo site: TODOOSITE1308
    15%OFF Limite de R$ 189: TVS1208CELULAR
    10%OFF (Tecnologia) limite R$200 OFF: PROMOCERTAML
    R$50 OFF em R$399: CASA1508
    25% OFF Acessórios para veículos: OMELHOR

São sete cupons numa mensagem só, com desconto, mínimo, teto e escopo — e o regex
não via nenhum, porque ele procurava a palavra "cupom" colada no código. Outro canal
escreve `🎟️ AMIG4ASPROM0 30%`; outro, `(Cupom MELIMODA/SEMPRENAMODA)`. Não existe um
padrão: existe linguagem natural.

O projeto já fala com o Claude (`llm.py`, `ANTHROPIC_API_KEY` em produção), então a
leitura passa a ser feita por quem sabe ler. O modelo NÃO decide se o cupom é bom nem
se pode ser publicado — ele só transcreve o que está escrito, em campos. Todo o resto
continua sendo regra nossa, verificável e testável:

* o formato do código é validado aqui, não confiado ao modelo;
* percentual fora de 1–99 é recusado (100% é erro de leitura ou promessa falsa);
* a loja precisa ser uma que sabemos afiliar;
* o cupom entra com a precedência mais fraca do sistema e vale por corroborar uma
  fonte oficial — a mesma mecânica que já se provou quando os 10 cupons de ML do
  Promobit bateram 10/10 com a página oficial de afiliados.

Custo: só mensagem que parece conter cupom é enviada ao modelo, e cada mensagem é
processada uma vez (cache por hash do texto). Sem chave, sem flag ou sem sinal de
cupom, a função devolve lista vazia e ninguém quebra.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

_MODELO_PADRAO = "claude-sonnet-5"

# 30 dias: um cupom lido hoje não muda de texto amanhã, e a mensagem do canal é
# imutável. O cache existe para não pagar duas vezes pela mesma leitura.
_TTL_CACHE_S = 30 * 24 * 3600

# Lojas que sabemos afiliar. Cupom de loja fora daqui é trabalho para o
# influenciador e comissão para outra pessoa.
LOJAS_ACEITAS = {"mercadolivre", "amazon", "shopee"}

# Só manda ao modelo o que tem cara de cupom. Filtro de CUSTO, não de qualidade —
# na dúvida, deixa passar: uma leitura a mais é barata, um cupom perdido não volta.
#
# O emoji de ticket e o percentual solto entraram porque um canal real escreve o
# cupom assim, sem a palavra: "🎟️ AMIG4ASPROM0 30%". Exigir "off" ou "cupom"
# descartava a mensagem inteira antes de alguém lê-la.
_SINAL_DE_CUPOM = re.compile(
    r"(cupom|cupons|voucher|desconto|resgat|\boff\b|\d\s*%|🎟|🎫)", re.I,
)

# Código digitável: sem espaço, 4 a 30, começa por letra ou número.
_CODIGO_OK = re.compile(r"^[A-Z0-9][A-Z0-9._-]{3,29}$")

_DESCONTO_PERCENTUAL = re.compile(r"(?<!\d)(\d{1,2})\s*%\s*(?:OFF)?", re.I)
_DESCONTO_FIXO = re.compile(r"R\$\s*([\d.]+(?:,\d{1,2})?)\s*OFF", re.I)
_DINHEIRO = re.compile(r"R\$\s*([\d.]+(?:,\d{1,2})?)", re.I)
_TOKEN_CODIGO = re.compile(r"\b[A-Z0-9][A-Z0-9._-]{3,29}\b")
_NAO_CODIGOS = {
    "AMAZON", "MERCADO", "LIVRE", "SHOPEE", "CUPOM", "CUPONS", "VOUCHER",
    "DESCONTO", "DESCONTOS", "OFERTA", "OFERTAS", "PROMO", "PROMOCAO",
    "PROMOCOES", "LIMITADO", "LIMITE", "MINIMO", "MINIMA", "TODO", "SITE",
    "HOJE", "AGORA", "APENAS", "SOMENTE", "VALIDO", "VALIDA", "FRETE",
    "GRATIS", "CLIQUE", "ATIVE", "AQUI", "SELECIONADOS", "PRODUTOS",
}

_PROMPT = """Extraia os cupons de desconto desta mensagem de um canal brasileiro de ofertas.

Uma mensagem pode conter vários cupons, um só, ou nenhum.

REGRAS:
1. "codigo": o código que a pessoa digita no checkout, exatamente como escrito, em CAIXA ALTA. Se a mensagem diz para resgatar no app/banner e não mostra código digitável, NÃO invente: ignore esse cupom.
2. "loja": uma de "mercadolivre", "amazon", "shopee". Deduza pelos links e pelo texto. Se não der para saber, use "".
3. "tipo": "porcentagem" ou "fixo".
4. "valor": o número do desconto (20 para 20% OFF; 50 para R$50 OFF).
5. "minimo": valor mínimo de compra em reais, 0 se não houver.
6. "teto": limite máximo de desconto em reais, 0 se não houver.
7. "escopo": a que se aplica, em poucas palavras ("todo site", "Tecnologia", "entregas Full"). "" se não disser.
8. Não invente nada. Campo que a mensagem não informa vai vazio ou 0.
9. Responda SOMENTE com JSON válido: {{"cupons":[{{"codigo":"","loja":"","tipo":"","valor":0,"minimo":0,"teto":0,"escopo":""}}]}}
10. Sem cupom com código digitável na mensagem, responda {{"cupons":[]}}.

Exemplo:
Mensagem: "10% OFF, Limite de R$ 20 OFF em todo site: TODOOSITE1308 / R$50 OFF em R$399: CASA1508 - https://mercadolivre.com.br/sec/abc"
Resposta: {{"cupons":[{{"codigo":"TODOOSITE1308","loja":"mercadolivre","tipo":"porcentagem","valor":10,"minimo":0,"teto":20,"escopo":"todo site"}},{{"codigo":"CASA1508","loja":"mercadolivre","tipo":"fixo","valor":50,"minimo":399,"teto":0,"escopo":""}}]}}

Agora esta:
Mensagem: {mensagem}
Resposta:"""


def parece_ter_cupom(texto: str) -> bool:
    return bool(_SINAL_DE_CUPOM.search(texto or ""))


def _chave_cache(texto: str) -> str:
    digest = hashlib.sha256((texto or "").encode("utf-8")).hexdigest()[:32]
    return f"cupom-extraido:{digest}"


def _numero(valor) -> float:
    try:
        return round(float(str(valor).replace(",", ".")), 2)
    except (TypeError, ValueError):
        return 0.0


def _loja_mencionada(texto: str, padrao="") -> str:
    bruto = (texto or "").casefold()
    if "mercado livre" in bruto or "mercadolivre" in bruto or "meli.la" in bruto:
        return "mercadolivre"
    if "amazon" in bruto or "amzn.to" in bruto:
        return "amazon"
    if "shopee" in bruto or "shope.ee" in bruto:
        return "shopee"
    return padrao if padrao in LOJAS_ACEITAS else ""


def _dinheiro(valor: str) -> float:
    return _numero(str(valor or "").replace(".", "").replace(",", "."))


def _candidatos_codigo(linha: str) -> list[str]:
    """Tokens com cara de código, nunca palavras de marketing ou valores."""
    texto = (linha or "").upper()
    candidatos = []
    for token in _TOKEN_CODIGO.findall(texto):
        token = token.strip("._-")
        if token in _NAO_CODIGOS or not _CODIGO_OK.match(token):
            continue
        tem_letra = any(c.isalpha() for c in token)
        tem_numero = any(c.isdigit() for c in token)
        # Código alfanumérico é o padrão. Código só de letras precisa ser longo;
        # isso preserva casos reais como OMELHOR sem aceitar OFF/HOJE/CUPOM.
        if not tem_letra or (not tem_numero and len(token) < 7):
            continue
        candidatos.append(token)
    return list(dict.fromkeys(candidatos))


def extrair_deterministico(texto: str, *, loja_padrao="") -> list[dict]:
    """Fallback local para formatos comuns; não inventa campos ausentes.

    O LLM continua cobrindo linguagem livre. Este parser impede que falta de chave,
    cota ou rede transforme mensagens estruturadas (`10% OFF: CODIGO`) em zero.
    """
    if not parece_ter_cupom(texto):
        return []
    loja = _loja_mencionada(texto, loja_padrao)
    if not loja:
        return []
    brutos = []
    linhas = [linha.strip() for linha in (texto or "").splitlines() if linha.strip()]
    for indice, linha in enumerate(linhas):
        percentual = _DESCONTO_PERCENTUAL.search(linha)
        fixo = _DESCONTO_FIXO.search(linha)
        if not percentual and not fixo:
            continue
        # O código precisa ocupar uma posição explícita: depois de `:`/`-`, depois
        # da palavra cupom, ou na linha seguinte `Use o cupom: X`. Varrer a linha
        # inteira aceitava nome de produto como SMARTPHONE/MOTOROLA/100ML.
        fragmentos = []
        if ":" in linha:
            fragmentos.append(linha.rsplit(":", 1)[1])
        depois_hifen = re.search(r"\)\s*[-–—]\s*(.+)$", linha)
        if depois_hifen:
            fragmentos.append(depois_hifen.group(1))
        cupom_inline = re.search(
            r"\bcupom\s*:?[ \t]+([A-Z0-9][A-Z0-9._/-]{3,60})", linha, re.I,
        )
        if cupom_inline:
            fragmentos.append(cupom_inline.group(1))
        for proxima in linhas[indice + 1:indice + 3]:
            uso = re.search(
                r"(?:usem?|digite|aplique)\s+(?:o\s+)?cupom\s*:\s*(.+)$",
                proxima, re.I,
            )
            if uso:
                fragmentos.append(uso.group(1))
                break
        codigos = list(dict.fromkeys(
            codigo for fragmento in fragmentos
            for codigo in _candidatos_codigo(fragmento)
        ))
        if not codigos:
            continue
        tipo = "porcentagem" if percentual else "fixo"
        valor = float(percentual.group(1)) if percentual else _dinheiro(fixo.group(1))
        if valor <= 0 or (tipo == "porcentagem" and valor >= 100):
            continue
        valores = [_dinheiro(v) for v in _DINHEIRO.findall(linha)]
        minimo = 0.0
        teto = 0.0
        limite = re.search(
            r"limite(?:\s+de)?\s+R\$\s*([\d.]+(?:,\d{1,2})?)", linha, re.I,
        )
        if limite:
            teto = _dinheiro(limite.group(1))
        compra = re.search(
            r"(?:em|acima\s+de|compras?\s+(?:a\s+partir\s+)?de)\s+(?:R\$\s*)?"
            r"([\d.]+(?:,\d{1,2})?)", linha, re.I,
        )
        if compra:
            minimo = _dinheiro(compra.group(1))
        elif tipo == "fixo" and len(valores) > 1:
            minimo = valores[1]
        escopo = ""
        if ":" in linha:
            antes = linha.rsplit(":", 1)[0]
            antes = _DESCONTO_PERCENTUAL.sub("", antes)
            antes = _DESCONTO_FIXO.sub("", antes)
            antes = re.sub(r"limite.*?(?=\bem\b|$)", "", antes, flags=re.I)
            escopo = antes.strip(" ,-–—()")[-120:]
        for codigo in codigos:
            brutos.append({
                "codigo": codigo, "loja": loja, "tipo": tipo, "valor": valor,
                "minimo": minimo, "teto": teto, "escopo": escopo,
            })
    return _limpar({"cupons": brutos}, loja_padrao=loja)


def _limpar(bruto, loja_padrao="") -> list[dict]:
    """Valida o que o modelo devolveu. Nada aqui confia na saída do LLM.

    O modelo transcreve; a decisão de aceitar é regra nossa, e é aqui que ela mora —
    por isso esta função é testável sem rede e sem chave de API.
    """
    if not isinstance(bruto, dict):
        return []
    limpos = []
    vistos = set()
    for item in bruto.get("cupons") or []:
        if not isinstance(item, dict):
            continue
        codigo = str(item.get("codigo") or "").strip().upper()
        if not _CODIGO_OK.match(codigo) or codigo in vistos:
            continue
        loja = str(item.get("loja") or "").strip().lower() or loja_padrao
        if loja not in LOJAS_ACEITAS:
            continue
        tipo = "fixo" if str(item.get("tipo") or "").lower().startswith("fix") else "porcentagem"
        valor = _numero(item.get("valor"))
        if valor <= 0:
            continue
        # Percentual só existe entre 1 e 99. 100% é erro de leitura ou promessa
        # falsa; acima disso é ruído. Valor fixo não tem esse teto.
        if tipo == "porcentagem" and valor >= 100:
            continue
        vistos.add(codigo)
        limpos.append({
            "codigo": codigo,
            "loja": loja,
            "tipo": tipo,
            "valor": valor,
            "minimo": max(0.0, _numero(item.get("minimo"))),
            "teto": max(0.0, _numero(item.get("teto"))),
            "escopo": str(item.get("escopo") or "").strip()[:120],
        })
    return limpos


_OBJETO_FECHADO = re.compile(r"\{[^{}]*\}")


def _resgatar_parcial(texto: str) -> dict:
    """Cupons inteiros de uma resposta que foi cortada no meio.

    Existe por causa de um erro visto em produção: `Unterminated string`. Quando o
    modelo é interrompido, tudo o que ele já tinha fechado continua correto — e o
    que ficou pela metade não é meio-cupom, é lixo que `_limpar` recusa de qualquer
    jeito. Varrer os objetos `{...}` completos recupera a maior parte da mensagem em
    vez de descartá-la inteira. Não é o caminho normal: o orçamento de tokens é que
    tem de caber. É a rede de segurança para quando não couber.
    """
    achados = []
    for bruto in _OBJETO_FECHADO.findall(texto or ""):
        try:
            item = json.loads(bruto)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("codigo"):
            achados.append(item)
    if achados:
        logger.info("Resposta da IA veio truncada; %s cupom(ns) inteiro(s) "
                    "recuperado(s).", len(achados))
    return {"cupons": achados}


def extrair(texto: str, *, loja_padrao="", timeout=20) -> list[dict]:
    """Cupons de uma mensagem. Lista vazia quando não há, não dá, ou falha.

    Nunca levanta: isto roda dentro de uma coleta, e uma falha de leitura não pode
    derrubar a fonte inteira.
    """
    texto = (texto or "").strip()
    if not texto or not parece_ter_cupom(texto):
        return []
    fallback = extrair_deterministico(texto, loja_padrao=loja_padrao)
    if not getattr(settings, "CUPOM_LLM_ATIVO", True):
        return fallback
    if not getattr(settings, "ANTHROPIC_API_KEY", ""):
        logger.debug("Extração de cupom por IA sem ANTHROPIC_API_KEY; ignorando.")
        return fallback

    chave = _chave_cache(texto)
    guardado = cache.get(chave)
    if guardado is not None:
        return guardado

    try:
        from apps.scrapers.llm import _cliente, _json_resposta, _texto_resposta

        resposta = _cliente(timeout).messages.create(
            model=getattr(settings, "LLM_MODELO", _MODELO_PADRAO),
            # 2500, não 900. Em produção o primeiro erro real foi
            # `JSONDecodeError: Unterminated string` — a resposta era CORTADA no
            # meio do JSON. A mensagem que estourou é justamente a mais valiosa: o
            # "LISTÃO" do @cupombr, com sete cupons, cada um com código, escopo,
            # mínimo e teto. Orçamento apertado descartava exatamente a mensagem
            # que mais rende. Sete cupons cabem com folga aqui, e o custo só é
            # pago pelo que o modelo realmente escreve.
            max_tokens=2500,
            thinking={"type": "disabled"},
            messages=[{"role": "user",
                       "content": _PROMPT.format(mensagem=texto[:2500])}],
        )
        texto_resposta = _texto_resposta(resposta)
        dados = _json_resposta(texto_resposta)
        if dados is None:
            # Resposta truncada ou ilegível. Recupera os cupons COMPLETOS que já
            # vieram antes do corte em vez de perder a mensagem inteira: numa lista
            # de sete, salvar seis é melhor que salvar zero, e cada objeto fechado
            # é um cupom inteiro — não há meio-cupom válido.
            dados = _resgatar_parcial(texto_resposta)
        cupons = _limpar(dados, loja_padrao)
    except Exception as exc:
        logger.warning("Extração de cupom por IA falhou (%s: %s).",
                       type(exc).__name__, exc)
        # Não cacheia falha: a próxima coleta tenta de novo.
        return fallback

    resultado = cupons or fallback
    cache.set(chave, resultado, _TTL_CACHE_S)
    return resultado
