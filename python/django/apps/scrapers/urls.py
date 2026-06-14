from django.urls import path
from apps.scrapers import views

urlpatterns = [
    path("", views.dashboard, name="scraper-dashboard"),
    path("run/", views.run_scraper_stream, name="scraper-run"),
    path("gerar-links/", views.gerar_links_stream, name="scraper-gerar-links"),
    path("ofertas/", views.scrape_ofertas_stream, name="scraper-ofertas-run"),
    path("cupons-codigo/", views.scrape_cupons_codigo_stream, name="scraper-cupons-codigo"),
    path("buscar-termo/", views.buscar_termo_stream, name="scraper-buscar-termo"),
    path("automacao/", views.automacao_control, name="scraper-automacao"),
    path("auth/", views.auth_stream, name="scraper-auth"),
    path("top/", views.top_promocoes, name="scraper-top"),
    path("enviar-produto/", views.enviar_produto_stream, name="scraper-enviar-produto"),
    path("buscar-promocoes/", views.buscar_promocoes_stream, name="scraper-buscar-promocoes"),
    path("config/", views.configuracoes, name="scraper-configuracoes"),
    path("enviar-agora/", views.enviar_agora_stream, name="scraper-enviar-agora"),
    path("whatsapp/", views.whatsapp_painel, name="scraper-whatsapp"),
    path("whatsapp/status/", views.whatsapp_status_json, name="scraper-whatsapp-status"),
    path("whatsapp/qr.png", views.whatsapp_qr_png, name="scraper-whatsapp-qr"),
    path("whatsapp/refresh-grupos/", views.whatsapp_refresh_grupos, name="scraper-whatsapp-refresh"),
    path("whatsapp/grupos/", views.whatsapp_grupos_json, name="scraper-whatsapp-grupos"),
    path("telegram/", views.telegram_painel, name="scraper-telegram"),
    path("telegram/status/", views.telegram_status_json, name="scraper-telegram-status"),
]
