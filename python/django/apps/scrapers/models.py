from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone
import secrets
import uuid

from apps.accounts.fields import EncryptedCharField

# Alfabeto base62 do slug curto de publicação. 7 caracteres dão 62^7 (~3,5
# trilhões) de combinações: colisão é estatisticamente irrelevante e, se
# acontecer, o unique do banco barra e o envio seguinte gera outro slug.
_ALFABETO_SLUG = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def gerar_slug_curto():
    return "".join(secrets.choice(_ALFABETO_SLUG) for _ in range(7))

class Cupom(models.Model):
    campanha_id = models.CharField(max_length=100, unique=True)
    titulo = models.CharField(max_length=255)
    tipo_desconto = models.CharField(max_length=20) # 'fixo' ou 'porcentagem'
    valor_desconto = models.FloatField()
    valor_minimo = models.FloatField(default=0.0)  # compra mínima para o cupom ser válido
    link_original = models.URLField(max_length=1000)
    codigo = models.CharField(max_length=512, blank=True, default="")
    data_criacao = models.DateTimeField(auto_now_add=True)
    fonte = models.CharField(max_length=80, blank=True, default="")
    validade = models.DateTimeField(null=True, blank=True)
    ultima_verificacao = models.DateTimeField(null=True, blank=True, db_index=True)
    estado = models.CharField(max_length=20, default="ativo", db_index=True)

class Produto(models.Model):
    # Marketplace de origem ('mercadolivre' | 'amazon' | 'shopee'). Permite que a
    # seleção/envio sejam agnósticos: o link de afiliado certo é resolvido via registry.
    marketplace = models.CharField(max_length=20, default="mercadolivre", db_index=True)
    # Dono do item (multi-tenant). null = pool COMPARTILHADO (ML raspado p/ todos).
    # set = item privado daquele usuário (Amazon, raspado com a conta Creators dele).
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                              null=True, blank=True, db_index=True,
                              related_name="produtos")
    # ASIN da Amazon (vazio p/ outros marketplaces). Usado p/ link canônico /dp/{ASIN},
    # dedup por (marketplace, asin) e refresh de preço/liveness via getItems.
    asin = models.CharField(max_length=20, blank=True, default="", db_index=True)
    campanha_id = models.CharField(max_length=100, db_index=True, blank=True, default="")
    origem = models.CharField(max_length=20, default="cupom", db_index=True)  # 'cupom' | 'oferta'
    nome = models.CharField(max_length=255)
    preco_sem_desconto = models.FloatField()
    preco_com_cupom = models.FloatField()
    link_produto = models.URLField(max_length=1000)
    categoria = models.CharField(max_length=100, null=True, blank=True, db_index=True) # Lembra do domain_id?
    macro_categoria = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    # Cache do link de afiliado pré-gerado (evita abrir Playwright na hora do envio)
    url_isca = models.URLField(max_length=1000, blank=True, default="")
    link_afiliado = models.URLField(max_length=1000, blank=True, default="")
    imagem_url = models.URLField(max_length=1000, blank=True, default="")
    frete_full = models.BooleanField(default=False)
    # Marcado quando o card do ML traz o selo "Oferta relâmpago". Feedback da cliente:
    # ofertas relâmpago vendem muito; o ranking dá boost a elas (selecionar_item_para_grupo).
    relampago = models.BooleanField(default=False, db_index=True)
    # Código digitável no checkout, quando o item vem de um cupom de código (ex: CASINHA)
    codigo_checkout = models.CharField(max_length=60, blank=True, default="")
    # True quando o link_afiliado foi verificado e carrega a tag de afiliado (A3).
    # False = link sem atribuição -> não enviar (perda de comissão silenciosa).
    afiliado_ok = models.BooleanField(default=False)
    # Frase de marketing gerada por LLM, cacheada na raspagem (evita bloquear o envio).
    frase_llm = models.CharField(max_length=255, blank=True, default="")
    # Proveniência e confiança: a UI e o seletor nunca precisam adivinhar se o
    # dado ainda é publicável.
    fonte = models.CharField(max_length=80, blank=True, default="", db_index=True)
    primeira_observacao = models.DateTimeField(auto_now_add=True, null=True)
    ultima_observacao = models.DateTimeField(auto_now=True, null=True, db_index=True)
    ultima_verificacao = models.DateTimeField(null=True, blank=True, db_index=True)
    estado = models.CharField(max_length=20, default="ativo", db_index=True)
    falha_verificacao = models.CharField(max_length=255, blank=True, default="")
    preco_fonte = models.FloatField(null=True, blank=True)
    preco_efetivo = models.FloatField(null=True, blank=True)
    confianca = models.CharField(max_length=20, default="media", db_index=True)
    evidencia = models.JSONField(default=dict, blank=True)
    valido_ate = models.DateTimeField(null=True, blank=True, db_index=True)
    falhas_consecutivas = models.PositiveIntegerField(default=0)


