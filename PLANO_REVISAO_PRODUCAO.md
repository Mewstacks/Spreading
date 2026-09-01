# Plano de revisão e aceite de produção

Este documento é o contrato técnico de entrega. “Encontrado” não significa
“válido”, “válido” não significa “afiliado” e “afiliado” não significa “enviado”.
Cada etapa precisa de evidência própria.

## Objetivo competitivo

O produto deve ser melhor, no recorte brasileiro de **cupons para Mercado Livre,
Amazon e Shopee entregues por WhatsApp**, do que as capacidades combinadas dos
principais sistemas comparáveis. Isso significa unir:

- a descoberta comunitária e a moderação individual do Promobit;
- a temperatura, os votos e os alertas do Pelando;
- o catálogo amplo, as regras e o feedback de funcionamento do Cupom.org;
- a descoberta durante a compra do Méliuz e do Cuponomia;
- o teste no checkout, a escolha do maior desconto e a taxa histórica de sucesso
  do Honey/Capital One Shopping;
- a velocidade e a segmentação por loja/categoria dos grupos de WhatsApp e
  Telegram, sem transformar o canal em spam.

“Melhor” não será uma alegação subjetiva nem a tentativa de superar o catálogo
global de concorrentes que cobrem dezenas de milhares de lojas. Será comprovado
por métricas reproduzíveis no nicho escolhido: cobertura equilibrada dos três
marketplaces, validação real, frescor, economia obtida, taxa de sucesso, clareza
das regras, velocidade de entrega e ausência de duplicatas.

## Metas competitivas mensuráveis

- [ ] **Abundância equilibrada:** manter, quando houver inventário público
  disponível, ao menos 100 cupons distintos prontos para Mercado Livre, 100 para
  Amazon e 100 para Shopee. Nenhum total agregado compensa marketplace abaixo da
  meta. Códigos e cupons de ativação por produto/campanha contam separadamente,
  mas promoções sem cupom não contam.
- [ ] **Descoberta diária:** observar ao menos 250 candidatos novos ou
  reobservados por marketplace a cada 24 horas, provenientes de no mínimo três
  classes de fonte por marketplace (oficial/afiliada, web pública/agregador e
  comunidade/Telegram), sem teto artificial de dez itens.
- [ ] **Precisão superior:** obter pelo menos 95% de precisão numa auditoria
  estratificada de 100 publicações por marketplace; nenhum código publicado pode
  depender apenas do texto de uma fonte comunitária.
- [ ] **Prova de aplicação:** registrar tentativas, sucessos, falhas, produto ou
  carrinho usado, economia observada, restrições e instante da prova. Um cupom com
  código só é “comprovado” por fonte oficial inequívoca ou aplicação sem compra;
  cupom de ativação exige evidência na página oficial elegível.
- [ ] **Frescor competitivo:** cupom relâmpago descoberto e elegível em até 10
  minutos no p95; código que falhou de modo conclusivo ou desapareceu da fonte
  oficial sai da fila de envio em até 10 minutos no p95.
- [ ] **Ranking pela utilidade real:** ordenar por economia realmente observada,
  recência da confirmação, taxa de sucesso, aderência ao perfil e urgência, e não
  por comissão ou texto publicitário da fonte.
- [ ] **Funil e conversão auditáveis:** atribuir 100% das publicações e links ao
  usuário, destino, fonte, cupom, variante de mensagem e instante; reconciliar ao
  menos 99% dos cliques/pedidos importados que tragam identificador suportado.
  Testes A/B só declaram vencedor com amostra suficiente e devem buscar ganho
  relativo de pelo menos 10% em CTR ou conversão sem piorar entrega, opt-out ou
  precisão. Sem amostra, o placar diz “inconclusivo”, nunca “melhor”.
- [ ] **Entrega superior no WhatsApp:** sustentar uma fila de ao menos 40 mensagens
  de cupom úteis por dia para cada marketplace, quando o inventário validado
  permitir, com limite configurável por usuário, segmentação por loja/categoria,
  opt-out, idempotência e zero mensagens duplicadas em 24 horas.
- [ ] **Mensagem completa:** 100% das mensagens informam código ou forma de
  ativação, produtos/escopo elegíveis, mínimo, teto, público restrito, validade,
  preço/economia quando comprovados, horário da última confirmação e link afiliado
  da conta correta; campo desconhecido é omitido, nunca inventado.
- [ ] **Confiabilidade:** sete dias em produção com disponibilidade de coleta e
  envio de pelo menos 99,5%, nenhuma fonte silenciosamente seca por mais de dois
  ciclos, nenhuma compra concluída durante validação e nenhum incidente crítico.
