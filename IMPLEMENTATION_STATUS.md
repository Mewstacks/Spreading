# Plano e estado da implementação — endurecimento estrutural do Spreading

Atualizado em 2026-08-10 (America/Sao_Paulo).

Este é o documento canônico de handoff da auditoria e do endurecimento estrutural.
Ele reúne o plano aprovado, o que foi realmente alterado no workspace, as provas já
executadas, as lacunas que ainda impedem o aceite de produção e o procedimento de
deploy/reversão. “Implementado” neste documento significa código existente e
validado localmente; não significa que a mudança já esteja publicada.

## Estado do release

**Não está em produção.** As alterações estão no workspace local sobre a branch
`main`, commit-base `7c03561`, sem push nem deploy. O deploy permanece
intencionalmente bloqueado até os builds das duas imagens e os smoke tests
autenticados em um ambiente de staging.

Status técnico local:

- implementação aditiva concluída para tenancy/WhatsApp, fila manual, fontes de
  cupons, readiness, envio v2, relatórios, telemetria e redaction;
- kill switches permanecem desligados por padrão;
- migrações são aditivas; as colunas legadas necessárias ao rollback foram mantidas;
- suíte local: **920/920 Django + 159/159 Node = 1079/1079**;
- **isolamento RLS provado em PostgreSQL real** (ver “Prova de isolamento”), o que
  fecha o bloco P0 de tenancy;
- aceite de produção ainda não concluído pelos bloqueios externos descritos abaixo.

A rodada de 2026-08-10 fez auditoria independente do handoff anterior, corrigiu duas
regressões e executou a prova de isolamento em PostgreSQL real. Detalhe na seção
“Rodada de continuação — 2026-08-10”.

## Resumo executivo por frente

| Frente do plano | Estado atual | Falta para aceite |
|---|---|---|
| Tenancy e WhatsApp | Implementado; **RLS/roles provados em PostgreSQL real** | Reconciliar volume real em modo sombra e executar canário de duas organizações |
| Fila manual/Chromium | Implementado e testado localmente | Subir processo `manual`, comprovar heartbeat/fairness/crash recovery e observar CPU/memória no ambiente Fly |
| Cupons Amazon | Implementado e testado localmente | Executar canário contra a página pública atual, validar diagnósticos/anti-bot e medir o funil real após 24 h e 7 dias |
| Cupons Mercado Livre | Implementado e testado localmente | Executar canário das fontes atuais e manter ativações desligadas até rollout explícito por organização |
| Readiness/UI | Implementado e testado localmente | Aplicar migrações/RLS e validar a UI autenticada com dados reais sem atravessar tenants |
| Envio v2/Node | Implementado atrás de kill switch | Build das duas imagens, deploy Node compatível, canário por organização, restart pré/pós-transporte e reconciliação de resultados incertos |
| Relatórios | Adaptadores/parsers implementados localmente | Homologar layouts autenticados, período e paginação com staging/fixtures saneadas antes de habilitar browsers |
| Segurança/logs | Implementado e testado localmente | Verificar logs/Sentry/diagnósticos do ambiente real e confirmar permissões/retenção dos volumes |
| Testes | 1079/1079 locais aprovados; suíte também executada em PostgreSQL real | Builds e smoke tests autenticados de staging |
| Produção | Não iniciado | Criar release imutável, backup/snapshots, cumprir gates, implantar em fases e observar antes de expandir flags |

## Fluxo auditado e taxonomia

Fluxo preservado e tornado observável:

`fonte/portal` → `IngestedItem` → `Produto`/`CupomNormalizado` → regras →
`ProdutoCupom`/`CupomPreparacao` → link por usuário → validação →
`CupomDisponibilidade` → `Publicacao` → worker Django → ledger/worker Node →
WhatsApp → confirmação/reconciliação.

As perdas deixaram de ser representadas por um único “não aparece”:

| Categoria | Significado persistido |
|---|---|
| `not_found` | item anterior ausente numa execução comprovadamente completa e saudável |
| `rejected` | item legível reprovado por regra de negócio |
| `waiting` | item válido aguardando processamento ou recurso |
| `no_session` | sessão obrigatória ausente, desconectada ou expirada |
| `no_link` | link ausente, pendente ou ainda não verificado |
| `invalid` | evidência incompleta/contraditória ou schema ilegível |
| `operational_failure` | falha de fonte, banco, browser ou transporte sem desqualificar o item |

Os estágios projetados por organização/usuário são `collected`, `eligible`,
`prepared`, `waiting_link`, `ready` e `discarded`, com razão durável e detalhe
saneado.

## Causas-raiz confirmadas

### WhatsApp

- `WhatsAppConnection.organization` já era OneToOne; portanto
  `MAX_WHATSAPP_SESSIONS=2` limita o worker inteiro e **não** autoriza dois números
  numa organização.
- A ambiguidade real vinha do legado `Perfil.wa_session`, da organização pessoal
  ter precedência e da ausência de identidade persistida nos diretórios `LocalAuth`.
- O restore lexical de diretórios podia consumir capacidade com sessão órfã. Reset
  e logout tinham path sanitizado, mas não possuíam manifest para provar propriedade.

### Fila manual

- Apenas o processo `scrape` consumia a fila manual.
- Um advisory lock único serializava tarefas HTTP/DB e tarefas Chromium sem expor
  proprietário, recurso, fila, deadline ou motivo da espera.
- Jobs ainda `queued` não possuíam lease/deadline e podiam esperar indefinidamente
  quando o consumidor desaparecia.

### Amazon

- A fonte lia somente o DOM inicialmente carregado, com seletores rígidos e
  exceções por card descartadas sem categoria.
- A coleta pública era bloqueada pela ausência de tag Amazon e o catálogo era
  multiplicado por usuário.
- A UI mostrava apenas o fim do funil, escondendo itens válidos aguardando preparo
  ou link.

### Mercado Livre

- O parser dependia de um símbolo JavaScript e schema específicos; SSR e
  `?page=` não provavam avanço real.
- Código e ativação passavam por gates de produto/link equivalentes em parte da UI,
  embora códigos válidos possam ser avisos sem associação falsa a produto.
- A ativação tinha apenas flag global/piloto e não deixava uma razão durável por
  organização.

### Envios

- Duas regressões introduzidas pela própria rodada de endurecimento — órfã de envio que
  nunca drenava com a flag no default e mídia enfileirada residente no Postgres — foram
  encontradas e corrigidas em 2026-08-10. Detalhe em “Regressões corrigidas”.
- A idempotência Node era um `Map` de dez minutos e se perdia em restart.
- Resultado perdido depois de `sendMessage` podia ser promovido a sucesso.
- `Publicacao` não registrava etapa/tentativa e o reconciliador não distinguia crash
  antes ou depois de iniciar o transporte.
- Exceções HTTP podiam persistir/encaminhar corpo sensível; a UI podia aguardar uma
  reconexão longa.

### Relatórios

- O ML dependia simultaneamente de flag, sessão de relatório e URL; a Amazon usava
  URL fixa em vez da configuração equivalente.
- O adaptador genérico não suportava XLSX nem comprovava período/paginação.
- Números ilegíveis viravam zero e datas inválidas viravam a data final do período,
  impossibilitando distinguir métrica zero legítima de parser quebrado.

### Observabilidade

- O formatter já saneava o console, mas `EventoOperacional.erro` e o envio ao
  Sentry ainda podiam receber a exceção original. A redaction agora acontece antes
  da persistência/integração.

## Hipóteses descartadas

- Não foi encontrada evidência de dois números permitidos por uma conta; a
  capacidade `2` é global.
- A fonte pública de cupons ML não estava fora do ar durante a auditoria; o defeito
  confirmado era fragilidade de parser e ausência de prova de paginação/saúde.
- Não foi encontrada exclusão direta de diretório de outra organização no reset
  anterior; faltava, porém, correlação persistente suficiente para prová-lo.
- A suíte original não estava quebrada: baseline confirmado de 848 Django + 152
  Node = 1000 testes.

## Implementação realizada

### Tenancy, WhatsApp e RLS

- `Perfil.active_organization` referencia uma organização com membership ativa;
  a organização pessoal e `wa_session` foram preservadas para compatibilidade.
- A conexão OneToOne da organização é a autoridade em runtime e possui telemetria
  segura de worker, fase, heartbeat, consistência, capacidade e identificador
  mascarado.
