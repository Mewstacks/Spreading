# Fase 0 — Redução de risco e bloqueio de perdas

## Janela: 0 a 2 semanas

**Estado do documento:** implantado em staging e produção em 26/07/2026;
freeze e soak permanecem ativos

**Escopo:** somente a fundação P0 de engenharia e segurança

**Regra de negócio:** nenhum novo usuário ou tenant externo entra durante a fase

**Referência principal:** [`PLANO_SAAS_VIABILIDADE.md`](PLANO_SAAS_VIABILIDADE.md)

---

## 1. Resultado esperado

Ao fim desta fase:

1. `Organization`, e não `User`, é a fronteira de autorização e de dados.
2. Dados privados não podem ser lidos ou alterados por outra organização, mesmo se
   uma view, worker ou consulta esquecer um filtro.
3. Sessões do Mercado Livre não existem em texto puro no volume ou no banco, não
   usam fallback para a sessão “mais recente” e são criptograficamente vinculadas
   à organização correta.
4. O serviço de WhatsApp não possui endpoint público nem uma chave simétrica global
   capaz de operar todas as sessões.
5. Produção e staging recusam iniciar com SQLite, sem `DATABASE_URL` ou com uma URL
   que não seja PostgreSQL.
6. A aplicação continua segura e compreensível quando Playwright, Link Builder,
   leitura de portal ou WhatsApp Web forem desligados.
7. O rollout pode ser interrompido ou revertido para uma versão compatível sem
   desfazer migrações destrutivas.

O prazo de duas semanas é um alvo, não uma autorização para reduzir os gates. Se um
gate não passar, o cadastro permanece fechado e a fase continua.

---

## 2. Diagnóstico confirmado no repositório

| Achado | Evidência atual | Consequência |
|---|---|---|
| Tenant é `User` | `Perfil` é 1:1 com `User`; diversos modelos usam `owner` ou `usuario` | Equipe/agência não tem fronteira própria e o isolamento depende de filtros manuais |
| Há registros privados com owner anulável | `Produto`, `CupomNormalizado`, `CupomPreparacao`, `HistoricoEnvio` e `ConfiguracaoEnvio` | `NULL` mistura “público”, “legado” e, em alguns lugares, “sem contexto” |
| Migração legada atribuiu dados órfãos ao primeiro superusuário | `scrapers/migrations/0019_backfill_owner.py` | Não se pode repetir esse padrão na migração para organizações |
| Sessão principal do ML é JSON em texto puro | `ml_conexao.py` grava `storage_state` em `auth_{user_id}.json` | Roubo do volume ou snapshot expõe cookies de sessão |
| Existe fallback de sessão ML | `session_paths.py` escolhe `auth.json` ou o arquivo mais recente quando não recebe usuário | Um job pode usar a identidade e a tag do tenant errado |
| WhatsApp usa uma chave global | Django envia `WHATSAPP_API_KEY`; Node valida `API_KEY` | Quem obtém a chave pode operar qualquer sessão |
| O app WhatsApp ainda declara portas 80/443 | `node.js/fly.toml` contém `services.ports` | A configuração contradiz o comentário “só rede privada” |
| SQLite é fallback silencioso | `core/settings.py` usa SQLite quando `DATABASE_URL` está vazia | Um erro de secret/configuração pode iniciar uma produção divergente |
| Cadastro público já nasce fechado | `PERMITIR_CADASTRO_PUBLICO` tem default `0` | Há uma boa primeira trava, mas falta bloquear criação operacional de novos tenants |
| Deploy é single-machine nos componentes com sessão | volumes `ml_data` e `wa_data` são presos a uma máquina | Zero downtime real não é possível nesta fase |

O `PLANO_SAAS_VIABILIDADE.md` também manda congelar automações frágeis, usar
`Organization` como boundary, eliminar fallback entre usuários, retirar a chave
master do WhatsApp e falhar sem Postgres. Este plano transforma essas diretrizes
em uma sequência implantável.

---

## 3. Escopo e fora de escopo

### Incluído

