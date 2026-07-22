from django.contrib import admin

from apps.scrapers.models import (
    Cupom, Produto, HistoricoEnvio, ConfiguracaoEnvio, CupomCodigo, LinkAfiliadoUsuario,
    Publicacao, CliquePublicacao, ReceitaAfiliado, RelatorioSync, EventoOperacional,
    FonteIngestao, ExecucaoIngestao, CupomNormalizado, ProdutoCupom,
    IntegracaoAfiliado, ProgramaAfiliado,
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


@admin.register(Publicacao)
class PublicacaoAdmin(admin.ModelAdmin):
    list_display = ("usuario", "produto", "destino_nome", "canal", "status", "variante", "criada_em")
    list_filter = ("status", "canal", "variante", "usuario")
    search_fields = ("produto__nome", "destino_nome", "erro")
    readonly_fields = [field.name for field in Publicacao._meta.fields]


@admin.register(CliquePublicacao)
class CliquePublicacaoAdmin(admin.ModelAdmin):
    list_display = ("publicacao", "clicado_em")
    readonly_fields = ("publicacao", "clicado_em")


@admin.register(ReceitaAfiliado)
class ReceitaAfiliadoAdmin(admin.ModelAdmin):
    list_display = ("usuario", "marketplace", "data", "etiqueta", "cliques", "pedidos", "comissao")
    list_filter = ("marketplace", "usuario", "origem", "granularidade")


@admin.register(RelatorioSync)
class RelatorioSyncAdmin(admin.ModelAdmin):
    list_display = ("usuario", "marketplace", "status", "ultimo_sucesso", "proxima_execucao")
    list_filter = ("marketplace", "status")
    search_fields = ("usuario__username", "erro")


@admin.register(EventoOperacional)
class EventoOperacionalAdmin(admin.ModelAdmin):
    list_display = ("criado_em", "level", "pipeline", "evento", "usuario", "mensagem")
    list_filter = ("level", "pipeline", "evento")
    search_fields = ("mensagem", "erro", "usuario__username")
    readonly_fields = [field.name for field in EventoOperacional._meta.fields]
    date_hierarchy = "criado_em"


@admin.register(FonteIngestao)
class FonteIngestaoAdmin(admin.ModelAdmin):
    list_display = ("nome", "marketplace", "status", "habilitada", "ultimo_total",
                    "ultimo_sucesso", "falhas_consecutivas")
    list_filter = ("status", "habilitada", "marketplace")
    readonly_fields = ("ultimo_sucesso", "ultima_tentativa", "ultimo_total",
                       "erro_publico", "falhas_consecutivas")


@admin.register(ExecucaoIngestao)
class ExecucaoIngestaoAdmin(admin.ModelAdmin):
    list_display = ("fonte", "status", "total_ofertas", "total_cupons",
                    "iniciada_em", "finalizada_em")
    list_filter = ("status", "fonte")
    readonly_fields = [field.name for field in ExecucaoIngestao._meta.fields]


@admin.register(CupomNormalizado)
class CupomNormalizadoAdmin(admin.ModelAdmin):
    list_display = ("titulo", "codigo", "marketplace", "fonte", "confianca",
                    "estado", "validade", "ultima_observacao")
    list_filter = ("marketplace", "fonte", "confianca", "estado")
    search_fields = ("titulo", "codigo", "external_id")


@admin.register(ProdutoCupom)
class ProdutoCupomAdmin(admin.ModelAdmin):
    list_display = ("produto", "cupom", "status", "verificado_em")
    list_filter = ("status",)


@admin.register(IntegracaoAfiliado)
class IntegracaoAfiliadoAdmin(admin.ModelAdmin):
    list_display = ("owner", "provedor", "nome_conta", "status", "habilitada",
                    "ultimo_sucesso", "proxima_sincronizacao")
    list_filter = ("provedor", "status", "habilitada")
    search_fields = ("owner__username", "nome_conta", "identificador_conta")
    exclude = ("token",)
    readonly_fields = ("ultimo_sucesso", "ultima_tentativa", "erro_publico",
                       "falhas_consecutivas")


@admin.register(ProgramaAfiliado)
class ProgramaAfiliadoAdmin(admin.ModelAdmin):
    list_display = ("nome", "integracao", "status_vinculo", "link_status", "habilitado")
    list_filter = ("status_vinculo", "link_status", "habilitado")
    search_fields = ("nome", "external_id", "integracao__owner__username")
