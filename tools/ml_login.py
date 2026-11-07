"""Login do Mercado Livre para o Link Builder. Roda no SEU computador, com janela.

    .venv\\Scripts\\python tools\\ml_login.py [caminho-do-arquivo]

O login é feito por você num Google Chrome comum, SEM automação ligada (o ML trava a página quando
detecta o Playwright no login). Entre na conta de afiliado (2 etapas, se pedir), deixe o Link
Builder
aberto e feche a janela. Aí o script reabre o mesmo perfil por trás, confere que o Link Builder abre
logado e salva a sessão em .secrets/ml-state.json. Nada sai da sua máquina nem entra no git
(.secrets/ está no .gitignore e no .dockerignore).

Precisa de `pip install playwright==1.64.1` e `python -m playwright install chromium`.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apps.linkbuilder import pw  # noqa: E402  (sem Django: só constantes e heurísticas)

DEFAULT_STATE = ROOT / ".secrets" / "ml-state.json"
WAIT_SECONDS = 15 * 60
PROFILE = ROOT / ".secrets" / "chrome-profile"
CHROME_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    "/usr/bin/google-chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)


def builder_is_open(page) -> bool:
    try:
        return (
            "/afiliados/linkbuilder" in page.url and not pw.is_login(page.url)
            and not pw.is_interstitial(page.url)
            and pw.url_field(page) is not None and pw.generate_button(page) is not None
        )
    except Exception:  # noqa: BLE001  a janela está navegando
        return False


def find_chrome() -> str:
    return next((p for p in CHROME_PATHS if os.path.isfile(p)), "")


def manual_login(target: Path) -> int:
    chrome = find_chrome()
    if not chrome:
        print("Não achei o Google Chrome instalado.")
        return 2
    PROFILE.mkdir(parents=True, exist_ok=True)
    print("Entre na conta do ML na janela do Chrome, deixe o Link Builder aberto e FECHE a janela.")
    subprocess.run([chrome, f"--user-data-dir={PROFILE}", "--no-first-run",
                    "--lang=pt-BR", pw.LB_URL])
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(PROFILE), channel="chrome", headless=True, args=pw.LAUNCH_ARGS,
            locale=pw.LOCALE, timezone_id=pw.TIMEZONE_ID, **pw.LAUNCH_KWARGS)
        page = context.new_page()
        page.goto(pw.LB_URL, wait_until="domcontentloaded")
        deadline = time.time() + 30
        while time.time() < deadline and not builder_is_open(page):
            time.sleep(2)
        ok = builder_is_open(page)
        if ok:
            context.storage_state(path=str(target))
        context.close()
    if not ok:
        print("O Link Builder não abriu logado; nada foi salvo. Rode de novo e conclua o login.")
        return 1
    print(f"Sessão salva em {target}")
    return 0


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
    if not headless:
        return manual_login(target)
    with sync_playwright() as playwright:  # automático: só nos testes, contra um portal falso
        browser = playwright.chromium.launch(headless=True, args=pw.LAUNCH_ARGS, **pw.LAUNCH_KWARGS)
        options = pw.context_options(browser)
        if target.is_file():
            options["storage_state"] = str(target)
        context = browser.new_context(**options)
        page = context.new_page()
        page.goto(pw.LB_URL, wait_until="domcontentloaded")
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
