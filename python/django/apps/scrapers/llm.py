import logging

from django.conf import settings

logger = logging.getLogger(__name__)

_PROMPT = """Você é um vendedor brasileiro especialista em grupos de WhatsApp. Seu estilo é direto, malandro e muito bem-humorado.
Escreva só o TÍTULO da promoção — aquela chamada curta que vem em CIMA da mensagem, como nos grupos de oferta.

REGRAS OBRIGATÓRIAS:
1. TUDO EM CAIXA ALTA. Máximo de 6 palavras. Sem aspas, sem emoji, sem ponto final.
2. Seja 100% brasileiro e descontraído. Uma chamada engraçada/apelativa, não uma descrição do produto.
3. PROIBIDO citar preço, porcentagem ou a palavra "cupom" — a mensagem já mostra isso embaixo.
4. NÃO invente característica que não esteja no nome do produto. Use desconto/categoria só como gancho de humor.
5. Devolva UMA linha só: o título.

Exemplos de como responder:
Produto: Multivitamínico 120 Cáps. Growth Supplements
Título: PRA TU QUE NÃO COME SALADA

Produto: Cadeira Gamer Wells Preta Healer
Título: PARA VOCÊ SE SENTIR UM PROPLAYER

Produto: Achocolatado Nescau 2,01kg
Título: 2 QUILÃO DE NESCAU É SONHO

Produto: Moletom Adidas Essentials
Título: MOLETONZIN TOP DA ADIDAS

Agora faça o seu, no mesmo estilo:
{contexto}
Título:"""


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


def gerar_descricao(nome: str, timeout: int = 30, preco=None,
                    desconto_percent=None, categoria=None) -> str:
    """Gera a frase engraçada (resuminho) para o produto via API do Claude.
    Retorna '' em qualquer falha, para nunca travar/derrubar o envio.

    Gate: settings.LLM_ATIVO e uma ANTHROPIC_API_KEY presente. Motor trocado do
    Ollama local (que não roda no Fly) para a API do Claude (anthropic SDK).
    Preço/desconto/categoria são opcionais e afiam o gancho da frase; o prompt
    proíbe citar o preço em números porque a frase fica em cache (frase_llm) e
    é reaproveitada em envios com preço já atualizado.
    """
    if not getattr(settings, "LLM_ATIVO", False) or not nome:
        return ""
    api_key = getattr(settings, "ANTHROPIC_API_KEY", "")
    if not api_key:
        # Sem título por IA na mensagem = quase sempre isto. Loga uma vez p/ o
        # painel de saúde mostrar o motivo em vez de "sumiu o título".
        logger.warning("LLM sem ANTHROPIC_API_KEY: título por IA não será gerado")
        return ""

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key, timeout=float(timeout))
        contexto = _bloco_contexto(nome, preco, desconto_percent, categoria)
        resposta = client.messages.create(
            model=getattr(settings, "LLM_MODELO", "claude-haiku-4-5"),
            max_tokens=80,
            messages=[{"role": "user", "content": _PROMPT.format(contexto=contexto)}],
        )
        texto = "".join(
            bloco.text for bloco in resposta.content if getattr(bloco, "type", "") == "text"
        ).strip()
        # Título é uma linha só, em caixa alta; corta prefixos que o modelo às vezes
        # devolve ("Título:") e limita o tamanho da chamada.
        texto = texto.splitlines()[0] if texto else ""
        texto = texto.replace('"', "").strip().strip("'").strip()
        if texto.lower().startswith("título:"):
            texto = texto.split(":", 1)[1].strip()
        return texto.upper()[:80]
    except Exception as exc:
        # Antes engolia tudo em silêncio — por isso "o título sumiu" não deixava
        # rastro. Agora o motivo real (import, auth, timeout, modelo) fica no log.
        logger.warning("Falha ao gerar título por IA: %s: %s", type(exc).__name__, exc)
        return ""
