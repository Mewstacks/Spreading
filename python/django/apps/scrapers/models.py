from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

class Cupom(models.Model):
    campanha_id = models.CharField(max_length=100, unique=True)
    titulo = models.CharField(max_length=255)
    tipo_desconto = models.CharField(max_length=20) # 'fixo' ou 'porcentagem'
    valor_desconto = models.FloatField()
    valor_minimo = models.FloatField(default=0.0)  # compra mínima para o cupom ser válido
    link_original = models.URLField()
    codigo = models.CharField(max_length=512, blank=True, default="")
    data_criacao = models.DateTimeField(auto_now_add=True)

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
    link_produto = models.URLField()
    categoria = models.CharField(max_length=100, null=True, blank=True) # Lembra do domain_id?
    macro_categoria = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    # Cache do link de afiliado pré-gerado (evita abrir Playwright na hora do envio)
    url_isca = models.URLField(max_length=1000, blank=True, default="")
    link_afiliado = models.URLField(max_length=1000, blank=True, default="")
    imagem_url = models.URLField(max_length=1000, blank=True, default="")
    frete_full = models.BooleanField(default=False)
    # Código digitável no checkout, quando o item vem de um cupom de código (ex: CASINHA)
    codigo_checkout = models.CharField(max_length=60, blank=True, default="")
    # True quando o link_afiliado foi verificado e carrega a tag de afiliado (A3).
    # False = link sem atribuição -> não enviar (perda de comissão silenciosa).
    afiliado_ok = models.BooleanField(default=False)
    # Frase de marketing gerada por LLM, cacheada na raspagem (evita bloquear o envio).
    frase_llm = models.CharField(max_length=255, blank=True, default="")

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