- congelamento de aquisição e flags de contenção;
- `Organization`, `Membership` e papéis mínimos;
- migração dos dados privados atuais de `User` para `Organization`;
- contexto obrigatório de organização em request, serviço e worker;
- defesa no banco com PostgreSQL Row-Level Security (RLS);
- criptografia autenticada e rotação das sessões existentes do Mercado Livre;
- remoção do fallback de sessão/tag;
- autenticação por capacidade curta e escopada no serviço WhatsApp;
- remoção da exposição pública do WhatsApp;
- falha fechada sem Postgres;
- testes, telemetria, backup, rollback e gates de liberação.

### Não incluído

- billing self-service;
- RBAC completo de agência ou seller;
- Redis/Celery e separação total dos workers;
- troca definitiva de WhatsApp Web pela API oficial;
- obtenção de API oficial de afiliados do Mercado Livre;
- atribuição, payout, campanhas e marketplace seller–creator;
- remoção física imediata de todas as colunas legadas de `User`.

As colunas antigas ficam por uma janela de observação, sem serem fonte de
autorização. A remoção física será uma migração posterior.

---

## 4. Decisões de arquitetura

### 4.1 Fronteira de identidade

```text
User
  └── Membership ── Organization
                       ├── OrganizationSettings
                       ├── Marketplace/Channel Connections
                       ├── Configurações e publicações
                       ├── Credenciais e sessões
                       └── Métricas, receitas e incidentes
```

- `User` representa a pessoa autenticada e permanece como ator de auditoria.
- `Organization` representa o dono dos dados, integrações, regras e cobrança.
- `Membership` autoriza a pessoa a agir naquela organização.
- Todo usuário atual recebe uma organização individual e uma membership `owner`.
- IDs de organização enviados pelo navegador nunca são confiados isoladamente. A
  organização ativa é resolvida no servidor a partir da sessão e da membership.
- Serviços de domínio recebem `organization` explicitamente. Parâmetros opcionais
  como `usuario=None` deixam de escolher escopo.
- Jobs privados carregam `organization_id` no payload/estado e falham fechados se
  ele estiver ausente.
- Processos de catálogo compartilhado usam um contexto de sistema explícito, não
  a organização do “primeiro usuário”.

Papéis mínimos desta fase:

| Papel | Uso nesta fase |
|---|---|
| `owner` | membros, credenciais, conexões, configurações e leitura |
| `operator` | operação de ofertas e canais, sem membros/billing |
| `viewer` | somente leitura |

Não é necessário entregar a interface completa de equipes nesta fase. É
necessário que o backend já aplique a matriz.

### 4.2 Classificação de dados

Antes de migrar, cada modelo recebe uma classificação explícita:

| Classe | Exemplos atuais | Regra |
|---|---|---|
| Estritamente privado | integrações, configurações, publicações, links afiliados, receita, syncs, canais, envios | `organization_id NOT NULL`; RLS por igualdade |
| Misto público/privado | `Produto`, `CupomNormalizado`, `CupomPreparacao`, relações derivadas | `data_scope` explícito (`public` ou `organization`) e constraint coerente com `organization_id` |
| Público/sistema | catálogo ML público, `FonteIngestao`, `PrecoHistorico`, cupons curados globais | leitura compartilhada; escrita somente por papel de sistema |
| Auditoria | eventos/incidentes | `organization_id` quando houver tenant e `actor_user_id` quando houver pessoa; evento realmente global é `system` |

`NULL` deixa de significar ao mesmo tempo “público”, “legado” e “esquecemos o
tenant”. Nos modelos mistos:

- `data_scope='public'` exige `organization_id IS NULL`;
- `data_scope='organization'` exige `organization_id IS NOT NULL`;
- o runtime comum pode ler público, mas não criar ou alterar registros públicos;
- relações como produto–cupom não podem unir duas organizações diferentes.

Os campos editoriais, cotas, billing e credenciais hoje presentes em `Perfil`
ficam vinculados à `Organization` nesta fase e a própria tabela `Perfil` recebe
RLS forçado. A normalização física desses campos em configurações/conexões
organizacionais separadas é posterior; a autorização já não usa `User` como
fronteira.