class FonteIngestao(models.Model):
    """Estado durável de um conector. Nunca contém credenciais."""
    STATUS = [(s, s) for s in ("ok", "degraded", "blocked", "disabled")]
    slug = models.CharField(max_length=80, unique=True)
    marketplace = models.CharField(max_length=20, db_index=True)
    nome = models.CharField(max_length=120)
    habilitada = models.BooleanField(default=True)
    status = models.CharField(max_length=20, choices=STATUS, default="degraded")
    ultimo_sucesso = models.DateTimeField(null=True, blank=True)
    ultima_tentativa = models.DateTimeField(null=True, blank=True)
    ultimo_total = models.PositiveIntegerField(default=0)
    erro_publico = models.CharField(max_length=255, blank=True, default="")
    falhas_consecutivas = models.PositiveIntegerField(default=0)

    def __str__(self):
        return self.nome


class IntegracaoAfiliado(models.Model):
    """Conta de uma rede de afiliacao pertencente a um usuario.

    Mercado Livre e Amazon ainda usam os fluxos legados do Perfil. Este modelo e o
    contrato extensivel das redes com API, com Awin como primeiro provedor.
    """

    STATUS = [
        ("pendente", "Pendente"), ("conectada", "Conectada"),
        ("degradada", "Degradada"), ("reconectar", "Reconectar"),
        ("desativada", "Desativada"),
    ]
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                              related_name="integracoes_afiliado")
    provedor = models.CharField(max_length=30, default="awin", db_index=True)
    identificador_conta = models.CharField(max_length=120, blank=True, default="")
    nome_conta = models.CharField(max_length=160, blank=True, default="")
    token = EncryptedCharField(max_length=4096, blank=True, default="")
    habilitada = models.BooleanField(default=True)
    status = models.CharField(max_length=20, choices=STATUS, default="pendente",
                              db_index=True)
    ultima_tentativa = models.DateTimeField(null=True, blank=True)
    ultimo_sucesso = models.DateTimeField(null=True, blank=True)
    proxima_sincronizacao = models.DateTimeField(null=True, blank=True, db_index=True)
    programas_sincronizados_em = models.DateTimeField(null=True, blank=True)
    erro_publico = models.CharField(max_length=255, blank=True, default="")
    falhas_consecutivas = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["owner", "provedor"],
                                    name="uniq_integracao_provedor_usuario"),
        ]

    def __str__(self):
        return f"{self.provedor}:{self.nome_conta or self.identificador_conta}"


