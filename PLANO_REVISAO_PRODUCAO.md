# Plano de revisão e aceite de produção

Este documento é o contrato técnico de entrega. “Encontrado” não significa
“válido”, “válido” não significa “afiliado” e “afiliado” não significa “enviado”.
Cada etapa precisa de evidência própria.

## Metas obrigatórias

- [ ] Descoberta sem teto artificial: Mercado Livre, Amazon, Shopee, feeds de
  afiliados, páginas públicas, agregadores e ao menos 12 canais públicos do
  Telegram. Paginação prossegue até fim real, repetição, orçamento documentado ou
  bloqueio explícito.
- [ ] Cobertura: coletar pelo menos 95% de uma amostra manual de cada fonte ativa e
  superar 50 cupons/ofertas-com-cupom prontos no total quando as fontes observadas
  tiverem esse inventário, sem fabricar volume e sem limitar uma loja a dez itens.
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
- Promobit: 61–62 candidatos reais por ciclo em cerca de 4 segundos.
- Telegram: 12 canais, 45 códigos e 18 ofertas reais na amostra de 27/08/2026;
  ciclo caiu de 151,7 s/6 canais para 31,1 s/12 canais.
- Amazon pública: 10 ofertas e 9 cupons distintos, inventário completo e zero
  rejeições em 31,9 s na amostra real.
- Landing oficial do ML: seis regulamentos reconhecidos e corretamente rejeitados
  por estarem vencidos; parser pronto para novos códigos ativos.
- Testes direcionados: 229 aprovados; nenhuma migração pendente.

## Condições externas para o canário final

- Sessão do Mercado Livre da conta `lules` conectada.
- WhatsApp da conta `lules` conectado e fora de `recuperacao_pausada`.
- Destino Telegram de teste configurado.
- Credencial de IA válida é opcional para formatos estruturados e necessária para
  cobrir linguagem livre com maior recall; falha da IA usa parser local seguro.