### 4.3 Defesa no banco

Somente filtros Django não atendem ao requisito de isolamento completo. O Postgres
também deve negar acesso cruzado:

- papel `spreading_runtime`: usado pelo web; não é dono das tabelas, não
  é superuser e não possui `BYPASSRLS`;
- papel `spreading_system`: usado somente pelos workers/commands cross-tenant,
  também sem ownership, `SUPERUSER` ou `BYPASSRLS`;
- papel `spreading_migration`: usado apenas no `release_command`;
- RLS habilitado e forçado nas tabelas privadas;
- request/job abre transação e instala organização, ator ou system com HMAC-SHA-256;
  as policies validam UUID/GUC e assinatura dentro de função `SECURITY DEFINER`
  com `search_path` fixo e segredo ilegível para runtime/system;
- sem organização configurada, a política privada é default-deny;
- consultas de catálogo público continuam disponíveis;
- suporte cross-tenant não usa `is_superuser` como bypass silencioso. Acesso de
  suporte é explícito, curto, auditado e separado do caminho normal.

PostgreSQL alerta que o dono da tabela normalmente ignora RLS; por isso separar o
papel de runtime e usar `FORCE ROW LEVEL SECURITY` faz parte do gate, não é
hardening opcional.

### 4.4 Sessão Mercado Livre

Criar um repositório de sessão por organização, por exemplo
`MercadoLivreSession`, no Postgres:

```text
organization_id
connection_id
cipher_version
key_version
wrapped_dek
nonce
ciphertext
status
created_at / rotated_at / last_used_at
```

Padrão criptográfico:

- AES-256-GCM, por ser criptografia autenticada;
- uma DEK aleatória de 256 bits por sessão;
- envelope encryption: a DEK é protegida por uma KEK versionada;
- AAD inclui ao menos `organization_id`, `connection_id`, marketplace e versão do
  formato;
- alteração do ciphertext, troca entre organizações ou chave errada falha de modo
  explícito;
- chaves ficam fora do banco e do repositório, com `key_version` para rotação;
- plaintext existe apenas em memória pelo tempo necessário para criar o contexto
  Playwright;
- logs, Sentry, eventos e mensagens de erro nunca recebem cookie, storage state,
  senha ou eventos de teclado.

O Playwright aceita `storage_state` como objeto. Portanto:

- leitura: descriptografar em memória e passar o objeto ao browser;
- gravação: obter `context.storage_state()` em memória e criptografar antes de
  persistir;
- não criar arquivo temporário plaintext;
- não manter `auth.json`, `auth_{id}.json` ou fallback “arquivo mais recente”.

O fluxo de migração é idempotente:

1. localizar o arquivo pelo usuário atual;
2. resolver sua organização individual;
3. validar JSON e domínio dos cookies;
4. criptografar e gravar atomicamente no novo repositório;
5. reler, autenticar e comparar um hash calculado apenas em memória;
6. marcar a versão migrada;
7. remover o plaintext lógico;
8. registrar apenas IDs, versões e resultado.

Depois do cutover, cada conta ativa é reconectada/rotacionada de forma individual.
Isso invalida material antigo que possa existir em snapshots. Snapshots/volumes
legados recebem retenção curta e descarte controlado; não se promete “secure erase”
de storage gerenciado.

### 4.5 Autorização do WhatsApp

Substituir `x-api-key` global por uma capacidade assimétrica e curta:

- Django guarda uma chave privada Ed25519;
- Node recebe somente as chaves públicas de verificação;
- cada chamada recebe um token assinado com algoritmo fixo e claims:
  `issuer`, `audience`, `organization_id`, `session_id`, `actions`, `iat`, `exp`,
  `jti` e `kid`;
- validade máxima de 60 segundos;
- `session_id` do token deve coincidir com a rota/body;
- cada rota exige ação específica, como `status`, `groups`, `send`, `reset` ou
  `logout`;