- [ ] **Desempenho e custo:** painel p95 abaixo de 2 segundos, geração de fila p95
  abaixo de 15 segundos por regra, validação desacoplada da experiência do usuário
  e operação 24/7 completa abaixo de R$300/mês.

As metas de volume são condicionadas à existência real de inventário observável:
quando uma loja não expuser a quantidade mínima, o sistema deverá provar a
exaustão das fontes e exibir o déficit explicitamente. É proibido atingir a meta
reciclando duplicatas, promoções sem cupom ou códigos não comprovados.

## Benchmark pesquisado em 27/08/2026

| Sistema | O que entrega | Como entrega/valida | Critério para superá-lo neste produto |
| --- | --- | --- | --- |
| Promobit | Comunidade, lista de desejos, alertas e grupos segmentados com mais de 30 ofertas/dia | Moderação aplica o cupom, confere desconto, uso por terceiros, preço, link e estoque | Mais de 30 **cupons** úteis/dia por marketplace disponível, prova estruturada e expiração automática |
| Pelando | Comunidade, temperatura, comentários, alertas e páginas próprias de Amazon/ML/Shopee | Votos e classificação social das ofertas | Combinar sinal social com aplicação/prova oficial; voto nunca substitui validação |
| Cupom.org | Cerca de 4.000 lojas e 40.000 cupons declarados, regras, ranking semanal e votos | Testes/atualizações diárias declaradas e feedback dos usuários | Não competir em número global de lojas; superar em precisão, frescor e cobertura equilibrada de Amazon/ML/Shopee |
| Méliuz | Cupons, cashback, app e extensão que testa/aplica códigos | Integrações de parceiros e aplicação no momento da compra | Testar sem compra, registrar economia e escolher o melhor cupom também fora de fontes parceiras |
| Cuponomia | Cupons/cashback no site, app e extensão contextual | Alerta ao navegar e ativação antes do fluxo de compra | Entregar o contexto completo direto no WhatsApp e revalidar antes do envio |
| Honey | Códigos comunitários em milhares de lojas, teste automático e taxa de sucesso | Tentativas reais no checkout; sucesso = aplicações com redução / total de tentativas | Exibir taxa por cupom, não só por loja, condições do carrinho, recência e motivo de falha |
| Capital One Shopping | Pesquisa, teste automático, melhor código e alerta de queda de preço | Dados de uso da comunidade e aplicação no checkout | Unir o melhor código comprovado a personalização e distribuição imediata por marketplace/categoria |
| SimplyCodes | Transparência por código, teste no carrinho, consenso independente e confiança que decai com o tempo | Três camadas de verificação e placar público de saúde de 0 a 100 | Expor prova, recência, consenso e histórico por cupom; nunca usar o score comercial de ranking como selo de validade |

Fontes primárias consultadas:

- Promobit — critérios de moderação:
  https://www.promobit.com.br/institucional/criterios-de-moderacao/
- Promobit — grupos WhatsApp:
  https://www.promobit.com.br/grupos-whatsapp/?grupo=magalu
- Promobit — FAQ e lista de desejos:
  https://www.promobit.com.br/institucional/faq/
- Pelando — páginas e recursos da comunidade:
  https://www.pelando.com.br/
- Cupom.org — catálogo, atualização, regras e votação:
  https://www.cupom.org/
- Méliuz — extensão e funcionamento:
  https://www.meliuz.com.br/como-funciona
- Cuponomia — extensão contextual:
  https://ajuda.cuponomia.com.br/hc/pt-br/articles/360057631852-Como-utilizar-a-Extens%C3%A3o-do-Cuponomia
- Honey — teste e taxa de sucesso:
  https://help.joinhoney.com/article/364-coupon-success-rates
- Honey — submissão comunitária e teste antes de publicar:
  https://help.joinhoney.com/article/44-can-i-add-a-coupon-code-to-honey
- PayPal Honey — teste de vários códigos e maior economia:
  https://www.paypal.com/us/digital-wallet/ways-to-pay/paypal-honey
- Capital One Shopping — teste, comunidade e alertas:
  https://capitaloneshopping.com/ai-instructions
- SimplyCodes — processo de verificação e uso:
  https://simplycodes.com/how-it-works
  https://simplycodes.com/blog/simplycodes-getting-started

## Metas obrigatórias

- [ ] Descoberta sem teto artificial: Mercado Livre, Amazon, Shopee, feeds de
  afiliados, páginas públicas, agregadores e ao menos 12 canais públicos do
  Telegram. Paginação prossegue até fim real, repetição, orçamento documentado ou
  bloqueio explícito.
