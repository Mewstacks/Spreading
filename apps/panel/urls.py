from django.urls import path

from . import catalog_views, manual_views, revenue_views, sources_views, views

app_name = "panel"

urlpatterns = [
    path("", views.groups, name="groups"),
    path("adicionar/", views.add_group, name="add"),
    path("ofertas/", catalog_views.catalog, name="offers"),
    path("cupons/", catalog_views.catalog, {"kind": "coupon"}, name="coupons"),
    path("ofertas/<int:offer_id>/enviar/", catalog_views.send_offer, name="offer_send"),
    path("ofertas/<int:offer_id>/previa/", catalog_views.preview, name="offer_preview"),
    path("whatsapp/", views.whatsapp, name="whatsapp"),
    path("links/", views.links, name="links"),
    path("manual/", manual_views.page, name="manual"),
    path("receita/", revenue_views.revenue, name="revenue"),
    path("fontes/", sources_views.sources, name="sources"),
    path("grupo/<int:rule_id>/", views.group, name="group"),
    path("grupo/<int:rule_id>/enviar/", views.send_now, name="send_now"),
]