- `reset`, `logout` e provisionamento rejeitam replay de `jti`;
- envio recebe uma chave de idempotência para evitar duplicação em timeout/retry;
- Node fixa o algoritmo aceito e nunca confia no algoritmo informado livremente
  pelo token;
- rotação publica uma segunda chave, troca o signer e só depois remove a anterior.

Assim, comprometimento do serviço Node não entrega um segredo capaz de fabricar
acesso a todas as sessões. A autoridade de assinatura permanece no backend
autorizador, e o Node só consegue verificar capacidades.

Rede:

- remover `services.ports` 80/443 e qualquer `http_service` público do app Node;
- alcançar o serviço apenas pelo DNS `.internal`/6PN;
- API com bind explícito no endereço privado indicado pela Fly;
- health listener mínimo em porta separada, sem dados de sessão, disponível ao
  top-level health check da Fly, mas sem `services`/`http_service`;
- listar e liberar IPs públicos antigos;
- validar de um runner externo que portas/host públicos não respondem;
- validar de `spreading-web` que o endereço interno responde.

A Fly documenta que uma aplicação sem `services`/`http_service` não é roteada pela
internet e que serviços 6PN podem ser alcançados por `.internal`. A mera presença
de comentário “private-only” não é controle de segurança.

### 4.6 Banco fail-closed

Adicionar `APP_ENV=development|test|staging|production` e não inferir produção
somente de `DEBUG` ou `FLY_APP_NAME`.

Regras:

- `staging` e `production` exigem `DATABASE_URL`;
- a URL deve resolver para backend PostgreSQL;
- SQLite só é permitido em `development` e `test`;
- erro ocorre ao carregar settings, antes de migration, worker ou servidor iniciar;
- `check --deploy` e um system check repetem a validação;
- o health check confirma conectividade e tipo do banco, sem expor URL;
- `fly.toml` de produção e staging declaram `APP_ENV`;
- CI cobre matriz de ambiente ausente, SQLite indevido, Postgres válido e dev local.

O deploy usa `release_command` com a credencial de migração. A Fly executa esse
comando uma vez, antes de substituir as máquinas, e interrompe o deploy se ele
falhar. O runtime recebe apenas a credencial limitada.

### 4.7 Independência das automações frágeis

Nesta fase, “independência” significa que a segurança e a disponibilidade do
painel não dependem do sucesso do browser:

- flags separadas para login ML por browser, Link Builder, relatório por browser,
  Telethon/relink e WhatsApp Web;
- defaults fechados em produção; ativação somente para organizações piloto
  explicitamente permitidas;
- desligar uma integração pausa sua fila, não derruba web, health check ou outras
  organizações;
- item sem link autorizado não é publicado por fallback;
- canal indisponível entra em estado degradado com ação clara, sem retry infinito;
- Telegram oficial e fluxos manuais/exportáveis continuam independentes;
- nenhum material comercial promete automação indisponível ou não autorizada.

Esta fase não torna Playwright ou WhatsApp Web confiáveis. Ela retira ambos da
fundação obrigatória e limita seu raio de impacto.

---

## 5. Plano de trabalho — 10 dias úteis

Os trilhos podem avançar em paralelo, mas os cutovers seguem a ordem da seção 6.

### Dias 0–1 — contenção e inventário

Entregas:

- confirmar `PERMITIR_CADASTRO_PUBLICO=0`;
- criar `SECURITY_FREEZE_NEW_TENANTS=1`, cobrindo signup, convite e criação
  operacional, exceto break-glass interno auditado;
- congelar deploys não relacionados;
- ligar flags de automação por organização;
- inventariar usuários, arquivos `auth*.json`, sessões WhatsApp, linhas privadas,
  owners nulos, relações inconsistentes e IPs do app Node;
- snapshot do Postgres e volumes;
- ensaio de restauração em ambiente isolado;
- definir janela de manutenção e contato de cada usuário piloto;
- registrar baseline de envios, falhas, sessões e filas.

Critério de saída:

- nenhum caminho cria tenant externo;
- inventário fecha em 100% ou lista explicitamente cada órfão/ambiguidade;
- restauração foi executada, não apenas documentada.

