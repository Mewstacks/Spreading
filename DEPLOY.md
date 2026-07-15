# Deploy — Fly.io

Two Fly apps in region **gru** (São Paulo):
- **spreading-web** — Django (gunicorn + workers + Playwright). Volume `ml_data` at `/data`.
- **spreading-wa** — Node WhatsApp service. Volume `wa_data` at `/app/.wwebjs_auth`. Private-only.
- **spreading-db** — Fly Postgres, attached to web.

> Fill placeholders in `.env.fly` first. Logged in as `germano@garantiabpo.com` (`fly auth whoami`).
> Rough cost: ~US$15–30/mo (1× web 2GB, 1× wa 1GB, Postgres shared-cpu-1x, 2× small volumes).

Run from repo root `C:\Users\gege\Documents\Spreading`.

## 1. Create apps (free) — ✅ JÁ FEITO (org personal)
```
# já criados nesta sessão:
#   fly apps create spreading-wa  --org personal
#   fly apps create spreading-web --org personal
```

## 2. Volumes (billable, persistent state) — 2 usuários: 1GB basta
```
fly volumes create wa_data --app spreading-wa  --region gru --size 1 -y
fly volumes create ml_data --app spreading-web --region gru --size 1 -y
```

## 3. Postgres (billable) + attach (sets DATABASE_URL on web)
```
fly postgres create --name spreading-db --region gru --initial-cluster-size 1 --vm-size shared-cpu-1x --volume-size 1
fly postgres attach spreading-db --app spreading-web
```

## 4. Secrets
Push all secrets from `.env.fly` (helper does both apps):
```
powershell -ExecutionPolicy Bypass -File deploy\push-secrets.ps1
```
`API_KEY` → spreading-wa. Everything else + `WHATSAPP_API_KEY`(=API_KEY) → spreading-web.
(O login web do ML **não** precisa de secret: roda no Chromium local da imagem.)

## 5. Deploy the WhatsApp service
```
cd node.js
fly deploy --app spreading-wa
cd ..
```

## 6. Deploy the web app
```
cd python
fly deploy --app spreading-web        # release_command roda migrate automático
cd ..
```

## 7. Post-deploy
```
# superusuário (nasce verificado)
fly ssh console --app spreading-web -C "python /app/django/manage.py createsuperuser"

# conectar WhatsApp: abra o painel e escaneie o QR
fly open --app spreading-web           # /scrapers/whatsapp/
# conectar Mercado Livre: /scrapers/ml/ -> "Conectar Mercado Livre" (login web, sem script)
```

### Migrar dados do dev (opcional)
```
# no dev (sqlite):
python django/manage.py dumpdata --natural-primary --natural-foreign \
  -e contenttypes -e auth.permission -e admin.logentry -e sessions > dump.json
# subir e carregar no prod:
fly ssh console --app spreading-web -C "python /app/django/manage.py loaddata /app/dump.json"
```

## Notas / limitações
- **Login ML (web-native)**: o usuário conecta o Mercado Livre pela própria interface (`/scrapers/ml/`). Rodamos um Chromium **local** (o mesmo da imagem/Playwright) e transmitimos a tela pro navegador dele via **CDP screencast** desenhado num `<canvas>`, com mouse/teclado encaminhados de volta (`Input.dispatch*`) — sem script local, sem colar `auth.json`, sem serviço externo nem secret. A senha é digitada direto na página real do ML. Ver `apps/scrapers/ml_conexao.py`.
- **Escala**: 1 máquina web só (volume de sessões preso a ela). Escala-out exige mover sessões p/ storage compartilhado.
- **Telegram/canais (B4)**: opcional — só ativa com `TELEGRAM_API_ID/HASH/SESSION` setados.
- Ver `plan` completo em `.claude/plans/what-is-missing-for-ticklish-prism.md`.
