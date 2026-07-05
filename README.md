# Variáveis de Ambiente (.env)

Crie um arquivo `.env` na pasta `node.js/` com as variáveis abaixo.

```dotenv
# Senha para autenticar requisições na sua API (você define, coloque algo forte)
API_KEY=sk_live_sua_chave_aqui

# Porta do servidor (Railway define automaticamente, só coloque se rodar local)
# PORT=3000

# Endereço da Evolution API
# - Rodando local com Docker:  http://localhost:8080
# - Rodando no Railway:        URL interna fornecida pelo Railway (ex: http://evolution-api.railway.internal:8080)
EVOLUTION_API_URL=http://localhost:8080

# Senha da Evolution API (você define, use a mesma ao criar o container)
EVOLUTION_API_KEY=sua_senha_evolution

# Nome da instância criada na Evolution (você define ao criar via Swagger em /instance/create)
EVOLUTION_INSTANCE=principal
```

## Conexão do Mercado Livre (login web, sem script local)

O login do Mercado Livre acontece dentro do próprio app: abrimos um Chromium num
serviço de browser hospedado (Browserbase) e transmitimos a tela pro navegador do
usuário (live view). Ele loga no ML ali — no celular ou desktop — e a sessão é
salva sozinha. Não precisa mais rodar script nenhum nem colar `auth.json`.

Adicione ao `.env` do Django (mesmo `.env` que o `core/settings.py` carrega):

```dotenv
# Crie a conta em https://browserbase.com e pegue a chave + o Project ID
BROWSERBASE_API_KEY=bb_live_sua_chave_aqui
BROWSERBASE_PROJECT_ID=seu_project_id

# País do proxy residencial usado no login do ML (default BR)
# BROWSERBASE_PROXY_COUNTRY=BR

# Em produção (gunicorn com +1 worker) o estado da conexão precisa ir pro Redis,
# senão o polling não enxerga o login. Aponte pro mesmo Redis do Celery:
# REDIS_URL=redis://...:6379/1      (ou USE_REDIS_CACHE=1 pra reusar o broker)
```

Sem essas chaves a tela `/scrapers/ml/` avisa o usuário que a conexão está
indisponível, em vez de quebrar.