- [ ] Cobertura: coletar pelo menos 95% de uma amostra manual de cada fonte ativa e
  cumprir as metas separadas de cupons prontos para Mercado Livre, Amazon e Shopee
  quando as fontes observadas tiverem esse inventário, sem fabricar volume e sem
  limitar uma loja a dez itens.
- [ ] Veracidade: comissão de afiliado nunca vira desconto; alegação comunitária
  nunca vira preço; código só recebe selo “validado” após fonte oficial, aplicação
  em checkout/carrinho ou confirmação equivalente registrada. Falha, CAPTCHA e
  timeout são inconclusivos e bloqueiam publicação.
- [ ] Frescor: radar de cupom relâmpago a cada 5–15 minutos; fontes caras respeitam
  TTL próprio; preço, estoque, destino e vínculo afiliado são revalidados antes do
  envio. Cupom expirado ou ausente sai sem apagar catálogo por falha de coleta.
- [ ] Afiliação: todo link enviado contém a atribuição da conta `lules`, abre o
  produto/campanha correto e preserva as regras do cupom. Link sem prova fica na
  fila e não é publicado.
- [ ] Mensagem: texto mobile-first informa produto, preço real, economia, código,
  mínimo/teto/escopo/restrições, validade e CTA sem alegações inventadas.
- [ ] Entrega: canário real na conta `lules` para o grupo WhatsApp “Teste ofertas”
  e destino Telegram de teste, com idempotência, confirmação e sem duplicatas.
  Nenhuma compra será concluída.
- [ ] Experiência: páginas principais responsivas, estados vazios/erro acionáveis,
  sem travamento sob raspagem e com métricas de fonte, fila, validação e envio.
- [ ] Segurança: isolamento entre contas, CSRF, autorização, SSRF, XSS e SQLi
  testados; segredos não aparecem em log, HTML ou diff.
- [ ] Produção: deploy canário, rollback testado, smoke tests, diagnóstico completo
  e sete dias consecutivos sem incidente crítico, fila órfã ou fonte silenciosamente
  seca.
- [ ] Custo: operação 24/7 abaixo de R$300/mês, incluindo aplicação, banco, volumes,
  backup e margem. Mudança de hospedagem só ocorre com backup e ensaio de restore.

## Evidências já obtidas nesta revisão

- Shopee: campanha/comissão deixou de ser convertida em cupom do comprador.
- Shopee oficial: 17 vouchers observados em produção, oito ativos aceitos, oito
  indisponíveis e um cashback corretamente rejeitado. Os oito ativos ficam
  elegíveis, mas não são enviados sem integração afiliada própria da conta.
- Shopee/Fly em 01/09: a landing passou a redirecionar o IP da Fly para
  `/verify/traffic/error` com “Login Necessário” antes de chamar a API de vouchers.
  O estado agora é `auth_required`, nunca “inventário vazio”. Quando a sessão de
  compras existe, a fonte usa o estado cifrado e persiste o resultado no escopo do
  dono da sessão; a conta `lules` ainda precisa ser conectada para a prova real.
- Méliuz: novo radar público sem Chromium encontrou 84 códigos distintos em 3,9 s
  (23 Amazon, 38 Mercado Livre e 23 Shopee), descartando 14 placeholders, sete
  entradas inválidas e cinco duplicatas. Em produção, 13 códigos do ML coincidiram
  com fonte oficial; os demais permaneceram retidos para validação.
- Promobit: 61–62 candidatos reais por ciclo em cerca de 4 segundos.
- Telegram: 24/24 canais públicos e 64 códigos distintos por ciclo em produção;
  duas execuções automáticas consecutivas começaram com intervalo de 5min05s e
  terminaram saudáveis. O ciclo específico
  de cupons caiu de 11,1 s para 3,53 s ao deixar de resolver 124 redirecionamentos
  que não produziram nenhum produto. Cache de HTML agora vence em 120 segundos,
  portanto novas mensagens não dependem de reiniciar a VM.
- Amazon pública em 01/09: amostra real completa de cinco páginas com 10 ofertas de
  ativação e 10 cupons com código, zero rejeições e primeira leitura em 13,54 s. A
  execução persistida terminou em 35,9 s e reconciliou 80 entradas históricas
  ausentes somente porque o ciclo foi saudável e completo. O catálogo ativo ficou
  com 59 cupons Amazon: 49 códigos e 10 ativações oficiais.
- Landing oficial do ML: seis regulamentos reconhecidos e corretamente rejeitados
  por estarem vencidos; parser pronto para novos códigos ativos.
- ML afiliados: 29 códigos oficiais ativos aceitos em produção. O monitor HTTP de
  cupons-relâmpago roda a cada cinco minutos e leva cerca de 2,35 s; na amostra de
  28/08, a página oficial servia cinco campanhas de 08/06 já encerradas, todas
  rejeitadas sem apagar o catálogo anterior.
