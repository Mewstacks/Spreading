# Runbook — Fase 0: isolamento, criptografia e corte de superfície

Este roteiro é o procedimento operacional da Fase 0. Enquanto ele estiver em
andamento, cadastro público e criação de contas pelo superadmin permanecem
bloqueados, e ML/WhatsApp/Telethon ficam desligados salvo piloto explícito.

> **⚠️ A homologação não existe mais.** Em 09/08/2026 os apps
> `spreading-web-staging`, `spreading-wa-staging` e `spreading-staging-db` foram
> destruídos no Fly (com seus volumes) para cortar custo, e os `fly.staging.toml`
> saíram do repo. Todos os comandos abaixo que citam `*-staging` ou
> `--config fly.staging.toml` **não rodam como estão**.
>
> Este runbook foi escrito para ensaiar cada passo em homologação antes de tocar
> produção — e essa rede de proteção sumiu. Antes de executar a Fase 0, escolha:
> recriar a homologação temporariamente (custa ~US$200/mês enquanto estiver de pé,
> ou ~US$63/mês se você recriar em `shared-cpu-2x` em vez de `performance`), ou
> reescrever o roteiro para rodar direto em produção — com snapshot validado do
> banco e dos volumes, e ciente de que não haverá ensaio.

## Resultado obrigatório

- `spreading_runtime`, `spreading_system` e `spreading_migration` sem
  `SUPERUSER`, `CREATEROLE`, `CREATEDB` ou `BYPASSRLS`;
- somente `spreading_migration` é dona das tabelas;
- 24 tabelas de tenant/controle com `ENABLE + FORCE ROW LEVEL SECURITY`,
  incluindo `Organization`, `Membership` e `Perfil`;
- 17 constraints validadas e contexto de organização/ator/system assinado por
  HMAC-SHA-256 (UUID sozinho não seleciona tenant);
- sessões ML somente em AES-256-GCM, sem `auth*.json`;
- WhatsApp somente na 6PN privada, autenticado por capacidades Ed25519;
- `API_KEY`/`WHATSAPP_API_KEY` removidas;
- `/healthz`, auditorias e suites verdes antes de qualquer piloto.

## 0. Gates antes da primeira alteração

Na raiz do repositório:

```powershell
cd python\django
python manage.py test apps
python manage.py makemigrations --check --dry-run
python manage.py check
cd ..\..\node.js
npm test
npm run audit:production
```

Confirme que há snapshot recente do banco e dos volumes `ml_data`/`wa_data`.
Registre IDs e horários no ticket de mudança. Não avance se o último snapshot
válido tiver mais de 24 horas ou se a restauração nunca tiver sido ensaiada.

## 1. Expand: roles e material criptográfico

O script abaixo é deliberadamente one-shot. Ele:

1. gera senhas, KEK AES-256, chave HMAC de contexto e par Ed25519 somente em memória;
2. cria as três roles mínimas usando a conexão administrativa existente;
3. transfere ownership das tabelas para a role de migração;
4. instala URLs separadas e chaves como Fly Secrets;
5. remove o secret temporário de bootstrap;
6. ativa `PHASE0_EXPAND_ONLY=1` para a primeira onda não trocar RLS antes de a
   imagem compatível estar servindo.

Ele se recusa a rodar se detectar que o ambiente já foi inicializado.

```powershell
powershell -ExecutionPolicy Bypass -File deploy\phase0-bootstrap.ps1 staging
```

Não copie os valores para `.env`, terminal, ticket ou gerenciador de logs.

## 2. Staging: primeira onda com todas as automações frágeis desligadas

```powershell
cd python
fly deploy --app spreading-web-staging --config fly.staging.toml
cd ..
```

O `release_command` executa `check --deploy`, migrações, auditoria de tenants e
bootstrap idempotente do superusuário. Na primeira onda, `PHASE0_EXPAND_ONLY`
adia apenas constraints/RLS; cadastro e automações continuam congelados. Isso
evita que a policy assinada quebre a imagem antiga durante o rolling deploy.
Qualquer saída diferente de zero cancela o deploy antes da troca da máquina.

Valide as roles em processos distintos:

```powershell
fly ssh console --app spreading-web-staging --pty=false --command "python /app/django/manage.py tenant_db_role_audit"
fly ssh console --app spreading-web-staging --pty=false --command "env TENANT_SYSTEM_PROCESS=1 python /app/django/manage.py tenant_db_role_audit"
fly ssh console --app spreading-web-staging --pty=false --command "env RELEASE_COMMAND=1 python /app/django/manage.py tenant_db_role_audit"
```

## 3. Migração de sessões ML

Primeiro faça o dry-run; ele deve atribuir cada `auth_<user>.json` a exatamente
um tenant. `auth.json` global, usuário inexistente, cookie inválido ou erro de
decrypt bloqueia a mudança.

```powershell
fly ssh console --app spreading-web-staging --pty=false --command "env TENANT_SYSTEM_PROCESS=1 python /app/django/manage.py ml_sessions_migrate"
fly ssh console --app spreading-web-staging --pty=false --command "env TENANT_SYSTEM_PROCESS=1 python /app/django/manage.py ml_sessions_migrate --apply"
fly ssh console --app spreading-web-staging --pty=false --command "env TENANT_SYSTEM_PROCESS=1 python /app/django/manage.py session_plaintext_audit"
```

O `--apply` só remove o plaintext depois de cifrar, ler de volta e comparar o
digest do conteúdo.

