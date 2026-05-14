import asyncio
import os
import queue
import threading
from contextlib import redirect_stdout

from django.db.models import F, ExpressionWrapper, FloatField
from django.http import StreamingHttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from apps.scrapers.models import Produto
from apps.scrapers.scraper_mercadolivre.scraper import main as scrapper_main


class _QueueWriter:
    """File-like object that feeds lines into a Queue for SSE streaming."""

    def __init__(self, q: queue.Queue):
        self._q = q
        self._buf = ""

    def write(self, text: str):
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line:
                self._q.put(line)

    def flush(self):
        if self._buf:
            self._q.put(self._buf)
            self._buf = ""


def dashboard(request):
    return render(request, "scrapers/dashboard.html")


def top_promocoes(request):
    categoria = request.GET.get("categoria", "").strip()
    macro = request.GET.get("macro", "").strip()
    macro_categorias = (
        Produto.objects
        .exclude(macro_categoria__isnull=True)
        .exclude(macro_categoria="")
        .values_list("macro_categoria", flat=True)
        .distinct()
        .order_by("macro_categoria")
    )
    categorias_qs = (
        Produto.objects
        .exclude(categoria__isnull=True)
        .exclude(categoria="DESCONHECIDO")
        .exclude(categoria="")
    )
    if macro:
        categorias_qs = categorias_qs.filter(macro_categoria=macro)
    categorias = categorias_qs.values_list("categoria", flat=True).distinct().order_by("categoria")
    qs = Produto.objects.annotate(
        economia=ExpressionWrapper(F("preco_sem_desconto") - F("preco_com_cupom"), output_field=FloatField())
    )
    if macro:
        qs = qs.filter(macro_categoria=macro)
    if categoria:
        qs = qs.filter(categoria=categoria)
    produtos = qs.order_by("-economia")[:10]
    return render(request, "scrapers/top_promocoes.html", {
        "produtos": produtos,
        "categorias": categorias,
        "categoria_selecionada": categoria,
        "macro_categorias": macro_categorias,
        "macro_selecionada": macro,
    })


@require_GET
def run_scraper_stream(request):
    """SSE endpoint — streams every print() from the scraper to the browser."""

    def _event_stream():
        q: queue.Queue = queue.Queue()
        writer = _QueueWriter(q)

        def _run():
            # Playwright's sync API leaves asyncio's event loop in a running state
            # on the calling thread, which trips Django's ORM async-safety check.
            # DJANGO_ALLOW_ASYNC_UNSAFE is the documented bypass for this scenario.
            os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
            asyncio.set_event_loop(asyncio.new_event_loop())
            try:
                with redirect_stdout(writer):
                    scrapper_main()
            except Exception as exc:
                q.put(f"[ERRO] {exc}")
            finally:
                writer.flush()
                q.put(None)  # sentinel

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

        while True:
            line = q.get()
            if line is None:
                yield "data: __DONE__\n\n"
                break
            yield f"data: {line}\n\n"

    response = StreamingHttpResponse(_event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


@require_GET
def auth_stream(request):
    """SSE endpoint — abre browser visível para login no ML e salva auth.json."""
    from apps.scrapers.auxiliar import iniciar_browser, BrowserError

    auth_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "scrapper_mercadolivre", "auth.json"
    )

    def _event_stream():
        q: queue.Queue = queue.Queue()
        writer = _QueueWriter(q)

        def _run():
            os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
            asyncio.set_event_loop(asyncio.new_event_loop())
            try:
                with redirect_stdout(writer):
                    with iniciar_browser(auth_path=auth_path, headless=True) as (page, context):
                        pass  # só valida/salva a sessão
            except BrowserError as exc:
                msg = str(exc)
                if "LOGIN_REQUIRED" in msg or "login" in msg.lower():
                    q.put("LOGIN_REQUIRED")
                else:
                    q.put(f"[ERRO] {exc}")
            except Exception as exc:
                q.put(f"[ERRO] {exc}")
            finally:
                writer.flush()
                q.put(None)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

        while True:
            line = q.get()
            if line is None:
                yield "data: __DONE__\n\n"
                break
            yield f"data: {line}\n\n"

    response = StreamingHttpResponse(_event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response