- Radar relâmpago em produção (versão 280): ML oficial, Pelando e Telegram
  executaram dois ciclos consecutivos em aproximadamente cinco minutos. O novo
  contrato `coupons-carousel` do ML expôs seis vouchers personalizados/opacos;
  todos os seis foram rejeitados como `invalid_code`, sem fabricar códigos. O
  Pelando aceitou oito cupons de três lojas em cada ciclo.
- Snapshot `lules`/WhatsApp em 01/09 após reconciliação saudável: 1.237 estados,
  901 prontos, 256 coletados, 56 aguardando link e 24 descartados. Por marketplace,
  o ML tem 892 prontos; a Amazon tem 10 prontos e 49 códigos comunitários retidos;
  a Shopee tem 58 comunitários retidos. Os retidos não foram promovidos apenas por
  coincidência entre Telegram/agregadores: faltou prova oficial fresca ou carrinho.
  A redução em relação ao snapshot de 28/08 é reconciliação de catálogo ausente e
  gates mais estritos, não perda silenciosa de coleta.
- Validação sem compra: ledger com constraint PostgreSQL `no_purchase`, RLS por
  tenant, alvos por categoria e executor transacional. Em produção, 42 hipóteses
  incompatíveis foram encerradas com evidência e 163 tentativas coerentes ficaram
  na fila (37 Amazon e 126 ML). O executor evita trabalho duplicado, recupera worker
  interrompido, preserva o carrinho auditado e recusa “aceito” sem redução monetária.
- Mensagens: validade só aparece quando existe, desconto só é anunciado quando
  comprovado pelo histórico, selo relâmpago só aparece em oferta realmente marcada
  como relâmpago e campos configuráveis são escapados contra HTML/XSS.
- Desempenho/RLS: a validação criptográfica do contexto tenant passou a ser um
  `InitPlan` por consulta, em vez de ser recalculada para cada linha. O plano real
  do PostgreSQL de produção confirmou `InitPlan` e `One-Time Filter`; o isolamento
  e a assinatura HMAC permanecem obrigatórios para dados privados.
- Ranking e conversão (versão 281): métricas oficiais de ML/Amazon alimentam o
  ranking por limite inferior de Wilson e confiança de amostra; falhas de envio
  não contam como exposição A/B. Na conta `lules`, os dois portais passaram de
  `url_missing` para o preflight correto `session_missing`; ainda existem zero
  linhas de receita até a conexão das sessões exclusivas de relatório.
- Contenção Amazon/Shopee: a busca HTML genérica da Amazon deixou de reservar o
  único Chromium por até 12 termos consecutivos. Ela agora percorre duas categorias
  rotativas por ciclo, usa timeout menor, cede entre termos para uma fonte em fila
  e publica métricas de duração/completude. Fontes Chromium que perdem o lease
  sinalizam a fila automaticamente. O preparo caro de produtos também cede depois
  do primeiro item quando outra esteira aguarda, em vez de readquirir o slot até
  12 vezes. A verificação de até 20 destinos também cede entre links; a validação
  de produção é o gate dos deploys 284/285/286.
- Corroboração deduplicada: a auditoria de 01/09 encontrou 20 códigos comunitários
  com observação aceita de fonte oficial nos sete dias anteriores que o gate não
  enxergava, porque consultava somente linhas duplicadas do catálogo. O livro de
  evidências agora também corrobora, limitado à mesma loja, inventário público,
  resultado aceito e janela fresca de 48 horas; evidência privada não cruza tenant.
- Testes automatizados: 1.404 testes Django e 169 testes Node aprovados; 24 testes
  direcionados de isolamento, permissões, CSRF, SQLi e XSS aprovados; nenhuma
  migração pendente e compilação limpa.
- Auditoria de produção: papel runtime sem `SUPERUSER`, `BYPASSRLS` ou ownership
  indevido; nenhum token legado do Mercado Livre armazenado em texto puro.
- Isolamento executado no deploy v309: o `tenant_isolation_probe` rodou com a role
  mínima `spreading_runtime` e os tenants reais `lules`/`teste1`. UUID e GUC
  falsificados enxergaram zero linhas privadas, `FORCE RLS` permaneceu ativo, o
  segredo HMAC ficou inacessível e a tentativa de escrita cruzada foi bloqueada.