### Dias 1–4 — schema de organização e compatibilidade

Entregas:

- modelos `Organization`, `Membership` e configurações/conexões organizacionais;
- `organization_id` e `data_scope` adicionados de forma nullable/aditiva;
- índices concorrentes onde o volume justificar;
- middleware/context manager e escopo obrigatório para jobs;
- managers/services que exigem organização;
- dual-write temporário;
- comando `tenant_audit --dry-run`;
- comando idempotente de backfill em lotes, com checkpoint;
- testes de matriz de acesso.

Critério de saída:

- cada usuário atual tem exatamente uma organização pessoal e membership `owner`;
- toda linha privada tem destino inequívoco;
- nenhum registro ambíguo é atribuído ao primeiro superusuário;
- leitura em modo sombra encontra zero divergências entre owner antigo e
  organização nova.

### Dias 3–6 — sessão ML criptografada

Entregas:

- repositório criptografado de sessão;
- leitor compatível com sessão criptografada e legado, com métrica de uso legado;
- toda nova gravação já criptografada;
- migrador `ml_sessions_migrate --dry-run/--apply`;
- remoção de fallback global/arquivo mais recente;
- teste de tamper, troca de tenant, versão de chave e rotação;
- varredura que falha se encontrar storage state plaintext.

Critério de saída:

- 100% das sessões inventariadas migradas ou classificadas para reconexão;
- zero leitura legada durante um ciclo completo;
- zero plaintext ativo no volume e banco;
- cada sessão só abre com a organização associada.

### Dias 3–7 — WhatsApp privado e sem master key

Entregas:

- Node aceita token Ed25519 escopado;
- Django emite capacidades após validar membership/organização;
- período compatível de dupla autenticação, medido e curto;
- anti-replay em ações destrutivas e idempotência de envio;
- chave global desativada e removida dos dois apps;
- portas públicas removidas do `fly.toml`;
- IPs públicos liberados;
- health check top-level e sondas interna/externa.

Critério de saída:

- zero chamadas com `x-api-key` global durante a janela definida;
- token de org A não lê, envia, reseta ou desloga sessão de org B;
- token expirado, replay, audience/algoritmo incorreto e sessão divergente falham;
- sonda pública não alcança o Node e sonda interna do Django alcança.

### Dias 5–7 — Postgres fail-closed

Entregas:

- `APP_ENV` explícito;
- validação de backend;
- papéis separados de migration/runtime;
- testes de inicialização;
- `release_command` fail-closed;
- documentação e checklist de secrets sem valores.

Critério de saída:

- staging/produção sem `DATABASE_URL` encerram com erro antes de servir;
- URL SQLite ou backend não Postgres também encerra;
- runtime conecta com papel limitado;
- migration role não é usado por web/workers.

### Dias 6–8 — cutover de tenant e RLS

Entregas:

- reads passam a usar somente `Organization`;
- constraints de escopo e non-null aplicadas;
- RLS ativado e forçado;
- queries brutas com papel runtime incluídas nos testes;
- endpoints e workers legados sem organização falham fechados;
- acesso de suporte auditado.

Critério de saída:

- testes de IDOR e raw SQL não atravessam tenant;
- uma request sem contexto vê zero linha privada;
- catálogos públicos permanecem somente leitura para runtime;
- métricas mostram zero `tenant_scope_missing` não esperado.

### Dias 8–10 — staging, canário e produção

Entregas:

- suíte Django e Node completa;
- testes de segurança e migração no CI;
- ensaio de deploy/rollback em staging;
- canário com organização interna já existente;
- cutover dos pilotos atuais um por vez;
- rotação/reconexão das sessões ML;
- soak de ao menos um ciclo completo de automação;
- decisão go/no-go registrada.

Critério de saída:

- todos os gates da seção 9 passam;
- nenhuma fila ficou presa e nenhum envio duplicado foi observado;
- cada piloto confirma conexão e publicação no canal permitido;
- rollback compatível foi ensaiado.

---

## 6. Implantação graceful

### Limite honesto

