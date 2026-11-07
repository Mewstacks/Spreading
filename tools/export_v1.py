"""Exporta dados NÃO secretos do v1 (somente leitura). Rodar dentro do v1:

    base64 -w0 tools/export_v1.py | fly ssh console -a spreading-web -C \
        "sh -c 'echo <b64> | base64 -d | python django/manage.py shell'"

Usa os contextos de tenant normais do app (sem a role privilegiada). Imprime uma linha
`@@V1JSON@@<json>`; nada de credencial, cookie ou token sai daqui.
"""
import json

from apps.accounts.models import Organization
from apps.accounts.tenant import actor_context, organization_context
from apps.scrapers.models import ConfiguracaoEnvio, LinkAfiliadoCupomUsuario, LinkAfiliadoUsuario

OWNER_USER_ID = 4  # lules


def rule_row(r):
    return {
        "id": r.id, "tipo": r.tipo, "canal": r.canal, "grupo_id": r.grupo_id,
        "grupo_nome": r.grupo_nome, "termo_busca": r.termo_busca,
        "termos_negativos": r.termos_negativos, "preco_min": r.preco_min, "preco_max": r.preco_max,
        "marketplace": r.marketplace, "min_desconto_percent": r.min_desconto_percent,
        "janela_inicio": r.janela_inicio, "janela_fim": r.janela_fim, "dias_semana": r.dias_semana,
        "intervalo_minutos": r.intervalo_minutos, "max_envios_dia": r.max_envios_dia,
        "horas_cooldown": r.horas_cooldown, "ativo": r.ativo,
    }


out = {"orgs": []}
with actor_context(OWNER_USER_ID):
    orgs = list(Organization.objects.values_list("id", "name"))
for oid, name in orgs:
    with organization_context(oid):
        rules = [rule_row(r) for r in ConfiguracaoEnvio.objects.filter(organization_id=oid)]
        containers = [
            {"container_url": c.url_origem, "affiliate_url": c.link_afiliado}
            for c in LinkAfiliadoCupomUsuario.objects.filter(organization_id=oid, estado="pronto")
            .exclude(verificado_ok=False)
        ]
        # No ML o v1 publica o meli.la do Link Builder (`link_afiliado`); `url_canonica` é a
        # expansão /social/ (vitrine) e nunca vai no post. `verificado` = destino conferido.
        products = [
            {"url": lk.produto.link_produto, "affiliate_url": lk.link_afiliado,
             "verificado": lk.verificado_ok, "marketplace": lk.produto.marketplace}
            for lk in LinkAfiliadoUsuario.objects.filter(organization_id=oid, afiliado_ok=True)
            .exclude(verificado_ok=False).select_related("produto").iterator(chunk_size=2000)
            if lk.produto.marketplace == "mercadolivre" and lk.link_afiliado
        ]
    out["orgs"].append({"id": str(oid), "name": name, "rules": rules,
                        "container_links": containers, "product_links": products})
print("@@V1JSON@@" + json.dumps(out, default=str))