class ProgramaAfiliado(models.Model):
    """Anunciante/programa habilitado dentro de uma integracao do usuario."""

    integracao = models.ForeignKey(IntegracaoAfiliado, on_delete=models.CASCADE,
                                   related_name="programas")
    external_id = models.CharField(max_length=80)
    nome = models.CharField(max_length=180)
    dominio = models.CharField(max_length=255, blank=True, default="")
    dominios_validos = models.JSONField(default=list, blank=True)
    logo_url = models.URLField(max_length=1000, blank=True, default="")
    status_vinculo = models.CharField(max_length=30, default="joined", db_index=True)
    link_status = models.CharField(max_length=30, default="online", db_index=True)
    deeplink_habilitado = models.BooleanField(default=True)
    habilitado = models.BooleanField(default=True)
    comissao_min = models.FloatField(null=True, blank=True)
    comissao_max = models.FloatField(null=True, blank=True)
    comissao_tipo = models.CharField(max_length=20, blank=True, default="")
    comissao_sincronizada_em = models.DateTimeField(null=True, blank=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["integracao", "external_id"],
                                    name="uniq_programa_por_integracao"),
        ]

    def __str__(self):
        return self.nome


class ExecucaoIngestao(models.Model):
    STATUS = [(s, s) for s in ("running", "ok", "empty", "error", "blocked")]
    fonte = models.ForeignKey(FonteIngestao, on_delete=models.CASCADE,
                              related_name="execucoes")
    integracao = models.ForeignKey(IntegracaoAfiliado, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name="execucoes")
    iniciada_em = models.DateTimeField(auto_now_add=True, db_index=True)
    finalizada_em = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS, default="running")
    total_ofertas = models.PositiveIntegerField(default=0)
    total_cupons = models.PositiveIntegerField(default=0)
    erro_publico = models.CharField(max_length=255, blank=True, default="")


class CupomNormalizado(models.Model):
    """Cupom independente de produto; só é publicável via ProdutoCupom confirmado."""
    fonte = models.ForeignKey(FonteIngestao, on_delete=models.CASCADE,
                              related_name="cupons")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                              null=True, blank=True, db_index=True,
                              related_name="cupons_normalizados")
    integracao = models.ForeignKey(IntegracaoAfiliado, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name="cupons")
    programa = models.ForeignKey(ProgramaAfiliado, on_delete=models.SET_NULL,
                                 null=True, blank=True, related_name="cupons")
    external_id = models.CharField(max_length=160)
    marketplace = models.CharField(max_length=20, db_index=True)
    tipo_conteudo = models.CharField(max_length=20, default="voucher", db_index=True)
    anunciante_nome = models.CharField(max_length=180, blank=True, default="")
    titulo = models.CharField(max_length=255)
    codigo = models.CharField(max_length=120, blank=True, default="")
    # Categoria/ação da fonte (Sellers, Fashion, "site inteiro", ...). Vem do
    # `escopo` das regras normalizadas; alimenta o filtro por categoria dos cupons.
    categoria = models.CharField(max_length=100, blank=True, default="", db_index=True)
    regras = models.JSONField(default=dict, blank=True)
    link = models.URLField(max_length=1000, blank=True, default="")
    inicio = models.DateTimeField(null=True, blank=True, db_index=True)
    validade = models.DateTimeField(null=True, blank=True, db_index=True)
    restrito = models.BooleanField(default=False, db_index=True)
    relampago = models.BooleanField(default=False, db_index=True)
    estado = models.CharField(max_length=20, default="ativo", db_index=True)
    confianca = models.CharField(max_length=20, default="baixa", db_index=True)
    evidencia = models.JSONField(default=dict, blank=True)
    primeira_observacao = models.DateTimeField(auto_now_add=True)
    ultima_observacao = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["fonte", "external_id"], condition=models.Q(owner__isnull=True),
                name="uniq_cupom_compartilhado_fonte_external"),
            models.UniqueConstraint(
                fields=["owner", "fonte", "external_id"],
                condition=models.Q(owner__isnull=False),
                name="uniq_cupom_privado_owner_fonte_external"),
        ]


