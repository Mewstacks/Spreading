# Ambiente de homologação (staging)

Objetivo: ter onde testar o Spreading inteiro — raspagem, cupons, links, WhatsApp,
login ML — **sem tocar em produção**. Duas etapas: (1) fechar o staging no Fly, que
já existe pela metade; (2) mais adiante, um ambiente local em Docker.

---

## Parte 0 — Situação atual (verificado em 2026-07-27)

O staging **já existe** e está no ar. O que já está pronto:

| Recurso | Estado |
|---|---|
| `spreading-web-staging` | deployed, healthz **200**, 1 máquina em `gru` |
| `spreading-wa-staging` | deployed, health check passando |
| `spreading-staging-db` | Postgres próprio, deployed |
| Volumes `ml_data_staging` / `wa_data_staging` | 1GB cada, criados |
| `python/fly.staging.toml` / `node.js/fly.staging.toml` | versionados |
| Secrets de banco/cripto | `DATABASE_URL`, `MIGRATION_DATABASE_URL`, `SYSTEM_DATABASE_URL`, `DJANGO_SECRET_KEY`, `SECRETS_FERNET_KEY`, `ML_SESSION_*`, `TENANT_CONTEXT_SIGNING_KEY`, `WA_CAPABILITY_*` — todos próprios, **nenhum compartilhado com prod** |
| RLS / roles do Postgres | funcionando (o `/healthz` valida que o usuário do banco não é superuser nem `BYPASSRLS`, e retorna 200) |

O isolamento está certo: banco, volume, chave Fernet e keyring ML são separados.
A chave Ed25519 do WhatsApp usa o mesmo `kid` (`wa-ed25519-v1`) que produção, mas
o **par de chaves é diferente** — um token emitido pelo staging não é aceito pelo
`spreading-wa` de produção (a assinatura não confere). Está seguro; só evite copiar
`WA_CAPABILITY_PUBLIC_KEYS_JSON_B64` de um app para o outro.

### O que falta (por isso o staging ainda não serve para testar)

1. **Não existe conta para logar.** `DJANGO_SUPERUSER_USERNAME/PASSWORD/EMAIL` não
   estão setados. O `release_command` roda `bootstrap_superuser`, mas sem esses
   secrets ele é no-op silencioso — nenhum usuário foi criado. Este é o bloqueador nº 1.
2. **`APP_ENV` não está definido.** Em `settings.py:38` o default é
   `"production" if FLY_APP_NAME else "development"` — ou seja, o staging hoje se
   identifica como **produção** (Sentry, logs e qualquer decisão por ambiente).
3. **As 4 flags de automação estão OFF.** `_AUTOMATION_DEFAULT` é `"0"` em
   staging/produção (`settings.py:66`). Sem `ML_BROWSER_LOGIN_ENABLED`,
   `ML_LINK_BUILDER_ENABLED`, `ML_BROWSER_REPORTS_ENABLED` e `WHATSAPP_WEB_ENABLED`,
   o QR do WhatsApp, o login do ML e o Link Builder morrem antes de qualquer HTTP —
   sem log nenhum. Produção tem as 4; staging não tem nenhuma.
4. **`WA_WEB_VERSION` ausente no `spreading-wa-staging`.** Sem o pin do bundle, o
   WhatsApp conecta mas falha ao enviar com o erro minificado `"r"`.
5. **`ANTHROPIC_API_KEY` ausente** → sem o resuminho de IA nas promoções.
6. **Deploy defasado.** Staging está na build de 26/07 18:21, três commits atrás
   (`95f7645`, `1d288b4`, `c24ee06` — justamente os fixes de QR, cupons e conexão ML).
7. **`fly.staging.toml` mais frouxo que produção**: sem `[[restart]] policy = "always"`,
   sem healthcheck HTTP explícito, e o toml do WhatsApp não tem `GROUP_SYNC_TIMEOUT_MS`
   nem `QR_IDLE_DESTROY_MS`.

---

## Parte 1 — Fechar o staging no Fly

