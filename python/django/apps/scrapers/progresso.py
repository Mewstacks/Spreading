import sys


def emitir_progresso(mensagem: str) -> None:
    """Emite mensagens consumidas pelos endpoints SSE sem espalhar print() nos scrapers."""
    sys.stdout.write(f"{mensagem}\n")
    sys.stdout.flush()