- Diretórios Node recebem manifest atômico sem cookies, telefone completo ou
  credenciais. Órfãos/divergentes são desabilitados ou quarentenados, nunca
  removidos automaticamente.
- Reset/logout correlacionam capability, organização, sessão, manifest e
  `instance_id`; concorrência reutiliza a operação idempotente.
- Novos modelos tenant-aware receberam políticas ENABLE/FORCE RLS. O probe verifica
  políticas, leituras legítimas, contexto forjado e, quando fornecida outra
  organização, escrita cruzada com SQLSTATE esperado `42501`.

### Fila e recursos limitados

- Processo `manual` dedicado adicionado ao `Procfile`.
- Leases PostgreSQL observáveis substituem a exclusão indiferenciada:
  `django_chromium`, `ml_site_session:<org>`, `ml_report_session:<org>`,
  `amazon_report_session:<org>` e `source_ingest:<slug>`.
- O slot global é reentrante apenas dentro do mesmo contexto de execução; não há
  reentrância entre threads, processos ou organizações.
- Todo ponto legado que abre Chromium nos scrapers ML/Amazon agora adquire o slot
  global no instante da abertura; quando usa storage state ML, adquire também a
  chave da organização. Requests web PostgreSQL falham fechado antes de tocar o
  lease `system-only`, em vez de bloquear a thread ou furar a fila.
- Os live views de login do site ML, relatórios ML e Amazon compartilham um
  semáforo na VM web, com `CHROMIUM_GLOBAL_SLOTS=1` por default; uma segunda tela
  recebe motivo acionável e nenhum segundo Chromium é aberto naquele processo.
- Heartbeat/lease, posição, worker, recurso/ocupante, razão, deadline, espera e ETA
  amostral são duráveis. ETA só existe com pelo menos dez amostras comparáveis.
- Fairness aplica dois jobs manuais para um agendado e aging após dez minutos.
- Worker ausente/stale, crash, lease perdido, retomada única e timeout terminal têm
  causas distintas. Métricas p50/p90/máximo são calculadas apenas de intervalos
  realmente observados, nunca estimadas retroativamente.

### Cupons Amazon e Mercado Livre

- Amazon coleta catálogo público uma vez, independentemente de tag; tag continua
  obrigatória no link/envio por usuário.
- Scroll/load-more progressivo, teto por páginas/itens/tempo, parada por ausência de
  novos IDs, deduplicação promoção+ASIN, seletores semânticos/estruturados e
  diagnóstico de schema foram implementados.
- Cada card Amazon gera exatamente um resultado: aceito, duplicado ou rejeitado por
  razão. Promoção, ASIN, preço atual e preço final precisam de evidência coerente.
- ML passou a registrar observações por fonte e precedência: afiliados oficial,
  SSR público, containers e integração licenciada quando configurada.
- Parsers procuram assinatura de schema/scripts JSON, detectam repetição e só
  avançam por cursor/continuation ou IDs novos.
- Código validado pode aparecer como aviso sem produto fictício; ativação continua
  exigindo container público, produto, preço e link verificados.
- Kill switch global da ativação segue desligado; overrides `enabled`, `disabled`
  e `inherit` são avaliados por organização e projetam motivo visível.
- Zero itens só é saudável com schema válido, marcador explícito de vazio e
  paginação concluída; caso contrário o catálogo anterior é preservado e a fonte
  fica degradada.
- Ausência só vira `not_found` após inventário saudável e explicitamente completo;
  limites de páginas/itens/tempo, CAPTCHA, schema ou paginação inconclusiva ficam
  `partial/degraded`. O veredito append-only sobrevive à reprojeção, é idempotente
  e volta a aceito quando a fonte observa novamente o item.
- Readiness distingue link ainda não iniciado, geração pendente, verificação
  pendente e rejeição. Somente `verificado_ok=True` promove uma ativação para
  `ready`; expiração/reprovação conhecida nunca é reclassificada como ausência.
- Observações privadas de fonte agora persistem `organization_id` e usam chaves
  únicas condicionais separadas para catálogo público e por tenant. A mesma
  identidade externa em duas organizações não colide nem atualiza o outro tenant.
- Diagnósticos de fonte pública são saneados, gravados com permissão restrita e
  retidos por sete dias. Portais autenticados não salvam DOM/screenshot.

### Pipeline de envio

- Máquina de estados persistida:
  `selected → reserved → composing → price_revalidation → media_preparation →`
  `transport_queued → transport_started → confirmation_pending → confirmed`.
- Terminais: `rejected`, `permanent_failed`, `cancelled` e `uncertain`.
- `PublicacaoEvento`/`PublicacaoTentativa` formam trilha append-only com etapa,
  duração, tentativa, próximo retry e razão segura.
- Chave de entrega é única por organização/canal/destino/publicação.
- Ledger Node atômico no volume não armazena mensagem nem mídia. Reinício devolve o
  resultado conhecido; transporte iniciado sem resposta fica `uncertain` e nunca
  é reenviado automaticamente.
- Retry existe somente antes de transporte confirmado, com até cinco tentativas e
  exponential backoff com full jitter de 30 s a 15 min, serializado por
  sessão/destino.
- Destino, texto de até 4096 caracteres, base64 estrito, MIME/magic, imagem
  decodificável e mídia de até 16 MiB são validados sem truncamento silencioso.
- Endpoints web enfileiram quando o rollout v2 está habilitado para a organização;
  falhas transitórias de infraestrutura não pausam regras permanentemente.
- Avisos agregados de código escolhem marketplace/link/mensagem a partir do
  primeiro cupom realmente aceito, evitando que uma entrada inválida ou de outro
  marketplace contamine o lote.

### Relatórios

- Pré-requisitos são tipados por marketplace (`not_configured`, sessão ausente,
  login expirado e outros) antes da abertura do browser.
- Sessões de site, afiliados/Link Builder e relatórios permanecem separadas.
- Adapters Amazon e ML separados usam URLs configuradas, validam redirect/login,
  período aplicado, paginação/exportação e cabeçalhos normalizados.
- CSV, TSV, XLSX read-only e tabelas HTML são suportados.
- Células preservam estado válido/vazio/inválido; zero só persiste quando schema e
  célula são válidos. Persistência usa chave estável por usuário, marketplace,
  relatório, período e linha.
- Os adapters autenticados ainda precisam ser comprovados contra os layouts reais
  de staging; nenhum seletor ou métrica foi declarado válido sem essa prova.

## Arquivos alterados e razões

### Node/WhatsApp

- `node.js/capability_auth.js`, `node.js/index.js`, `node.js/payloads.js` e
  `node.js/session_policy.js`: capabilities ampliadas, reconciliação segura,
  telemetria, validação de payload, estados incertos e reset vinculado à propriedade.
- `node.js/idempotency_ledger.js`, `node.js/session_manifest.js` e
  `node.js/safe_logging.js` (novos): ledger durável, manifests atômicos e redaction.
- `node.js/fly.toml`: diretório/retenção do ledger no volume; capacidade global `2`
  preservada.
- `node.js/package-lock.json`: atualização auditada de `js-yaml` 4.3.0 para 4.3.1;
  não houve redução de proteções.
- `node.js/test/session_policy.test.js` e `node.js/test/hardening_state.test.js`
  (novo): isolamento, órfãos/divergência, ledger/restart, timeout e resposta incerta.

### Accounts/tenancy

- `python/django/apps/accounts/models.py`, `feature_flags.py`, `rls.py` e
  `management/commands/tenant_rls.py`: organização ativa, overrides tenant-aware,
  telemetria WhatsApp e políticas RLS.
- `python/django/apps/accounts/management/commands/tenant_isolation_probe.py`:
  prova executável de ENABLE/FORCE RLS e isolamento.
- `python/django/apps/accounts/migrations/0020_perfil_active_organization_and_more.py`
  (novo): migração aditiva/backfill dos contratos.
- `python/django/apps/accounts/tests.py`: membership, sessão por organização,
  capabilities e classificação das políticas.

### Controle operacional, fila e modelos

- `python/django/apps/scrapers/models.py`, `admin.py` e
  `migrations/0059_hardening_control_plane.py` (novo): readiness, observações de
  fonte, leases/heartbeats, telemetria, feature overrides, eventos e syncs tipados.
