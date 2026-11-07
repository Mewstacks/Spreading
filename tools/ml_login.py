"""Login do Mercado Livre para o Link Builder. Roda no SEU computador, com janela.

    .venv\\Scripts\\python tools\\ml_login.py [caminho-do-arquivo]

Entre na conta de afiliado (com a verificação em duas etapas, se o ML pedir). Quando o Link
Builder abrir, a sessão é salva em .secrets/ml-state.json e o navegador fecha. O arquivo nunca sai
da sua máquina nem entra no git (.secrets/ está no .gitignore e no .dockerignore).

Precisa de `pip install playwright==1.64.1` e `python -m playwright install chromium`.
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apps.linkbuilder import pw  # noqa: E402  (sem Django: só constantes e heurísticas)

DEFAULT_STATE = ROOT / ".secrets" / "ml-state.json"
WAIT_SECONDS = 15 * 60


def builder_is_open(page) -> bool:
    try:
        return (
            "/afiliados/linkbuilder" in page.url and not pw.is_login(page.url)
            and not pw.is_interstitial(page.url)
            and pw.url_field(page) is not None and pw.generate_button(page) is not None
        )
    except Exception:  # noqa: BLE001  a janela está navegando
        return False


def main(argv=None, headless: bool = False) -> int:
    argv = sys.argv[1:] if argv is None else argv
    target = Path(argv[0]) if argv else DEFAULT_STATE
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Falta o Playwright: pip install playwright==1.64.1 && "
              "python -m playwright install chromium")
        return 2
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless, args=pw.LAUNCH_ARGS)
        options = pw.context_options(browser)
        if target.is_file():
            options["storage_state"] = str(target)  # renova a sessão que já existe
        context = browser.new_context(**options)
        page = context.new_page()
        page.goto(pw.LB_URL, wait_until="domcontentloaded")
        print("Entre na conta do Mercado Livre na janela que abriu (até 15 minutos).")
        deadline = time.time() + WAIT_SECONDS
        while time.time() < deadline and not builder_is_open(page):
            time.sleep(2)
        if not builder_is_open(page):
            print("Não vi o Link Builder aberto a tempo; nada foi salvo.")
            browser.close()
            return 1
        context.storage_state(path=str(target))
        browser.close()
    print(f"Sessão salva em {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