class ProdutoCupom(models.Model):
    STATUS = [
        ("confirmado", "Confirmado"), ("provavel", "Provável"),
        ("nao_aplicavel", "Não aplicável"), ("expirado", "Expirado"),
    ]
    produto = models.ForeignKey(Produto, on_delete=models.CASCADE,
                                related_name="cupons_normalizados")
    cupom = models.ForeignKey(CupomNormalizado, on_delete=models.CASCADE,
                              related_name="produtos")
    status = models.CharField(max_length=20, choices=STATUS, default="provavel")
    verificado_em = models.DateTimeField(null=True, blank=True)
    evidencia = models.JSONField(default=dict, blank=True)

    class Meta:
        unique_together = ("produto", "cupom")

class PrecoHistorico(models.Model):
    """Uma observação de preço por raspagem — base p/ detectar QUEDA REAL e derrubar
    'de/por' inflado (o preço "de" do ML costuma ser fictício). Chave por identidade
    do produto (asin na Amazon; URL normalizada no ML), não pelo id do Produto (que
    é recriado a cada raspagem)."""
    marketplace = models.CharField(max_length=20, db_index=True)
    chave = models.CharField(max_length=300, db_index=True)
    preco = models.FloatField()
    data = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["marketplace", "chave", "data"])]


class HistoricoEnvio(models.Model):
    produto = models.ForeignKey(Produto, on_delete=models.CASCADE)
    # Dono do envio (multi-tenant): dedup "nunca repetir" passa a ser POR usuário.
    # null = envios legados (single-tenant) — tratados como do owner default na migração.
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                null=True, blank=True, db_index=True,
                                related_name="envios")
    data_envio = models.DateTimeField(auto_now_add=True, db_index=True)

    def __str__(self):
        return f"{self.produto.nome} enviado em {self.data_envio}"


class Publicacao(models.Model):
    """Registro imutável da decisão e do resultado de uma publicação."""
    STATUS = [
        ("pendente", "Pendente"), ("enviado", "Enviado"),
        ("falhou", "Falhou"), ("incerto", "Confirmação pendente"),
        ("ignorado", "Ignorado"),
    ]
    id_publico = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    origem = models.CharField(max_length=30, default="produto", db_index=True)
    # Slug do link curto publicado na mensagem (/r/<slug>/). null p/ as linhas
    # anteriores ao campo — o token assinado antigo continua funcionando p/ elas.
    slug_curto = models.CharField(max_length=12, unique=True, null=True, blank=True,
                                  default=gerar_slug_curto, editable=False)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="publicacoes")
    produto = models.ForeignKey(Produto, on_delete=models.SET_NULL, null=True,
                                related_name="publicacoes")
    cupom_normalizado = models.ForeignKey(
        "CupomNormalizado", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="publicacoes",
    )
    configuracao = models.ForeignKey("ConfiguracaoEnvio", on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name="publicacoes")
    canal = models.CharField(max_length=20)
    destino_id = models.CharField(max_length=100)
    destino_nome = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS, default="pendente", db_index=True)
    erro = models.CharField(max_length=500, blank=True, default="")
    variante = models.CharField(max_length=1, default="A")
    mensagem = models.TextField(blank=True, default="")
    link_afiliado = models.URLField(max_length=1500, blank=True, default="")
    link_rastreado = models.URLField(max_length=1500, blank=True, default="")
    preco_original = models.FloatField(default=0)
    preco_final = models.FloatField(default=0)
    cupom = models.CharField(max_length=255, blank=True, default="")
    categoria = models.CharField(max_length=100, blank=True, default="")
    score = models.FloatField(default=0)
    motivos_score = models.JSONField(default=list, blank=True)
    criada_em = models.DateTimeField(auto_now_add=True, db_index=True)
    enviada_em = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        # O dashboard filtra sempre por (usuario, janela de data): os índices de
        # coluna única obrigavam o banco a escolher um e filtrar o resto na mão.
        indexes = [models.Index(fields=["usuario", "criada_em"])]


