"""O driver real contra um portal de mentira em localhost (Chromium de verdade, nada do ML)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

pytest.importorskip("playwright.sync_api")

from apps.linkbuilder import pw  # noqa: E402
from apps.linkbuilder.errors import (  # noqa: E402
    Blocked,
    LoginRequired,
    NoSession,
    Rejected,
    Unavailable,
)

PORTAL = """<!doctype html><html lang="pt-BR"><body><h1>Link Builder</h1>
<textarea placeholder="Ex: https://www.mercadolivre.com.br/produto"></textarea>
<button>Gerar</button><div id="out"></div>
<script>
const field = document.querySelector('textarea'), button = document.querySelector('button');
let n = 0;
button.addEventListener('click', () => {
  const url = field.value;
  setTimeout(() => {
    let box = document.querySelector("textarea[id^='textfield-copyLink']");
    if (!box) { box = document.createElement('textarea'); box.id = 'textfield-copyLink-' + (++n);
                document.getElementById('out').appendChild(box); }
    box.value = url.includes('MLB000')
      ? '\\u26a0\\ufe0f Este produto n\\u00e3o cumpre com os requisitos do Programa.'
      : 'https://meli.la/FAKE' + url.slice(-6);
  }, 150);
});
</script></body></html>"""


class Portal(BaseHTTPRequestHandler):
    mode = "ok"

    def _send(self, status, body="", location=""):
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def do_GET(self):  # noqa: N802
        if self.path.startswith(("/jms/mlb/lgz/login", "/gz/account-verification")):
            return self._send(200, "<html><body>entrar</body></html>")
        if not self.path.startswith("/afiliados/linkbuilder"):
            return self._send(404)
        if self.mode == "login":
            return self._send(302, location="/jms/mlb/lgz/login?go=x")
        if self.mode == "wall":
            return self._send(302, location="/gz/account-verification?go=x")
        if self.mode == "empty":
            return self._send(200, "<html><body><h1>em manutenção</h1></body></html>")
        return self._send(200, PORTAL)

    def log_message(self, *args):
        pass


@pytest.fixture
def portal(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Portal)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    Portal.mode = "ok"
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setattr(pw, "LB_URL", f"{base}/afiliados/linkbuilder#hub")
    monkeypatch.setattr(pw, "CONTROLS_TIMEOUT_MS", 500)
    monkeypatch.setattr(pw, "time", SimpleNamespace(sleep=lambda s: None))
    yield Portal
    server.shutdown()
    server.server_close()


@pytest.fixture
def state_file(tmp_path):
    path = tmp_path / "ml-state.json"
    path.write_text(json.dumps({"cookies": [], "origins": []}))
    return path


def test_without_a_saved_session_nothing_is_opened(tmp_path):
    driver = pw.PlaywrightDriver(tmp_path / "nao-existe.json")
    with pytest.raises(NoSession):
        driver.open()
    driver.close()  # idempotente


def test_generates_one_link_per_url_and_never_reuses_the_previous_result(portal, state_file):
    driver = pw.PlaywrightDriver(state_file)
    driver.open()
    try:
        assert driver.generate("https://www.mercadolivre.com.br/p/MLB111") == (
            "https://meli.la/FAKEMLB111")
        assert driver.generate("https://www.mercadolivre.com.br/p/MLB222") == (
            "https://meli.la/FAKEMLB222")  # o resultado velho foi zerado antes de esperar o novo
    finally:
        driver.close()
    assert json.loads(state_file.read_text()) == {"cookies": [], "origins": []}  # sessão regravada


def test_program_refusal_becomes_rejected(portal, state_file):
    driver = pw.PlaywrightDriver(state_file)
    driver.open()
    try:
        with pytest.raises(Rejected):
            driver.generate("https://www.mercadolivre.com.br/p/MLB000111")
        assert driver.generate("https://www.mercadolivre.com.br/p/MLB333").endswith("MLB333")
    finally:
        driver.close()


def test_login_screen_means_login_required_and_the_saved_session_is_left_alone(portal, state_file):
    portal.mode = "login"
    cookie = {"name": "keep", "value": "1", "domain": "127.0.0.1", "path": "/", "expires": -1,
              "httpOnly": False, "secure": False, "sameSite": "Lax"}
    state_file.write_text(json.dumps({"cookies": [cookie], "origins": []}))
    kept = state_file.read_text()
    driver = pw.PlaywrightDriver(state_file)
    with pytest.raises(LoginRequired):
        driver.open()
    assert state_file.read_text() == kept  # cookies de tela de login não sobrescrevem a sessão


def test_security_check_means_blocked_not_logged_out(portal, state_file):
    portal.mode = "wall"
    with pytest.raises(Blocked):
        pw.PlaywrightDriver(state_file).open()


def test_portal_without_controls_is_unavailable(portal, state_file):
    portal.mode = "empty"
    with pytest.raises(Unavailable):
        pw.PlaywrightDriver(state_file).open()


def _login_tool():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "tools" / "ml_login.py"
    spec = importlib.util.spec_from_file_location("ml_login_tool", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_login_tool_saves_the_session_once_the_builder_is_open(portal, tmp_path):
    tool = _login_tool()
    target = tmp_path / "secrets" / "ml-state.json"
    assert tool.main([str(target)], headless=True) == 0
    assert set(json.loads(target.read_text())) == {"cookies", "origins"}


def test_login_tool_saves_nothing_if_the_builder_never_opens(portal, tmp_path, monkeypatch):
    portal.mode = "login"
    tool = _login_tool()
    monkeypatch.setattr(tool, "WAIT_SECONDS", 3)
    target = tmp_path / "ml-state.json"
    assert tool.main([str(target)], headless=True) == 1
    assert not target.exists()