- Restore ensaiado em 01/09 sem alterar a produção: o snapshot de 15 horas do volume
  `vol_vwn1owm691gj6p8v` foi restaurado em um cluster isolado com a mesma imagem e
  volume de 3 GB. O PostgreSQL recuperou o WAL e abriu `spreading_web` com 54 tabelas,
  111 migrações, quatro tenants e 9.199 disponibilidades de `lules` no ponto do
  snapshot. `pg_amcheck` verificou 533 relações/49.136 páginas sem erro; havia zero
  índices inválidos e zero constraints não validadas. O cluster temporário foi
  destruído após a prova para não gerar custo recorrente.
- Infraestrutura após os deploys `spreading-wa` v89 e `spreading-web` v289: web com
  2 shared CPUs/1 GB; worker e WhatsApp com 2 shared CPUs/2 GB; PostgreSQL com
  1 shared CPU/1 GB; sete GB de volumes. Pela tabela oficial GRU, o custo-base caiu
  de cerca de US$81,61 para US$57,37/mês (US$56,32 de compute + US$1,05 de volume),
  sem crédito na organização. Pela cotação de R$5,185/USD observada em 01/09, são
  cerca de R$297,45 antes de câmbio/tributos/tráfego: ainda não há margem suficiente
  para marcar a meta de R$300 como cumprida. Uma reserva anual de máquina fornece
  US$5/mês de crédito por US$36/ano e abre margem, mas é compra e depende de ação do
  titular da cobrança; não será adquirida automaticamente.
- Benchmark v289 da VM web reduzida: 100/100 health checks sequenciais com p95
  161 ms; 200/200 sob 40 conexões simultâneas; tela autenticada `/scrapers/top/` da
  conta `lules`, com 145 KB, respondeu 80/80 vezes sob oito conexões simultâneas,
  p50 538 ms e p95 1,54 s. Um Chromium real abriu o Mercado Livre usando cerca de
  542 MB e deixou aproximadamente 443 MB disponíveis. Após os testes, os checks
  continuaram verdes, sem OOM, reinício ou pressão de memória.
- Relatório de abundância no deploy v290: a consulta de exaustão carregava todo o
  histórico de execuções para usar somente a última linha por fonte e levou 10,88 s.
  O subselect limitado a uma linha por fonte reduziu essa etapa para 145 ms (cerca
  de 75 vezes) e o relatório completo para 186 ms no PostgreSQL de produção. O
  resultado fresco e não inflado ficou em 983/10/0 cupons prontos e 452/152/59
  candidatos observados em 24 h para ML/Amazon/Shopee; portanto Amazon e Shopee
  continuam abaixo das metas de abundância e descoberta.

- Busca oficial Amazon (deploys v291/v292): o parser reconhece somente a frase
  inequívoca do card `Você paga R$ X com o cupom`, associada a ASIN e preço final
  plausível. Uma prova real local encontrou 36 ofertas e dois cupons oficiais em
  uma página de `eletronicos`; produto que apenas contém “cupom” no título não
  entra. A mesma URL em GRU e numa VM efêmera IAD do Fly devolveu HTTP 503/“Algo
  deu errado”. Isso agora é falha técnica, nunca inventário vazio. Foi preparada
  saída residencial PAYG exclusiva para páginas públicas, bloqueando imagens,
  fontes e mídia para conter banda; cookies de compradores não passam pelo proxy.
- Acelerador Amazon e prova v294: depois da primeira navegação aceita, a paginação
  usa a própria sessão Amazon e extrai cada página em uma única avaliação, sem
  baixar imagens. Em amostra real local, três páginas de `casa` caíram para 4,70 s
  e encontraram quatro cupons oficiais; 36 páginas em 12 categorias levaram 46,19 s,
  com 739 ofertas e 28 cupons oficiais. A fatia padrão passou de dois termos de
  produto para quatro categorias amplas, rotativas e específicas para cupons. A
  suíte completa está verde em 1.406 testes. No primeiro canário Fly, a coleta
  respeitou duas lanes já enfileiradas, cedeu após uma página em 3,46 s e ainda
  persistiu 35 ofertas e dois cupons. Na janela seguinte, a fatia completa de 12
  páginas terminou saudável em 17,13 s: 230 ofertas, 18 cupons oficiais, uma única
  navegação e 11 paginações internas em 12,10 s. A conta `lules` subiu de 12 para
  30 cupons Amazon prontos, sem proxy e sem custo adicional. Uma sonda mais funda
  de dez páginas de `brinquedos` encontrou 13 cupons, mas apenas quatro eram novos,
  elevando a conta a 34; a evidência de rendimento decrescente favorece ampliar
  categorias antes de simplesmente aumentar profundidade.
