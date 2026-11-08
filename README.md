# Spreading v2

Ofertas e cupons do Mercado Livre (e Amazon/Shopee) → regras por grupo → WhatsApp (WAHA/GOWS), com foto,
preço provado ao vivo e o link de afiliado do dono. Django 6 + Postgres 17. Só WhatsApp por enquanto.

## O que sai no grupo
Uma oferta por vez, no formato único do v1: foto do produto + legenda
`🛍️ *Nome curto*` / `De ❌ ~R$ x~` / `Por 🔥 *R$ y*` / `🔗 https://meli.la/…` (com cupom pareado, a linha do
cupom e da validade). Nada de aviso de afiliação nem de lista de cupons.

Invariantes (cada uma tem teste): preço conferido ao vivo na API oficial do ML; "De" e cupom só com prova;
um cupom por mensagem; foto obrigatória (≥400 px, JPEG); link do dono verificado e **nunca** a vitrine
`/social/`; cooldown de 15 dias por produto e grupo; sem link do dono, não envia.

## Rodar local
1. `.env` (nunca versionado): copie `.env.example` e preencha `DJANGO_SECRET_KEY` (≥50), `POSTGRES_PASSWORD`,
   `WAHA_API_KEY`, `WAHA_DASHBOARD_PASSWORD`, `STATUS_TOKEN`; opcional `ML_API_CLIENT_ID/SECRET`, `CANARY_CHAT_ID`.
2. `docker compose up -d --build` (db, web, worker, waha). O web migra sozinho.
3. Admin: `docker compose exec web python manage.py createsuperuser`.
4. WhatsApp: http://localhost:8000/pair (staff) mostra o QR; se o WhatsApp pedir passkey, a página mostra
   "Abrir WAHA" e a extensão "WhatsApp Browser Extension" num desktop completa o passo. O dashboard
   (http://localhost:3000/dashboard/) pede o login básico (`WAHA_DASHBOARD_USERNAME`, padrão `admin`, e
   `WAHA_DASHBOARD_PASSWORD`) e, no modal Server, Name qualquer, API URL `http://localhost:3000` e API Key
   igual a `WAHA_API_KEY` (o aviso vermelho some com a chave colada). `python tools/waha_access.py` abre o
   dashboard e copia cada segredo para a área de transferência, sem imprimir nenhum.
5. **Painel** (staff): http://localhost:8000/painel/ — adicionar grupo (vem pausado), escolher um ou mais nichos
   (macro ou sub-nicho), lojas, teto por dia, janela, desconto mínimo e intervalo. "Enviar agora" manda uma
   oferta ignorando janela e intervalo; "Por que não saiu" conta o que barrou o resto.
6. Dados do v1 (somente leitura no v1): `tools/export_v1.py` e `tools/export_catalog_v1.py` /
   `tools/export_pairs_v1.py` (ver o cabeçalho de cada um) → `python manage.py import_v1 export.json
   --catalog catalogo.json --pairs pares.json --org lules --amazon-tag <tag>`. Só entra link `meli.la`
   verificado; reimportar não desfaz o que o painel já configurou.
7. Operação: o worker roda `run_cycle` a cada 5 min (feeds → catálogo pela API → fila por regra → envio);
   `canary` confere cada número em uso (mensagem e ACK no chat de `CANARY_CHAT_ID`/`CANARY_CHATS`; sem
   chat, só pergunta ao WhatsApp, sem mandar nada); `GET /status` com o header `X-Status-Token`.

## Shopee
API oficial de afiliados (GraphQL assinado), sem navegador: `productOfferV2` já devolve o link comissionado.
Preencha `SHOPEE_APP_ID` e `SHOPEE_APP_SECRET` no `.env` e marque Shopee na regra do grupo; o ciclo consulta
os termos da regra no máximo uma vez por hora. A API não traz o preço original: o "De" é derivado do
`priceDiscountRate`, então só entra produto com desconto de 5% a 70%, nota ≥ 4,5 e ≥ 50 vendas.