class CliquePublicacao(models.Model):
    """Clique sem IP, cookie ou identificador pessoal."""
    publicacao = models.ForeignKey(Publicacao, on_delete=models.CASCADE, related_name="cliques")
    clicado_em = models.DateTimeField(auto_now_add=True, db_index=True)


class LinkAfiliadoCupomUsuario(models.Model):
    """Cache de link afiliado de um cupom por usuario e URL de origem."""
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="links_cupons")
    cupom = models.ForeignKey(CupomNormalizado, on_delete=models.CASCADE,
                              related_name="links_usuarios")
    url_origem = models.URLField(max_length=1000)
    link_afiliado = models.URLField(max_length=1500)
    afiliado_ok = models.BooleanField(default=False)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["usuario", "cupom"],
                                    name="uniq_link_cupom_usuario"),
        ]


class ReceitaAfiliado(models.Model):
    """Linha normalizada de relatório sincronizado do marketplace."""
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="receitas_afiliado")
    marketplace = models.CharField(max_length=20, db_index=True)
    data = models.DateField(db_index=True)
    etiqueta = models.CharField(max_length=120, blank=True, default="")
    produto_nome = models.CharField(max_length=255, blank=True, default="")
    cliques = models.PositiveIntegerField(default=0)
    conversoes = models.PositiveIntegerField(default=0)
    pedidos = models.PositiveIntegerField(default=0)
    receita = models.FloatField(default=0)
    comissao = models.FloatField(default=0)
    periodo_inicio = models.DateField(null=True, blank=True)
    periodo_fim = models.DateField(null=True, blank=True)
    origem = models.CharField(max_length=20, default="auto")
    granularidade = models.CharField(max_length=20, default="dia")
    hash_origem = models.CharField(max_length=64, unique=True)
    importada_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        # resumo_financeiro busca o snapshot mais recente por (usuario, marketplace).
        indexes = [models.Index(fields=["usuario", "marketplace", "data"])]


class RelatorioSync(models.Model):
    STATUS = [
        ("nunca", "Nunca sincronizado"),
        ("rodando", "Sincronizando"),
        ("ok", "Sincronizado"),
        ("erro", "Erro"),
        ("acao", "Precisa de ação"),
        # Distinto de "acao": não há nada que o usuário possa fazer. A leitura
        # automática daquele portal ainda não existe/não está configurada, e mandar
        # ele "reconectar" uma conta que já está conectada é um loop sem saída.
        ("nao_configurado", "Sincronização automática indisponível"),
    ]
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="syncs_relatorio")
    marketplace = models.CharField(max_length=20, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS, default="nunca", db_index=True)
    ultimo_inicio = models.DateTimeField(null=True, blank=True)
    ultimo_fim = models.DateTimeField(null=True, blank=True)
    ultimo_sucesso = models.DateTimeField(null=True, blank=True)
    proxima_execucao = models.DateTimeField(null=True, blank=True, db_index=True)
    erro = models.CharField(max_length=500, blank=True, default="")
    registros_criados = models.PositiveIntegerField(default=0)
    registros_atualizados = models.PositiveIntegerField(default=0)
    atualizado_em = models.DateTimeField(auto_now=True)

    @property
    def erro_publico(self):
        """Texto para a UI. `erro` guarda a exceção crua (admin/logs), que não
        pode vazar para o usuário; a home monta instâncias não salvas, então
        isto não pode tocar o banco."""
        if self.status == "acao":
            return "Reconecte o portal de afiliados para voltar a sincronizar."
        if self.status == "erro":
            return "Falha temporária na leitura dos relatórios — tentaremos de novo."
        if self.status == "nao_configurado":
            return "Esta loja ainda não tem leitura automática de relatórios."
        return ""

    class Meta:
        unique_together = ("usuario", "marketplace")