- Evidência visível v294: a vitrine final agrupa prova por cupom e mostra confirmação
  no carrinho, corroboração, observação direta na loja ou fonte estruturada, além
  da quantidade de fontes e da recência. A página real de `lules` respondeu HTTP
  200 em 739 ms (244 KB); os 20 cupons exibidos tinham recência, três estavam
  marcados como observados na loja e 17 como provenientes de fonte estruturada.
- Consenso independente preparado para o próximo deploy: a auditoria dos códigos
  retidos encontrou dez códigos Amazon repetidos em duas ou três fontes distintas;
  todos os dez concordam também no tipo e valor do desconto. O gate agora aceita
  fonte direta, checkout sem compra ou ao menos duas fontes públicas independentes,
  recentes e concordantes. Mesmo código com escopos descritos de forma diferente
  conta uma vez; discordância entre valor fixo e percentual continua bloqueada.
  A suíte completa passou em 1.408 testes e a regressão focada em mais 100 testes.
- Consenso comprovado no deploy v295: a reprojeção da conta `lules` promoveu seis
  códigos Amazon únicos e descartou sete duplicatas de menor precedência, levando
  a loja de 34 para 40 cupons prontos (déficit 60), sem inflar o placar. A página
  autenticada respondeu em 697 ms e exibiu seis selos `Corroborado`, dois
  `Observado na loja`, recência nos 20 itens e 14 badges de múltiplas fontes. No
  ML, a mesma deduplicação reduziu a fila `ml_session_expired` de 93 para 56 sem
  alterar os 892 cupons únicos prontos.
- Novas fontes nos deploys v296/v297: Bia Garimpa e CupomSpot passaram a integrar
  o radar comunitário; Prima Ryca entrou com filtro estrito de marketplace e
  código. O Telegram permaneceu saudável em 24/24 canais, e o ciclo mais recente
  observado trouxe 71 cupons. O catálogo fresco/publicável chegou a 1.498 cupons,
  sem promover alegação comunitária isolada como prova.
- Amazon sustentável no deploy v298: o catálogo passou a percorrer 37 categorias
  amplas em rotação, quatro termos e até três páginas por ciclo. Isso aumenta a
  cobertura ao longo do dia sem monopolizar o Chromium nem elevar a máquina.
- Justiça de capacidade no deploy v299: indisponibilidade temporária do único
  Chromium passou a ser `capacity_deferred`, sem degradar fonte ou catálogo. A
  varredura completa do ML cede a vaga e retoma da página exata; no ciclo real,
  coletou 38 ofertas/12 itens na página 1, e cinco minutos depois retomou somente
  o ML na página 2, sem repetir Amazon e demais fontes já concluídas.
- Fila de validação sem esconder estoque no deploy v300: a seleção agora aplica a
  projeção `ready` antes do corte por recência. O defeito anterior tinha 1.498
  cupons publicáveis e 53 prontos para `lules`, mas mostrava zero nas três regras
  porque 80 recém-coletados pendentes ocupavam o recorte. Após a correção, as
  regras 35/36/37 passaram respectivamente a 41/6/23 cupons candidatos, além dos
  produtos de fallback.
- Estratégia cupom-first no deploy v301: cupons validados passaram a preceder
  promoções comuns; score, comissão e desempenho continuam ordenando apenas a
  qualidade dentro de cada tipo. Os cinco primeiros itens das três regras reais
  de `lules` passaram a ser cupons, sem remover o fallback de produtos quando o
  estoque entra em cooldown.
- Canário read-only nos deploys v302/v303: o diagnóstico usa o seletor e o
  renderizador reais, mas não cria `Publicacao`, não abre navegador e não envia.
  A v303 reproduz também o caminho correto das campanhas ML — container oficial
  específico mais rastreio persistido da conta, sem cache redundante por campanha.
  Em produção, as três regras de `lules` passaram: ML e grupo misto usaram
  `_Container_13975432` com `matt_word=lpohoffmann&matt_tool=24634771`; Amazon usou
  o ASIN escolhido com `tag=luizahfn-20`. Todas as mensagens continham link,
  instrução explícita de resgate e tamanho válido para WhatsApp.
- Produção v303: web e worker iniciados e saudáveis, check externo `/healthz` HTTP
  200. A suíte completa passou em 1.424/1.424 testes Django. O envio confirmado ao
  grupo ainda não foi executado porque o WhatsApp de `lules` está `inactive` em
  `recuperacao_pausada`; o sistema corretamente não cria/publica nada nesse estado.
- Relatório por conta no deploy v304: o comando de abundância passou a projetar o
  estoque com a configuração e as integrações reais de `lules`, evitando confundir
  inventário global com cupom publicável. A linha de base verificável ficou em
  892/83/0 cupons prontos para ML/Amazon/Shopee.
