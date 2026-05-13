import asyncio
import os
import queue
import threading
from contextlib import redirect_stdout

from django.http import StreamingHttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from apps.scrapers.scrapper_mercadolivre.scrapper import main as scrapper_main


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
