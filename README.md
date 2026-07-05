# Variáveis de Ambiente (.env)

Crie um arquivo `.env` na pasta `node.js/` com as variáveis abaixo.

```dotenv
# Senha para autenticar requisições na sua API (você define, coloque algo forte)
API_KEY=sk_live_sua_chave_aqui

# Porta do servidor (só coloque se rodar local; Fly injeta via fly.toml)
# PORT=3000
```

Cada usuário do painel Django ganha a própria sessão de WhatsApp automaticamente:
no primeiro "Conectar", o serviço cria uma instância `whatsapp-web.js` isolada
(pasta própria em `.wwebjs_auth/<sessao>`) e mostra o QR Code. Sem configuração
manual por usuário.
