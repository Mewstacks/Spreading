from django.db import models

class Cupom(models.Model):
    campanha_id = models.CharField(max_length=100, unique=True)
    titulo = models.CharField(max_length=255)
    tipo_desconto = models.CharField(max_length=20) # 'fixo' ou 'porcentagem'
    valor_desconto = models.FloatField()
    link_original = models.URLField()
    data_criacao = models.DateTimeField(auto_now_add=True)

class Produto(models.Model):
    campanha_id = models.CharField(max_length=100, db_index=True)
    nome = models.CharField(max_length=255)
    preco_sem_desconto = models.FloatField()
    preco_com_cupom = models.FloatField()
    link_produto = models.URLField()
    categoria = models.CharField(max_length=100, null=True, blank=True) # Lembra do domain_id?
    macro_categoria = models.CharField(max_length=100, null=True, blank=True, db_index=True)