- `python/django/apps/scrapers/migrations/0060_publicacao_v2_queue_payload.py`
  (novo): payload enfileirado e estado de transporte aditivos.
- `python/django/apps/scrapers/resource_control.py` (novo), `carga.py`,
  `manual_scraping.py`, `management/commands/automacao.py`, `monitorar.py`,
  `monitorar_canais.py`, `saude.py` e `maintenance.py`: leases granulares,
  worker dedicado, fairness, recovery, métricas medidas e reconciliação.
- `python/Procfile`: processo `manual` dedicado.
- `python/fly.toml`: release command faz check, migrate e aplicação idempotente das
  políticas RLS com a role de migração.

### Fontes, cupons e readiness

- `python/django/apps/scrapers/sources/amazon_coupons.py`, `amazon_public.py`,
  `ml_public_coupons.py`, `persistence.py` e `registry.py`: paginação/scroll,
  parsers resilientes, métricas/rejeições, saúde, precedência e locks exatos.
- `python/django/apps/scrapers/source_diagnostics.py` (novo): snapshots somente de
  páginas públicas, saneados e com retenção curta.
- `python/django/apps/scrapers/coupon_pipeline.py`, `coupon_products.py`,
  `coupon_rules.py` e `coupon_readiness.py` (novo): coleta independente de tag,
  distinção código/ativação, regras estritas e projeção tenant-aware.
- `python/django/apps/scrapers/marketplaces/amazon.py`, `mercadolivre.py`,
  `scraper_mercadolivre/cupons_codigo_scraper.py`, `cupons_container.py`, `link.py`,
  `ofertas_scraper.py` e `scraper.py`: evidência, cursor/IDs novos, containers e
  sessão/browser isolados.
- `python/django/apps/scrapers/content_ranking.py` e `tenant_signals.py`: consumo da
  projeção e invalidação coerente por tenant.

### Envio, relatórios, UI e logging

- `python/django/apps/scrapers/send_pipeline.py` (novo), `ofertas.py`,
  `whatsapp_client.py`, `conexoes.py` e `monitor_conexao.py`: máquina de estados,
  enqueue tenant-aware, idempotência, resposta incerta, telemetria e erros seguros.
- `python/django/apps/scrapers/ml_live_transport.py`, `ml_conexao.py`,
  `ml_relatorio_conexao.py` e `amazon_conexao.py`: capacidade comum dos live views,
  semáforo fail-fast e mensagem acionável sem misturar sessões de portal.
- `python/django/apps/scrapers/relatorios.py` e `report_sessions.py`: adapters,
  pré-requisitos, período, paginação, formatos e validação tipada.
- `python/django/apps/scrapers/eventos.py` e `python/django/core/logging.py` (novo):
  redaction antes de log, banco e Sentry.
- `python/django/apps/scrapers/views.py`, `templates/home.html`,
  `templates/scrapers/dashboard.html`, `templates/scrapers/top_promocoes.html` e
  `templates/scrapers/superadmin/saude.html`: estados/motivos, contadores do funil,
  fila/worker e falhas de envio sem mudanças cosméticas amplas.
- `python/django/core/settings.py`: defaults seguros e URLs efetivamente usadas.
- `python/requirements.txt`: `openpyxl==3.1.5` para XLSX read-only.

### Rodada de continuação (2026-08-10)

- `python/django/apps/scrapers/maintenance.py`: correção de D1 e D2 — reagendamento de
  órfã condicionado a existir consumidor da fila v2, contagem de tentativa ao reagendar
  e limpeza de `queued_media` em todo desfecho terminal.
- `python/django/apps/scrapers/test_structural_hardening.py`: nova classe
  `OrphanPublicationDrainTests` (4 testes) — fechamento em um ciclo sem consumidor e sem
  reciclar evento, retomada com contagem até esgotar o teto, e liberação da mídia nos
  dois desfechos terminais.
- `python/django/apps/scrapers/tests.py`: `PublicacaoOrfaTests` passou a declarar
  explicitamente o estado da flag que exercita. O teste anterior consagrava o
  reagendamento sem verificar que alguém terminava a linha; virou um par —
  `..._quando_ha_fila` e `..._sem_fila_que_a_retome_e_fechada`.

### Testes

- `python/django/apps/scrapers/test_structural_hardening.py` e
  `test_hardening_ui_e2e.py` (novos): concorrência, tenancy, fila, fontes, envio,
  relatórios, redaction e UI móvel/desktop.
- `python/django/apps/scrapers/tests.py`, `test_sources.py`,
  `test_coupon_pipeline.py`, `test_coupon_products.py`, `test_manual_scraping.py` e
  `test_recovery.py`: cenários adversos integrados aos contratos existentes.

## Configuração

Novas/ativadas, com defaults seguros:

| Variável | Default | Observação |
|---|---:|---|
| `AMAZON_COUPON_MAX_PAGES` | `5` | teto de páginas/continuações |
| `AMAZON_COUPON_MAX_ITEMS` | `500` | teto de itens deduplicados |
| `AMAZON_COUPON_MAX_SECONDS` | `180` | deadline da fonte |
| `CHROMIUM_GLOBAL_SLOTS` | `1` | exclusão global continua unitária |
| `MANUAL_QUEUE_NO_WORKER_TIMEOUT_SECONDS` | `600` | falha acionável sem consumidor |
| `MANUAL_QUEUE_MAX_WAIT_SECONDS` | `2700` | espera máxima da fila |
| `SEND_PIPELINE_V2_ENABLED` | `0` | rollout opt-in por organização |
| `SEND_RETRY_BASE_SECONDS` | `30` | base do jitter |
| `SEND_RETRY_MAX_SECONDS` | `900` | teto do backoff |
| `SEND_MAX_ATTEMPTS` | `5` | máximo antes do terminal |
| `WA_SEND_LEDGER_DIR` | `/app/.wwebjs_auth/.send-ledger` | no volume Node |
| `WA_SEND_LEDGER_RETENTION_HOURS` | `168` | sete dias |
| `WA_SESSION_RECONCILE_SECONDS` | `60` | cache da reconciliação Django↔Node |
| `SCRAPER_DIAGNOSTIC_RETENTION_DAYS` | `7` | somente fontes públicas |
| `AMAZON_BROWSER_REPORTS_ENABLED` | `0` | rollout explícito |

`AMAZON_ASSOCIATES_REPORT_URL` agora é efetivamente consumida e continua vazia por
padrão (`not_configured`). Permanecem inalterados e seguros:
`MAX_WHATSAPP_SESSIONS=2`, `ML_CUPONS_ATIVACAO_ENABLED=0` e o slot Chromium único.
Nenhuma variável nova deve conter token em log, métrica, teste ou snapshot.

## Rodada de continuação — 2026-08-10

Auditoria independente do handoff, correção de duas regressões e execução da prova de
isolamento em PostgreSQL real.

### O que foi confirmado de forma independente

Cada validação abaixo foi reexecutada, não copiada do checkpoint anterior. O inventário
de 81 caminhos bate com o workspace, e o código corresponde ao que o documento declara:
nenhuma frente listada como implementada estava ausente do código.

Contratos reverificados diretamente no código, além do texto:

- migrações `0059`/`0060`/accounts `0020` aditivas, sem remoção de coluna legada;
  `Perfil.wa_session` preservado;
- kill switches no default seguro; `_EXPLICIT_ROLLOUT_FLAGS` em `feature_flags.py`
  **endurece** o gate — allowlist vazia deixou de liberar o recurso para todas as contas;
- `python/fly.toml` e `node.js/fly.toml` não desligam envio nem login ML;
- `automacao.handle` é `@system_job`, então o claim da fila v2 e a limpeza de mídia
  rodam em contexto de sistema, sem cair no problema de query nua sob RLS;
- `classify_result` não classifica errado as respostas reais do worker Node: as
  permanentes mandam `classe: PERMANENTE` sem `repetir`.

### Regressões corrigidas

**D1 — órfãs de envio não drenavam com a flag no default.**
`reconciliar_publicacoes_orfas` havia trocado um desfecho terminal por um reagendamento
para `stage="transport_queued"` mantendo `status="pendente"`, sem nunca incrementar
`attempt_count`. Quem drena essa fila é `process_queued_publications`, chamado só quando
`SEND_PIPELINE_V2_ENABLED` está ligada. Com a flag no default `0` — que é também o estado
de rollback prescrito neste documento — a linha voltava a casar com o filtro a cada ciclo,
para sempre, gravando um `PublicacaoEvento` novo a cada 30 minutos, sem teto.

