# Spreading v2: operação

Runbook de quem opera o sistema. O README explica o que ele faz; aqui está como subir, cuidar e
consertar.

**O que foi verificado em 08/11/2026:** `compose.prod.yaml` e `deploy/Caddyfile` validam; a cadeia
Caddy → web em modo HTTPS foi exercitada num stack isolado (`healthz` por TLS, redirecionamento
http→https sem laço, HSTS, cookie `Secure`, `X-Frame-Options`); healthchecks de web e worker; o
backup e a restauração (`tools/restore_check.py`). **Não foi executado:** nada num servidor real (DNS,
certificado público, cópia para fora com rclone, cron), porque o VPS não foi contratado. Esses passos
estão marcados "só no servidor".

## 1. Peças

| Serviço | Papel | Porta no servidor |
|---|---|---|
| `db` | Postgres 17 | nenhuma |
| `web` | painel, `/pair`, `/healthz`, `/status` (gunicorn, 2 workers) | 127.0.0.1:8000 |
| `worker` | `run_cycle --loop 300`: fontes, fila, envio, reconciliação, checagem dos números | nenhuma |
| `waha` | WhatsApp (WAHA, engine GOWS); sessões no volume `wasessions` | 127.0.0.1:3000 |
| `caddy` | TLS e proxy (só em produção) | 80, 443 |
| `backup` | `pg_dump` diário, 14 cópias (só em produção) | nenhuma |
| `linkbuilder` | gerador de `meli.la` (opcional, perfil `linkbuilder`) | nenhuma |

Volumes que importam: `pgdata` (banco, tem backup) e **`wasessions` (a sessão do WhatsApp, sem backup
restaurável: perdeu, re-pareia)**. Nunca apague `wasessions` para "limpar".

## 2. Subir em um servidor novo (só no servidor)

1. VPS com Ubuntu 24.04, 2 GB de RAM (o stack ocioso usa ~0,6 GB), Docker e o plugin Compose.
   Firewall: liberar 22, 80 e 443; nada mais.
2. DNS: registro A do domínio apontando para o IP do servidor.
3. `git clone https://github.com/Mewstacks/Spreading.git && cd Spreading && git checkout v2`.
4. `cp .env.example .env` e preencher (cada segredo com `python3 -c "import secrets;print(secrets.token_urlsafe(32))"`):
   `DJANGO_SECRET_KEY` (64), `POSTGRES_PASSWORD`, `WAHA_API_KEY`, `WAHA_DASHBOARD_PASSWORD`,
   `STATUS_TOKEN`, `ML_API_CLIENT_ID/SECRET`, `DOMAIN`, e se usar `SHOPEE_APP_ID/SECRET`.
5. `docker compose -f compose.yaml -f compose.prod.yaml up -d --build`. O web migra sozinho; o Caddy
   emite o certificado na primeira visita ao domínio.
6. `docker compose exec web python manage.py createsuperuser` (o login do painel).
7. Conferir: `docker compose ps` (web e worker `healthy`), `https://DOMAIN/healthz` → `ok`,
   `curl -H "X-Status-Token: $STATUS_TOKEN" https://DOMAIN/status`.

## 3. Parear o WhatsApp

1. Painel → WhatsApp → **Conectar** (ou `https://DOMAIN/pair?session=default`): mostra o QR.
2. No celular: WhatsApp → Aparelhos conectados → Conectar aparelho → ler o QR.
3. Se o WhatsApp pedir **passkey**, a página mostra "Abrir WAHA". O dashboard fica só em
   `127.0.0.1:3000` do servidor: abra um túnel `ssh -L 3000:127.0.0.1:3000 usuario@servidor` e
   rode `python tools/waha_access.py` na sua máquina (abre o dashboard e copia a senha e a chave sem
   imprimir). Em Server: Name `local`, API URL `http://localhost:3000`, API Key colada.
4. Mais números: painel → WhatsApp → **Novo número** (até 5). Cada grupo sai pelo número em que foi
   adicionado.
5. **Celular principal parado por cerca de 14 dias desvincula os aparelhos**: abra o WhatsApp no
   celular de cada número ao menos uma vez por semana.

## 4. Dia a dia

| Quero ver | Onde |
|---|---|
| O que saiu e por que não saiu | Painel → Grupos → o grupo ("Por que não saiu") |
| Estado de cada fonte | Painel → Fontes |
| Estado e última checagem de cada número | Painel → WhatsApp (`ok 14:05` ou `falhou 3×`) |
| Vendas e comissão da Shopee, por grupo | Painel → Receita |
| Como a mensagem vai ficar antes de mandar | Painel → Ofertas/Cupons → ícone de olho (Prévia) |
| Saúde para monitor externo | `GET /status` com `X-Status-Token` (`ok`, `cycle_age_s`, `whatsapp_checks`) |
| Log do ciclo | `docker compose logs -f worker` |

`/status` fica `ok: false` quando o número principal não está `WORKING`, o feed de cupons está velho
ou o ciclo do worker parou há mais de 15 minutos (`cycle_age_s`).

## 5. Chaves de desligar

Cada uma é uma variável do `.env`; depois de mudar: `docker compose up -d worker web`.

| Para parar | Variável |
|---|---|
| Vitrine pública do ML | `ML_PUBLIC_PAGES=0` |
| Coleta da Amazon (busca e cupons) | `AMAZON_COLLECT=0` |
| Link curto com sub-id da Shopee | `SHOPEE_TRACK=0` |
| Link Builder do ML | parar o serviço `linkbuilder` ou `LINKBUILDER_ENABLED=0` |
| Todo envio | `docker compose stop worker` (a fila espera) |
| Um grupo | painel → Grupos → desmarcar "Ativa" na regra |
| Teto de mensagens por hora e por número | `SESSION_HOURLY_CAP` (0 desliga o teto) |

