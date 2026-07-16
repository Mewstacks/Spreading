from django.conf import settings

_PROMPT = """Você é um vendedor brasileiro especialista em grupos de WhatsApp. Seu estilo é direto, malandro e muito bem-humorado.
Escreva UMA frase curta e apelativa para vender o produto abaixo.

REGRAS OBRIGATÓRIAS:
1. Seja 100% brasileiro na fala.
2. PROIBIDO usar traduções literais ou robóticas (NÃO use "bate a concorrência", "a melhor escolha", "não precisa de despesas").
3. USE termos como: "bota no chinelo", "amassa", "tá de graça", "preço de banana", "pra parar de passar raiva".
4. Máximo de 20 palavras. Sem aspas.

Exemplos de como responder:
Produto: Fritadeira Airfryer Midea
Frase: Frita até o vento e te salva do cheiro de óleo na casa toda. Preço de banana!

Produto: Liquidificador Oster 1400W
Frase: Bate até cimento e não engasga. Bota qualquer outro no chinelo, e o preço tá ridículo.

Produto: Notebook Samsung Galaxy
Frase: Pra você parar de passar raiva com aquela sua carroça que trava no Excel. Leva logo!

Agora faça o seu, no mesmo estilo:
Produto: {nome}
Frase:"""


def gerar_descricao(nome: str, timeout: int = 30) -> str:
    """Gera a frase engraçada (resuminho) para o produto via API do Claude.
    Retorna '' em qualquer falha, para nunca travar/derrubar o envio.

    Gate: settings.LLM_ATIVO e uma ANTHROPIC_API_KEY presente. Motor trocado do
    Ollama local (que não roda no Fly) para a API do Claude (anthropic SDK).
    """
    if not getattr(settings, "LLM_ATIVO", False) or not nome:
        return ""
    api_key = getattr(settings, "ANTHROPIC_API_KEY", "")
    if not api_key:
        return ""

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key, timeout=float(timeout))
        resposta = client.messages.create(
            model=getattr(settings, "LLM_MODELO", "claude-haiku-4-5"),
            max_tokens=80,
            messages=[{"role": "user", "content": _PROMPT.format(nome=nome.strip())}],
        )
        texto = "".join(
            bloco.text for bloco in resposta.content if getattr(bloco, "type", "") == "text"
        ).strip()
        texto = texto.replace('"', "").replace("\n", " ").strip().strip("'").strip()
        return texto[:200]
    except Exception:
        return ""
