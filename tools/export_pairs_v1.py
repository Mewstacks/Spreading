"""Exporta pares cupom+produto confirmados e ainda válidos do v1 (somente leitura).

Linha `@@V1PAIRS@@<json>`. Mesmo esquema de execução de tools/export_v1.py.
"""
import json

from apps.accounts.tenant import organization_context
from apps.scrapers.models import LinkAfiliadoProdutoCupomUsuario, ProdutoCupom
from django.utils import timezone

OID = "fe78ccee-3766-49ea-af72-a0e3ec3bf434"
now = timezone.now()
rows = []
with organization_context(OID):
    links = {
        lk.relacao_id: (lk.url_canonica or lk.link_afiliado)
        for lk in LinkAfiliadoProdutoCupomUsuario.objects.filter(
            organization_id=OID, estado="pronto"
        ).exclude(verificado_ok=False)
    }
    qs = (
        ProdutoCupom.objects.filter(status="confirmado", cupom__validade__gte=now)
        .select_related("produto", "cupom")
    )
    for pc in qs.iterator(chunk_size=500):
        c, p = pc.cupom, pc.produto
        rows.append({
            "marketplace": p.marketplace, "nome": p.nome, "url": p.link_produto,
            "imagem": p.imagem_url, "macro": p.macro_categoria,
            "preco_original": pc.preco_original, "preco_atual": pc.preco_atual,
            "preco_final": pc.preco_final, "verificado_em": pc.verificado_em,
            "codigo": c.codigo, "titulo": c.titulo, "validade": c.validade,
            "redemption_mode": c.redemption_mode, "scope_type": c.scope_type,
            "regras": c.regras, "link_cupom": c.link, "relampago": bool(c.relampago),
            "restrito": bool(c.restrito), "link_afiliado": links.get(pc.id, ""),
        })
print("@@V1PAIRS@@" + json.dumps(rows, default=str))
