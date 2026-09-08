# Spreading — plano final de entrega

Data: 07/09/2026. Base: `origin/main` (commit `65895a0`).
Fontes: auditoria de três frentes (contrato dos documentos, defeitos no HEAD, mapa do funil),
estado vivo de produção lido em 06–07/09, pesquisa de mercado e das saídas técnicas, e as
decisões que você tomou nesta sessão.

---

## Contexto — por que este plano existe

O sistema coleta bem e entrega mal. Em 06/09 o funil fechou uma varredura completa com 2.534
cupons e, no mesmo minuto, não entregou nada. As causas foram encontradas e corrigidas ao longo
da sessão (seis commits em `main`), mas o diagnóstico maior é outro: **o produto foi construído
contra um contrato que ele não precisa cumprir, e não cumpre o contrato que você precisa.**

Os documentos exigem 100 cupons prontos por marketplace, 250 candidatos novos por dia por
marketplace, auditoria de 95% de precisão sobre 100 publicações e operação 24/7 — enquanto a
operação real é a lules, com cerca de dez grupos, poucas ofertas por hora, e a produção dorme
das 01:00 às 07:45 porque é isso que faz a conta caber. São vinte contradições entre os
documentos e quinze promessas que nenhum deles chega a especificar.

Este plano fecha isso: fixa o que o produto é, arbitra as contradições, lista o que falta e
entrega em etapas com prova em produção.

---

## O que o produto é (escopo final)

**Um funil que acha cupom bom, prova que o cupom é real, e entrega no grupo certo.**

O usuário principal é a **lules**: ~10 grupos de WhatsApp e Telegram, divididos por nicho —
casa/cozinha/eletrodoméstico, eletrônicos/celular/informática, moda/beleza/perfumaria e um
grupo geral de achadinhos. Poucas ofertas por hora, subindo para três ou quatro na hora de uma
relâmpago. O grupo do dev é secundário e entra depois.

Três regras de produto que governam todo o resto:

1. **Cupom é o produto; promoção é acompanhamento.** Cupom vende. Promoção comum entra na fila
   com score baixo e só passa na frente quando for relâmpago muito boa.
2. **O preço anunciado é o preço do site.** Validado uma vez quando o item entra no programa e
   obrigatoriamente de novo antes de publicar. Se não bater, não sai.
3. **Nicho, não volume.** A meta é cobertura por nicho do grupo, não contagem por marketplace.
   Nicho específico e de pouco uso não precisa de abundância.

Marketplaces: Mercado Livre, Amazon e Shopee. Canais de entrega: WhatsApp e Telegram.

---

## O que está sólido (não mexer sem motivo)

Vale registrar, porque a maior parte do sistema é boa e o plano se apoia nela:

- **Isolamento multi-tenant** é a melhor parte do repositório. RLS com `FORCE`, contexto
  assinado por HMAC dentro do próprio Postgres, três roles sem `BYPASSRLS`, e agora um job de
  CI que provisiona as roles reais e prova com `tenant_isolation_probe` que GUC falsificado não
  atravessa organização.
- **Nunca duplicar** é o invariante que o sistema realmente protege, em três camadas
  independentes: ledger no Node, resultado "incerto" que não repete, e o cliente Django que
  pergunta ao ledger antes de rotular um timeout.
- **Máquina de estados do envio** (`send_pipeline.py`) com transições declaradas, `confirmed`
  terminal e auditoria por evento.
- **Fail-soft dos loops**: `write_state` nunca levanta, estado tem tri-estado explícito, e erro
  de banco não vira "desligado em silêncio".
- **Comentários que citam data e medição** — é o que impede alguém de "otimizar" de volta para
  o bug.

---

## Decisões que este plano fixa

Os documentos se contradizem em vinte pontos. Estas são as arbitragens; o resto do plano
depende delas.

