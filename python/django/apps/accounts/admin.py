from django.contrib import admin

from .models import Perfil


@admin.register(Perfil)
class PerfilAdmin(admin.ModelAdmin):
    list_display = ("user", "email_verificado", "afiliado_tag_ml", "afiliado_tag_amazon",
                    "amazon_conectado", "wa_estado", "ml_estado")
    list_filter = ("email_verificado",)
    search_fields = ("user__username", "user__email", "afiliado_tag_ml", "afiliado_tag_amazon")
    readonly_fields = ("verificado_em", "alerta_wa_em", "alerta_ml_em")
