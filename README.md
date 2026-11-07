# Spreading v2

Feed de cupons e ofertas do ML → regras → WhatsApp (WAHA/GOWS). Django 6 + Postgres 17.

## Rodar local
1. `.env` (nunca versionado): copie `.env.example` e preencha `DJANGO_SECRET_KEY` (≥50), `POSTGRES_PASSWORD`,
   `WAHA_API_KEY`, `WAHA_DASHBOARD_PASSWORD`, `STATUS_TOKEN`; opcional `ML_API_CLIENT_ID/SECRET`, `CANARY_CHAT_ID`.
2. `docker compose up -d --build` (db, web, worker, waha).
3. Pareamento: dashboard do WAHA em http://localhost:3000/dashboard (usuário `admin`), sessão `default`,
   QR ou código. Se o WhatsApp pedir passkey, use a extensão "WhatsApp Browser Extension" num desktop.
4. Dados do v1: `tools/export_v1.py` (somente leitura) → `python manage.py import_v1 arquivo.json`.
5. Links de ofertas novas: `python manage.py pending_links --org lules` → cole no Link Builder do ML →
   `python manage.py import_links --org lules arquivo` (linhas `url_do_produto link_afiliado`).
6. Operação: `run_cycle` (o worker já repete a cada 5 min), `canary` (precisa de `CANARY_CHAT_ID`),
   `GET /status` com o header `X-Status-Token`.

## Verificação
`python tools/verify.py` (ruff + pytest). Testes rodam em SQLite; com `DATABASE_URL` rodam em Postgres.