> Confira antes: `fly auth whoami` deve dizer `pedro@mewstack.com.br` (org `germano-argenta`).
> Todos os comandos rodam da raiz do repo.

### 1.1 — Alinhar os `fly.staging.toml`

Editar `python/fly.staging.toml`, no bloco `[env]`:

```toml
  APP_ENV = "staging"
  SENTRY_ENV = "staging"
  AWIN_INTEGRATION_ENABLED = "1"
  ML_BROWSER_LOGIN_ENABLED = "1"
  ML_BROWSER_REPORTS_ENABLED = "1"
  ML_LINK_BUILDER_ENABLED = "1"
  WHATSAPP_WEB_ENABLED = "1"
```

As flags vão em `[env]` (versionado e visível), não em secrets: o valor `"1"` não é
segredo, e em produção elas viraram secret só por acidente histórico. Deixe
`PILOT_ORGANIZATION_IDS` **vazio** no staging — lista vazia significa "vale para todas
as organizações" (`feature_flags.py:9`), que é o que se quer num sandbox.

Ainda no mesmo arquivo, adicione o que falta em relação a produção:

```toml
[[restart]]
  policy = "always"
```

e o healthcheck HTTP dentro de `[http_service]`:

```toml
  [[http_service.checks]]
    interval = "15s"
    timeout = "5s"
    grace_period = "30s"
    method = "GET"
    path = "/healthz"
    headers = { Host = "spreading-web-staging.fly.dev", X-Forwarded-Proto = "https" }
```

Em `node.js/fly.staging.toml`, acrescentar `[[restart]] policy = "always"` e as duas
variáveis que produção tem e o staging não:

```toml
  GROUP_SYNC_TIMEOUT_MS = "45000"
  QR_IDLE_DESTROY_MS = "180000"
```

### 1.2 — Secrets que faltam

```bash
fly secrets set --app spreading-web-staging \
  DJANGO_SUPERUSER_USERNAME=admin \
  DJANGO_SUPERUSER_EMAIL=gasparin.machado@gmail.com \
  DJANGO_SUPERUSER_PASSWORD='<senha-forte-EXCLUSIVA-do-staging>'
```

Use uma senha diferente da de produção. O `bootstrap_superuser` é idempotente e roda
a cada deploy: o secret é a fonte da verdade da senha, então trocar o secret e
redeployar já sincroniza a conta.

A `ANTHROPIC_API_KEY` pode ser a mesma de produção (é uma chave de terceiro, não um
segredo do tenant) — ou uma chave separada se quiser isolar o consumo:

```bash
fly secrets set --app spreading-web-staging ANTHROPIC_API_KEY='<sua-chave>'
```

Pin do bundle do WhatsApp — copie o valor exato que está em produção:

```bash
fly ssh console --app spreading-wa -C 'printenv WA_WEB_VERSION'
```

```bash
fly secrets set --app spreading-wa-staging WA_WEB_VERSION='<valor-obtido-acima>'
```

### 1.3 — Deploy dos dois apps

WhatsApp primeiro (o web depende dele pela rede `.internal`):

```bash
cd node.js && fly deploy -c fly.staging.toml --app spreading-wa-staging && cd ..
```

```bash
cd python && fly deploy -c fly.staging.toml --app spreading-web-staging && cd ..
```

O `release_command` do staging roda `migrate --noinput && bootstrap_superuser`. Se
quiser o release fail-closed completo (com `check --deploy`, auditoria de tenants,
constraints e RLS), existe o comando `phase0_release` — mas rode **manualmente** por
enquanto, porque o `check --deploy` acusa erro quando as flags de automação estão
ligadas com `PILOT_ORGANIZATION_IDS` vazio (`checks.py:57`, `accounts.E010`):

```bash
fly ssh console --app spreading-web-staging -C "python /app/django/manage.py tenant_rls --status"
```

