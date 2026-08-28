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
- Méliuz: novo radar público sem Chromium encontrou 84 códigos distintos em 3,9 s
  (23 Amazon, 38 Mercado Livre e 23 Shopee), descartando 14 placeholders, sete
  entradas inválidas e cinco duplicatas. Em produção, 13 códigos do ML coincidiram
  com fonte oficial; os demais permaneceram retidos para validação.
- Promobit: 61–62 candidatos reais por ciclo em cerca de 4 segundos.
- Telegram: 12/12 canais e 51 códigos distintos em produção; o ciclo específico
  de cupons caiu de 11,1 s para 3,53 s ao deixar de resolver 124 redirecionamentos
  que não produziram nenhum produto. Cache de HTML agora vence em 120 segundos,
  portanto novas mensagens não dependem de reiniciar a VM.
- Amazon pública: 10 ofertas e 9 cupons distintos, inventário completo e zero
  rejeições em 31,9 s na amostra real.
- Landing oficial do ML: seis regulamentos reconhecidos e corretamente rejeitados
  por estarem vencidos; parser pronto para novos códigos ativos.
- ML afiliados: 29 códigos oficiais ativos aceitos em produção. O monitor HTTP de
  cupons-relâmpago roda a cada cinco minutos e leva cerca de 2,35 s; na amostra de
  28/08, a página oficial servia cinco campanhas de 08/06 já encerradas, todas
  rejeitadas sem apagar o catálogo anterior.
- Snapshot `lules`/WhatsApp após o quarto deploy: 1.182 cupons frescos — 998
  prontos, 173 comunitários aguardando validação, oito vouchers Shopee aguardando
  integração própria e três descartes explícitos. A projeção levou 1,19 s.
- Mensagens: validade só aparece quando existe, desconto só é anunciado quando
  comprovado pelo histórico, selo relâmpago só aparece em oferta realmente marcada
  como relâmpago e campos configuráveis são escapados contra HTML/XSS.
- Desempenho/RLS: a validação criptográfica do contexto tenant passou a ser um
  `InitPlan` por consulta, em vez de ser recalculada para cada linha. O plano real
  do PostgreSQL de produção confirmou `InitPlan` e `One-Time Filter`; o isolamento
  e a assinatura HMAC permanecem obrigatórios para dados privados.
- Testes automatizados: 1.304 testes Django e 169 testes Node aprovados; 24 testes
  direcionados de isolamento, permissões, CSRF, SQLi e XSS aprovados; nenhuma
  migração pendente e compilação limpa.
- Auditoria de produção: papel runtime sem `SUPERUSER`, `BYPASSRLS` ou ownership
  indevido; nenhum token legado do Mercado Livre armazenado em texto puro.
- Infraestrutura atual: web e worker com 2 shared CPUs/2 GB, WhatsApp com 2 shared
  CPUs/4 GB e PostgreSQL com 1 shared CPU/1 GB. O custo-base oficial é cerca de
  US$81,61/mês antes de tráfego e excede a meta de R$300; a topologia final ainda
  depende do benchmark após o deploy e de migração/resize com restore ensaiado.

## Gates de deploy desta revisão

- [x] Suítes Django e Node integralmente verdes.
- [x] Verificação direcionada de isolamento, permissões, CSRF, SQLi e XSS.
- [x] Auditoria do papel runtime e de segredos legados em produção.
- [x] Snapshot verificável dos volumes e comando de rollback anotado.
- [x] Deploy canário da correção de RLS e benchmark comparativo em produção.
- [ ] `tenant_isolation_probe` aprovado no código já implantado.
- [ ] Canário funcional pela conta `lules`, sem concluir compra.
- [ ] Restore ensaiado e topologia 24/7 comprovadamente abaixo de R$300/mês.
- [ ] Sete dias consecutivos de observação sem incidente crítico.

## Condições externas para o canário final

- Sessão do Mercado Livre da conta `lules` conectada.
- WhatsApp da conta `lules` conectado e fora de `recuperacao_pausada`.
- Destino Telegram de teste configurado.
- Credencial de IA válida é opcional para formatos estruturados e necessária para
  cobrir linguagem livre com maior recall; falha da IA usa parser local seguro.