| Tema | Decisão |
|---|---|
| Meta de volume | **Revogada.** Vale cobertura por nicho da operação da lules, não 100 cupons/marketplace nem 40 mensagens/dia/marketplace. |
| 24/7 | **Revogado.** Produção dorme 01:00–07:45; é o que faz o custo caber. O que entra no lugar é consertar o religamento, que já falhou três dias seguidos. |
| Teto de custo | **R$350/mês, tudo incluído** (Fly + IA + desbloqueio). Substitui o R$300 dos documentos e o "sem teto duro" do plano anterior. |
| Cupom-first | **Confirmado como regra de produto.** Resolve a contradição entre o seletor unificado e a estratégia cupom-first: o cupom-first é peso no score, não pré-corte. |
| WhatsApp Web | **Fica.** A API oficial não envia para grupo — não é atalho disponível. Risco de ban é aceito e mitigado. |
| Telegram | **Canal de entrega de verdade**, não só fonte. É a rede de segurança para quando o WhatsApp cair. |
| Monetização e SaaS | **Fora.** Patrocinado, leilão, cobrança, onboarding self-service e site de marketing viram fase futura. |
| Sessões WhatsApp | **Uma, da lules.** O grupo do dev entra depois, por Telegram ou por uma segunda sessão medida. |
| Documento canônico | **Este plano.** `IMPLEMENTATION_STATUS.md` fica marcado como histórico. |

---

## Etapas

### E0 — Fechar o que ficou pela metade (imediato)

Há três arquivos modificados não commitados, meus, com um teste vermelho: a linha do cupom
passou de "ative na página do Mercado Livre" para "ative no Mercado Livre — o preço já é com
ele", e o badge da tela de "Cupom na página" para "Cupom já no preço". A correção está certa —
a mensagem anunciava `POR 98,77`, preço que só existe depois de ativar — mas
`test_precos_mensagem.py` ainda procura o texto antigo em `TelaOfertasMercadoLivreTests`.

Ajustar o teste, rodar a suíte, commitar. Depois disso, validar a tela nos dois viewports com
Playwright, como manda `python/CLAUDE.md`.

### E1 — Parar de mentir sobre preço e disponibilidade

É a regra 2 do produto, e hoje ela está furada em dois pontos.

**`ofertas.esta_vivo` está cego.** Faz `requests.get` sem cookies; o Mercado Livre responde 200
com o interstitial, o texto "Anúncio pausado" não aparece, e a função devolve `True`. O portão
de "o produto ainda existe" aprova qualquer coisa. Reusar `link_http._motivo_bloqueio`, que já
sabe classificar interstitial, e devolver *indeterminado* em vez de *vivo* — indeterminado não
publica.

**O frescor real é de 48 horas.** `DEAL_FRESCOR_MAXIMO_MIN` existe em `settings.py` com um
comentário longo explicando que nasceu do incidente da air fryer anunciada a R$199,90 e cobrada
a R$249,50 — e **nenhum módulo lê essa setting**. O gate que sobrou é `produtos_frescos_q`, de
48h. Ligar a setting no caminho de publicação e usar o valor que o comentário defende.

**Revalidação obrigatória antes de publicar.** `preco_ao_vivo.revalidar` já existe e já tem o
modo `exigir_medicao`; hoje ele só é exigido quando o item vem da camada Deal, que está
desligada. Passa a ser exigido em todo envio: preço da publicação e preço do site têm de bater,
e discordância bloqueia.

### E2 — Destravar o cupom do Mercado Livre cortando a demanda

Os cupons de ativação do ML — o maior volume do sistema, ~2,4 mil ativos — estão **100%
parados**. Todos dependem de `lista.mercadolivre.com.br`, que responde 403 no IP da Fly. Sem
container não nasce `ProdutoCupom` confirmado, sem par confirmado não existe deal com cupom de
ML, e a prova de escopo da ativação é justamente esse host.

O erro é preparar em massa: ~400 requisições por ciclo, quatro ciclos por hora, ~1.600/h — a
esmagadora maioria para cupons que ninguém vai publicar. Nenhum desbloqueio pago sobrevive a
esse volume (passaria de mil dólares por mês).

**Inverter a ordem: selecionar primeiro, preparar depois.** O seletor escolhe os poucos cupons
que vão publicar naquele ciclo; só esses passam pelo preparo, e só esses consomem o
desbloqueio. A demanda cai de milhares por hora para dezenas por dia, e cabe na cota gratuita
de 5.000 requisições/mês do Web Unlocker. Arquivos: `coupon_products.preparar_lote`,
`coupon_products._coletar_ml_remoto`, `automacao.py` lane `cupons`.