### 1.4 — Verificar que subiu

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://spreading-web-staging.fly.dev/healthz
```

200 significa processo vivo **e** banco utilizável com RLS ativo. Depois, entrar em
`https://spreading-web-staging.fly.dev` com `admin` e a senha do secret. O superusuário
nasce com e-mail verificado e ganha uma Organization pessoal automaticamente (signal
`criar_perfil`, `models.py:328`), então já cai direto no painel.

### 1.5 — Contas de teste adicionais

`SECURITY_FREEZE_NEW_TENANTS` nasce ligado em staging (`settings.py:62`) e bloqueia a
criação de contas pelo `/scrapers/painel-admin/` (`views_admin.py:108`). **Não desligue**
— o check `accounts.E002` trata isso como erro de implantação. Crie os usuários de
teste pelo shell, que não passa por esse gate:

```bash
fly ssh console --app spreading-web-staging -C "python /app/django/manage.py shell -c \"from django.contrib.auth import get_user_model; U=get_user_model(); u=U.objects.create_user('teste1','teste1@exemplo.com','<senha>'); p=u.perfil; p.email_verificado=True; p.save()\""
```

### 1.6 — Conectar as contas externas (só de teste)

Nada de credencial de produção aqui — vale para ML, Amazon e WhatsApp:

1. **Mercado Livre**: `/scrapers/ml/` → "Conectar Mercado Livre". O login roda no
   Chromium da própria imagem via live view; use uma conta ML de teste. Nunca copie
   `auth.json`, cookie ou sessão de produção.
2. **WhatsApp**: `/scrapers/whatsapp/` → escaneie o QR com um **número dedicado ao
   staging**. Um mesmo número em duas sessões web ao mesmo tempo derruba as duas
   (o problema de `takeoverOnConflict`). O toml de staging já limita
   `MAX_WHATSAPP_SESSIONS = 1`.
3. **Grupo de destino**: crie um grupo só seu, com você e talvez um segundo número.
   Nunca aponte o staging para um grupo real de clientes.

### 1.7 — Ciclo de validação

Só considere o staging saudável depois de um ciclo inteiro:

- raspagem normal e a rápida (`scrape`, `scrapeflash`)
- cupons (a raspagem via HTTP autenticado, ~43s — não deve cair para o Chromium)
- geração de link de afiliado (`links`) — a tela de Promoções deve sair de "pendente"
- `relatorios` e `monitor` sem erro nos logs
- **envio** para o grupo de teste — é aqui que aparece o erro `"r"` se o
  `WA_WEB_VERSION` estiver errado

```bash
fly logs --app spreading-web-staging
```

### 1.8 — Custo

O `fly.staging.toml` do web pede `performance-2x` + 4GB, igual à produção. Isso dobra
a conta para uma máquina que fica ociosa a maior parte do tempo. Duas saídas:

- **Recomendado — desligar quando não estiver testando:**

  ```bash
  fly scale count 0 --app spreading-web-staging --yes
  ```

  e `fly scale count 1` para voltar. O volume e o banco sobrevivem; só a máquina para.
  Faça o mesmo com `spreading-wa-staging`.
- Não troque para `shared-cpu-*` no web: o Chromium do login ML e da raspagem queima a
  cota de baseline e derruba o painel inteiro junto — foi por isso que produção está em
  CPU dedicada. Para testes curtos vale mais desligar do que encolher.

---

## Parte 2 — Como usar no dia a dia

O repositório é commit direto no `main`, então o staging não é um "branch de
homologação" — é um **destino de deploy**. O fluxo fica:

1. Faça a alteração localmente.
2. Suba o staging **a partir do working tree**, sem commitar ainda:
   `fly deploy -c fly.staging.toml --app spreading-web-staging`
   (o `fly deploy` empacota os arquivos locais, não o commit).
3. Teste no `spreading-web-staging.fly.dev`.
4. Deu certo → commit e push no `main`, depois `fly deploy` em produção.

Regras que valem sempre:

- Alteração de schema (migration) **nasce no staging**. O `MIGRATION_DATABASE_URL`
  próprio existe justamente para isso.
- Nunca aponte o `WHATSAPP_API_URL` do staging para `spreading-wa.internal`, nem o
  contrário. Hoje está certo em ambos os tomls; é o erro mais fácil de cometer.
