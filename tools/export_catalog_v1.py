"""Exporta o catálogo compartilhado do v1 observado nos últimos 30 dias (somente leitura).

Rodar dentro do v1 (mesmo esquema de tools/export_v1.py). Linha `@@V1CAT@@<json>`.
"""
import json
from datetime import timedelta

from apps.accounts.tenant import organization_context
from apps.scrapers.models import Produto
from django.utils import timezone

OID = "fe78ccee-3766-49ea-af72-a0e3ec3bf434"
since = timezone.now() - timedelta(days=30)
rows = []
with organization_context(OID):
    qs = Produto.objects.filter(ultima_observacao__gte=since).exclude(imagem_url="")
    for p in qs.iterator(chunk_size=2000):
        rows.append({
            "marketplace": p.marketplace, "nome": p.nome, "url": p.link_produto,
            "imagem": p.imagem_url, "de": p.preco_sem_desconto, "efetivo": p.preco_efetivo,
            "fonte_preco": p.preco_fonte, "categoria": p.categoria, "macro": p.macro_categoria,
            "asin": p.asin, "relampago": bool(p.relampago), "frete_full": bool(p.frete_full),
            "visto": p.ultima_observacao.isoformat(),
        })
print("@@V1CAT@@" + json.dumps(rows, default=str))
