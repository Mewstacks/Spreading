from django.conf import settings

_PROMPT = """Você é um vendedor brasileiro especialista em grupos de WhatsApp. Seu estilo é direto, malandro e muito bem-humorado.
Escreva UMA frase curta e apelativa para vender o produto abaixo.

REGRAS OBRIGATÓRIAS:
1. Seja 100% brasileiro na fala.
2. PROIBIDO usar traduções literais ou robóticas (NÃO use "bate a concorrência", "a melhor escolha", "não precisa de despesas").
3. USE termos como: "bota no chinelo", "amassa", "tá de graça", "preço de banana", "pra parar de passar raiva".
4. Máximo de 20 palavras. Sem aspas.
5. Use os dados extras (desconto, categoria) como gancho quando ajudarem, mas NUNCA repita o preço em números na frase — a mensagem já mostra o preço, e a frase é reaproveitada em envios futuros com preço diferente.
6. NÃO invente característica que não esteja no nome do produto.

Exemplos de como responder:
Produto: Fritadeira Airfryer Midea
Frase: Frita até o vento e te salva do cheiro de óleo na casa toda. Preço de banana!

Produto: Liquidificador Oster 1400W
Desconto: 45%
Frase: Bate até cimento e não engasga. Quase metade do preço — bota qualquer outro no chinelo.

Produto: Notebook Samsung Galaxy
Categoria: Informática
Frase: Pra você parar de passar raiva com aquela sua carroça que trava no Excel. Leva logo!

Agora faça o seu, no mesmo estilo:
{contexto}
Frase:"""


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
        texto = texto.replace('"', "").replace("\n", " ").strip().strip("'").strip()
        return texto[:200]
    except Exception:
        return ""
