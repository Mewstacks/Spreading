"""Arranque e desligamento do Playwright sem erro opaco.

`sync_playwright()` devolve um `PlaywrightContextManager`. O `__enter__` dele sobe o
processo driver (node) por um pipe e só define `self._playwright` dentro do callback
que o driver dispara. Quando o driver NÃO sobe — memória insuficiente na VM, limite
de processos, dependência de sistema faltando, binário ausente — o callback nunca
roda, o `__enter__` cai direto no `playwright = self._playwright` e o que chega ao
Sentry é:

    AttributeError: 'PlaywrightContextManager' object has no attribute '_playwright'

Uma mensagem que não diz nada sobre a causa e não é acionável por ninguém. É um
problema conhecido do upstream, aberto desde a 1.8 e ainda presente na 1.59 que este
projeto usa:

  - https://github.com/microsoft/playwright-python/issues/999
  - https://github.com/microsoft/playwright/issues/29379

O desligamento tem o defeito espelhado: `__exit__` fala com o driver, e se a conexão
já caiu ele levanta ("Connection closed"). Rodando dentro de um `finally`, essa
exceção SUBSTITUI a falha original — o Sentry recebe o erro do close e perde o erro
que de fato derrubou a corrida.

Este módulo fecha os dois buracos: traduz a falha de arranque numa exceção com causa
medida e torna o desligamento best-effort.
"""
import logging
import os
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class NavegadorIndisponivel(Exception):
    """O runtime do Playwright não pôde ser iniciado nesta máquina."""


def _memoria_disponivel_mb() -> int | None:
    """MemAvailable de /proc/meminfo em MB, ou None fora do Linux."""
    try:
        with open("/proc/meminfo", "r", encoding="ascii") as arquivo:
            for linha in arquivo:
                if linha.startswith("MemAvailable:"):
                    return int(linha.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _memoria_cgroup_mb() -> tuple[int, int] | None:
    """Uso e limite de memoria em cgroup v2 ou v1, se o limite for finito."""
    caminhos = (
        ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"),
        ("/sys/fs/cgroup/memory/memory.usage_in_bytes",
         "/sys/fs/cgroup/memory/memory.limit_in_bytes"),
    )
    for caminho_uso, caminho_limite in caminhos:
        try:
            with open(caminho_uso, "r", encoding="ascii") as arquivo:
                usada = int(arquivo.read().strip())
            with open(caminho_limite, "r", encoding="ascii") as arquivo:
                limite_texto = arquivo.read().strip()
            if limite_texto == "max":
                continue
            limite = int(limite_texto)
            # cgroup v1 devolve um inteiro proximo a 2**63 quando nao ha limite.
            if 0 < limite < (1 << 60) and usada >= 0:
                return usada // (1024 * 1024), limite // (1024 * 1024)
        except (OSError, ValueError):
            continue
    return None


def diagnostico_do_host() -> str:
    """Registra memoria do cgroup ou MemAvailable do sistema convidado.

    A folga do cgroup ajuda a investigar OOM sem afirmar que toda falha do
    driver e OOM. Na Fly o cgroup v1 e ilimitado e /proc/meminfo reflete a VM.
    """
    partes = []
    memoria_cgroup = _memoria_cgroup_mb()
    if memoria_cgroup is not None:
        usada, limite = memoria_cgroup
        partes.append(f"memoria_vm={usada}/{limite}MB")
        partes.append(f"folga_vm={max(0, limite - usada)}MB")
    else:
        disponivel = _memoria_disponivel_mb()
        if disponivel is not None:
            partes.append(f"memoria_disponivel={disponivel}MB")
    caminho = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if caminho:
        partes.append(f"browsers_path={caminho}")
    return " ".join(partes) or "sem diagnostico de host"


@contextmanager
def playwright_sincrono(fabrica=None):
    """Substitui `with sync_playwright() as p:` em todo fluxo de produção.

    Mesmo contrato do original — entrega o `SyncPlaywright` e desliga o driver na
    saída — com duas diferenças: falha de arranque vira `NavegadorIndisponivel` com
    a causa medida, e falha de desligamento é registrada sem apagar a exceção que
    estava subindo.

    `fabrica` existe para o chamador passar o `sync_playwright` que ELE importou, de
    modo que os testes que já remendam esse nome no módulo do chamador continuem
    valendo. Sem ela, usa o do pacote.
    """
    if fabrica is None:
        from playwright.sync_api import sync_playwright as fabrica

    manager = fabrica()
    try:
        playwright = manager.__enter__()
    except AttributeError as erro:
        # So a falta deste atributo identifica o driver que nao completou o boot.
        # Outra AttributeError dentro do __enter__ continua com a causa original.
        if getattr(erro, "name", None) != "_playwright":
            raise
        diagnostico = diagnostico_do_host()
        logger.error(
            "Driver do Playwright não subiu (%s): %s", diagnostico, erro,
        )
        raise NavegadorIndisponivel(
            f"O runtime do navegador não iniciou nesta máquina ({diagnostico})."
        ) from erro

    try:
        yield playwright
    finally:
        try:
            manager.__exit__(None, None, None)
        except Exception:
            # Best-effort de propósito: ver o docstring do módulo. O processo driver
            # morre junto com o pipe, então não há vazamento a cobrar aqui.
            logger.warning(
                "Desligamento do driver do Playwright falhou.", exc_info=True,
            )


def fechar_silenciosamente(recurso, rotulo: str) -> None:
    """Fecha contexto/navegador dentro de `finally` sem mascarar a falha original.

    `context.close()` e `browser.close()` são chamadas de rede para o driver: quando
    a corrida morreu porque o Chromium caiu, elas caem junto. Chamadas cruas num
    `finally`, a exceção delas substitui a causa real — foi assim que "BrowserContext
    .close: Connection closed" virou o erro mais visível do Sentry, sem nunca dizer o
    que derrubou o navegador.
    """
    if recurso is None:
        return
    try:
        recurso.close()
    except Exception:
        logger.warning("%s não fechou limpo.", rotulo, exc_info=True)