- LinkerHub no deploy v305: o coletor público lê uma única página, não segue link
  afiliado e só aceita código, marketplace, desconto e validade coerentes. Na prova
  local, 101 cards produziram 53 candidatos conservadores (43 ML, quatro Amazon e
  seis Shopee); foram rejeitados placeholders, código divergente, URL de outra loja,
  produto sem desconto explícito e duplicatas. Os 53 foram persistidos em produção,
  mas permaneceram sujeitos a corroboração ou checkout — a fonte não inflou o placar
  de prontos. A reprojeção encontrou 40 cupons Shopee elegíveis, ainda bloqueados
  pela integração desconectada.
- Qualidade e independência no deploy v306: preço de produto em texto livre deixou
  de ser interpretado como desconto, e o falso cupom Amazon `10OFFAGORAOU` (R$656 do
  monitor) foi expirado em produção. Méliuz, Promobit e Picodi agora contam como uma
  única família editorial no consenso, pois não constituem evidências independentes.
  A suíte completa passou em 1.434/1.434 testes; web e worker v306 permaneceram
  saudáveis e `/healthz` respondeu HTTP 200.
- Reprojeção final v306, idêntica para WhatsApp e Telegram: 975 prontos, 40 elegíveis,
  59 aguardando link, 210 coletados ainda sem prova suficiente e 272 descartados.
  Por loja são 893/83/0 prontos para ML/Amazon/Shopee; Amazon continua 17 abaixo da
  meta e Shopee tem 40 corroborados, porém nenhum publicável sem sessão/afiliado.
  A regra de família editorial não derrubou os 975 prontos, demonstrando que o
  estoque atual não dependia de consenso duplicado entre empresas do mesmo grupo.
- Retomada Amazon nos deploys v307/v308: a prova no Fly mostrou que a busca oficial
  conseguia encontrar cupons, mas sob disputa sempre recomeçava na página 1. O novo
  cursor é amarrado à fatia rotativa e persiste a página seguinte. O canário v307
  encontrou quatro cupons na primeira página, mas reprovou porque tentou escrever o
  cursor ainda dentro do contexto assíncrono interno do Playwright. Na v308 a escrita
  passou a ocorrer somente depois de liberar navegador e lease; 1.438/1.438 testes
  Django e 169/169 Node ficaram verdes.
- Prova de produção v308: uma passada sem contenção concluiu 12/12 páginas em 51,9 s,
  com 188 ofertas e 16 cupons oficiais. No canário controlado com fila, a primeira
  execução registrou `cursor_start=0`/`cursor_next=1`; a seguinte iniciou em 1 e
  registrou 2, confirmando avanço em vez de repetição do topo. Após reprojeção,
  `lules` chegou a 995 cupons prontos: 894 ML, 102 Amazon e zero Shopee. Amazon passou
  a meta mínima de 100 com 70 cupons de ativação e 32 avisos de código; Shopee segue
  como o único déficit de abundância publicável, com 40 elegíveis bloqueados pela
  integração desconectada.
- Contrato oficial Shopee no deploy v309: a investigação de rede identificou o POST
  `/api/v1/microsite/get_vouchers_by_collections`, que devolve `promotion_id`, tipo e
  valor do benefício, mínimo, teto, validade, quota e assinatura pública. O coletor
  agora prefere esse JSON ao texto visual, rejeita cashback, expirado, esgotado,
  assinatura inválida e resposta truncada. Na prova residencial real, 14 vouchers
  produziram nove a dez descontos válidos conforme a quota instantânea; datas-limite
  artificiais de 2038 não são exibidas como promessa de validade.
- Bloqueio Shopee comprovado: o mesmo endpoint oficial no worker GRU respondeu HTTP
  403 com `error=90309999` e redirecionamento anti-tráfego. A v309 preserva o catálogo
  nesse estado e aceita uma saída residencial opcional, compartilhada por padrão com
  a Amazon; imagens, fontes e mídia são bloqueadas para reduzir banda. A suíte ficou
  verde em 1.443/1.443 testes Django e 169/169 Node, e web/worker v309 permaneceram
  saudáveis com `/healthz` HTTP 200. Sem credencial de afiliado e sem sessão/proxy da
  Shopee, os 40 candidatos corroborados ainda não podem virar links comissionados nem
  ser validados em checkout.