Com um único volume e uma única máquina por serviço de sessão, não há garantia de
zero downtime. O objetivo operacional é:

- painel disponível sempre que possível;
- pausa explícita dos envios durante a troca;
- nenhuma perda silenciosa;
- nenhum envio duplicado;
- reconexão por tenant, não interrupção coletiva;
- retorno rápido à última versão **compatível com o novo formato**.

### Onda A — preparação sem mudar comportamento

1. Fechar entrada de tenants e habilitar banners de manutenção/degradação.
2. Fazer backup e restaurá-lo em ambiente isolado.
3. Publicar schema aditivo e readers compatíveis.
4. Publicar Node com token novo + chave antiga temporária.
5. Configurar secrets/chaves novas antes de exigir seu uso.
6. Executar auditorias e backfills em dry-run.

Rollback: voltar o código; manter tabelas/colunas aditivas.

### Onda B — backfill e observação

1. Criar organizações pessoais e memberships.
2. Preencher `organization_id` em lotes pequenos.
3. Comparar owner antigo e organização nova em shadow read.
4. Importar e criptografar sessões ML.
5. Passar Django a usar capacidades no WhatsApp.
6. Observar métricas de legado e divergência.

Rollback: desligar reads novos por flag, sem apagar dados novos. Sessões já
criptografadas continuam legíveis pelo release compatível.

### Onda C — cutover de segurança

1. Ativar read por organização.
2. Bloquear operações tenant sem contexto.
3. Drenar os schedulers:
   - não aceitar novos envios;
   - aguardar chamadas em voo até o deadline;
   - persistir cursores;
   - confirmar fila sem item `running` abandonado.
4. Desligar `x-api-key` global e remover os secrets.
5. Tornar WhatsApp privado e liberar IPs públicos.
6. Remover fallback e plaintext ML.
7. Aplicar constraints e RLS.
8. Ativar fail-closed de Postgres.

Rollback permitido: somente para a versão de compatibilidade que entende
`Organization`, sessão criptografada e token escopado. Não voltar para um release
que reintroduza plaintext, fallback de tenant ou master key.

### Onda D — canário e retomada

1. Subir em staging e passar sondas.
2. Ativar a organização canário já existente.
3. Retomar scheduler apenas para o canário.
4. Confirmar status, grupos, link, publicação, idempotência e métricas.
5. Migrar os pilotos atuais individualmente.
6. Retomar filas por organização.
7. Manter aquisição fechada durante o soak.

### Onda E — contrato sem destruição

1. Tornar organização obrigatória nos registros privados.
2. Desligar dual-read e dual-auth.
3. Manter campos de `User` somente como compatibilidade sem autoridade.
4. Agendar remoção física para depois de 7–14 dias estáveis.

Não apagar colunas legadas nem migrações na mesma janela do cutover. Isso preserva
o caminho de retorno e reduz locks.

---

## 7. Runbook de produção

### Antes

- [ ] freeze de novos tenants confirmado por teste HTTP e admin;
- [ ] responsável técnico e responsável pelo go/no-go nomeados;
- [ ] backup do Postgres concluído;
- [ ] restore testado;
- [ ] inventário de owners/sessões/órfãos salvo;
- [ ] chaves novas criadas, armazenadas e com rotação ensaiada;
- [ ] build de compatibilidade identificado e imutável;
- [ ] automações frágeis desligáveis separadamente;
- [ ] pilotos avisados da janela;
- [ ] métricas e alertas visíveis.

### Durante

- [ ] ativar maintenance/drain dos schedulers;
- [ ] esperar operações em voo;
- [ ] executar release expand;
- [ ] rodar `tenant_audit --dry-run`;
- [ ] rodar backfill em lotes e validar contagens;
- [ ] rodar migração ML e validar zero plaintext;
- [ ] trocar Django para token WhatsApp;
- [ ] comprovar zero uso da API key global;
- [ ] remover auth global e exposição pública;
- [ ] aplicar RLS/constraints;
- [ ] validar engine Postgres e papel runtime;
- [ ] canário;
- [ ] retomar organização por organização.

### Depois

