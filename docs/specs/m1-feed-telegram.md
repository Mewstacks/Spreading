# M1 — feed de cupons do ML → aviso → WhatsApp (WAHA)

Decisões do dono: sem aviso de afiliação nem enfeite que o v1 não tinha; sem Telegram por ora; só WhatsApp.

## Problema
Publicar cupons do Mercado Livre em grupos de WhatsApp sem Chromium, sem scraping do ML e sem duplicar
mensagem.

## Critérios de aceitação (testáveis)
1. O parser lê o array `COUPONS` do `index.html` do feed e devolve cupons normalizados (código, %,
   mínimo, teto, validade, container). Cupom vencido, não iniciado ou sem código é descartado.
2. Um cupom só é **publicável** se: ativo hoje (America/Sao_Paulo), visto no feed há ≤ 1 h e com link de
   afiliado do dono para o seu container. Sem link do dono → não envia (falha fechada).
3. O aviso agrupa **até 3 códigos** do mesmo container/link, em texto simples do WhatsApp (`*negrito*`),
   só com informação do feed.
4. Outbox: chave de idempotência única; o mesmo conjunto de códigos nunca é enfileirado duas vezes para o
   mesmo destino; um código já entregue não volta; no máximo 1 item pendente por destino; respeita
   `min_interval_seconds`, `daily_cap` e `paused_until` do destino.
5. Entrega via WAHA (`POST /api/sendText`): sucesso → `confirmado` com o id da mensagem; falha
   transitória (5xx/rede) → nova tentativa com backoff (até 5); falha permanente (4xx) → `falhou`;
   timeout → `incerto` e **nunca reenvia sozinho**.
6. Isolamento: consulta de modelo por organização sem informar a organização levanta erro.
7. `python tools/verify.py` (ruff + pytest) passa.

## Fora de escopo
Telegram, ofertas e catálogo (M4), painel (M5), pareamento no painel (usa o dashboard do WAHA).
