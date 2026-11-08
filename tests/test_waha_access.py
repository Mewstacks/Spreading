import importlib.util
from pathlib import Path

PASSWORD, KEY = "senha-de-teste-123", "chave-de-teste-456"


def _tool():
    path = Path(__file__).resolve().parent.parent / "tools" / "waha_access.py"
    spec = importlib.util.spec_from_file_location("waha_access_tool", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _env(tmp_path, body):
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return path


def test_read_env_skips_comments_and_strips_quotes(tmp_path):
    path = _env(tmp_path, "# nada\n\nA=1\nB = \"dois\"\nC='tres'\nD=a=b\nsemigual\n")
    assert _tool().read_env(path) == {"A": "1", "B": "dois", "C": "tres", "D": "a=b"}
    assert _tool().read_env(tmp_path / "nao-existe") == {}


def test_password_then_key_go_to_the_clipboard_and_nothing_secret_is_printed(tmp_path, capsys):
    tool, copied, opened, asked = _tool(), [], [], []
    path = _env(tmp_path, f"WAHA_DASHBOARD_PASSWORD={PASSWORD}\nWAHA_API_KEY={KEY}\n")
    code = tool.main([], env_path=path, copier=lambda text: copied.append(text) or True,
                     opener=opened.append, ask=asked.append)
    out = capsys.readouterr().out
    assert code == 0 and copied == [PASSWORD, KEY, ""]  # o último limpa a área de transferência
    assert opened == [tool.DASHBOARD] and len(asked) == 2
    assert PASSWORD not in out and KEY not in out
    assert "usuário admin" in out and "API URL http://localhost:3000" in out


def test_a_custom_username_is_shown_and_no_open_keeps_the_browser_closed(tmp_path, capsys):
    path = _env(tmp_path, f"WAHA_DASHBOARD_USERNAME=dono\nWAHA_DASHBOARD_PASSWORD={PASSWORD}\n"
                          f"WAHA_API_KEY={KEY}\n")
    opened = []
    code = _tool().main(["--no-open"], env_path=path, copier=lambda text: True,
                        opener=opened.append, ask=lambda prompt: "")
    assert code == 0 and opened == [] and "usuário dono" in capsys.readouterr().out


def test_without_a_clipboard_it_names_the_variable_never_the_value(tmp_path, capsys):
    path = _env(tmp_path, f"WAHA_DASHBOARD_PASSWORD={PASSWORD}\nWAHA_API_KEY={KEY}\n")
    code = _tool().main(["--no-open"], env_path=path, copier=lambda text: False,
                        ask=lambda prompt: "")
    out = capsys.readouterr().out
    assert code == 0 and "copie WAHA_DASHBOARD_PASSWORD do .env" in out
    assert "copie WAHA_API_KEY do .env" in out
    assert PASSWORD not in out and KEY not in out


def test_missing_values_stop_before_opening_anything(tmp_path, capsys):
    path = _env(tmp_path, "WAHA_API_KEY=\n")
    opened = []
    code = _tool().main([], env_path=path, copier=lambda text: True, opener=opened.append,
                        ask=lambda prompt: "")
    assert code == 1 and opened == []
    assert "WAHA_DASHBOARD_PASSWORD, WAHA_API_KEY" in capsys.readouterr().out


def test_a_closed_stdin_does_not_break_the_run(tmp_path):
    path = _env(tmp_path, f"WAHA_DASHBOARD_PASSWORD={PASSWORD}\nWAHA_API_KEY={KEY}\n")

    def eof(prompt):
        raise EOFError

    assert _tool().main(["--no-open"], env_path=path, copier=lambda text: True, ask=eof) == 0
