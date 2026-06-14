from django.contrib import admin

from apps.scrapers.models import (
    Cupom, Produto, HistoricoEnvio, ConfiguracaoEnvio, CupomCodigo, LinkAfiliadoUsuario,
)


@admin.register(CupomCodigo)
class CupomCodigoAdmin(admin.ModelAdmin):
    list_display = ("codigo", "descricao", "tipo_desconto", "valor_desconto", "valor_minimo", "categorias", "validade", "ativo")
    list_filter = ("ativo", "tipo_desconto")


@admin.register(Cupom)
class CupomAdmin(admin.ModelAdmin):
    list_display = ("campanha_id", "titulo", "tipo_desconto", "valor_desconto", "valor_minimo", "data_criacao")
    search_fields = ("campanha_id", "titulo")


@admin.register(Produto)
class ProdutoAdmin(admin.ModelAdmin):
    list_display = ("nome", "marketplace", "macro_categoria", "preco_sem_desconto", "preco_com_cupom", "afiliado_ok", "campanha_id", "asin")
    list_filter = ("marketplace", "macro_categoria", "afiliado_ok", "origem")
    search_fields = ("nome", "campanha_id", "asin")


@admin.register(HistoricoEnvio)
class HistoricoEnvioAdmin(admin.ModelAdmin):
    list_display = ("produto", "usuario", "data_envio")
    list_filter = ("usuario",)
    date_hierarchy = "data_envio"


@admin.register(ConfiguracaoEnvio)
class ConfiguracaoEnvioAdmin(admin.ModelAdmin):
    list_display = ("owner", "macro_categoria", "termo_busca", "canal", "marketplace", "grupo_nome",
                    "grupo_id", "intervalo_minutos", "min_desconto_percent", "ativo", "ultimo_envio")
    list_filter = ("ativo", "owner", "canal", "marketplace", "macro_categoria")


@admin.register(LinkAfiliadoUsuario)
class LinkAfiliadoUsuarioAdmin(admin.ModelAdmin):
    list_display = ("usuario", "produto", "afiliado_ok", "criado_em")
    list_filter = ("usuario", "afiliado_ok")
    search_fields = ("produto__nome",)