Correção: reagendar só quando existe consumidor, isto é, quando a linha está de fato na
fila v2 (`transport_state in QUEUE_STATES`) **e** o rollout está ligado para a organização
dona, decidido pelo gate tenant-aware já existente em `feature_flags`. Sem consumidor, o
desfecho volta a ser terminal, com `reason_code="restart_without_queue_consumer"`. Ao
reagendar, `attempt_count` é incrementado, o que torna o teto `SEND_MAX_ATTEMPTS`
alcançável — o ramo `retry_exhausted` era inalcançável por este caminho.

**D2 — bytes de mídia ficavam no Postgres indefinidamente.**
`Publicacao.queued_media` guarda a imagem (até 16 MiB, replicada por linha do lote) e só
era limpo no caminho feliz do worker. Publicações finalizadas pelo reconciliador — ou
paradas pela D1 — mantinham os bytes para sempre.

Correção: todo desfecho terminal do reconciliador limpa `queued_media` e
`queued_media_mime` no mesmo `save()`.

Cobertura nova, em `OrphanPublicationDrainTests`: fechamento em um ciclo sem consumidor
e sem reciclar evento no ciclo seguinte; retomada com contagem de tentativa até esgotar o
teto; liberação da mídia nos dois desfechos terminais. `PublicacaoOrfaTests` deixou de
presumir o estado da flag e passou a exercitar explicitamente os dois lados.

### Observações registradas, sem mudança de código

- **`queued_media` é imagem dentro do Postgres.** `queue_publications` grava os bytes em
  **cada** linha do lote, então um aviso agregado para N cupons replica a mesma imagem N
  vezes. Está limitado a 16 MiB por linha e agora é liberado em todo desfecho terminal,
  mas continua sendo carga de banco por design. Vale medir o crescimento da tabela no
  canário do envio v2 antes de expandir o rollout.
- **`_advance` pressupõe etapa não terminal.** `send_pipeline._advance` faz
  `STAGES.index(current.stage)`, que levanta `ValueError` se a publicação já estiver num
  terminal. Hoje os chamadores garantem isso; se um caminho novo chamar `begin_transport`
  sobre linha terminal, o erro será obscuro.

### Prova de isolamento em PostgreSQL real

Cluster PostgreSQL 17.10 efêmero e local, criado só para a prova, com três roles
separadas e **nenhum dado de produção**. Nenhuma credencial ou conta real foi usada.

| Verificação | Resultado |
|---|---|
| Roles `spreading_runtime` / `system` / `migration` | criadas; runtime com `rolsuper=false` e `rolbypassrls=false` |
| `migrate` com a role de migração | todas as migrações aplicadas, incluindo `0059`/`0060` |
| `tenant_rls --enable` | **RLS habilitado e forçado em 34 tabelas** |
| ENABLE + FORCE RLS nas tabelas novas | 9 de 9 com `relrowsecurity` e `relforcerowsecurity` |
| `tenant_isolation_probe` com duas organizações | **aprovado**, com escrita cruzada bloqueada |
| Leitura cruzada em SQL puro, role runtime | 0 linhas do outro tenant; 1 do próprio; 0 nas tabelas system-only |
| Escrita cruzada em SQL puro, role runtime | `ERROR: 42501: new row violates row-level security policy` |

Dois guards do `tenant.py` foram exercitados de verdade no caminho, e recusaram como
deviam: a role runtime não abre contexto cross-tenant, e a role de sistema é rejeitada
quando é dona das tabelas. Só a topologia correta — tabelas da role de migração, worker
conectando como role de sistema — passa pelos dois.

Limite honesto do cluster local: ele é local e sintético. Repetir o probe no PostgreSQL
de staging, onde as três roles são provisionadas de verdade pela infraestrutura, continua
pendente e está listado no P0.

Para reproduzir, sem nenhum segredo no comando:

```bash
python3 manage.py tenant_isolation_probe --organization-id <ORG_A> --other-organization-id <ORG_B>
```

### A suíte completa ainda não roda de ponta a ponta em PostgreSQL

Este é o único item P0 de isolamento que **não** ficou verde, e a causa é do harness de
teste, não do endurecimento. Registrado aqui para não ser confundido com aprovação.

Resultado no PostgreSQL real, com a topologia de produção (tabelas da role de migração,
suíte conectando como role de sistema, via base pré-criada e `--keepdb`):

**920 testes, 872 aprovados, 48 com erro/falha.**

Diagnóstico das 48, feito caso a caso e não por amostragem:

- 46 vêm de `connections.close_all()`, que é comportamento **pré-existente** e deliberado
  de produção — a raspagem passa minutos no browser sem tocar o banco, e o proxy da Fly
  derruba a conexão ociosa, então o scraper a descarta de propósito. Sob `TestCase`, que
  envolve tudo numa transação na conexão persistente, esse descarte mata a conexão do
  próprio teste e a query seguinte falha com “the connection is closed”. A chamada já
  existe no commit-base `7c03561`, antes de qualquer mudança desta auditoria;
- as demais são efeitos colaterais das primeiras, como um `ResourceLease` que ficou
  ocupado porque o `release()` não conseguiu rodar na conexão morta.

Em SQLite nada disso aparece, porque fechar e reabrir a conexão em memória é inócuo.

Consequências, sem atalho:

- **nenhuma das 48 está nas classes de tenancy, RLS, fila ou envio**, e nenhuma está nas
  classes alteradas nesta rodada;
- a prova de isolamento RLS não depende dessas 48: foi feita fora do runner, no banco
  aplicado pela role de migração, com SQL direto e com o `tenant_isolation_probe`;
- tornar a suíte inteira executável em PostgreSQL exige trabalho de harness — isolar o
  `close_all()` sob teste, ou migrar as classes afetadas para `TransactionTestCase`. É
  trabalho legítimo, fora do escopo desta auditoria, e está registrado como pendência.

Um primeiro run havia acusado 198 falhas por erro meu de ambiente, e não do código: o
perfil forçava `DJANGO_DEBUG=0`, o que liga `SECURE_SSL_REDIRECT` e faz todo GET do test
client virar 301, deixando `response.context` nulo. Corrigido o perfil, o número caiu para
as 48 acima.

## Validações executadas

Reexecutadas em 2026-08-10, já com as correções desta rodada:

| Comando | Resultado |
|---|---|
| `APP_ENV=test python3 manage.py test --verbosity 1` | **920/920, OK**, 98,4 s |
| `npm test` | 159/159, OK |
| `npm run audit:production` | 0 vulnerabilidades |
| `APP_ENV=test python3 manage.py makemigrations --check --dry-run` | nenhuma mudança detectada |
| teste Playwright | 1/1 dentro da suíte, mobile 390×844 e desktop 1440×900 |
| `python3 -m compileall -q .` | OK |
| `python3 manage.py check --deploy --fail-level WARNING` com perfil sintético de staging | 0 issues |
| `flyctl config validate --config fly.toml` (Django e Node) | ambos válidos |
| `git diff --check` | OK |
| `migrate` + `tenant_rls --enable` em PostgreSQL 17 real | OK; RLS forçado em 34 tabelas |
| `tenant_isolation_probe` com duas organizações | aprovado; escrita cruzada = `42501` |

O perfil sintético usa somente chaves descartáveis e URLs PostgreSQL não conectadas;
ele prova configuração/imports/checks, não conectividade nem migrations de staging.

### Métricas antes/depois

- Cobertura executada: 1000 testes no baseline → 1074 na rodada do Codex → **1079
  agora** (+79 cenários sobre o baseline).
- Métricas operacionais históricas de cards, fila, envios e relatórios: **baseline
  não disponível**, porque o sistema anterior não persistia todos esses estágios e
  motivos. Nenhum número foi reconstruído ou estimado.
- A comparação operacional deve ser feita em sombra após 24 horas e sete dias,
  usando as mesmas organizações e janelas.

## Validações ainda bloqueadas pelo ambiente

- Builds das imagens: não há runtime Docker/Podman local. Os `fly.toml` foram
  validados, mas isso não substitui o build.
