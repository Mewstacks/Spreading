# Spreading — v1 × v2, mercado e cupons (07/11/2026)

Medições de hoje no stack local (docker compose) e nas páginas públicas. O que não foi medido está marcado
como estimativa. Documento de decisão: o v1 segue intocado e `main` não é substituída até o dono mandar.

## 1. Resumo

1. **O v2 já faz o essencial do v1 com muito menos peso**: WhatsApp sem Chromium (WAHA/GOWS), fila com
   idempotência, preço conferido ao vivo, foto, link do dono, painel no desenho do v1. RAM medida do stack
   inteiro: **553 MiB** (waha 344, web 108, worker 54, db 47), contra ~1,17 GB só do Chromium do WhatsApp no v1.
2. **O gargalo de cupons não é fonte, é link.** O banco tem 634 pares cupom+produto do ML (439 conferíveis ao
   vivo na API), mas **só 2 têm link `meli.la` do dono**. Gerar esses links (Link Builder logado na conta de
   afiliado da lules) destrava até 438 pares de uma vez. Nenhuma fonte nova rende isso.
3. **O que o v1 tem e o v2 não tem** é, quase tudo, escolha: SaaS multiusuário, Telegram, raspagem de ML
   logado, agregadores, IA de texto, redirect rastreado. Faltam de verdade: **receita/comissão no painel**,
   **cupom manual** e **Awin/Lomadee**. **Várias contas de WhatsApp** (até 5) e **alertas** ficaram prontos
   nesta rodada. Estado de cada um na seção 7.
4. **Mercado**: o padrão dos concorrentes (R$ 29,90 a R$ 399/mês) é sessão em nuvem, piloto automático por
   filtros, espelho de grupos, conversão de redes e comissão no painel. Ninguém documenta **cupom verificado**:
   é o diferencial que o v2 já tem (par provado pelo preço-base ao vivo).
5. **Risco que só o dono decide**: raspar páginas públicas (Amazon, vitrine do ML) viola termos; aviso de
   afiliação foi dispensado (CONAR 2026 pede identificação imediata). Chaves de desligar existem (seção 6).

## 2. O que o v1 tem e o v2 não tem

Fonte: rotas e módulos do v1 (`python/django/apps/scrapers`, 19 fontes registradas) contra o v2 de hoje.

| Área | v1 | v2 | Leitura |
|---|---|---|---|
| WhatsApp | whatsapp-web.js, 1 Chromium por sessão, supervisor e vigia | WAHA/GOWS, 344 MiB, outbox, canário | **v2 melhor** |
| Várias contas de WhatsApp | 1 sessão por usuário | até 5 números: cada grupo sai pelo seu, teto por hora por número, tela WhatsApp | **v2 melhor** (novo) |
| Fila de envio | reconciliação parcial (grupo parava 6 h) | `incerto` nunca reenvia sozinho | **v2 melhor** |
| Agendamento | janela, dias, intervalo, teto | idem + variação de ±25% no intervalo | igual |
| Envio manual | produto, cupom, aviso de cupons | "Enviar agora" e "Enviar" por oferta | igual (aviso removido por decisão) |
| Telegram (envio e radar de canais) | sim | não, por decisão ("só WhatsApp") | decisão |
| ML: feeds da equipe de afiliados | sim | sim | igual |
| ML: API de catálogo | sim | sim | igual |
| ML: raspagem logada (containers, relâmpago, ofertas) | sim, ~2,3 mil pares em agosto | não: murado para IP de datacenter e vedado pelos termos | decisão |
| ML: vitrine pública `/ofertas/cupons` | não | **sim** (novo) | v2 à frente |
| Amazon | login e raspagem | busca pública + página de cupons | equivalente |
| Shopee | API e cupons públicos | API (ofertas e comissão); sem vouchers | parcial |
| Awin | catálogo, programas, sincronização | não | falta (credencial) |
| Agregadores (Promobit, Pelando, Méliuz, LinkerHub, comunidade) | sim | não | decisão |
| Cupom manual | tela própria | não | **falta** |
| Prova de cupom por carrinho (Playwright) | sim | não, por desenho | decisão |
| Guarda de queda real em 30 dias | sim | **sim** (novo nesta rodada) | igual |
| IA de texto (Haiku), política editorial | sim | não: formato único, sem IA | decisão |
| Redirect rastreado `/r/<token>` | sim (já removido do ML) | não: ML veda encurtador/redirect | decisão |
| Receita e comissão (sincronizar, painel) | sim | não | **falta** |
| Saúde, incidentes, alertas | sim | `/status` por número, tela **Fontes**, canário, **alertas opt-in** (WhatsApp e e-mail) | quase igual (sem reteste de incidente) |
| Multiusuário SaaS (cadastro, e-mail, cotas, impersonar, admin) | sim | um dono; orgs prontas no modelo | fora de escopo |
| Vitrine pública `/top` | sim | não | Fase 7 |
| Conexão de ML/Amazon por tela remota | sim | Link Builder por linha de comando | menor |