- Ao criar um secret novo em produção, crie o equivalente no staging no mesmo dia —
  o descompasso de secrets é a razão de o staging atual estar meio quebrado.

---

## Parte 3 — Ambiente local em Docker (para daqui uns dias)

O staging no Fly cobre a homologação de verdade (mesmo Postgres, mesmo Chromium, mesma
rede privada). O local serve para iterar rápido, sem esperar deploy. A prática comum é
ter os dois: local para desenvolver, staging para homologar.

### O que muda no local

`APP_ENV=development` destrava vários caminhos: SQLite é permitido, as flags de
automação nascem **ligadas** (`_AUTOMATION_DEFAULT = "1"`), a chave Ed25519 do WhatsApp
é gerada em memória se ausente (`wa_capabilities.py:23`) e o keyring ML aceita uma
chave `dev`. Ou seja: local exige muito menos configuração que o Fly.

### Opção A — sem Docker (mais rápida, já documentada)

É o que o `README.md` descreve: dois terminais, `npm start` no `node.js/` e
`python manage.py runserver` no `python/django/`. Boa para mexer em template e view.
Limitação: roda em SQLite, então **não exercita RLS nem as políticas multi-tenant** —
o bloco de RLS só existe no PostgreSQL.

### Opção B — Docker Compose com Postgres (recomendada para homologar de verdade)

Já existe um `node.js/docker-compose.yml`, mas ele cobre só o worker de WhatsApp,
Redis e a Evolution API legada. Falta o Django e o Postgres. O caminho é criar um
`docker-compose.yml` **na raiz** com quatro serviços:

1. `db` — `postgres:16`, volume próprio, porta 5432.
2. `web` — build de `python/Dockerfile`, com `APP_ENV=development`,
   `DATABASE_URL=postgres://...@db:5432/spreading`, `WHATSAPP_API_URL=http://wa:3000`,
   e o volume `./python:/app` montado para hot reload.
3. `wa` — build de `node.js/Dockerfile`, volume nomeado em `/app/.wwebjs_auth` para a
   sessão sobreviver a restart.
4. (opcional) `redis`, só se for testar algo que dependa dele.

Três detalhes que costumam morder:

- **Chromium/Playwright**: o `python/Dockerfile` já instala o Chromium. Se você montar
  `./python:/app` por cima, tome cuidado para não sobrescrever o diretório de browsers
  do Playwright — ele fica fora de `/app`, então normalmente está seguro.
- **RLS**: com Postgres de verdade, você precisa criar as roles `spreading_migration` e
  a role de sistema antes de rodar `tenant_rls --enable`. O comando falha explicitamente
  se as roles não existirem (`tenant_rls.py:53`). Como `APP_ENV=development` não exige
  RLS, dá para começar sem — e ligar quando quiser testar isolamento entre tenants.
- **Memória**: cada WhatsApp conectado é um Chromium de ~350MB, mais o Chromium do
  login ML. Reserve pelo menos 4GB para o Docker Desktop.

O passo a passo detalhado (com o compose escrito) faz sentido montar quando você for
de fato fazer isso — o desenho acima é a decisão de arquitetura; o resto é digitação.

---

## Checklist rápido

- [ ] `APP_ENV`, `SENTRY_ENV` e as 4 flags no `python/fly.staging.toml`
- [ ] `[[restart]]` + healthcheck HTTP nos dois tomls de staging
- [ ] `DJANGO_SUPERUSER_*` no `spreading-web-staging`
- [ ] `ANTHROPIC_API_KEY` no `spreading-web-staging`
- [ ] `WA_WEB_VERSION` no `spreading-wa-staging`
- [ ] deploy dos dois apps com `-c fly.staging.toml`
- [ ] `/healthz` = 200 e login no painel funcionando
- [ ] número de WhatsApp dedicado + grupo de teste
- [ ] ciclo completo verde: scrape → cupons → links → relatórios → envio
- [ ] `fly scale count 0` quando terminar de usar
