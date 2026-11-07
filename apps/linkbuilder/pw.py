"""Link Builder do Mercado Livre pelo Chromium, com a sessão do próprio afiliado.

Sem imports do Django de propósito: `tools/ml_login.py` reusa isto no computador do dono.
O Playwright só é importado quando o navegador abre (a imagem do web não o traz).

Fluxo herdado do v1 (que o roda há meses): abrir o portal, achar o campo de URL e o botão
Gerar, zerar o campo de resultado antes de cada geração (senão o link do produto anterior fica na
tela e o lote inteiro herda o link do vizinho) e ler o resultado do textarea que o botão
Copiar usa. Cookies renovados voltam para o arquivo no fim, mas nunca os de uma tela de login.
"""
import logging
import time
import unicodedata
from pathlib import Path

from .errors import Blocked, BuilderError, LoginRequired, NoSession, Rejected, Unavailable

logger = logging.getLogger(__name__)

LB_URL = "https://www.mercadolivre.com.br/afiliados/linkbuilder#hub"
RESULT_SELECTOR = "textarea[id^='textfield-copyLink']"
RESULT_TIMEOUT_MS = 30000
PAGE_TIMEOUT_MS = 60000
CONTROLS_TIMEOUT_MS = 15000  # o React do portal monta o formulário depois do load
LOCALE = "pt-BR"
TIMEZONE_ID = "America/Sao_Paulo"
ACCEPT_LANGUAGE = "pt-BR,pt;q=0.9,en;q=0.8"
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage",
               f"--lang={LOCALE}"]
INTERSTITIAL_PATHS = ("/gz/", "account-verification", "/captcha", "/challenge", "/blocked",
                      "/security-check", "/verificacion", "/verify")
LOGIN_PATHS = ("/login", "/lgz/", "/registration", "loginhub")
REFUSALS = ("nao e permitido", "nao permitido", "nao e do mercado livre brasil",
            "url nao e do mercado livre", "url invalido", "url invalida",
            "nao cumpre com os requisitos", "nao cumpre os requisitos", "requisitos do programa")


def is_interstitial(url: str) -> bool:
    low = (url or "").lower()
    return any(part in low for part in INTERSTITIAL_PATHS)


def is_login(url: str) -> bool:
    low = (url or "").lower()
    return any(part in low for part in LOGIN_PATHS)


def classify_result(raw: str) -> str:
    """O campo de resultado traz o link ou a recusa do Programa. Só devolve link de verdade."""
    text = (raw or "").strip()
    if not text:
        raise Unavailable("o Link Builder devolveu vazio")
    folded = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
    if any(phrase in folded for phrase in REFUSALS):
        raise Rejected(text[:200])
    if not text.startswith("http"):
        raise Unavailable(f"resultado não é uma URL: {text[:80]}")
    return text


def binary_user_agent(browser) -> str:
    """UA do Chromium em execução sem o marcador de headless (um UA inventado contradiz o resto)."""
    try:
        session = browser.new_browser_cdp_session()
        try:
            raw = (session.send("Browser.getVersion") or {}).get("userAgent", "")
        finally:
            session.detach()
    except Exception:  # noqa: BLE001
        return ""
    return raw.replace("HeadlessChrome", "Chrome") if "Chrome" in raw else ""


def context_options(browser) -> dict:
    options = {"locale": LOCALE, "timezone_id": TIMEZONE_ID,
               "extra_http_headers": {"Accept-Language": ACCEPT_LANGUAGE}}
    agent = binary_user_agent(browser)
    if agent:
        options["user_agent"] = agent
    return options


def _first_visible(candidates):
    for candidate in candidates:
        try:
            if candidate.is_visible(timeout=1000):
                return candidate
        except Exception:  # noqa: BLE001
            continue
    return None


def url_field(page):
    return _first_visible((
        page.locator("textarea[placeholder*='http']").first,
        page.locator("textarea").first,
        page.get_by_role("textbox", name="Insira 1 ou mais URLs"),
    ))


