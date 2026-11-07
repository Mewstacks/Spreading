from django.urls import path

from . import views

app_name = "panel"

urlpatterns = [
    path("", views.groups, name="groups"),
    path("adicionar/", views.add_group, name="add"),
    path("links/", views.links, name="links"),
    path("grupo/<int:rule_id>/", views.group, name="group"),
    path("grupo/<int:rule_id>/enviar/", views.send_now, name="send_now"),
]