Peso: v1 ≈ 64 mil linhas de Python, 6,6 mil de Node, 45 modelos, RLS de 3 papéis, 9 filas, leases, watchdogs;
custo de nuvem do v1 US$ 74,65/mês (setembro). O v2 roda em Docker Compose único.

## 3. Produtos validados no mercado

Material público consultado em 07/11/2026 (planos e páginas de venda; sem conta, login ou download).

| Produto | Faixa de preço | Padrão que valida |
|---|---|---|
| DivulgaNinja | R$ 49,90–149,90 | sessão em nuvem, piloto automático |
| Busqy | R$ 99–399 | volume, várias contas |
| Afilira | R$ 47–197 | filtros, templates |
| Oferteiro | R$ 29,90–99,90 | espelho de grupos |
| Espelha Grupos | R$ 39–99 | espelho com troca de link |
| Shozap, LucreShop, Divulgador Inteligente | n/d | fila, janela, vitrine |
| Extensões GDW Afiliados (91 usuários), DivulgaLinks (3 mil) | grátis/baixo | gerar link com a sessão do próprio afiliado |

| Recurso do mercado | v2 hoje | Ação |
|---|---|---|
| Sessão WhatsApp por QR/código | sim | — |
| Piloto automático por filtros (nicho, loja, preço, horário) | sim | — |
| Bloqueio de oferta repetida | sim (15 dias por produto e grupo) | — |
| Várias contas/números | **sim**, até 5 | — |
| Comissão e cliques no painel | não | **construir** (Shopee `conversionReport` primeiro) |
| Conversão de redes (Awin, Lomadee, Rakuten) | não | credenciais do dono |
| Espelho de grupos | não | só com decisão do dono (termos, direitos) |
| Link curto e cliques | não | **vedado no ML** (cláusula 1.8) |
| Link inteligente para grupo com vaga | não | Fase 7 |
| IA de texto e A/B | não | fora por decisão (formato único) |
| Gerar link com a sessão do afiliado (extensão) | Link Builder no servidor | extensão = Fase 7 |
| **Cupom verificado** | **sim** (par com preço-base conferido) | manter como vitrine do produto |

## 4. Cupons: mais e melhores

### 4.1 Onde estão hoje (medido em 07/11)

| Fonte | Rendimento medido | Estado no v2 | Termos/risco |
|---|---|---|---|
| Pares cupom+produto do v1 | 634 pares, 439 conferíveis ao vivo, **2 com link do dono** | importados | sem risco novo |
| Feed de cupons da equipe de afiliados do ML | 103 cupons de campanha (container, sem produto) | sim | feed sem contrato (conta pessoal no GitHub) |
| Vitrine `/ofertas/cupons` do ML | 6 campanhas ativas (de R$ 10 OFF a 80% OFF, 9 a 1.171 itens) + 5 ofertas por carga | **sim (novo)**, 1 carga/hora | extração do site: `ML_PUBLIC_PAGES=0` desliga |
| Amazon `/deals` cupons | 21 ofertas com cupom e preço final na própria página | sim | termos da Amazon: `AMAZON_COLLECT=0` desliga |
| Amazon busca (selo de cupom) | 142 ofertas, parte com cupom | sim | idem |
| Shopee API | 79 ofertas; **a API não lista voucher**, só comissão | sim | oficial |
| Páginas de container do ML (produtos do cupom) | v1: ~2,3 mil pares em agosto; hoje murado (IP de datacenter) | não | vedado pelos termos; só com sessão do afiliado |
| Awin Offers API | vouchers de anunciantes aderidos | não | precisa conta e adesões |
| Lomadee | campanhas e cupons | não | precisa chave |
| Agregadores e Telegram público | volume alto, sem validação própria | não | termos cinzentos; só como descoberta |
| Cupom manual (a lules cola) | volume humano | não | zero risco |

### 4.2 Como trazer mais (em ordem de retorno)

1. **Links do dono para os 439 pares conferíveis.** É a única ação que multiplica o envio hoje. O Link Builder
   está pronto e testado; falta o login da conta de afiliado da lules (`tools/ml_login.py`) e um lugar para
   rodar com IP que o ML aceite. Plano B já na tela **Links**: gerar à mão no Link Builder do ML e colar.
2. **Vitrine e Amazon rodando de hora em hora** (feito): entram sozinhas, sem trabalho humano.
3. **Cupom manual / colar produto**: a lules vê uma promoção boa, cola o link; o v2 confere o preço e entra.
   Zero risco, desbloqueia o que nenhuma fonte automática pega.
4. **Awin e Lomadee**: vouchers com contrato. Dependem de credenciais e adesões do dono.
5. **Páginas de container com a sessão do afiliado**: o maior volume do v1, mas é raspagem logada vedada
   pelos termos do ML (cláusulas 3.1 c e 4). Só com decisão explícita do dono.

### 4.3 Como trazer melhores

Gates que já barram o ruim (cada um tem teste): preço conferido ao vivo; "De" só com prova; desconto ≥ 70%
só com o anúncio confirmando; cupom no prazo; um cupom por mensagem; foto ≥ 400 px; cooldown de 15 dias;
link do dono e nunca a vitrine `/social/`. **Novos nesta rodada:**