O backoff de 20 minutos por falha de transporte continua; o que muda é quantos itens chegam a
tentar.

### E3 — Score cupom-first e cobertura por nicho

Hoje quem publica é o caminho legado: `DEAL_LAYER_LIVE=0`, e a camada Deal roda em shadow
gravando divergência num evento que **nenhuma tela lê** e que a purga apaga.

- Cupom validado entra com peso alto; promoção comum entra com score baixo; relâmpago com queda
  grande recupera o peso. Reusar `deals.pontuar` e `coupon_rules.score_cupom`, que já existem.
- A meta de abundância passa a ser **por nicho**, lida de `ConfiguracaoEnvio.macro_categoria`
  (o campo já existe, com `termo_busca` e `termos_negativos`). Alvo por nicho ativo, com folga
  para o pico de relâmpago; nicho de baixo giro tem alvo menor, declarado, não zero.
- **Segmentação por nicho no ML está morta**: `buscar_por_termo` usa o host bloqueado. Enquanto
  o desbloqueio da E2 não cobre busca, o nicho no ML sai de filtro local sobre `/ofertas`, que
  responde — menos profundidade, mas honesto.
- Ligar a camada Deal para a organização da lules só depois que o shadow parar de divergir, e
  dar leitor ao shadow (hoje o único é um comando órfão).

### E4 — Fechar os buracos do envio

Três defeitos abertos, todos no caminho que entrega dinheiro.

**A fila v2 não tem consumidor quando a chave está desligada.** `_consumir_fila_v2()` e
`reconciliar_publicacoes_orfas()` estão dentro de `_loop_envio`, que aborta no topo se a lane
"envio" estiver desligada. A tela enfileira mesmo assim e responde "reservado" — e o item nunca
sai, sem consumidor e sem coveiro. Tirar as duas chamadas de dentro do gate.

**A válvula de segurança está morta sob v2.** Em `ofertas.py`, o ramo que incrementa
`cfg.falhas_consecutivas` testa `sucesso and not queued` / `not sucesso`; o resultado
enfileirado é `sucesso=True, queued=True` e não cai em nenhum. Regra com destino inválido
re-enfileira para sempre, sem nunca pausar. E `max_envios_dia` conta só `enviado`, então o teto
por regra não segura o caminho v2.

**Telegram pode duplicar.** O sender aceita `operation_id` e ignora. Um timeout depois de a Bot
API já ter aceitado vira "transitório", o resultado padroniza `repetir=True`, e a oferta sai
duas vezes. O WhatsApp recebeu quatro camadas contra isso; o Telegram, nenhuma — e agora é
canal de entrega. Aplicar a mesma ideia: chave de operação e consulta antes de rotular.

Ainda: o token do bot vaza no texto do erro de conexão (`api.telegram.org/bot<ID>:<SEGREDO>/…`)
e a redação de log só cobre query-string, não o caminho da URL.

### E5 — Mitigar o risco de ban do WhatsApp

O risco é permanente e não eliminável: a detecção é heurística. O que reduz, com números da
pesquisa: **cadência irregular** (intervalo fixo é o maior sinal de robô), teto por hora,
aquecimento de número novo por duas a quatro semanas, e taxa de bloqueio abaixo de 2%.

A favor: o envio é para grupos que a dona administra, não para desconhecidos — o cenário de
menor risco. Contra: hoje o intervalo entre envios é regular.

Introduzir variação aleatória no espaçamento e teto por hora por sessão, reusando
`ConfiguracaoEnvio.horas_cooldown` e o prazo por etapa que já existe no Node. E manter o
Telegram pronto como rota alternativa quando a sessão cair.

### E6 — Enxergar o que está acontecendo

**Alerta não sai de produção.** Sem `EMAIL_HOST_USER`/`PASSWORD`, incidente só aparece para
quem abrir a tela — foi assim que a produção passou 16, 17 e 18/08 fora do ar. Você escolheu
SMTP; Brevo tem 300 e-mails/dia grátis e encaixa no `EMAIL_HOST*` que o Django já usa. O secret
é seu; eu não defino secret de produção. No código, a falha de alerta hoje é engolida em
WARNING e passa a marcar o incidente como *não notificado*.

