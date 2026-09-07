# Restaurar o banco a partir de um snapshot

O que fazer quando o `spreading-db` corromper, for apagado por engano, ou uma
migração destruir dados. Escrito a partir do ensaio de 01/09/2026, registrado em
`PLANO_REVISAO_PRODUCAO.md` — o procedimento abaixo é o que foi realmente executado
naquele dia, não um roteiro imaginado.

**Regra que vale para tudo aqui: nunca restaure por cima da produção.** O snapshot
vira um volume novo, num cluster novo. Só depois de conferir o conteúdo é que se
decide o que fazer com ele. Um restore em cima do primary não tem botão de desfazer.

---

## 0. O que existe hoje

O `spreading-db` é Postgres não gerenciado (`postgres-flex`), um único primary em
`gru`, volume `pg_data` de 3 GB. A Fly tira snapshot diário do volume e guarda por
**5 dias**. Confirme antes de qualquer coisa:

```bash
fly volumes list --app spreading-db
fly volumes snapshots list vol_vwn1owm691gj6p8v
```

Em 07/09/2026 havia seis snapshots, o mais novo com 8 horas — dentro da exigência de
"menos de 24 horas" do runbook. **Se o mais novo tiver mais de 24 horas, isso é o
incidente**, mesmo que o banco pareça saudável: a retenção é de 5 dias e não há
segunda cópia em lugar nenhum.

Não existe backup lógico (`pg_dump`) agendado. A cópia é o snapshot do volume, e ela
é do cluster inteiro — restaura tudo ou nada, e traz o WAL para o PostgreSQL recuperar
na subida.

---

## 1. Escolher o snapshot

```bash
fly volumes snapshots list vol_vwn1owm691gj6p8v
```

Pegue o **mais recente anterior ao estrago**. Se o problema foi uma migração ruim às
14:00, o snapshot das 08:00 serve; o das 20:00 já contém o estrago.

---

## 2. Subir um cluster isolado a partir dele

```bash
fly pg create --name spreading-db-restore --region gru \
  --vm-size shared-cpu-1x --volume-size 3 \
  --snapshot-id vs_XXXXXXXXXXXXXXXXXXXXXXXX
```

Mesma imagem e mesmo tamanho de volume do primary — restaurar num volume menor que o
original falha, e num tamanho diferente de imagem major o PostgreSQL não abre o
datadir.

> **Isto custa dinheiro.** Uma máquina `shared-cpu-1x`/1 GB mais 3 GB de volume,
> enquanto o cluster existir. É pouco por hora, mas é uma cobrança nova: destrua no
> passo 6 e confirme que sumiu.

Espere `fly status --app spreading-db-restore` mostrar o primary `started` com os
checks passando. O PostgreSQL recupera o WAL nessa primeira subida; num volume de
3 GB isso levou menos de um minuto no ensaio.

---

## 3. Conferir que o conteúdo presta

Não confie no "started". Entre e conte:

```bash
fly ssh console --app spreading-db-restore
```

Dentro da máquina:

```sql
-- as quatro perguntas que o ensaio de 01/09 respondeu
\c spreading_web
SELECT count(*) FROM information_schema.tables WHERE table_schema='public';
SELECT count(*) FROM django_migrations;
SELECT count(*) FROM accounts_organization;
SELECT count(*) FROM scrapers_cupomdisponibilidade;
```

No ensaio: 54 tabelas, 111 migrações, 4 tenants, 9.199 disponibilidades da `lules`
no ponto do snapshot. Os números de hoje serão outros — o que importa é que sejam
compatíveis com o dia do snapshot, e não zero.

Depois, a checagem física — é ela que separa "abriu" de "está íntegro":

```bash
pg_amcheck --all --heapallindexed --parent-check --progress
```

No ensaio: 533 relações e 49.136 páginas sem um único erro. Qualquer saída diferente
de limpa significa escolher outro snapshot, não seguir em frente.

E as duas perguntas que o `pg_amcheck` não responde:

```sql
SELECT count(*) FROM pg_index WHERE NOT indisvalid;          -- tem de ser 0
SELECT count(*) FROM pg_constraint WHERE NOT convalidated;   -- tem de ser 0
```

---

## 4. Conferir que a aplicação sobe contra ele

Restaurar o banco não prova nada se o app não abrir. Este passo é o que fecha o
buraco que o CI já mostrou uma vez: **o banco não sobe do zero sem o contexto
assinado**, e foi assim que a ordem do `tenant_rls --only-context` apareceu.

Aponte uma máquina descartável para o cluster restaurado — nunca a produção:

```bash
fly machine clone 7817966b6d2d78 --app spreading-web --region gru
# na máquina clonada, e SÓ nela:
fly ssh console --app spreading-web --machine <ID_DO_CLONE>
export DATABASE_URL=... SYSTEM_DATABASE_URL=... MIGRATION_DATABASE_URL=...   # do cluster restaurado
RELEASE_COMMAND=1 python /app/django/manage.py check --deploy
RELEASE_COMMAND=1 python /app/django/manage.py tenant_rls --only-context
RELEASE_COMMAND=1 python /app/django/manage.py migrate --noinput
RELEASE_COMMAND=1 python /app/django/manage.py tenant_constraints --ensure
RELEASE_COMMAND=1 python /app/django/manage.py tenant_rls --enable
python /app/django/manage.py tenant_isolation_probe
```

É a mesma sequência do `release_command` em `python/fly.toml`, na mesma ordem, mais o
probe. Se o `tenant_isolation_probe` passar, o banco restaurado tem RLS válido e GUC
falsificado não atravessa organização — que é a única prova de isolamento que vale.

Destrua o clone quando terminar:

```bash
fly machine destroy <ID_DO_CLONE> --app spreading-web --force
```

---

## 5. Promover (só se for para valer)

Só chegue aqui com os passos 3 e 4 limpos.

Não existe "trocar o volume do primary": o caminho é **apontar a aplicação para o
cluster restaurado**. Ou seja, reescrever as três URLs de banco nos secrets, em uma
transação de operação, com o app parado:

```bash
fly machine stop --app spreading-web            # todas as máquinas: web e worker
fly machine stop --app spreading-wa
fly secrets set --app spreading-web \
  DATABASE_URL=... SYSTEM_DATABASE_URL=... MIGRATION_DATABASE_URL=...
fly machine start --app spreading-web
fly machine start --app spreading-wa
```

Parar antes é obrigatório: com o app escrevendo, metade das linhas iria para o banco
velho e metade para o novo, e não há como reconciliar isso depois.

Renomeie o cluster antigo em vez de destruí-lo. Ele é a única cópia do estado que
você acabou de abandonar; guarde pelo menos até o dia seguinte fechar limpo.

---

## 6. Limpar

Se o restore foi só ensaio — que é o caso normal, uma vez por trimestre — destrua
tudo que subiu:

```bash
fly apps destroy spreading-db-restore
fly volumes list --app spreading-db-restore     # tem de vir vazio
```

E confirme na fatura que a cobrança parou. Cluster de ensaio esquecido ligado é a
maneira mais boba de estourar o teto de R$350/mês.

---

## O que este procedimento não cobre

- **Perda de menos de 24 horas.** O snapshot é diário; o que foi escrito depois dele
  não existe em lugar nenhum. Não há PITR nesta topologia.
- **Snapshot com mais de 5 dias.** A retenção é 5 dias e não há arquivo frio.
- **Corrupção que já entrou no snapshot.** Se o estrago é antigo, todos os seis
  snapshots o contêm. É por isso que o passo 3 conta linhas antes do passo 5.
