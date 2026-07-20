import sys


def emitir_progresso(mensagem: str) -> None:
    """Emite mensagens consumidas pelos endpoints SSE sem espalhar print() nos scrapers."""
    sys.stdout.write(f"{mensagem}\n")
    sys.stdout.flush()


def emitir_fase(rotulo: str, fracao: float = 0.0, faixa=None) -> None:
    """Emite uma linha de progresso com o % já mapeado na faixa da etapa.

    Um pipeline com várias etapas (cupons: campanhas → códigos → projeção → links)
    não tem um contador único; cada etapa recebe uma FAIXA do total (ex.: 0–45%) e
    reporta `fracao` (0..1) do próprio trabalho. Sem isso, a barra ou voltava a zero
    a cada etapa ou (o que acontecia) nunca aparecia.

    Sem `faixa` a linha sai só com o rótulo — a UI usa como legenda e mantém a barra
    indeterminada, que é o certo para etapa de duração desconhecida.
    """
    if not faixa:
        emitir_progresso(f"[PROGRESSO] {rotulo}")
        return
    ini, fim = faixa
    pct = int(ini + (fim - ini) * max(0.0, min(1.0, fracao)))
    emitir_progresso(f"[PROGRESSO] {rotulo} ({pct}%)")
