# Deploy — Fly.io

> O procedimento atual de segurança é
> [`deploy/PHASE0_RUNBOOK.md`](deploy/PHASE0_RUNBOOK.md). As instruções antigas
> abaixo são apenas histórico de bootstrap e não devem ser usadas para produção:
> elas ainda mencionam a chave mestre removida e não contemplam roles separadas,
> RLS, criptografia das sessões ML ou rollout expand/contract.

Two Fly apps in region **gru** (São Paulo):
- **spreading-web** — Django (gunicorn + workers + Playwright). Volume `ml_data` at `/data`.
- **spreading-wa** — Node WhatsApp service. Volume `wa_data` at `/app/.wwebjs_auth`. Private-only.
- **spreading-db** — Fly Postgres, attached to web.

> Fill placeholders in `.env.fly` first. Logged in as `pedro@mewstack.com.br`
> (`fly auth whoami`), org `germano-argenta`.

## Custo (medido em 09/08/2026, tabela da região `gru`)

`gru` tem markup de ~1,55× sobre a tabela base do Fly — o mesmo preset custa bem mais
em São Paulo do que em `ams`. Valores por mês:

| Item | US$/mês |
|---|---|
| `spreading-web` — `shared-cpu-2x` 2GB | 18,40 |
| `spreading-wa` — `shared-cpu-2x` 4GB | 34,56 |
| `spreading-db` — `shared-cpu-1x` 1GB | 9,20 |
| Volumes (7GB × US$0,15) | 1,05 |
| **Total** | **~63,21** + egress (~US$0,04/GB na América do Sul) |

Não há homologação: foi destruída em 09/08/2026 para cortar custo.

**Antes de mexer em `[[vm]]`, saiba o preço.** Trocar os dois serviços para
`performance-2x`/4gb leva a conta de ~US$63 para ~US$210/mês. O passo intermediário
barato é `shared-cpu-4x` (+US$2,24/mês por VM), que dobra vCPU e cota de baseline.
Há ainda reservation blocks do Fly: 40% de desconto em compute, com pagamento anual
adiantado (levaria o compute de ~US$62 para ~US$40/mês).

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

### Piloto Awin

A integração não usa credencial global: cada afiliado cola seu próprio token na tela
**Conta**. O piloto está ativo com `AWIN_INTEGRATION_ENABLED = "1"` em
`python/fly.toml`. Conecte inicialmente uma conta com dois anunciantes e mantenha
`SECRETS_FERNET_KEY` configurada, pois ela criptografa os tokens no banco.

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