- Smoke tests autenticados: não existe ambiente/credencial de staging acessível
  nesta tarefa. Não serão usados portais ou contas de produção como substituto.
- Layouts autenticados dos relatórios Amazon/ML: precisam de fixtures saneadas
  capturadas em staging; cookies/storage state nunca devem entrar no repositório.

## Trabalho ainda necessário

Esta seção é a fila restante. Nenhum item marcado como pendente deve ser tratado
como concluído apenas porque o código correspondente existe localmente.

### P0 — formar um release reproduzível

- [x] Revisar os 81 caminhos modificados/não rastreados e confirmar que não há
      mudança alheia à auditoria nem artefato sensível. Revisado em 2026-08-10:
      nenhum segredo, binário ou artefato no diff; as únicas dependências novas são
      `js-yaml` 4.3.0→4.3.1 e `openpyxl==3.1.5`; `git diff --check` limpo e sem
      line endings mistos nos arquivos novos.
- [x] Revisar as duas migrações Django novas e seu SQL, incluindo índices,
      constraints condicionais e backfill. São aditivas (`AddField`/`CreateModel`),
      não removem coluna legada, e o `operation_key` não pode colidir porque
      `Publicacao.id_publico` já é `unique=True`. **Duração medida do backfill: 16,4 s
      para 100.000 publicações** em PostgreSQL 17 local com SSD — ver a ressalva de
      release_command abaixo.
- [ ] Confirmar a contagem real de `scrapers_publicacao` em produção e decidir se o
      backfill da `0059` continua dentro do `release_command`. A `RunPython` faz um
      `UPDATE` por linha; a 16,4 s/100 mil linhas medidos localmente, uma tabela de
      1 milhão de linhas passa de 2,5 min só de backfill, e a VM da Fly é mais lenta
      que o host da medição. Se a contagem for alta, mover o preenchimento para um
      comando pós-deploy e deixar só o DDL na migração.
- [ ] Commitar no `main` com tag de release; registrar o SHA e não fazer deploy a
      partir do worktree sujo.
- [x] Executar novamente:
      `APP_ENV=test python3 manage.py test --verbosity 1`, `npm test`,
      `npm run audit:production`, `git diff --check` e
      `APP_ENV=test python3 manage.py makemigrations --check --dry-run`.
- [ ] Construir as imagens Django e Node pelo builder remoto Fly, registrar os
      digests e testar que ambas iniciam com os processos declarados.

### P0 — pré-condição de secrets do novo `release_command` (nova, encontrada em 2026-08-10)

O `release_command` do `python/fly.toml` passou a rodar com `RELEASE_COMMAND=1` e a
executar `check --deploy` antes do `migrate`. Isso cria dependências bloqueantes que o
release_command antigo não tinha: **sem elas o deploy aborta no release, antes de
qualquer migração.**

- [ ] Confirmar no secret store da Fly, sem imprimir valores, que existem:
      `MIGRATION_DATABASE_URL` (obrigatória sob `RELEASE_COMMAND=1`, com
      `ImproperlyConfigured` explícito), `SYSTEM_DATABASE_URL`,
      `TENANT_CONTEXT_SIGNING_KEY` com pelo menos 256 bits, `ML_SESSION_KEKS_JSON`
      no formato `{"v1":"<base64 de 32 bytes>"}`, `WA_CAPABILITY_PRIVATE_KEY` e
      `PILOT_ORGANIZATION_IDS` não vazio.
- [ ] Confirmar que a role de `MIGRATION_DATABASE_URL` é dona das tabelas — é ela
      que aplica DDL e `tenant_rls --enable`.

### P0 — provar isolamento e segurança no PostgreSQL real ✅ concluído em 2026-08-10

Executado num cluster PostgreSQL 17.10 efêmero e local, sem nenhum dado de produção.
Detalhe e evidência em “Prova de isolamento em PostgreSQL real”.

- [x] Disponibilizar banco efêmero ou staging PostgreSQL com as roles separadas de
      runtime, sistema e migração.
- [x] Confirmar que a role runtime não possui `BYPASSRLS`, superuser ou ownership
      que torne `FORCE RLS` ineficaz.
- [x] Aplicar migrações e `tenant_rls --enable` nesse banco.
- [~] Rodar a suíte Django completa com `DATABASE_URL` real. Executada: **872/920
      aprovados**. As 48 restantes são artefato de harness — `connections.close_all()`,
      pré-existente ao commit-base, mata a conexão do `TestCase`. Nenhuma delas está em
      tenancy, RLS, fila ou envio. Ver “A suíte completa ainda não roda de ponta a ponta
      em PostgreSQL”.
- [x] Rodar `tenant_isolation_probe` com duas organizações e provar leitura e
      escrita cruzadas negadas em sessões, links, fila, disponibilidade,
      publicações, tentativas/eventos e relatórios.
- [x] Capturar somente resultados/SQLSTATE; não registrar strings de conexão,
      capabilities ou dados de tenant nos artefatos.

Continua pendente, porque depende do ambiente real e não do cluster local:

- [ ] Repetir a prova no PostgreSQL de staging/produção, onde as três roles são
      provisionadas de verdade e a role runtime não é dona das tabelas.

### P0 — preparar recuperação antes do primeiro deploy

- [ ] Confirmar backup consistente do PostgreSQL e testar restauração em ambiente
      separado.
- [ ] Criar snapshots dos volumes que contêm sessões ML/WhatsApp, manifests e
      ledger; nunca copiar esses dados para fixtures ou para o repositório.
- [ ] Registrar as imagens atualmente saudáveis para rollback:
      Django `deployment-01KZFTPRBXYPT4BW1N7JVFEGN6` e Node
      `deployment-01KZFTPHFZW4PPYTRZ3YWD7CYV`.
- [ ] Confirmar que a reversão de aplicação não depende de desfazer migrações
      aditivas e que as colunas legadas continuam disponíveis.

### P1 — staging e integrações reais

- [ ] Configurar no secret store, sem imprimir valores, as URLs de relatório,
      keyrings/capabilities e demais pré-requisitos exigidos pelo staging.
- [ ] Executar `python3 manage.py check --deploy` usando a configuração efetiva de
      staging; o check sintético já executado não substitui essa prova.
- [ ] Executar smoke autenticado Django/Node e validar `/health`, processos,
      assinatura de capabilities, relógio/TTL e comunicação interna.
- [ ] Capturar fixtures saneadas de CSV, TSV, XLSX e HTML dos layouts reais Amazon
      e ML; substituir identificadores por sintéticos e excluir cookies, URLs
      assinadas, tokens e storage state.
- [ ] Provar login expirado, aplicação do período, paginação/exportação,
      cabeçalhos alterados, zero legítimo e célula ilegível nos dois portais.
- [ ] Executar Playwright autenticado em mobile e desktop para a fila, funil de
      cupons, estados WhatsApp e dashboard de falhas.

### P1 — canários operacionais

- [ ] Reconciliar WhatsApp em modo sombra: comparar banco, manifests e volume;
      adotar apenas correspondência inequívoca e quarentenar divergências.
- [ ] Testar duas organizações e dois usuários na mesma organização; confirmar
      uma sessão ativa por organização, capacidade global `2` e reset/logout
      idempotente sem tocar a outra sessão.
- [ ] Subir o processo `manual` sem elevar slots Chromium e rodar jobs concorrentes:
      lock ocupado, worker stale, crash antes/depois do claim, lease perdido,
      retomada única, starvation e deadline terminal.
- [ ] Rodar fontes Amazon/ML como canário, validar balanços completos e confirmar
      que CAPTCHA/schema/paginação inconclusiva degradam a fonte sem apagar o
      catálogo anterior.
- [ ] Validar links para dois usuários de uma mesma organização e para duas
      organizações; nenhuma tag ou destino afiliado pode ser reutilizado entre
      tenants.
- [ ] Implantar o Node compatível antes do Django e verificar o ledger no volume.
- [ ] Habilitar `SEND_PIPELINE_V2_ENABLED` apenas em uma organização; provocar
      restart antes e depois de `transport_started`, timeout e resposta perdida e
      comprovar um único efeito de envio.

### P2 — dívida de harness de teste (nova, encontrada em 2026-08-10)