- Escrita idempotente no deploy v310: a auditoria de `pg_stat_user_tables` mostrou
  3.145.715 updates acumulados em `scrapers_cupomdisponibilidade`; numa amostra de
  nove minutos antes da correção, 4.540 eram apenas toques sem mudança (~500/min).
  A projeção deixou de tratar `updated_at` como heartbeat e a reconciliação completa
  do ML deixou de reexpirar cupons/produtos já no mesmo veredito. Duas reprojeções
  consecutivas da conta `lules`, com 1.575 estados cada, produziram delta zero nessa
  tabela em produção. O inventário permaneceu em 895 cupons ML e 102 Amazon prontos;
  Shopee permaneceu corretamente bloqueada na integração. A suíte integral passou em
  1.445/1.445 testes Django e 169/169 Node; web/worker v310 e `/healthz` ficaram verdes.
- PostgreSQL após v310: mesmo sem os updates inúteis, a VM de uma shared CPU voltou
  a falhar no check por esperar CPU e mostrou PSI `avg10=29,50`/`avg60=35,98`.
  Um snapshot fresco de 1,4 GiB foi concluído antes do resize e o mesmo primary/volume
  passou para duas shared CPUs com 1 GB (+US$1,12/mês). Sob 100 health checks
  sequenciais e 200 com concorrência 40, houve 300/300 respostas HTTP 200, p95 de
  49,8 ms e 65,2 req/s; depois da carga o PSI caiu para `avg10=0,76`/`avg60=0,38`,
  sem pressão de memória, e os três checks `pg`/`role`/`vm` permaneceram verdes.
  A imagem do Flex Postgres também foi atualizada de 17.2/v0.1.0 para 17.7/v0.2.0;
  depois do upgrade, os três checks continuaram verdes, `/healthz` respondeu 200 e
  web/worker permaneceram no v310. Uma nova execução do probe de isolamento aprovou
  RLS/FORCE RLS, bloqueio de escrita cruzada e inacessibilidade do segredo HMAC. A
  consulta pós-upgrade da `lules` preservou 1.576 estados, dos quais 997 prontos.
- Custo conservador da topologia estável: US$58,49/mês antes de câmbio/tributos.
  Três blocos de reserva shared/GRU de US$36/ano (US$108 adiantados) dão US$15/mês
  de crédito e custam US$9/mês amortizados, levando o custo econômico a US$52,49.
  Pela PTAX de referência de setembro (R$5,2236/USD) mais margem de 5%, isso equivale
  a aproximadamente R$287,90/mês. A compra é ação financeira no painel do titular e
  ainda não foi executada; portanto o gate de R$300 continua aberto até a reserva
  aparecer na cobrança real.
- Deploy v311: o alerta de backlog deixou de contar `amazon_tag_missing`, pois a
  tag é configuração da conta e não trabalho que o worker consiga concluir. O
  teste de regressão entrou na suíte integral (1.445/1.445 Django; 169/169 Node).
  Release, migrações, reforço de RLS e smoke checks passaram. Em produção,
  `code_not_ready_20m` caiu de 128 para zero, `projection_stale` passou de 647 para
  239 casos realmente acionáveis e `browser_wait_over_60m` de 26 para 25. A conta
  `lules` preservou 1.576 projeções e 997 cupons prontos; `/healthz` respondeu 200,
  web/worker v311 e todos os checks do PostgreSQL permaneceram verdes.

## Gates de deploy desta revisão

- [x] Suítes Django e Node integralmente verdes.
- [x] Verificação direcionada de isolamento, permissões, CSRF, SQLi e XSS.
- [x] Auditoria do papel runtime e de segredos legados em produção.
- [x] Snapshot verificável dos volumes e comando de rollback anotado.
- [x] Deploy canário da correção de RLS e benchmark comparativo em produção.
- [x] `tenant_isolation_probe` aprovado no código já implantado.
- [ ] Canário funcional pela conta `lules`, sem concluir compra.
- [x] Restore de snapshot ensaiado em cluster isolado e verificado até as páginas.
- [ ] Topologia 24/7 comprovadamente abaixo de R$300/mês com margem cambial/tributária.
- [ ] Sete dias consecutivos de observação sem incidente crítico.

## Condições externas para o canário final

- Sessão do Mercado Livre da conta `lules` conectada para criar/renovar links;
  campanhas que já possuem rastreio persistido continuam aptas sem Chromium.
- WhatsApp da conta `lules` conectado e fora de `recuperacao_pausada`.
- Destino Telegram de teste configurado.
- Credencial de IA válida é opcional para formatos estruturados e necessária para
  cobrir linguagem livre com maior recall; falha da IA usa parser local seguro.
- Sessão de compras Shopee da conta `lules` conectada; a vitrine pública exige
  autenticação no IP atual.
- Para ampliar a Amazon fora da central oficial: credencial de proxy residencial
  PAYG em `AMAZON_PUBLIC_PROXY_*`. A Creators API cobre catálogo e deals, mas o
  `OffersV2` atual não expõe `Promotions`, então não substitui a busca de cupons.