## Telas do painel
Visual do v1: grafite quente e papel (tema escuro/claro), acento laranja, Instrument Sans e IBM Plex Mono, sidebar
recolhível que vira gaveta no celular, tabelas que viram cartões a 375 px, ícones Lucide.
- **Grupos**: um card por grupo (estado, enviadas hoje, nichos) e o estado do WhatsApp e do Link Builder.
  Dentro do grupo: nichos, lojas, envio (teto, janela, desconto mínimo, intervalo) e filtros (cupom
  Primeiro/Só cupom/Sem cupom, faixa de preço, palavras, exclusões, dias).
- **Cupons**: pares cupom+produto ainda no prazo, com busca por produto ou código, filtro de loja e de link
  (com/sem), "Enviar" por linha e o feed de cupons de campanha do ML.
- **Ofertas**: o catálogo inteiro com a mesma busca e o mesmo "Enviar". O envio manual passa pelas mesmas
  provas do automático (link do dono, frescor, cupom no prazo, preço ao vivo, foto); só ignora janela,
  intervalo e cooldown.
- **Links**: URLs que faltam link do dono para gerar à mão e colar de volta.
- **Manual**: cole links de produto (ML `/p/MLB…`, Amazon `/dp/…`, Shopee), um por linha e até 20, com o link do
  dono ao lado no caso do ML. Cada linha vem pelas fontes oficiais (API do ML, API da Shopee, busca da Amazon)
  e só entra com a mesma prova do automático: sem "De" no anúncio, não há oferta. Cupom manual (só ML): código,
  valor (% ou R$), mínimo, teto e validade; o par nasce provado pelo preço ao vivo e o "Enviar" da linha usa o
  mesmo caminho da tela Ofertas. Link curto (`meli.la`, `s.shopee.com.br`) não vale como produto.
- **Receita**: vendas e comissão da Shopee (relatório de conversões da API), 7/30/60 dias, por estado e por
  origem do clique. Todo envio da Shopee sai com link curto de sub-id (`spreading` + o grupo; `SHOPEE_TRACK=0`
  desliga e o link comum assume se a API falhar), então a lista **Grupos** mostra o que o Spreading vendeu, à
  parte do que veio de YouTube, sites e outros canais da conta. ML e Amazon não têm API de relatório.

`python tools/sync_v1.py` (~2 min, somente leitura no v1) traz links verificados, catálogo e pares frescos
enquanto o v1 estiver vivo. `python tools/backup.py` faz o dump do Postgres.

## Amazon
Sem API utilizável (a Creators API exige 10 vendas qualificadas em 30 dias): a coleta lê a busca pública da
Amazon BR, uma consulta por termo da regra por hora, e usa o preço riscado do próprio anúncio como "De"
(desconto de 5% a 60%, nota ≥ 4,0, foto original). O link é `/dp/ASIN?tag=<tag da org>`; sem tag, não sai.
Captcha ou 503 pausa a loja por 1 h. Raspar viola os termos da Amazon: `AMAZON_COLLECT=0` desliga.

## Link Builder (links `meli.la` do dono)
Oferta sem link do dono não sai. Par cupom+produto **por código** usa o link do produto (o código vale no
carrinho, o link não precisa carregar nada); o de **ativação** precisa de um link que carregue a campanha.
O runner gera o que falta **só para o que as regras ativas vão publicar**, pares primeiro, com a conta do
próprio afiliado:

1. No seu computador: `pip install -r requirements-linkbuilder.txt && python -m playwright install chromium`
   e `python tools/ml_login.py` (abre uma janela; entre na conta; salva `.secrets/ml-state.json`, fora do
   git e da imagem).
2. `docker compose --profile linkbuilder up -d --build linkbuilder`. Parar o serviço é a chave geral
   (`LINKBUILDER_ENABLED=0` também). O estado aparece no painel (Pronto, Sem sessão, Verificação…).