## 4. Contract: constraints e RLS

As constraints entram `NOT VALID`, evitando uma reescrita longa. A validação é
uma segunda operação e só acontece após a auditoria.

```powershell
fly ssh console --app spreading-web-staging --pty=false --command "env TENANT_SYSTEM_PROCESS=1 python /app/django/manage.py tenant_audit"
fly ssh console --app spreading-web-staging --pty=false --command "env RELEASE_COMMAND=1 python /app/django/manage.py tenant_constraints --ensure"
fly ssh console --app spreading-web-staging --pty=false --command "env RELEASE_COMMAND=1 python /app/django/manage.py tenant_rls --enable"
fly ssh console --app spreading-web-staging --pty=false --command "env RELEASE_COMMAND=1 python /app/django/manage.py tenant_constraints --status"
fly ssh console --app spreading-web-staging --pty=false --command "env RELEASE_COMMAND=1 python /app/django/manage.py tenant_rls --status"
fly ssh console --app spreading-web-staging --pty=false --command "python /app/django/manage.py tenant_isolation_probe"
fly secrets unset PHASE0_EXPAND_ONLY --app spreading-web-staging
```

Ao remover `PHASE0_EXPAND_ONLY`, a máquina reinicia com o readiness estrito:
RLS ausente, policy sem HMAC, função insegura ou segredo legível passam a devolver
503 e impedem o deploy. Depois do `--enable`, teste obrigatoriamente:

- owner lê e altera somente o próprio tenant;
- viewer não altera e não inicia SSE;
- operator não gerencia credenciais/conexões;
- UUID/ID de outro tenant retorna 404/403 e não altera nada;
- worker de sistema processa dois tenants sem misturar estado;
- runtime não consegue `SET ROLE`, contornar policy, forjar os GUCs ou ler o
  segredo HMAC.

## 5. Corte do WhatsApp

Implante o Node somente depois de o web estar congelado e o RLS aprovado:

```powershell
cd node.js
fly deploy --app spreading-wa-staging --config fly.staging.toml
cd ..
fly ssh console --app spreading-web-staging --pty=false --command "env TENANT_SYSTEM_PROCESS=1 python /app/django/manage.py wa_capability_probe"
fly secrets unset API_KEY --app spreading-wa-staging
fly secrets unset WHATSAPP_API_KEY --app spreading-web-staging
```

O Node não possui `services`/`http_service`: responde apenas na rede privada
`*.internal`. O probe exige que a rota sem token seja negada e que uma capacidade
Ed25519 vinculada a tenant/sessão/ação seja aceita. Depois, libere com
`fly ips release` todos os endereços mostrados por `fly ips list`; confirme que
`fly services list` e `fly ips list` ficam vazios.

Não reative `WHATSAPP_WEB_ENABLED` aqui. O piloto só pode ser ativado mais tarde
com `PILOT_ORGANIZATION_IDS` explícito, métricas e rollback ensaiado.

## 6. Critérios para promover a produção

Mantenha staging por pelo menos um ciclo completo dos workers e confirme:

- zero falha em `tenant_audit`, `tenant_rls --status`,
  `tenant_constraints --status`, `tenant_isolation_probe`,
  `wa_capability_probe` e `session_plaintext_audit`;
- `npm run audit:production` sem vulnerabilidade;
- nenhuma tentativa cross-tenant no log;
- nenhum `decrypt_error`;
- nenhum restart/OOM inesperado;
- `/healthz` estável e role runtime correta;
- Node inacessível por IP público e acessível pelo nome `.internal`;
- snapshots posteriores ao cutover presentes.

Repita as etapas 1–5 trocando `staging` por `production` e usando apps sem o
sufixo `-staging`. Crie snapshots antes e depois do corte. Não copie secrets
entre ambientes.

## Rollback

### Antes de validar constraints

Pare o deploy, mantenha os flags desligados e corrija o backfill. Constraints
`NOT VALID` não bloqueiam dados antigos e podem ser removidas com:

```powershell
fly ssh console --app <web-app> --pty=false --command "env RELEASE_COMMAND=1 python /app/django/manage.py tenant_constraints --drop"
```

### Depois de habilitar RLS

Não volte a uma imagem que desconheça tenant enquanto RLS estiver ativo.
Preferência: corrigir e fazer roll-forward. Se a aplicação inteira estiver
indisponível e o rollback for inevitável:

1. mantenha cadastro e automações desligados;
2. pare processos de worker;
3. registre o motivo e a janela;
4. desabilite RLS com a role de migração;
5. reverta para a versão anterior;
6. não reabra tráfego até executar `tenant_audit`.

```powershell
fly ssh console --app <web-app> --pty=false --command "env RELEASE_COMMAND=1 python /app/django/manage.py tenant_rls --disable"
```

Esse rollback reduz a proteção e é apenas contingência de indisponibilidade.
Vazamento confirmado exige restauração/rotação, não apenas rollback de código.

### Sessões ML

Nunca restaure `auth*.json` no volume ativo. Restaure snapshot em volume isolado,
extraia somente o registro necessário e reexecute a migração. Preserve versões
anteriores da KEK no keyring até todas as sessões terem sido rotacionadas e
verificadas.

### WhatsApp

Reverter o Node não autoriza recolocar uma chave mestre nem publicar a porta.
Mantenha o recurso desligado e restaure o volume `LocalAuth` isoladamente se o
problema for de estado.
