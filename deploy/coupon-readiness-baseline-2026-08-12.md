# Linha de base — prontidão de cupons (2026-08-12)

Diagnóstico somente leitura executado em `spreading-web` antes das mudanças deste
plano, às 17:29 UTC. Nenhuma linha de produção foi alterada.

- Flags: `scrape.enabled` ausente; `links.enabled` ausente.
- Cupons: 6.470 no total; 2.381 ativos e frescos; 2.334 sem código.
- Preparos: 2.055 `vazio`, 412 `pronto`, 254 `erro`, 5 `pendente`.
- Erros de preparo: 206 por capacidade de browser, 47 por sessão ML e 1 deadlock.
- Todos os 2.055 vazios tinham o texto genérico “Nenhum produto comprovadamente
  aplicável”.
- Amostra de 30 projeções `product_match_pending`: nenhuma tinha relação confirmada
  ou relação de container.
- Produtos ML: 10.537 `stale`, 3.579 `ativo`, 3.553 `invalido`, 910 `expirado`.
- Projeções: 13.392 no total; 3.868 órfãs, todas ligadas a cupom inativo.
- Links por usuário: 12.267 `pronto`, 2.056 `pendente`, 913 `nao_afiliavel`,
  185 `erro`; 981 com `verificado_ok=False`.
- Motivos mais numerosos: `ml_session_missing` 4.664, `state_expirado` 3.831,
  `ml_session_expired` 2.332, `product_match_pending` 1.835,
  `affiliate_link_pending` 223 e `preparation_failed` 205.

Observação de rollout: o diagnóstico também mostrou que os 2.320 cupons de ativação
ML estavam bloqueados pela feature flag da organização. A correção dos gates não
substitui a decisão de liberar essa flag por organização.
