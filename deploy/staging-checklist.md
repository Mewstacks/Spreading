# Staging isolado

> Resumo histórico. O plano completo e atualizado (com o que já existe no Fly, o que
> falta e o ambiente local em Docker) está em [`homologacao.md`](homologacao.md).

1. Crie `spreading-web-staging`, `spreading-wa-staging`, um Postgres e os volumes
   `ml_data_staging` / `wa_data_staging`, todos na região `gru`.
2. Aplique segredos novos: `DJANGO_SECRET_KEY`, `SECRETS_FERNET_KEY`, banco e uma
   `API_KEY` exclusiva. Não reutilize cookies, sessão WhatsApp ou chaves produtivas.
3. Faça deploy com `fly deploy -c fly.staging.toml` em cada diretório e crie uma conta
   de teste verificada.
4. Conecte as contas de teste de ML e Amazon pelo live view; pareie somente um grupo
   WhatsApp de teste. Não copie senha, cookie ou QR da produção.
5. Execute um ciclo completo: scrape, cupons, links, relatórios e envio ao grupo de
   teste. Só promova quando os workers ficarem saudáveis durante um ciclo completo.