- [ ] Tornar a suíte executável de ponta a ponta em PostgreSQL: isolar
      `connections.close_all()` sob teste (ou mover as classes afetadas para
      `TransactionTestCase`), de modo que o descarte de conexão que existe por causa do
      proxy da Fly não mate a transação do `TestCase`. Hoje 48 de 920 falham só por isso,
      e a suíte só é verde em SQLite.

### P2 — observação e expansão controlada

- [ ] Registrar o primeiro baseline real dos novos contadores. Para séries que não
      existiam antes, declarar “baseline não disponível”.
- [ ] Comparar as mesmas organizações/janelas após 24 horas e sete dias: funil de
      cupons, espera por motivo, confirmações/incertezas de envio e linhas de
      relatório aceitas/rejeitadas.
- [ ] Observar CPU, memória, volume, duração de Chromium, CAPTCHA/anti-bot, uso de
      capacidade WhatsApp e crescimento do ledger/diagnósticos.
- [ ] Expandir envio v2 e relatórios organização por organização apenas depois dos
      canários. Manter `ML_CUPONS_ATIVACAO_ENABLED=0` até rollout próprio.
- [ ] Remover `Perfil.wa_session` e outros contratos legados somente em release
      posterior, após período de compatibilidade e migração comprovada.

### Critério para declarar concluído

O trabalho só pode ser marcado como concluído quando todos os P0 e P1 acima forem
executados com evidência, os critérios de aceite em produção estiverem marcados e
o resultado do rollout inicial for observado. Aprovação da suíte local, isolada,
não autoriza publicação nem expansão de feature flags.

**Em 2026-08-10 o projeto continua NÃO pronto para produção.** O bloco P0 de isolamento
foi fechado com prova em PostgreSQL real, mas seguem pendentes os builds das imagens, os
smoke tests autenticados de staging, a conferência dos secrets exigidos pelo novo
`release_command`, a decisão sobre o backfill da `0059` e todos os canários P1.

## Plano de deploy

1. Criar backup do banco, snapshot dos volumes ML/WhatsApp e baseline operacional;
   confirmar as três roles PostgreSQL sem `BYPASSRLS` indevido.
2. Configurar URLs, keyrings e signing keys pelo secret store; manter envio v2,
   relatórios browser e ativação ML desligados.
3. Subir primeiro o Node compatível, validar `/health`, capacidade `2`, manifests,
   quarentena e ledger sem iniciar segunda sessão por organização.
4. Subir Django em staging; o release executará `check --deploy`, migrações
   aditivas e `tenant_rls --enable`.
5. Executar `tenant_isolation_probe` com duas organizações e tentar leitura/escrita
   cruzada usando a role runtime; exigir SQLSTATE `42501` para escrita.
6. Confirmar heartbeat dos processos `manual`, `scrape`, `scrapeflash`, `cupons`,
   `links`, `relatorios`, `senders` e `monitor` sem abrir mais de um Chromium global.
7. Rodar job manual canário: observar posição, razão de espera, lock, heartbeat,
   deadline e conclusão/retomada.
8. Rodar fontes canário e comparar balanços `vistos = aceitos + duplicados +
   rejeitados`; comprovar CAPTCHA/schema/zero degradado sem apagar catálogo anterior.
9. Validar links em dois usuários de uma mesma organização e em duas organizações;
   nenhuma tag, sessão, link, readiness ou publicação pode cruzar tenants.
10. Habilitar envio v2 somente numa organização, testar restart antes/depois do
    transporte e exigir um único efeito. Expandir somente após observar incertezas.
11. Homologar relatórios contra exports saneados e staging autenticado; ativar por
    organização. Ativação ML continua globalmente desligada até rollout separado.
12. Comparar métricas após 24 horas e sete dias e verificar CPU, memória, anti-bot,
    fila, redaction e uso do volume antes de cada expansão.

## Reversão

Desligar `SEND_PIPELINE_V2_ENABLED` é seguro para publicações já enfileiradas: desde a
correção da D1, o reconciliador fecha a órfã que não tem mais consumidor em vez de
deixá-la `pendente` indefinidamente, e libera a mídia enfileirada junto.

1. Desligar `SEND_PIPELINE_V2_ENABLED`, os adapters browser de relatório e a
   projeção/rollouts por organização.
2. Reimplantar as imagens anteriores sem reverter/apagar migrações aditivas.
3. Preservar manifests, ledger, eventos, catálogo anterior e diretórios
   quarentenados para reconciliação posterior.
4. Não remover `Perfil.wa_session`, `Perfil.organization`, eventos ou colunas novas
   nesta janela; remoção contratual pertence a um release posterior.
5. Se a fila nova falhar, parar somente o processo `manual` novo e voltar o
   consumidor compatível, sem liberar dois Chromiums ou apagar jobs.

## Checklist de aceite em produção

- [ ] Backup/snapshots confirmados e reversão ensaiada em staging.
- [ ] Migrations aplicadas; ENABLE/FORCE RLS e roles provadas em PostgreSQL real.
- [ ] Duas organizações não conseguem ler/escrever sessões, links, fila,
      readiness, publicações ou relatórios uma da outra.
- [ ] Uma organização mantém no máximo uma sessão WhatsApp ativa; órfãos não
      consomem capacidade e reset concorrente não toca outra organização.
- [ ] Nenhum job manual fica `queued` além do deadline sem motivo/ação.
- [ ] Fairness e slot Chromium único comprovados sob concorrência.
- [ ] Amazon/ML apresentam balanço completo de aceites e perdas; zero degradado
      preserva catálogo anterior.
- [ ] Código ML válido aparece sem produto falso; ativação não aparece/envia sem
      produto, preço e link comprovados.
- [ ] Crash/retry antes e depois do transporte não duplica envio.
- [ ] Zero de relatório só persiste com célula e schema válidos.
- [ ] Logs/snapshots não contêm capability, cookie, storage state, base64, telefone
      completo, token ou credencial.
- [ ] CPU, memória, volume e bloqueios anti-bot estão dentro dos limites observados;
      nenhuma concorrência foi aumentada cegamente.

---

## Apêndice A — plano-base aprovado

Esta é a versão consolidada do plano de endurecimento que orientou a implementação.
O estado factual de cada item está nas seções anteriores; portanto, verbos no
futuro deste apêndice descrevem o plano original e não uma declaração de conclusão.

### 1. Diagnóstico confirmado

#### Fluxo original auditado

`fonte pública/portal` → `IngestedItem` → persistência em
`Produto`/`CupomNormalizado` → normalização e regras →
`ProdutoCupom`/`CupomPreparacao` → link por usuário → validação → filtros das views
→ `Publicacao` → sender Django → worker Node/WhatsApp.

Constatações que deram origem ao plano:

- WhatsApp:
  - `WhatsAppConnection` já impunha uma conexão por organização.
  - `MAX_WHATSAPP_SESSIONS=2` era capacidade global do worker, não permissão para
    dois números por conta.
  - O conflito estava no legado `Perfil.wa_session`, na precedência da organização
    pessoal e na modelagem OneToOne de `Perfil.organization`.
  - `restaurarSessoesDoVolume()` restaurava diretórios `LocalAuth` em ordem lexical
    sem manifest que vinculasse diretório, organização e conexão.
  - Reset/logout já usavam caminho sanitizado e capability vinculada à sessão, mas
    faltava correlação persistente para provar propriedade antes da remoção.
- Fila manual:
  - O consumidor manual existia apenas no processo `scrape`.
  - `carga.py` usava um único advisory lock para todas as operações pesadas sem
    proprietário, recurso ou motivo persistido.
  - Havia heartbeat/recovery para execução iniciada, mas jobs `queued` não tinham
    deadline, worker, lease, posição, motivo ou recuperação sem consumidor.
  - A VM de 2 vCPU compartilhadas/2 GB não permite aumentar Chromium cegamente.
- Amazon:
  - A fonte lia apenas o DOM inicial, com seletores rígidos e exceções por card
    descartadas sem motivo.
  - A coleta pública não rodava sem usuário com tag e o catálogo era duplicado por
    usuário.
  - A UI mostrava somente o fim do funil.
- Mercado Livre:
  - A fonte pública estava acessível durante a auditoria; a fragilidade confirmada
    era depender de um único símbolo `COUPONS` e schema.
  - SSR e paginação por `?page=` não provavam avanço real.
  - O envio agregado de códigos sem produto já existia; listagem e envio individual
    ainda passavam pelo gate de produto/link próprio de ativação.
  - A flag de ativação era global/piloto e não gerava motivo durável por organização.
