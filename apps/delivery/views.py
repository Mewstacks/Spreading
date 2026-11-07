"""Pareamento do WhatsApp: QR sempre fresco, só para staff logado."""
from django.contrib.admin.views.decorators import staff_member_required
from django.http import Http404, HttpResponse
from django.views.decorators.cache import never_cache

from .transports import WahaTransport

PAGE = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WhatsApp</title>{refresh}
<style>:root{{color-scheme:light dark}}body{{font-family:system-ui,sans-serif;margin:0;
min-height:100vh;display:grid;place-items:center;padding:16px;box-sizing:border-box}}
main{{text-align:center;max-width:360px;width:100%}}img{{width:100%;height:auto;
image-rendering:pixelated;background:#fff;padding:12px;box-sizing:border-box;border-radius:12px}}
h1{{font-size:1.25rem;margin:0 0 12px}}p{{margin:12px 0 0;font-size:.95rem}}</style></head>
<body><main><h1>WhatsApp</h1>{body}</main></body></html>"""


@staff_member_required
@never_cache
def pair(request):
    transport = WahaTransport()
    status = transport.ensure_started()
    if status == "WORKING":
        body, refresh = "<p><strong>Conectado</strong></p>", ""
    elif status == "SCAN_QR_CODE":
        body = '<img src="/pair/qr.png" alt="QR">' + f"<p>{status}</p>"
        refresh = '<meta http-equiv="refresh" content="12">'
    else:
        body = f"<p>{status or 'sem resposta do WAHA'}</p>"
        refresh = '<meta http-equiv="refresh" content="4">'
    return HttpResponse(PAGE.format(body=body, refresh=refresh))


@staff_member_required
@never_cache
def pair_qr(request):
    image = WahaTransport().qr_png()
    if not image:
        raise Http404
    return HttpResponse(image, content_type="image/png")