class EventoOperacional(models.Model):
    """Log estruturado para depuração de pipelines e suporte."""
    LEVELS = [
        ("debug", "Debug"),
        ("info", "Info"),
        ("warning", "Warning"),
        ("error", "Error"),
    ]
    PIPELINES = [
        ("onboarding", "Onboarding"),
        ("scraper", "Scraper"),
        ("ranking", "Ranking"),
        ("publicacao", "Publicação"),
        ("conexao", "Conexão"),
        ("whatsapp", "WhatsApp"),
        ("telegram", "Telegram"),
        ("relatorios", "Relatórios"),
        ("redirect", "Redirect"),
        ("sistema", "Sistema"),
    ]
    criado_em = models.DateTimeField(auto_now_add=True, db_index=True)
    level = models.CharField(max_length=10, choices=LEVELS, default="info", db_index=True)
    pipeline = models.CharField(max_length=30, choices=PIPELINES, db_index=True)
    evento = models.CharField(max_length=80, db_index=True)
    mensagem = models.CharField(max_length=500)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                null=True, blank=True, related_name="eventos_operacionais")
    contexto = models.JSONField(default=dict, blank=True)
    erro = models.TextField(blank=True, default="")
    # Evita que a leitura do painel reprocese o mesmo log histórico como uma
    # nova ocorrência do incidente.
    incidente_processado = models.BooleanField(default=False, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["pipeline", "level", "criado_em"])]


class IncidenteSaude(models.Model):
    """Problema operacional agregado e seu último diagnóstico confirmado."""
    STATUS = [("aberto", "Aberto"), ("concluido", "Ajuste concluído")]
    chave = models.CharField(max_length=64, unique=True)
    causa = models.CharField(max_length=80, db_index=True)
    pipeline = models.CharField(max_length=30, db_index=True)
    escopo = models.CharField(max_length=255, default="sistema", db_index=True)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                null=True, blank=True, related_name="incidentes_saude")
    level = models.CharField(max_length=10, default="warning")
    status = models.CharField(max_length=12, choices=STATUS, default="aberto", db_index=True)
    ocorrencias = models.PositiveIntegerField(default=1)
    primeira_ocorrencia = models.DateTimeField(default=timezone.now)
    ultima_ocorrencia = models.DateTimeField(default=timezone.now, db_index=True)
    ultima_mensagem = models.CharField(max_length=500, blank=True, default="")
    contexto = models.JSONField(default=dict, blank=True)
    evento_origem = models.ForeignKey(EventoOperacional, on_delete=models.SET_NULL,
                                      null=True, blank=True, related_name="incidentes")
    confirmado_em = models.DateTimeField(null=True, blank=True, db_index=True)
    confirmacao = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        indexes = [models.Index(fields=["status", "ultima_ocorrencia"])]


class LinkAfiliadoUsuario(models.Model):
    """Cache de link de afiliado POR usuário — cada um tem a própria tag/comissão.

    O `Produto.link_afiliado` global não serve mais: o link precisa carregar a tag
    do usuário que envia. Amazon é trivial (monta na hora); ML é caro (Link Builder
    via Playwright), então cacheamos por (usuario, produto).
    """
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="links_afiliado")
    produto = models.ForeignKey(Produto, on_delete=models.CASCADE,
                                related_name="links_usuario")
    url_isca = models.URLField(max_length=1000, blank=True, default="")
    link_afiliado = models.URLField(max_length=1000, blank=True, default="")
    afiliado_ok = models.BooleanField(default=False)
    criado_em = models.DateTimeField(auto_now_add=True)

    # ── Por que este item ainda não tem link ──
    # Sem isto, um produto que nunca afilia fica "pendente" para sempre e não há um
    # único registro do motivo: o gerador contava a falha e seguia (falhas += 1;
    # continue). A linha passa a existir mesmo sem link, carregando a explicação.
    ESTADOS = [
        ("pendente", "Na fila"),
        ("pronto", "Link gerado"),
        # Terminal: a URL não é afiliável pelo Programa (catálogo /up/, perfil,
        # /social/). Retentar não muda o resultado — e retentar para sempre era o
        # que consumia o lote e impedia os outros produtos de avançarem.
        ("nao_afiliavel", "Não afiliável"),
        ("erro", "Falhou"),
    ]
    estado = models.CharField(max_length=20, choices=ESTADOS, default="pendente",
                              db_index=True)
    tentativas = models.PositiveIntegerField(default=0)
    ultimo_erro = models.CharField(max_length=300, blank=True, default="")
    ultima_tentativa = models.DateTimeField(null=True, blank=True)
    # Quando tentar de novo. None + estado terminal = nunca mais.
    proxima_tentativa = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        unique_together = ("usuario", "produto")