- Envios:
  - A idempotência Node era um `Map` em memória de dez minutos, perdido em restart.
  - Frame recarregado depois de `sendMessage` podia voltar como sucesso incerto.
  - `Publicacao` não possuía etapas/tentativas; pendências antigas eram tratadas sem
    distinguir se o transporte havia começado.
  - Requests web podiam aguardar até 20 segundos por reconexão WhatsApp.
- Relatórios:
  - ML exigia flag, sessão própria e URL; Amazon ignorava a setting equivalente e
    usava URL fixa.
  - O parser genérico não comprovava período/paginação e não suportava XLSX.
  - Conteúdo ilegível era convertido em zero/data final, mascarando parser quebrado.
  - Layouts reais autenticados precisariam ser homologados com staging e fixtures
    saneadas, nunca com credenciais no repositório.
- Testes:
  - Baseline limpo confirmado: 848 Django + 152 Node = 1000/1000.
  - A lacuna era a ausência de falhas operacionais/concorrência real e RLS executada
    em PostgreSQL, não uma suíte baseline quebrada.

### 2. Contratos, dados e interfaces planejados

#### Tenancy e WhatsApp

- Manter `WhatsAppConnection.organization` como autoridade OneToOne.
- Adicionar `Perfil.active_organization` como ForeignKey validada por `Membership`,
  preservando a organização pessoal para compatibilidade e rollback.
- Fazer `organization_for_user()` preferir a organização ativa quando a membership
  estiver ativa, com fallback para a organização pessoal.
- Parar de ler `Perfil.wa_session` em runtime após backfill e manter a coluna por
  uma versão de compatibilidade.
- Criar manifest atômico por `LocalAuth` com `organization_id`, `instance_id` e
  versão, sem cookies, telefone completo ou credenciais.
- Adotar diretórios sem manifest apenas quando houver correspondência inequívoca;
  desabilitar/quarentenar órfãos e divergentes, sem apagar automaticamente.
- Estender `WhatsAppConnection` com worker, fase, identificador mascarado, último
  evento/heartbeat, indisponibilidade, uso/capacidade e consistência.
- Adicionar capabilities internas curtas para reconciliação e consulta de envio.
- Vincular reset/logout à organização e sessão exatas.

#### Estados e taxonomia

Criar projeção RLS `CupomDisponibilidade` por organização/usuário/canal, com
histórico de transições:

- Estágios: `collected`, `eligible`, `prepared`, `waiting_link`, `ready`,
  `discarded`.
- Categorias:
  - `not_found`: item anterior ausente em execução comprovadamente completa e
    saudável;
  - `rejected`: item legível reprovado por regra de negócio;
  - `waiting`: item válido aguardando processamento ou recurso;
  - `no_session`: sessão exigida ausente/expirada;
  - `no_link`: link ausente ou ainda não verificado;
  - `invalid`: evidência incompleta, contraditória ou schema ilegível;
  - `operational_failure`: falha de fonte, banco, browser ou transporte sem
    desqualificar o item.
- Motivos granulares: expiração, escopo, associação, feature flag, produto, preço,
  link, bloqueio e CAPTCHA.

Extensões de dados planejadas:

- `ExecucaoIngestao`: métricas, rejeições, páginas, duração por etapa, fingerprint
  de schema e saúde.
- `ExecucaoRaspagem`: posição, motivo/tempo de espera, worker, recurso, lease,
  deadline, estimativa e proprietário tipado do lock.
- `Publicacao`: operação idempotente, etapa, tentativa, próximo retry, duração e
  estado de transporte.
- `PublicacaoTentativa`/`PublicacaoEvento`: trilha append-only.
- `RelatorioSync`: pré-requisito, diagnóstico, schema, linhas vistas/aceitas/
  rejeitadas e período aplicado.
- `OrganizationFeatureOverride`: `enabled`, `disabled` ou `inherit`, sob RLS.

Interfaces planejadas:

- Fila: posição, `wait_reason`, `waiting_since`, worker/heartbeat,
  recurso/ocupante, deadline e ETA amostral.
- WhatsApp: estados distintos `global_capacity`, `organization_disconnected`,
  `recovering` e `worker_unavailable`.
- Node interno: reconciliação assinada de registry/manifests, consulta assinada de
  uma operação e health sem inventário público cross-tenant.
- Dashboard de cupons: coletados, elegíveis, preparados, aguardando link, prontos e
  descartados, com drill-down por motivo.
- Dashboard de envio: causa, etapa, tentativa, duração, próximo retry e ação.
- Todos os modelos tenant-aware sob `FORCE RLS`; tabelas globais/mistas graváveis
  somente em contexto de sistema.

### 3. Implementação planejada

#### WhatsApp e fila

- Reconciliar Django ↔ Node ↔ volume antes de restaurar Chromiums.
- No reset/logout, bloquear `WhatsAppConnection` no banco e validar capability,
  manifest e `instance_id`; operações concorrentes devem compartilhar o mesmo ID e
  resultado.
- Fazer o monitor iterar conexões/organizações, não perfis.
- Substituir o lock único por leases PostgreSQL com heartbeat:
  - `django_chromium`, capacidade global 1;
  - `ml_site_session:<org>`;
  - `ml_report_session:<org>`;
  - `amazon_report_session:<org>`;
  - `source_ingest:<slug>`.
- Adquirir recursos em ordem estável; tarefas HTTP/parsing/DB não devem adquirir
  Chromium.
- Adicionar processo `manual` dedicado, sem aumentar o número de Chromiums.
- Aplicar fairness de dois jobs manuais para um agendado e aging após dez minutos,
  sem preempção nem reacquisição infinita por uma tarefa agendada.
- Heartbeat a cada 15 s, worker stale em 90 s, alerta após 5 min, falha acionável
  após 10 min sem worker, espera máxima de 45 min e execução máxima de 45 min.
- Retomar job interrompido uma vez; a segunda perda deve ser terminal.
- Mostrar ETA somente com dez execuções comparáveis: mediana–p90 somada aos jobs
  anteriores; sem amostra, mostrar “indisponível”.

#### Cupons Amazon

- Coletar catálogo público independentemente de tag/Creators API e persistir uma
  única linha pública.
- Exigir tag somente na preparação do link/envio por usuário.
- Rolar/carregar progressivamente, usar continuation/load-more comprovado e parar
  por limite ou três rodadas sem item novo.
- Deduplicar por promoção + ASIN.
- Usar data attributes, links/ARIA e JSON estruturado como fallbacks.
- Aceitar somente promoção, ASIN, preço atual e preço final comprovados.
- Produzir um resultado mutuamente exclusivo por card, mantendo
  `vistos = aceitos + duplicados + rejeitados`.
- Não considerar link inconclusivo como verificado.

#### Cupons Mercado Livre

- Formalizar fontes e saúde individual com precedência: afiliados oficial → SSR
  público → containers públicos → integração licenciada configurada.
- Preservar observações de cada fonte e projetar o canônico pela fonte saudável de
  maior precedência.
- Localizar arrays/objetos JavaScript pela assinatura de schema e scripts JSON, sem
  depender de nome único.
- Avançar somente por cursor/continuation ou IDs novos; repetição deve degradar e
  encerrar paginação.
- Deduplicar códigos por código normalizado + escopo/container + vigência e
  ativações por campaign/container.
- Permitir código validado como aviso sem associação fictícia com produto; pronto
  para envio somente com destino afiliado válido.
- Manter ativação estrita: container público, produto comprovado, preço final e
  link verificado.
- Preservar `ML_CUPONS_ATIVACAO_ENABLED=0` como kill switch. Com global ligado,
  `disabled` por organização vence; `enabled` libera; `inherit` libera somente
  piloto; ausência de piloto fica desabilitada.
- Considerar zero saudável somente com schema válido, marcador explícito de vazio
  e paginação completa; nos demais casos, preservar catálogo e degradar a fonte.

#### Diagnósticos e interface

- Guardar somente screenshot/fragmento saneado de páginas públicas, sem query
  sensível, com permissão restrita e retenção de sete dias.
- Em portal autenticado, persistir apenas fingerprint de schema e contagens.
- Atualizar views/templates somente com informação operacional necessária,
  preservando o layout mobile-first e evitando mudança cosmética ampla.

#### Máquina de envio