- **Queda real**: produto com ≥ 3 outras observações em 30 dias só é anunciado se o preço está ≥ 3% abaixo
  da mediana delas; "De" de fachada não sai. Pares com cupom ficam de fora (o abatimento é o cupom).
- **Sem preço condicionado a pagamento**: cartão da vitrine com "no Pix" não vira oferta (a mensagem não diz
  a condição).
- **Comissão no ranking**: a Shopee informa `commissionRate`; entra no score (5%), desempatando ofertas
  parecidas. Sem dado, neutro.
- **Ritmo sem relógio**: o intervalo da regra varia ±25% por envio (determinístico: mesma espera por envio).

Ideias que ficam para depois: valor real do cupom no ranking (desconto efetivo = min(pct × preço, teto) ÷
preço), preferir produto de comissão maior no ML ("ganhos extras") e janela de melhor horário por comissão.

## 5. Números do stack local

| Item | Medido |
|---|---|
| RAM (docker stats, ocioso) | waha 344 MiB · web 108 · worker 54 · db 47 = **553 MiB** |
| Catálogo | ML 4.908 ofertas (2.796 com link do dono, 2.379 conferíveis ao vivo), Amazon 2.724, Shopee 79 |
| Funil da regra "robô aspirador + air fryer" | 118 prontas · 962 fora do nicho · 698 sem prova de preço · 15 em cooldown · 1 sem link |
| Suíte | 280 testes passam (`tools/verify.py`); Postgres 17 verificado antes desta rodada |

Hospedagem: 553 MiB ocioso cabe em VPS de 2 GB; o plano de 07/11 estimava R$ 40–90/mês (a confirmar no
checkout) contra US$ 74,65 da Fly. Nenhuma compra foi feita.

## 6. Conformidade e chaves de desligar

| Tema | Regra | Estado | Chave |
|---|---|---|---|
| ML: scraping (cláusula 4) | proíbe extração automatizada do site | vitrine pública 1×/hora; páginas de container fora | `ML_PUBLIC_PAGES=0` |
| ML: encurtador/redirect (1.8) | proíbe | nenhum redirect próprio; só `meli.la` do dono | — |
| ML: robôs que simulam comprador (1.9, 3.1 c) | proíbe | Link Builder só gera link do próprio afiliado, com teto/dia | `LINKBUILDER_ENABLED=0` |
| ML: só informação do anúncio (5.4) | proíbe comparar/inventar | "De" só de fonte oficial; sem histórico próprio impresso | — |
| Amazon: mineração/robôs | restringe | busca pública + `/deals`, 1×/hora por termo | `AMAZON_COLLECT=0` |
| Shopee: extração não autorizada | proíbe | só API oficial | — |
| CONAR 2026 | identificar publicidade logo no início | **sem aviso, por decisão do dono** | risco do dono |

## 7. Pedidos da conversa e estado

| Pedido | Estado |
|---|---|
| Reformular por completo, núcleo enxuto | feito na branch `v2`; `main` intocada |
| Lib de WhatsApp mais leve | feito: WAHA/GOWS, 344 MiB |
| Disparo melhor | feito: outbox, ACK, `incerto` sem reenvio, canário, ritmo variável |
| Mensagens melhores | feito: formato único com foto; sem aviso (decisão) |
| Cupons (principal) e promoções em volume | fontes feitas; **gargalo = links do dono** (seção 4) |
| Pesquisar e engenharia reversa | feito: termos, feeds, páginas, concorrentes |
| Hospedagem barata | medido; falta escolher e contratar o VPS (valor com OK do dono) |
| ML + Amazon + Shopee | coleta feita; links do ML dependem do login da lules |
| Telas no padrão do v1 | feito, 375 px a desktop |
| 1 a 5 números de WhatsApp | feito: número por grupo, teto por hora, tela WhatsApp; o canário ainda testa só o principal |
| Alertas e visibilidade das fontes | feito: tela Fontes; alertas opt-in (`ALERT_CHAT_ID`, `ALERT_EMAIL_TO`), sem canal configurado hoje |
| Telegram | fora por decisão |

## 8. Roadmap e decisões do dono

Ordem por retorno: (1) links do dono para os pares; (2) receita e comissão (Shopee `conversionReport`, depois
relatório do ML; falta uma conversão real para validar o formato); (3) cupom manual / colar produto; (4) Awin
e Lomadee; (5) canário por número; (6) extensão de navegador para gerar link; (7) vitrine pública e link
inteligente.

Só o dono decide: login da lules e onde roda o Link Builder em produção; **para onde vão os alertas** (chat do
WhatsApp e/ou e-mail com SMTP) e quais números de WhatsApp entram; manter ou não a raspagem de páginas
públicas (Amazon, vitrine do ML); aviso de afiliação; Telegram; agregadores; credenciais Awin/Lomadee;
qual VPS contratar e o gasto; quantos números de WhatsApp.