- [ ] executar E2E das organizações A e B;
- [ ] testar negação cruzada em HTTP, serviço, worker e SQL;
- [ ] confirmar que sonda externa do Node falha;
- [ ] confirmar que sonda interna passa;
- [ ] verificar métricas de sessão, filas, falha e duplicação;
- [ ] reconectar/rotacionar sessões ML;
- [ ] manter freeze durante o soak;
- [ ] registrar decisão go/no-go.

---

## 8. Testes obrigatórios

### Tenant

- usuário da organização A recebe 404 genérico ao usar IDs da B;
- tentativa de POST/PATCH/DELETE cruzado também falha;
- operador não altera membership ou credencial;
- viewer não escreve;
- worker com `organization_id=A` nunca seleciona config/link/sessão de B;
- job privado sem organização falha antes da primeira query;
- raw SQL com papel runtime não atravessa RLS;
- conexão reutilizada não herda contexto da request anterior;
- associação entre objetos de organizações diferentes é rejeitada;
- catálogo público é legível e não gravável pelo runtime tenant;
- superadmin comum não vira bypass invisível de RLS.

### Mercado Livre

- dois encrypts do mesmo JSON produzem ciphertexts diferentes;
- alteração de um byte falha;
- trocar `organization_id` no AAD falha;
- chave/versionamento incorretos falham;
- rotação mantém leitura da versão anterior até recriptografar;
- login e link usam somente a sessão da organização;
- chamada sem organização não escolhe arquivo global/mais recente;
- volume, banco, logs e Sentry não contêm cookies ou storage state plaintext;
- erro de decrypt não devolve vazio e segue: bloqueia a conexão e alerta.

### WhatsApp

- token de A não opera sessão B;
- ação `status` não autoriza `send` ou `logout`;
- token expirado, futuro, issuer/audience errados e algoritmo inesperado falham;
- `jti` repetido em reset/logout falha;
- retry do envio com mesma idempotency key não duplica mensagem;
- chamada sem token falha inclusive em status, QR e grupos;
- `/health` não retorna sessão, QR, grupo ou credencial;
- endereço público é inalcançável;
- endereço `.internal` funciona a partir do Django.

### Banco/deploy

- `APP_ENV=production` sem URL falha;
- production/staging com SQLite falham;
- development/test com SQLite funcionam;
- Postgres válido funciona;
- migration role executa schema e runtime role não;
- falha no `release_command` interrompe o deploy;
- deploy compatível lê dados durante o backfill;
- rollback para o build compatível preserva acesso.

### Degradação

- Link Builder desligado não derruba dashboard;
- WhatsApp desligado pausa somente as regras afetadas;
- Telegram/fluxo manual permitido continua;
- restart no deadline não gera publicação duplicada;
- mensagens de UI explicam o estado sem expor erro interno.

---

## 9. Gate para encerrar a fase

Todos os itens abaixo são obrigatórios:

- [ ] cadastro, convite e criação operacional de tenant externo continuam fechados;
- [ ] 100% das linhas privadas têm `organization_id` válido;
- [ ] zero órfão/ambiguidade não resolvido;
- [ ] aplicação não usa `User` como fronteira de autorização;
- [ ] RLS está `ENABLE` + `FORCE` nas tabelas privadas;
- [ ] papel runtime não é owner, superuser nem `BYPASSRLS`;
- [ ] testes cruzados HTTP, worker, serviço e SQL passam;
- [ ] fallback ML global/mais recente não existe;
- [ ] zero sessão ML plaintext ativa;
- [ ] sessões ML foram rotacionadas ou têm exceção explícita com prazo;
- [ ] WhatsApp não possui IP/porta pública utilizável;
- [ ] chave global foi removida dos dois apps;
- [ ] capacidades são curtas, escopadas e rotacionáveis;
- [ ] produção/staging falham sem Postgres;
- [ ] backup e restore foram testados;
- [ ] suíte CI e E2E passam;
- [ ] um ciclo operacional completo termina sem vazamento, perda ou duplicação;
- [ ] alertas e rollback foram ensaiados;
- [ ] decisão go/no-go foi registrada.