- Estados:
  `selected → reserved → composing → price_revalidation → media_preparation →`
  `transport_queued → transport_started → confirmation_pending → confirmed`.
- Terminais: `rejected`, `permanent_failed`, `cancelled`, `uncertain`.
- Fazer endpoints web somente reservar/enfileirar; worker executa link,
  revalidação, mídia e transporte.
- Chave única por organização, canal, destino e publicação.
- Ledger Node atômico em volume, sem mensagem: organização, sessão, hash da
  operação, fase, timestamps e ID nativo quando existir.
- Após restart, devolver resultado conhecido. Se `transport_started` não tiver
  resultado conhecido, manter `uncertain` e nunca reenviar automaticamente.
- Tratar `incerta_pos_frame` e aceitação sem ID nativo como
  `confirmation_pending/uncertain`, não como enviado.
- Retry apenas antes do transporte confirmado: até cinco tentativas, exponential
  backoff com full jitter de 30 s a 15 min, serializado por sessão/destino.
- Não pausar permanentemente configuração por infraestrutura, timeout ou classe
  desconhecida. Somente destino/payload inválido ou credencial explicitamente
  revogada são permanentes.
- Antes do transporte, validar destino, texto ≤4096, base64 estrito, MIME/magic,
  imagem decodificável e mídia ≤16 MiB; nunca truncar silenciosamente.

#### Relatórios

- Expor pré-requisitos antes de abrir browser.
- Separar sessões de site, Link Builder/afiliados e relatórios, sem fallback entre
  elas.
- Implementar adaptadores Amazon e ML específicos e comprovados em staging.
- Usar `AMAZON_ASSOCIATES_REPORT_URL` e `ML_AFFILIATE_REPORT_URL`; vazio resulta
  em `not_configured` com instrução exata.
- Detectar redirect/login expirado, aplicar/comprovar período, percorrer paginação
  real e preferir exportação.
- Suportar CSV, TSV, XLSX read-only e HTML.
- Normalizar cabeçalhos mantendo célula tipada como válida, vazia ou inválida.
- Aceitar zero apenas com schema/célula válidos; rejeitar datas/números ilegíveis.
- Persistir idempotentemente por usuário, marketplace, relatório, período e chave
  estável da linha.
- Sanear fixtures e proibir cookie, token ou sessão.

#### Configuração segura planejada

- Preservar `MAX_WHATSAPP_SESSIONS=2`,
  `ML_CUPONS_ATIVACAO_ENABLED=0` e `CHROMIUM_GLOBAL_SLOTS=1`.
- Adicionar:
  - `AMAZON_COUPON_MAX_PAGES=5`;
  - `AMAZON_COUPON_MAX_ITEMS=500`;
  - `AMAZON_COUPON_MAX_SECONDS=180`;
  - `MANUAL_QUEUE_NO_WORKER_TIMEOUT_SECONDS=600`;
  - `MANUAL_QUEUE_MAX_WAIT_SECONDS=2700`;
  - `SEND_PIPELINE_V2_ENABLED=0` durante rollout;
  - `SEND_RETRY_BASE_SECONDS=30`;
  - `SEND_RETRY_MAX_SECONDS=900`;
  - `SEND_MAX_ATTEMPTS=5`;
  - `WA_SEND_LEDGER_DIR=/app/.wwebjs_auth/.send-ledger`;
  - `WA_SEND_LEDGER_RETENTION_HOURS=168`;
  - `WA_SESSION_RECONCILE_SECONDS=60`;
  - `SCRAPER_DIAGNOSTIC_RETENTION_DAYS=7`;
  - `AMAZON_BROWSER_REPORTS_ENABLED=0`.
- Consumir `AMAZON_ASSOCIATES_REPORT_URL` efetivamente, com default vazio.
- Nunca incluir token em log, métrica, teste ou snapshot.

### 4. Testes e aceite planejados

Cobertura requerida:

- Amazon: múltiplas páginas, lazy load, fallback semântico, CAPTCHA, schema
  alterado, card inválido, preço contraditório, deduplicação e balanço exato.
- ML: símbolo JS renomeado, SSR deslocado, página repetida, cursor real, código,
  ativação, override/kill switch, container público, sessão ausente, zero saudável e
  zero degradado.
- Pipeline/UI: transição e motivo por categoria, código sem produto visível,
  ativação sem produto invisível e contadores coerentes.
- Fila: worker ausente/stale, lock tipado, posição, crash antes/depois do claim,
  lease perdido, retomada única, starvation e concorrência.
- WhatsApp: duas organizações, dois usuários na mesma organização, capacidade,
  órfão/divergência, reset/logout concorrente, traversal, ledger/restart, timeout
  pré/pós-transporte e resposta incerta.
- Relatórios: pré-requisitos, URL/sessão ausentes, login expirado, CSV/TSV/XLSX/
  HTML, paginação, período, colunas alteradas, zero legítimo, célula ilegível e
  idempotência.
- Segurança/RLS: PostgreSQL real com `FORCE RLS`, incluindo leitura e escrita
  cruzada em sessões, links, fila, readiness, publicações e relatórios.
- Redaction: logs sem capabilities, cookies, storage state, base64, telefone
  completo e credenciais.
- Playwright: estados novos em mobile e desktop.

Comandos finais obrigatórios do plano:

```text
APP_ENV=test python3 manage.py test --verbosity 1
# suíte tenant em PostgreSQL efêmero com DATABASE_URL
npm test
npm run audit:production
python3 manage.py check --deploy  # com configuração efetiva de staging
# builds das duas imagens e smoke tests autenticados em staging
```

Critérios de aceite:

- nenhuma execução manual permanece `queued` além dos deadlines sem motivo/ação;
- crash/retry não produz mais de um efeito de envio;
- nenhuma ativação é publicada sem produto, preço e link comprovados;
- código válido aparece como aviso sem produto falso;
- toda perda observada tem contador ou motivo durável;
- zero de relatório só persiste quando validado;
- nenhuma linha tenant-aware cruza organizações;
- a suíte final cobre cada correção e reporta total real, nunca presumido.

### 5. Entrega, métricas e reversão planejadas

#### Fases do rollout

1. Capturar baseline de banco/fonte, fixtures saneadas e backup; instalar migrações
   aditivas/RLS e endpoints Node compatíveis.
2. Ativar manifests, reconciliação, heartbeats e fila nova em piloto, mantendo um
   Chromium global.
3. Executar readiness/métricas em modo sombra, comparar com o funil anterior e só
   então liberar a UI nova.
4. Ativar envio v2 por organização; observar confirmações, incertezas e retries
   antes da expansão.
5. Homologar adaptadores de relatório em staging e ativá-los por organização.
6. Manter ativações ML globalmente desligadas até rollout explícito separado.

#### Métricas

- Antes de mudar comportamento, registrar contagens existentes e durações
  disponíveis.
- Comparar após 24 horas e sete dias, com as mesmas organizações/janelas:
  - cards/páginas, aceites, duplicados e rejeições;
  - espera por motivo, p50/p90 e maior espera;
  - envios confirmados, permanentes, transitórios e incertos por etapa;
  - linhas de relatório aceitas/rejeitadas e pré-requisitos.
- Onde não houver telemetria histórica, declarar “baseline não disponível”; nunca
  reconstruir ou estimar números.

#### Reversão

- Desligar `SEND_PIPELINE_V2_ENABLED`, adaptadores de relatório e projeções/
  rollouts novos por flag.
- Reverter versões Django/Node sem remover migrações aditivas.
- Preservar manifests, ledger, catálogo anterior e eventos.
- Nunca apagar diretórios órfãos no rollback; mantê-los desabilitados.
- Remover `Perfil.wa_session` e demais contratos legados apenas em release futuro,
  após observação.

#### Handoff previsto

O diff deve ser entregue agrupado em:

- accounts/tenancy, capabilities, migrações e RLS;
- modelos, fila, fontes, pipeline, views/templates, envio e relatórios Django;
- registry, ledger, payloads, reset e telemetria Node;
- `Procfile`, Fly configs, settings, requirements e testes.

A validação em produção deve confirmar migrações/RLS, saúde dos workers,
capacidade, reconciliação de sessões, job manual canário, fontes, link por usuário,
envio idempotente, relatórios homologados, redaction de logs e consumo de
CPU/memória antes de cada expansão.