3. Limites (termos 1.9 e 3.1c do programa): `LINKBUILDER_MAX_PER_RUN=8`, `LINKBUILDER_MAX_PER_DAY=120`,
   passo humano entre links, para na primeira verificação do ML, recusa do Programa só volta em 14 dias.
   O link entra no estoque depois que um GET anônimo confirma que cai na página do produto.

Sem sessão (ou se o ML recusar o navegador automático), a página **Links** do painel (`/painel/links/`) lista
as URLs que faltam, pares primeiro: gere no Link Builder da sua conta, cole `url link` de volta e importe.
Só entra a URL que o painel pediu e link `meli.la`.

## Vários números de WhatsApp
Cada grupo sai pelo número (sessão do WAHA) em que foi adicionado, até 5 números. Painel → **WhatsApp**: estado
de cada número, "Conectar" (QR em `/pair?session=nome`), grupos de cada um e **Novo número**. Em **Adicionar
grupo** escolha o número antes de buscar. Cada número tem teto de `SESSION_HOURLY_CAP` mensagens por hora
(padrão 20, soma de todos os grupos dele; 0 desliga). Se um número cai, só as regras dele esperam; as outras
seguem. O `canary` confere cada número em uso; o que não tem chat de canário é sondado (o WhatsApp responde
pelo próprio número?) sem receber mensagem, e a sonda também alimenta o alerta "conectado, mas o WhatsApp não
responde".

## Fontes, vitrine do ML e alertas
- **Fontes** (painel): estado e último resultado de cada fonte do ciclo (feeds do ML, catálogo, vitrine, Amazon,
  Shopee). "Sem novidade" = 6 h sem resultado novo; "Falhando N×" = N ciclos seguidos com erro.
- **Vitrine do ML** (`/ofertas/cupons`, página pública, uma carga por hora): campanhas de cupom ativas (aparecem
  em Cupons → "Campanhas do ML") e ofertas em destaque com preço à vista. Preço "no Pix" não vira oferta.
  Extração automatizada contraria os termos do programa: `ML_PUBLIC_PAGES=0` desliga.
- **Queda real**: produto com ≥ 3 outras observações em 30 dias só sai como promoção se o preço de hoje está ≥ 3%
  abaixo da mediana delas ("Sem queda real" em *Por que não saiu*). Par com cupom não entra na conta.
- **Alertas** (opt-in): `ALERT_CHAT_ID` (WhatsApp, sai por um número conectado) e/ou `ALERT_EMAIL_TO` + `EMAIL_*`.
  Avisa número fora do ar (após 10 min), fonte falhando 3×, envio incerto e fila parada; repete a cada 6 h e
  avisa quando volta.
- O intervalo da regra varia ±25% por envio; a comissão informada pela loja (Shopee) desempata o ranking.
- **Shopee · vendas** (alimenta a tela Receita): uma carga por hora, janela de 60 dias (90 na primeira vez); o
  estado de uma venda muda semanas depois, então a janela inteira é regravada.

## Produção
`DJANGO_HTTPS=1` atrás de TLS (Caddy, Fly; o proxy tem de sobrescrever `X-Forwarded-Proto`): cookies só por
https, redirect para https (`/healthz` fica de fora) e HSTS de 30 dias (`DJANGO_HSTS_SECONDS`). O painel manda
`X-Frame-Options: DENY`. Dashboard do WAHA e porta 3000 ficam em `127.0.0.1`: acesso por túnel SSH.

## Verificação
`python tools/verify.py` (ruff + pytest). Testes rodam em SQLite; com `DATABASE_URL` rodam em Postgres.
Os testes do driver abrem um Chromium de verdade contra um portal falso (pulam se o Playwright não estiver
instalado). Modelo de CI (ruff + pytest em Postgres 17) em `docs/ci.yml`: copie para `.github/workflows/`.
