import requests
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

def gerar_descricao(nome: str, timeout: int = 120) -> str:
    """Gera a frase engraçada para o produto. Retorna '' em qualquer falha."""
    if not getattr(settings, "LLM_ATIVO", False) or not nome:
        return ""
    
    try:
        r = requests.post(
            f"{settings.OLLAMA_URL.rstrip('/')}/api/generate",
            json={
                "model": settings.OLLAMA_MODEL,
                "prompt": _PROMPT.format(nome=nome.strip()),
                "stream": False,
                "keep_alive": "30m",
                "options": {
                    "temperature": 0.7, 
                    "num_predict": 50,
                    "top_p": 0.9
                },
            },
            timeout=timeout,
        )
        if r.status_code != 200:
            return ""
            
        texto = (r.json().get("response") or "").strip()
        texto = texto.replace('"', "").replace("\n", " ").strip().strip("'").strip()
        return texto[:200]
    except Exception:
        return ""