# Deploy no Fly.io

O Spreading tem dois serviços:

- **`python/`** — o app Django (painel, scraping, geração de links, envios). É o principal.
- **`node.js/`** — a ponte de WhatsApp (Evolution API). Opcional; só se for usar WhatsApp.

Tudo é web e degrada com elegância: sem `DATABASE_URL` usa SQLite; sem `REDIS_URL` o
cache é local; serviços não conectados aparecem como "não conectado" na interface, não
quebram. Você pode subir só o web e ir ligando o resto depois.

---

## 1. App Django — deploy mínimo (sobe em minutos)

Pré-requisitos: [flyctl](https://fly.io/docs/flyctl/install/) instalado e `fly auth login`.

```bash
cd python

# Cria o app (ajuste o nome; precisa ser único no Fly)
fly apps create spreading

# Segredos obrigatórios
fly secrets set DJANGO_SECRET_KEY="$(python -c 'import secrets;print(secrets.token_urlsafe(64))')"
# Conexão web do Mercado Livre (login sem script local) — ver README.md
fly secrets set BROWSERBASE_API_KEY="bb_live_..." BROWSERBASE_PROJECT_ID="..."

# Volume para o SQLite (fallback sem Postgres)
fly volumes create spreading_data --region gru --size 1

fly deploy
```

Isso sobe o **processo web** com SQLite no volume `/data`. As migrações rodam sozinhas
no start (`entrypoint.sh`). Crie o admin:

```bash
fly ssh console -C "python manage.py createsuperuser"
```

Pronto — o painel funciona: dá pra conectar ML/WhatsApp/Telegram, raspar e enviar na mão.
Falta só a **automação 24/7** (envio agendado), que precisa de Postgres + Redis.

---

## 2. Ligar a automação 24/7 (Postgres + Redis + worker/beat)

O agendamento roda no Celery (worker + beat), que precisa de um banco compartilhado
entre as máquinas (Postgres) e um broker (Redis).

```bash
cd python

# Postgres gerenciado (Fly) — anexa e injeta DATABASE_URL automaticamente
fly postgres create --name spreading-db --region gru
fly postgres attach spreading-db

# Redis (Upstash via Fly) — copie a URL que ele imprime para REDIS_URL
fly redis create
fly secrets set REDIS_URL="redis://default:...@fly-....upstash.io:6379"
```

Depois, no `fly.toml`, **descomente o bloco `[processes]`** (web/worker/beat) e remova o
bloco `[mounts]` (não precisa mais de SQLite). Então:

```bash
fly deploy
fly scale count web=1 worker=1 beat=1 --region gru
```

Com Postgres ativo, as migrações do release passam a rodar no `web` no start (idempotente).

---

## 3. Variáveis de ambiente (fly secrets set)

| Var | Obrigatória | Para quê |
|-----|-------------|----------|
| `DJANGO_SECRET_KEY` | ✅ | Segurança do Django (DEBUG=0 exige) |
| `BROWSERBASE_API_KEY` / `BROWSERBASE_PROJECT_ID` | ✅ p/ login ML | Browser remoto do login do Mercado Livre |
| `DATABASE_URL` | p/ automação | Postgres (sem ela: SQLite no volume) |
| `REDIS_URL` | p/ automação | Celery broker/backend + cache entre workers |
| `EMAIL_HOST` / `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` | p/ e-mails | Verificação de conta, alertas |
| `WHATSAPP_API_URL` / `WHATSAPP_API_KEY` | p/ WhatsApp | Aponta pro serviço `node.js` |
| `TELEGRAM_BOT_TOKEN` | opcional | Fallback global (cada usuário conecta o próprio bot pela web) |
| `AFILIADO_TAG` / `AMAZON_PARTNER_TAG` | opcional | Fallback global das tags de afiliado |

`DJANGO_DEBUG=0`, `FLY_APP_NAME` e o hostname `*.fly.dev` já são tratados no `settings.py`.

---

## 4. App Node (WhatsApp) — opcional

```bash
cd node.js
fly launch --no-deploy      # já existe Dockerfile; confirme o nome do app
fly secrets set API_KEY="..." EVOLUTION_API_URL="..." EVOLUTION_API_KEY="..." EVOLUTION_INSTANCE="principal"
fly deploy
```

Depois aponte o Django pra ele: `fly secrets set WHATSAPP_API_URL="https://SEU-APP-node.fly.dev" -a spreading`.

---

## Notas

- **Playwright/Chromium** vai dentro da imagem (o scraping e o Link Builder rodam local).
  Por isso a VM tem 1GB de RAM — não reduza abaixo disso.
- **Static** (admin/etc) é servido pelo WhiteNoise, empacotado no build (`collectstatic`).
- **Sessões do ML** (`auth_{id}.json`) ficam no filesystem do container. Em produção séria,
  migrar isso para o volume/`/data` ou storage externo é o próximo passo (single-machine por ora).
