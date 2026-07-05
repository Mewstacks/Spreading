# Variáveis de Ambiente (.env)

Crie um arquivo `.env` na pasta `node.js/` com as variáveis abaixo.

```dotenv
# Senha para autenticar requisições na sua API (você define, coloque algo forte)
API_KEY=sk_live_sua_chave_aqui

# Porta do servidor (só coloque se rodar local; Fly injeta via fly.toml)
# PORT=3000
```

## Conexão do WhatsApp (automática, por usuário)

Cada usuário do painel Django ganha a própria sessão de WhatsApp automaticamente:
no primeiro "Conectar", o serviço cria uma instância `whatsapp-web.js` isolada
(pasta própria em `.wwebjs_auth/<sessao>`) e mostra o QR Code. Sem configuração
manual por usuário.

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
```

O estado da conexão (fase do login) é guardado no cache do Django. Em produção com
`DATABASE_URL` (Postgres) o cache é no banco — compartilhado entre os workers do
gunicorn, então o polling do front enxerga o login mesmo com +1 worker. Sem Postgres
(dev), roda em processo único e também funciona.

Sem as chaves do Browserbase, a tela `/scrapers/ml/` avisa o usuário que a conexão
está indisponível, em vez de quebrar.
