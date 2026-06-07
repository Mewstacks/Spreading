from django.db import models

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

class HistoricoEnvio(models.Model):
    produto = models.ForeignKey(Produto, on_delete=models.CASCADE)
    data_envio = models.DateTimeField(auto_now_add=True, db_index=True)

    def __str__(self):
        return f"{self.produto.nome} enviado em {self.data_envio}"


class CupomCodigo(models.Model):
    """Cupom de CÓDIGO digitável no checkout (ex: SOUMELIMAIS). Curado manualmente."""
    codigo = models.CharField(max_length=60)
    descricao = models.CharField(max_length=255, blank=True, default="")
    tipo_desconto = models.CharField(max_length=20, default="porcentagem")  # 'porcentagem' | 'fixo'
    valor_desconto = models.FloatField(default=0.0)
    valor_minimo = models.FloatField(default=0.0)
    validade = models.DateField(null=True, blank=True)
    ativo = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.codigo} ({self.valor_desconto}{'%' if self.tipo_desconto=='porcentagem' else ' R$'})"


class ConfiguracaoEnvio(models.Model):
    """Regra de divulgação: qual nicho vai para qual grupo, com que frequência."""
    macro_categoria = models.CharField(max_length=100)
    grupo_id = models.CharField(max_length=100)          # ex '12345@g.us'
    grupo_nome = models.CharField(max_length=255, blank=True, default="")
    intervalo_minutos = models.PositiveIntegerField(default=60)
    min_desconto_percent = models.FloatField(default=15.0)
    horas_cooldown = models.PositiveIntegerField(default=24)
    ativo = models.BooleanField(default=True)
    ultimo_envio = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.macro_categoria} → {self.grupo_nome or self.grupo_id} (a cada {self.intervalo_minutos}min)"