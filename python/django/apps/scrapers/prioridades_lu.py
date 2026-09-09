"""Pauta editorial da Lu usada pela coleta, independente dos grupos ativos.

Publicar é uma decisão de destino; descobrir ofertas boas para a criadora não
deve parar enquanto os grupos reais aguardam homologação. Os termos abaixo vêm
do canal público dela, centrado em comparativos/testes de robôs aspiradores e
produtos que tornam a casa mais prática.
"""

TERMOS_PRIORITARIOS_LU = (
    "robô aspirador",
    "aspirador robô",
)


def termos_de_coleta(termos=()):
    """Une pauta editorial e regras ativas, removendo duplicatas estáveis."""
    vistos = set()
    resultado = []
    for termo in (*TERMOS_PRIORITARIOS_LU, *(termos or ())):
        limpo = str(termo or "").strip()
        chave = limpo.casefold()
        if limpo and chave not in vistos:
            vistos.add(chave)
            resultado.append(limpo)
    return resultado
