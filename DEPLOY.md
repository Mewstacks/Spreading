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

## Custo (lido de `fly machine list` em 03/09/2026, tabela da região `gru`)

`gru` tem markup de ~1,55× sobre a tabela base do Fly — o mesmo preset custa bem mais
em São Paulo do que em `ams`. São QUATRO máquinas, não três: `spreading-web` tem dois
process groups (`web` e `worker`) e cada um é uma máquina própria desde o split de
workers. Valores por mês:

| Máquina | Preset | US$/mês |
|---|---|---|
| `spreading-web` / `web` | `shared-cpu-2x` 1GB | 10,32 |
| `spreading-web` / `worker` | `shared-cpu-2x` 2GB | 18,40 |
| `spreading-wa` | `shared-cpu-2x` 4GB | 34,56 |
| `spreading-db` | `shared-cpu-2x` 1GB | 10,32 |
| Volumes: `ml_data` 1GB + `wa_data` 3GB + `pg_data` 3GB | 7GB × US$0,15 | 1,05 |
| **Compute** | | **73,60** |

| Cenário | US$/mês | R$/mês (a R$5,16) |
|---|---|---|
| 24/7 | 74,65 | R$385 — **acima do teto** |
| Com o desligamento noturno abaixo | **53,97** | **R$278** |

O teto do produto é **R$300/mês**, e é o desligamento noturno que o faz caber. Egress
na América do Sul é ~US$0,04/GB e não move a conta neste volume.

Não há homologação: foi destruída em 09/08/2026 para cortar custo.

### Desligamento noturno

A produção é parada diariamente à **01:00** e religada às **07:45**, no horário de
São Paulo, pelo workflow `.github/workflows/fly-nightly-power.yml`, com uma segunda
tentativa de start às **08:20**. As 6h45 paradas cortam 28,1% do custo de máquina —
é a diferença entre R$385 e R$278. O desligamento ocorre na ordem
`web -> WhatsApp -> Postgres`; a inicialização usa a ordem inversa e só avança quando
os health checks da dependência estão passando (`wait_until_healthy`).

Em 16, 17 e 18/08/2026 o RELIGAMENTO falhou e a produção ficou fora do ar o dia
inteiro — à noite não sobra nada de pé, então não há vigia externo para perceber. O
`wait_until_healthy`, a ordem de dependência, o filtro de checks do Postgres e o
segundo start das 08:20 existem por causa disso.

O workflow também aceita execução manual com `start` ou `stop`. Ele usa três
deploy tokens restritos a um único app, armazenados nos secrets do repositório:
`FLY_TOKEN_WEB`, `FLY_TOKEN_WA` e `FLY_TOKEN_DB`. Os tokens atuais expiram em um
ano e devem ser rotacionados antes de agosto de 2027.

**Antes de mexer em `[[vm]]`, saiba o preço, e pergunte.** Trocar os dois serviços
para `performance-2x`/4gb leva a conta para ~US$210/mês. O passo intermediário
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