**O check das esteiras cobre três de oito lanes.** `worker_health.ESTEIRAS` tem `scrape`,
`envio` e `links`; `monitor`, `manual`, `cupons`, `scrapeflash` e `canais` podem estar mortas
com o check verde. Pior: `monitorar_canais` **publica em grupo** e não escreve heartbeat nenhum
— lane cega de ponta a ponta.

**A Saúde mostra a flag errada para os links**: calcula com `is_enabled("scrape")` enquanto a
lane roda com `is_enabled("links")`, e as duas divergem assim que a lane ganha escolha própria.

**A fila v2 não tem tela.** Nenhum template lê `transport_state`, `attempt_count` ou
`next_retry_at`. Um envio com três tentativas queimadas é visualmente idêntico a um recém-nascido.
As duas tabelas construídas para contar essa história não têm leitor.

**Contador de gasto de IA**, que não existe em lugar nenhum: acumular tokens por chamada, somar
por mês, mostrar na Saúde e alertar ao cruzar o limite. É o que mantém o teto de R$350 honesto.

### E7 — Limpeza e verdade nos documentos

Peso morto confirmado: Celery inteiro (`tasks.py`, `core/celery.py`, `celery` e `redis` no
requirements — nenhum processo, `beat_schedule` vazio de propósito), `entrypoint.sh` (nunca
copiado nem invocado), `python/Procfile` da raiz (sobrescrito pelo `fly.toml`, e já divergiu
dos outros dois), o cache de versão do WA Web em disco que nunca é lido, `connect_ml.py`,
`falta.txt`, `cupons_consolidados.json`, e sete relatórios de management command sem leitor.
As cinco ferramentas de operação órfãs por desenho (chaves, release de fase 0, re-cifra, probe
do link público) ficam.

Documentos: `IMPLEMENTATION_STATUS.md` afirma "não está em produção" enquanto a produção passou
de trinta deploys — marcar como histórico. `GUIA_ATIVACAO.md` manda ligar um secret que hoje é
**erro fatal do próximo deploy**, diz que o SMTP já está configurado (falso) e manda deployar de
uma branch seis commits atrás. `README.md` descreve um cache que foi removido. `DEPLOY.md` e
`PLANO_REVISAO_PRODUCAO.md` discordam do custo. Uma fonte só, e as promessas sem especificação
(`policy_snapshot`, `algorithm_version`, restauração de backup) ou ganham definição ou saem.

**Restauração de backup não tem procedimento.** O runbook exige snapshot de menos de 24h com
restore ensaiado, e não existe script nem passo a passo. O CI já provou que o banco **não sobe
do zero** — foi assim que a ordem do `tenant_rls --only-context` apareceu. O ensaio de restore
é o que fecha esse buraco.

---

## Custo

| item | valor |
|---|---|
| Fly, quatro máquinas + 7 GB de volume, com desligamento noturno | US$ 53,97/mês = **R$ 276** |
| Sobra dentro do teto de R$350 | **R$ 74/mês ≈ US$ 14,40** |
| Desbloqueio ML (Web Unlocker) depois da E2 | **R$ 0** enquanto ficar abaixo de 5.000 requisições/mês |
| Anthropic Haiku 4.5 (US$1/MTok entrada, US$5/MTok saída) | cabe em ~**90 extrações de cupom por dia** |

O contador da E6 é o que transforma esse orçamento em fato observável em vez de estimativa.
Câmbio usado: R$5,12/US$.

---

## Definição de pronto

O produto está entregue quando, por **sete dias corridos**, para a conta da lules:

1. Cada nicho ativo tem cupons validados suficientes para sustentar a cadência do seu grupo, sem
   repetir oferta em 24 horas.
2. Todo item publicado teve preço revalidado no ciclo do envio, e o preço da mensagem é o preço
   do site.
3. Todo envio termina em estado honesto: confirmado com carimbo de hora e ACK, ou declarado não
   confirmado — nunca "enviado" sem prova.