class CupomCodigo(models.Model):
    """Cupom de CÓDIGO digitável no checkout (ex: SOUMELIMAIS). Curado manualmente."""
    codigo = models.CharField(max_length=60)
    descricao = models.CharField(max_length=255, blank=True, default="")
    tipo_desconto = models.CharField(max_length=20, default="porcentagem")  # 'porcentagem' | 'fixo'
    valor_desconto = models.FloatField(default=0.0)
    valor_minimo = models.FloatField(default=0.0)
    validade = models.DateField(null=True, blank=True)
    ativo = models.BooleanField(default=True)
    # macro_categorias em que o cupom é válido, separadas por vírgula. Vazio = vale p/ todas.
    # Usado para NÃO sugerir um código que não se aplica ao item (cupons não acumulam).
    categorias = models.CharField(max_length=255, blank=True, default="")

    def aplica_em(self, produto) -> bool:
        """True se este código de checkout é válido para o produto (categoria + mínimo + validade)."""
        from django.utils import timezone
        if not self.ativo:
            return False
        if self.validade and self.validade < timezone.now().date():
            return False
        if self.valor_minimo and produto.preco_com_cupom < self.valor_minimo:
            return False
        cats = [c.strip().lower() for c in self.categorias.split(",") if c.strip()]
        if cats:
            alvo = (produto.macro_categoria or "").strip().lower()
            if alvo not in cats:
                return False
        return True

    def __str__(self):
        return f"{self.codigo} ({self.valor_desconto}{'%' if self.tipo_desconto=='porcentagem' else ' R$'})"


class CanalMonitorado(models.Model):
    """Fonte curada (canal público de ofertas no Telegram) que o worker lê e
    RE-DIVULGA trocando os links pela tag de afiliado do dono (B4). É como
    BlueBot/Pro Afiliados operam: alto volume, baixa manutenção.

    Cuidado (ético/ToS): re-divulgar deals curados de terceiros é área cinzenta.
    Opt-in por usuário; trocar tag de afiliado é padrão no nicho."""
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                              related_name="canais_monitorados")
    # Canal-fonte no Telegram: @username público ou id numérico (-100...).
    handle = models.CharField(max_length=120)
    # Destino da re-divulgação (grupo do próprio usuário).
    destino_canal = models.CharField(max_length=20, default="whatsapp")  # whatsapp | telegram
    destino_grupo_id = models.CharField(max_length=100)
    ativo = models.BooleanField(default=True)
    # Último id de mensagem já processado (evita reprocessar no restart do worker).
    ultimo_id = models.BigIntegerField(default=0)

    def __str__(self):
        return f"{self.handle} → {self.destino_grupo_id} ({self.destino_canal})"


class EnvioCanal(models.Model):
    """Dedup do fluxo de canais curados: não re-divulga a MESMA oferta 2x por usuário.
    Chave = hash da URL-fonte do produto (HistoricoEnvio exige Produto; aqui não há)."""
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                              related_name="envios_canal")
    chave = models.CharField(max_length=64, db_index=True)  # sha1 da url-fonte
    data = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        unique_together = ("owner", "chave")


