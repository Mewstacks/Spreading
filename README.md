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
