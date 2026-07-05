from django.urls import path
from apps.scrapers import views, views_admin

urlpatterns = [
    # ── Workspace do superadmin ──
    path("painel-admin/", views_admin.superadmin_usuarios, name="superadmin-usuarios"),
    path("painel-admin/criar/", views_admin.superadmin_criar_usuario, name="superadmin-criar-usuario"),
    path("painel-admin/infra/", views_admin.superadmin_infra, name="superadmin-infra"),
    path("painel-admin/usuario/<int:user_id>/", views_admin.superadmin_usuario_detalhe, name="superadmin-usuario"),
    path("painel-admin/usuario/<int:user_id>/suspender/", views_admin.superadmin_suspender, name="superadmin-suspender"),
    path("painel-admin/usuario/<int:user_id>/cotas/", views_admin.superadmin_cotas, name="superadmin-cotas"),
    path("painel-admin/usuario/<int:user_id>/impersonar/", views_admin.superadmin_impersonar, name="superadmin-impersonar"),
    path("painel-admin/parar-impersonar/", views_admin.superadmin_parar_impersonar, name="superadmin-parar-impersonar"),

    path("", views.dashboard, name="scraper-dashboard"),
    path("comecar/", views.comecar, name="scraper-comecar"),
    path("run/", views.run_scraper_stream, name="scraper-run"),
    path("gerar-links/", views.gerar_links_stream, name="scraper-gerar-links"),
    path("ofertas/", views.scrape_ofertas_stream, name="scraper-ofertas-run"),
    path("cupons-codigo/", views.scrape_cupons_codigo_stream, name="scraper-cupons-codigo"),
    path("buscar-termo/", views.buscar_termo_stream, name="scraper-buscar-termo"),
    path("automacao/", views.automacao_control, name="scraper-automacao"),
    path("auth/", views.auth_stream, name="scraper-auth"),
    path("ml/upload/", views.ml_upload_sessao, name="scraper-ml-upload"),
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