def generate_button(page):
    return _first_visible((
        page.get_by_role("button", name="Gerar", exact=True),
        page.locator("button").filter(has_text="Gerar").first,
    ))


class PlaywrightDriver:
    """`open()` uma vez, `generate(url)` por link, `close()` no fim."""

    def __init__(self, state_path, headless: bool = True):
        self.state_path = str(state_path)
        self.headless = headless
        self._pw = self._browser = self._context = self._page = None
        self._refused = False

    # ── ciclo de vida ──
    def open(self) -> None:
        if not Path(self.state_path).is_file():
            raise NoSession("sem sessão do Mercado Livre: rode tools/ml_login.py")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise Unavailable("playwright não está instalado nesta imagem") from exc
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(headless=self.headless, args=LAUNCH_ARGS)
            self._context = self._browser.new_context(
                storage_state=self.state_path, **context_options(self._browser)
            )
            self._page = self._context.new_page()
            self._open_builder()
        except BuilderError:
            self.close()
            raise
        except Exception as exc:  # noqa: BLE001
            self.close()
            raise Unavailable(f"navegador: {str(exc)[:120]}") from exc

    def close(self) -> None:
        if self._context is not None and not self._refused:
            try:
                self._context.storage_state(path=self.state_path)  # cookies renovados
            except Exception:  # noqa: BLE001
                logger.warning("não consegui renovar a sessão salva", exc_info=True)
        for closer in (self._context, self._browser):
            try:
                if closer is not None:
                    closer.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._pw = self._browser = self._context = self._page = None

    # ── portal ──
    def _open_builder(self) -> None:
        page, navigated = self._page, False
        for attempt in range(2):
            try:
                page.goto(LB_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:  # noqa: BLE001
                    pass
                navigated = True
            except Exception:  # noqa: BLE001
                continue
            if not is_interstitial(page.url):
                break
            if attempt == 0:
                time.sleep(3)  # o gateway costuma liberar na segunda passada
        if not navigated:
            raise Unavailable("o Mercado Livre não respondeu ao abrir o Link Builder")
        if is_interstitial(page.url):
            self._refused = True
            raise Blocked("o ML pediu verificação de segurança antes do Link Builder")
        if self._login_screen():
            self._refused = True
            raise LoginRequired("o portal de afiliados pediu login de novo")
        try:
            page.locator("textarea").first.wait_for(state="visible", timeout=CONTROLS_TIMEOUT_MS)
        except Exception:  # noqa: BLE001
            pass
        if url_field(page) is None or generate_button(page) is None:
            raise Unavailable("os controles do Link Builder não ficaram disponíveis")

    def _login_screen(self) -> bool:
        if is_login(self._page.url):
            return True
        try:
            return self._page.get_by_test_id("user_id").is_visible()
        except Exception:  # noqa: BLE001
            return False

    def generate(self, url: str) -> str:
        page = self._page
        page.evaluate(
            "(sel) => { const el = document.querySelector(sel); if (el) el.value = ''; }",
            RESULT_SELECTOR,
        )
        field, button = url_field(page), generate_button(page)
        if field is None or button is None:
            raise Unavailable("os controles do Link Builder sumiram")
        field.fill(url)
        button.click()
        try:
            page.wait_for_function(
                "(sel) => { const el = document.querySelector(sel);"
                " return !!(el && el.value && el.value.trim()); }",
                arg=RESULT_SELECTOR, timeout=RESULT_TIMEOUT_MS,
            )
            raw = page.input_value(RESULT_SELECTOR)
        except Exception as exc:  # noqa: BLE001
            if self._login_screen():
                self._refused = True
                raise LoginRequired("a sessão caiu durante a geração") from exc
            if is_interstitial(page.url):
                self._refused = True
                raise Blocked("verificação de segurança durante a geração") from exc
            raise Unavailable("o Link Builder não respondeu") from exc
        return classify_result(raw)
