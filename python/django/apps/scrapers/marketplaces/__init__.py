"""
Marketplaces plugáveis (Mercado Livre, futuramente Amazon, Shopee).

Cada loja implementa `Marketplace` (base.py): como raspar ofertas, como gerar o link
de afiliado, como verificar a tag, e como checar se o anúncio ainda está vivo.
A orquestração (ofertas.py) é agnóstica: resolve a loja por `Produto.marketplace`
via `MARKETPLACES.get(...)`. Adicionar loja = um arquivo + uma linha no registry.
"""