4. Nenhum link publicado responde 404, e todo clique é registrado.
5. Qualquer incidente chega até você por um canal ativo, sem depender de alguém abrir a tela.
6. O custo do mês fecha abaixo de R$350, com o contador batendo com o console da Anthropic.
7. Nenhuma organização enxerga dado de outra — provado pelo probe, não por inspeção.

---

## Verificação

Cada etapa se prova em produção, não na suíte:

- **E1**: publicar um item cujo preço mudou entre a coleta e o envio e confirmar que ele é
  bloqueado; apontar `esta_vivo` para um anúncio pausado e confirmar que não aprova.
- **E2**: contar as requisições ao container por ciclo antes e depois; o número tem de cair de
  centenas para dezenas, e cupom de ativação do ML volta a chegar em `ready`.
- **E3**: numa rodada, a fila de cada grupo tem de vir dominada por cupom, com promoção comum
  só no rodapé — e a divergência do shadow tem de estar em zero antes de ligar a camada Deal.
- **E4**: desligar a lane "envio" com item enfileirado e confirmar que ele é drenado ou fechado;
  forçar destino inválido e confirmar que a regra pausa; enviar ao Telegram com timeout forçado
  e confirmar mensagem única.
- **E5**: medir o intervalo entre envios em produção e confirmar que não é constante.
- **E6**: derrubar uma lane de propósito e ver o check ficar vermelho; disparar incidente de
  teste e receber o e-mail; comparar o contador de IA com a fatura.
- **E7**: restaurar um snapshot num banco novo e subir a aplicação inteira contra ele.

---

## Fora de escopo (registrado, não construído)

Vitrine patrocinada, leilão, cobrança e planos, onboarding self-service, site público de
marketing, payout e marketplace aberto, ROAS por creator, API oficial de afiliados do Mercado
Livre (não existe), e a segunda sessão de WhatsApp para o grupo do dev — que entra depois, por
Telegram ou com medição de memória antes.

---

## Estado em 08/09/2026 — o que está pronto e o que trava

Fotografia, não plano: o plano acima continua valendo. Reescrita no fim do dia,
depois de medir em produção. **Boa parte do que esta seção dizia de manhã estava
errado**, e o registro do erro vale mais que a correção.

### O que se acreditava, e o que a medição mostrou

Dizia-se aqui que a sessão do Mercado Livre tinha caído e que por isso o envio
tinha parado. As duas metades eram falsas.

**A sessão nunca caiu.** `MercadoLivreSession.status = active`, sonda
`conectado`, 44 cookies no cofre. A página do Link Builder abre logada, com o
textarea de URLs, o botão Gerar e a etiqueta `lpohoffmann` à mostra. O que
respondia "indisponível" era código nosso: `Locator.is_enabled` **levanta**
`TimeoutError` quando o locator não assenta no prazo — não devolve `False`. A
chamada estava solta dentro do `try` que abraça `_linkbuilder_pronto`, então o
estouro virava "não está pronto" e o fallback logo abaixo (preencher o campo e
ver o botão habilitar) nunca rodava. Como `ml_conexao` julga a sessão pela mesma
função, o sistema mandava reconectar uma conta que estava conectada o tempo todo.
Corrigido, deployado e verificado em produção: página fria, `pronto? True`.

**O envio não estava parado.** 37 `send_ok` em sete dias, `send_concluido` da
lules no meio da tarde de hoje.

Lição para a próxima: `links_erro` e `links_sem_sessao` carimbavam a causa
errada, e a causa errada foi acreditada por dias porque combinava com um vilão
conhecido — o muro de IP. Antes de culpar o muro, medir o host exato.

### O bloqueio que sobra, e ele é real

O muro de IP do Mercado Livre, **por host e caminho** — remedido hoje da conta da
lules:

| Caminho | Resultado |
|---|---|
| `/afiliados/linkbuilder` | 200, logado e funcional |
| PDP de produto | 3 de 3 em `/captcha/wall/logged` |
| `lista.mercadolivre.com.br/cupons` | mesmo muro |

Daí saem os dois sintomas que restam, e são o mesmo sintoma:

