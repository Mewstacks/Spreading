from django.contrib import admin

from apps.scrapers.models import Cupom, Produto, HistoricoEnvio, ConfiguracaoEnvio, CupomCodigo


@admin.register(CupomCodigo)
class CupomCodigoAdmin(admin.ModelAdmin):
    list_display = ("codigo", "descricao", "tipo_desconto", "valor_desconto", "valor_minimo", "validade", "ativo")
    list_filter = ("ativo", "tipo_desconto")


@admin.register(Cupom)
class CupomAdmin(admin.ModelAdmin):
    list_display = ("campanha_id", "titulo", "tipo_desconto", "valor_desconto", "valor_minimo", "data_criacao")
    search_fields = ("campanha_id", "titulo")


@admin.register(Produto)
class ProdutoAdmin(admin.ModelAdmin):
    list_display = ("nome", "macro_categoria", "categoria", "preco_sem_desconto", "preco_com_cupom", "campanha_id")
    list_filter = ("macro_categoria",)
    search_fields = ("nome", "campanha_id")


@admin.register(HistoricoEnvio)
class HistoricoEnvioAdmin(admin.ModelAdmin):
    list_display = ("produto", "data_envio")
    date_hierarchy = "data_envio"


@admin.register(ConfiguracaoEnvio)
class ConfiguracaoEnvioAdmin(admin.ModelAdmin):
    list_display = ("macro_categoria", "grupo_nome", "grupo_id", "intervalo_minutos",
                    "min_desconto_percent", "ativo", "ultimo_envio")
    list_filter = ("ativo", "macro_categoria")