class ConfiguracaoEnvio(models.Model):
    """Regra de divulgação: qual nicho vai para qual grupo, com que frequência."""
    # Dono da regra (multi-tenant). null = regras legadas, migradas p/ owner default.
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                              null=True, blank=True, db_index=True,
                              related_name="configuracoes")
    macro_categoria = models.CharField(max_length=100, blank=True, default="")
    # Sub-nicho opcional: só envia itens cujo nome casa com algum destes termos
    # (separados por vírgula). Ex: "aspirador robo, robot vacuum, robô aspirador".
    termo_busca = models.CharField(max_length=255, blank=True, default="")
    # Canal de envio: 'whatsapp' (grupo @g.us) | 'telegram' (chat/channel id).
    canal = models.CharField(max_length=20, default="whatsapp")
    # Filtro opcional de marketplace ('' = qualquer). Ex: só 'mercadolivre'.
    marketplace = models.CharField(max_length=20, blank=True, default="")
    programas = models.ManyToManyField(ProgramaAfiliado, blank=True,
                                       related_name="configuracoes")
    incluir_restritos = models.BooleanField(default=True)
    incluir_sem_desconto = models.BooleanField(default=True)
    grupo_id = models.CharField(max_length=100)          # ex '12345@g.us' (WA) ou '@canal'/-100... (TG)
    grupo_nome = models.CharField(max_length=255, blank=True, default="")
    intervalo_minutos = models.PositiveIntegerField(default=60)
    # Janela de envio (hora local 0-23). Só envia dentro de [inicio, fim).
    # Se fim <= inicio, a janela cruza a meia-noite (ex: 20→6).
    janela_inicio = models.PositiveSmallIntegerField(default=8)
    janela_fim = models.PositiveSmallIntegerField(default=20)
    min_desconto_percent = models.FloatField(default=15.0)
    # Anti-repetição do MESMO produto p/ este grupo (não é o ritmo de envio). Oculto na UI.
    horas_cooldown = models.PositiveIntegerField(default=24)
    ativo = models.BooleanField(default=True)
    ultimo_envio = models.DateTimeField(null=True, blank=True)
    # Próximo envio agendado com jitter já aplicado (anti-robótico). None = envia já.
    proximo_envio = models.DateTimeField(null=True, blank=True)
    max_envios_dia = models.PositiveIntegerField(default=20)
    falhas_consecutivas = models.PositiveIntegerField(default=0)
    pausar_apos_falhas = models.PositiveIntegerField(default=5)
    motivo_pausa = models.CharField(max_length=255, blank=True, default="")
    variante_template = models.CharField(max_length=10, default="alternar")
    nome_marca = models.CharField(max_length=80, blank=True, default="")
    tom_marca = models.CharField(max_length=20, blank=True, default="")
    nivel_emoji = models.PositiveSmallIntegerField(null=True, blank=True)
    chamada_acao = models.CharField(max_length=120, blank=True, default="")
    divulgacao_afiliado = models.CharField(max_length=180, blank=True, default="")
    template_a = models.TextField(blank=True, default="")
    template_b = models.TextField(blank=True, default="")

    def dentro_da_janela(self, agora) -> bool:
        """True se a hora local de `agora` está na janela de envio."""
        h = timezone.localtime(agora).hour
        i, f = self.janela_inicio, self.janela_fim
        if i == f:
            return True                      # janela 24h
        if i < f:
            return i <= h < f                # mesma data: 8..20
        return h >= i or h < f               # cruza meia-noite: 20..6

    def agendar_proximo(self, agora):
        """Define proximo_envio = agora + intervalo ± jitter(1-10min). Anti-padrão robótico."""
        import random
        jitter = random.randint(1, 10) * random.choice((-1, 1))
        minutos = max(1, self.intervalo_minutos + jitter)
        self.proximo_envio = agora + timedelta(minutes=minutos)

    def __str__(self):
        return f"{self.macro_categoria} → {self.grupo_nome or self.grupo_id} (a cada {self.intervalo_minutos}min)"
