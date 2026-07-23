import json
import logging
import re

from django.conf import settings

logger = logging.getLogger(__name__)

_PROMPT = """Você é um vendedor brasileiro especialista em grupos de WhatsApp. Seu estilo é direto, malandro e muito bem-humorado.
Crie a chamada da promoção e um nome curto e claro para o produto.

REGRAS OBRIGATÓRIAS:
1. "titulo": TUDO EM CAIXA ALTA, máximo de 6 palavras, sem aspas, emoji, ponto final, preço, porcentagem ou a palavra "cupom".
2. O título deve ser brasileiro, descontraído e apelativo; não é uma descrição técnica.
3. "nome_curto": mantenha somente tipo do produto, marca, modelo e 1 ou 2 características essenciais para o cliente identificá-lo.
4. Remova listas de especificações, recursos secundários, texto publicitário, frete e repetições.
5. O nome curto deve ter no máximo 70 caracteres e não pode inventar informação.
6. Não use Markdown, asteriscos ou qualquer formatação.
7. Responda SOMENTE com JSON válido: {{"titulo":"...","nome_curto":"..."}}.

Exemplos de como responder:
Produto: Multivitamínico 120 Cáps. Growth Supplements
Resposta: {{"titulo":"PRA TU QUE NÃO COME SALADA","nome_curto":"Multivitamínico Growth 120 cápsulas"}}

Produto: Cadeira Gamer Wells Preta Healer
Resposta: {{"titulo":"PARA VOCÊ SE SENTIR UM PROPLAYER","nome_curto":"Cadeira Gamer Healer Wells Preta"}}

Produto: Monitor Gamer Samsung Odyssey G5 27, Resolução QHD, Taxa de atualização de 165Hz & 1ms de tempo de resposta (MPRT), Curvatura com 1000R, HDR 10, AMD FreeSync, Eye Saver Mode & Flicker Free Mode
Resposta: {{"titulo":"TELA BRABA PRA JOGAR BONITO","nome_curto":"Monitor Gamer Samsung Odyssey G5 27 QHD 165Hz"}}

Agora faça o seu:
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
            model=getattr(settings, "LLM_MODELO", "claude-haiku-4-5"),
            max_tokens=180,
            messages=[{"role": "user", "content": _PROMPT.format(contexto=contexto)}],
        )
        dados = _json_resposta(_texto_resposta(resposta))
        if not isinstance(dados, dict):
            return vazio
        return {
            "titulo": _sem_formatacao(dados.get("titulo"), 80).upper(),
            "nome_curto": _sem_formatacao(dados.get("nome_curto"), 70),
        }
    except Exception as exc:
        logger.warning("Falha ao gerar conteúdo por IA: %s: %s", type(exc).__name__, exc)
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
            model=getattr(settings, "LLM_MODELO", "claude-haiku-4-5"),
            max_tokens=max(180, len(nomes) * 60),
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
