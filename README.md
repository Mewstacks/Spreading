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

O login do Mercado Livre acontece dentro do próprio app: rodamos um Chromium **local**
(o mesmo Chromium/Playwright que o scraper já usa) e transmitimos a tela pro navegador
do usuário via **CDP screencast** desenhado num `<canvas>`; o mouse e o teclado dele
voltam por POST e viram comandos `Input.dispatch*`. Ele loga no ML ali — no celular ou
desktop, inclusive a verificação em duas etapas — e a sessão é salva sozinha. Não
precisa rodar script nenhum, colar `auth.json`, nem contratar serviço externo. A senha
é digitada direto na página real do ML (não passa pelo backend do Spreading).

Não há chaves a configurar: o login usa o Chromium da própria imagem. Basta ter o
Playwright + Chromium instalados (o `Dockerfile`/`setup.ps1` já fazem isso).

O estado da conexão (fase do login) é guardado no cache do Django; os frames e a fila
de input ficam em memória no processo do gunicorn (por isso 1 worker — ver `Procfile`).
Em produção com `DATABASE_URL` (Postgres) o cache de fase é no banco, então o polling do
front enxerga a fase mesmo entre threads.