- `send_failed: preço não confirmado agora e a última leitura foi há 2598 min
  (limite 90)` — a revalidação obrigatória não consegue medir porque a PDP é
  muro. O portão está certo; falta é a medida.
- Canário de mensagem 0 de 3, "primeiro candidato não é cupom". **Não é bug de
  ordenação**: medido, 25 de 25 candidatos são `deal` e nenhum é `coupon`. O
  container de cupom vem de `lista.`, que é muro. A fila de cupom está seca na
  origem.

### O apagão da manhã, e por que ele não se repete

A produção passou das 05:46 às 09:02 fora do ar, incluindo a janela de envio das
08:00. A parada das 04:00 UTC foi entregue às 08:46 UTC — 5h46 atrasada — e os
dois religamentos seguintes foram **descartados** pelo agendador do GitHub, que é
best-effort e nomeia o início de cada hora como pico. Quatro das seis tentativas
estavam exatamente em `:00`.

Três mudanças, todas medidas:

1. A janela de parada virou econômica. Uma hora de sono poupa ~R$0,37; a parada
   das 05:46 compraria menos de duas horas contra a manhã inteira. Só para se
   restarem ao menos quatro horas de sono. Hoje teria sido recusada.
2. Nenhum cron em `:00`, e sete tentativas de religamento em vez de cinco.
3. `web` e `db` **não são mais parados**. Os event logs mostram o proxy da Fly
   levantando os dois em 45s e 33s: pará-los trocava zero centavo por um bounce
   noturno do Postgres. Só worker e WhatsApp dormem de fato.

`test_energia_producao` cobra os três arquivos que só concordavam por convenção
(workflow, mapa de ações, script), e foi verificado por mutação.

### Correção de custo — a conta documentada estava R$30 abaixo

Consequência direta do achado acima: a conta de **R$278/mês** assumia as quatro
máquinas dormindo. Só duas dormem. O número real é **US$59,75 = R$308/mês** a
R$5,16. Continua abaixo do teto de R$350 deste plano. **Não é aumento de gasto —
é a fatura que sempre existiu, medida certo pela primeira vez.**

### Pendente com o dono da conta

1. **Canal de alerta** — SMTP (Brevo, 300/dia grátis) ou `ALERTA_TELEGRAM_CHAT_ID`.
   O incidente da manhã foi detectado e morreu com `alertado: None`. Enquanto não
   existir transporte, o sistema sabe e você não.
2. **Crédito da Anthropic** — sem ele a mensagem sai sem o gancho de abertura. O
   resto (nome, preço, cupom, validade, CTA, link) é código, com teste garantindo
   que sobrevive à chave em branco.
3. **Regras de envio dos grupos reais** — existem três, todas apontando para
   "Teste ofertas".

Reconectar o Mercado Livre **saiu desta lista**: não era necessário.

### Pendente de decisão de custo

**Desbloqueio do Mercado Livre.** É o que destrava revalidação de preço e o funil
de cupom de uma vez — os dois sintomas restantes têm essa única causa. Depois do
corte de demanda da E2 cabe na cota gratuita de 5.000 requisições/mês do Web
Unlocker, mas é conta nova em nome do dono.

### Medições que valem guardar

- Cobertura por nicho, na tela de Saúde: Eletrodomésticos 50 ofertas/2 cupons,
  Celulares 26/1, Ferramentas 37/0. A meta passou a cobrar cupom além de volume,
  porque 37 ofertas sem nenhum cupom passavam como "meta atingida".
- Custo de IA: `cupom_extractor` responde por 99% do gasto, 765 tokens por
  chamada. É volume, não desperdício por chamada. Prompt caching **não** se
  aplica: Haiku 4.5 exige 4.096 tokens mínimos e o prompt fixo tem 442 — abaixo
  do mínimo a API ignora o `cache_control` sem erro.
- CI contra PostgreSQL: de 12 falhas + 124 erros para verde, e o caminho até lá
  achou defeitos reais — `close_old_connections` dentro de transação (quatro
  lugares), reentrância de lease negada, RLS meio ligado por migração.
- Produtos: 64.152 no total, 2.335 frescos no ML e 1.464 na Amazon. Sete das
  esteiras vivas, 30 fontes de ingestão habilitadas.