## 6. Backup e restauração

- **Automático (produção):** o serviço `backup` grava `spreading-AAAAMMDD-HHMM.dump` no volume `backups`
  a cada 24 h e mantém as 14 mais novas. **Copie para fora do servidor**: backup no mesmo disco morre
  com o disco. Exemplo com rclone (configure um destino S3/R2/B2 uma vez com `rclone config`):

  ```bash
  docker run --rm -v spreading_backups:/b -v ~/.config/rclone:/config/rclone rclone/rclone \
    copy /b remoto:spreading-backups --max-age 2d
  ```

  Agende no cron do servidor (`crontab -e`: `30 4 * * * ...`).
- **Manual (local):** `python tools/backup.py` grava em `backups/` e guarda os 14 mais novos.
- **Teste de restauração:** `python tools/restore_check.py` faz o dump, restaura num Postgres 17
  descartável e compara a contagem de linhas de cada tabela. Rode depois de mudar o schema e uma vez
  por mês. Em 08/11/2026: 27 tabelas e 36.807 linhas restauradas sem diferença.
- **Restaurar de verdade** (banco novo ou corrompido):

  ```bash
  docker compose stop web worker
  docker compose exec -T db pg_restore -U spreading -d spreading --clean --no-owner < arquivo.dump
  docker compose up -d web worker
  ```

- **Sessão do WhatsApp:** não restaura de cópia (o estado muda a cada mensagem e uma cópia velha
  desincroniza ou derruba a sessão original). Perdeu o volume `wasessions`? Pareie de novo (seção 3).

## 7. Atualizar e voltar atrás

```bash
git pull origin v2
docker compose -f compose.yaml -f compose.prod.yaml up -d --build   # web migra sozinho
python3 tools/restore_check.py   # opcional, depois de migração nova
```

Voltar atrás: `git checkout <commit anterior>` e o mesmo `up -d --build`. Migração que já rodou não
desfaz sozinha: se a versão antiga não entende o schema novo, restaure o backup anterior (seção 6).

A versão do WAHA está fixa em `compose.prod.yaml`. Para atualizar, troque a tag, suba só o WAHA
(`up -d waha`), confira em Painel → WhatsApp que o número voltou a `Conectado` e veja o `canary`.

## 8. Quando algo para

| Sintoma | O que fazer |
|---|---|
| WhatsApp → "falhou 3×" ou `FAILED` | Painel → WhatsApp → Conectar; se pedir QR, ler de novo. Sessão que sumiu: pareie de novo |
| "conectado, mas o WhatsApp não responde" | Celular sem internet há dias ou aparelho desvinculado: abra o WhatsApp no celular; se não voltar, pareie de novo |
| Envio `incerto` | O ciclo reconcilia sozinho em até 10 minutos olhando o histórico do chat; nunca reenvia. Se ficar `incerto`, confira o grupo antes de qualquer coisa |
| Fila parada | `docker compose ps` (worker `unhealthy`?), `docker compose logs --tail 100 worker` |
| Fonte "Falhando N×" | Painel → Fontes mostra o erro; Amazon e ML pausam sozinhos por 1 h em captcha ou bloqueio |
| Link Builder em "Sem sessão" ou "Verificação" | Só a conta do afiliado resolve: refazer o login (`tools/ml_login.py`); enquanto isso use Painel → Links |
| Painel pede senha e barra com "Muitas tentativas" | 5 erros do mesmo endereço fecham o login por 15 min; espere ou `docker compose exec web python manage.py shell -c "from apps.orgs.models import LoginFailure; LoginFailure.objects.all().delete()"` |
| Grupo parado e "teto" cheio | O teto do dia foi atingido; o contador zera à 00:00 de Brasília. Ritmo "Espalhado" na regra reparte o teto pela janela |

## 9. Rotina

- **Semanal:** abrir o WhatsApp no celular de cada número; olhar Painel → Fontes e Receita.
- **Mensal:** `python tools/restore_check.py`; conferir se o backup foi copiado para fora; ver se há
  versão nova do WAHA e ler as notas antes de trocar a tag.
- **Antes de mexer em regra de grupo real:** Painel → Ofertas → Prévia mostra a legenda exata.

## 10. Segurança

- Segredos só no `.env` do servidor (fora do git). `STATUS_TOKEN` fecha o `/status`; `WAHA_API_KEY` e a
  senha do dashboard do WAHA ficam sem porta pública (só `127.0.0.1`, por túnel SSH).
- Login do painel: 5 senhas erradas do mesmo endereço fecham a porta por 15 minutos; cookies só por
  https, HSTS de 30 dias, `X-Frame-Options: DENY`, escape de tudo que vem das lojas.
- `manage.py check --deploy` com `DJANGO_HTTPS=1` só reclama de `HSTS_INCLUDE_SUBDOMAINS` e
  `HSTS_PRELOAD`, deixados de fora de propósito: são difíceis de desfazer e só valem com certeza sobre
  todos os subdomínios do domínio.
- Quem entrou no painel pode enviar mensagens pelo número de WhatsApp: crie um usuário por pessoa e
  desative o de quem sair (`/admin/`).

## 11. O que depende de quem

- **Conta de afiliado do ML (lules):** `tools/ml_login.py` no computador dela gera a sessão do Link
  Builder; sem ela, só saem pares de cupom por código e ofertas que já têm link. Passo a passo em
  `docs/lules.md`.
- **Alertas:** desligados por escolha do dono; ligam com `ALERT_CHAT_ID` e/ou `ALERT_EMAIL_TO`
  (`.env.example`).
- **Awin e Lomadee:** precisam de credenciais da conta de afiliado.