**Importante:** encerrar a Fase 0 não autoriza vender WhatsApp Web ou Playwright como
fundação escalável. Apenas autoriza prosseguir para a próxima fase do produto com
uma base isolada e fail-closed. A entrada de novos usuários só deve ser reavaliada
depois do soak e do fluxo permitido sem automações frágeis.

---

## 10. Métricas e alertas

| Métrica | Gate/alerta |
|---|---|
| `security_freeze_new_tenant_attempts_total` | toda tentativa gera auditoria |
| `tenant_scope_missing_total` | zero fora de testes/rotas públicas |
| `tenant_scope_denied_total` | alertar crescimento inesperado |
| `tenant_shadow_mismatch_total` | zero antes do cutover |
| `tenant_orphan_rows` | zero |
| `ml_session_legacy_read_total` | zero antes de remover compatibilidade |
| `ml_session_decrypt_failure_total` | alerta imediato |
| `ml_session_plaintext_files` | zero |
| `wa_legacy_api_key_requests_total` | zero antes de remover a chave |
| `wa_capability_denied_total` | alertar por org/IP interno |
| `wa_idempotency_replay_total` | observar; duplicação confirmada = rollback |
| `wa_public_probe_success` | sempre falso |
| `database_backend_info` | sempre `postgresql` em staging/prod |
| fila `running` acima do deadline | zero antes/depois do deploy |
| publicação duplicada | zero |

Contextos de log usam IDs opacos e nunca incluem token, cookie, QR, senha,
storage state, conteúdo de tecla ou URL com credencial.

---

## 11. Rollback por componente

| Componente | Sinal de rollback | Ação segura |
|---|---|---|
| Schema expand | migration falha | `release_command` bloqueia o deploy; corrigir e repetir |
| Backfill tenant | divergência ou órfão | parar lote, manter dual-read, corrigir mapeamento; não atribuir default |
| Read por Organization | aumento de 404/escopo ausente | voltar flag para reader compatível, mantendo dual-write |
| RLS | negação de tráfego legítimo | voltar para build compatível e corrigir policy; não trocar por role superuser |
| Sessão ML | decrypt/abertura falha | marcar apenas aquela conexão para reconexão; nunca restaurar plaintext |
| WhatsApp capability | Node rejeita chamadas válidas | voltar ao build dual-auth apenas na rede privada e por janela curta; rotacionar qualquer chave temporária |
| Rede WhatsApp | Django não alcança `.internal` | manter app privado, corrigir bind/DNS; não reabrir internet como primeira resposta |
| Postgres guard | produção não inicia | corrigir `DATABASE_URL`/`APP_ENV`; não habilitar fallback SQLite |
| Envio | risco de duplicação | manter scheduler drenado e reconciliar por idempotency key antes de retomar |

O rollback normal é de código/flag, não de dados. Migrações destrutivas e remoção
de colunas ficam fora desta janela.

---

## 12. Capacidade e responsáveis

Para caber com segurança em 10 dias úteis:

- 1 pessoa backend focada em Organization, scoping e migrações;
- 1 pessoa backend/infra focada em sessão ML, WhatsApp e Fly;
- revisão cruzada de segurança;
- um responsável operacional pelo canário e contato dos pilotos.

Com uma única pessoa, a previsão realista do próprio plano-base (2–4 semanas para
P0) deve prevalecer. O escopo não deve ser comprimido cortando RLS, migração
verificável, restore ou soak.

---

## 13. Fontes técnicas

- Plano local: [`PLANO_SAAS_VIABILIDADE.md`](PLANO_SAAS_VIABILIDADE.md)
- [Fly.io — Private Networking](https://fly.io/docs/networking/private-networking/)
- [Fly.io — Connect to an App Service](https://fly.io/docs/networking/app-services/)
- [Fly.io — App configuration e release_command](https://fly.io/docs/reference/configuration/)
- [PostgreSQL — Row Security Policies](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)
- [Django — Migrations](https://docs.djangoproject.com/en/6.0/topics/migrations/)
