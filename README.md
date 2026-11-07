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
4. WhatsApp: http://localhost:8000/pair (staff) mostra o QR; se o WhatsApp pedir passkey, use a extensão
   "WhatsApp Browser Extension" num desktop (dashboard do WAHA em http://localhost:3000/dashboard).
5. **Painel** (staff): http://localhost:8000/painel/ — adicionar grupo (vem pausado), escolher um ou mais nichos
   (macro ou sub-nicho), lojas, teto por dia, janela, desconto mínimo e intervalo. "Enviar agora" manda uma
   oferta ignorando janela e intervalo; "Por que não saiu" conta o que barrou o resto.
6. Dados do v1 (somente leitura no v1): `tools/export_v1.py` e `tools/export_catalog_v1.py` /
   `tools/export_pairs_v1.py` (ver o cabeçalho de cada um) → `python manage.py import_v1 export.json
   --catalog catalogo.json --pairs pares.json --org lules --amazon-tag <tag>`. Só entra link `meli.la`
   verificado; reimportar não desfaz o que o painel já configurou.
7. Operação: o worker roda `run_cycle` a cada 5 min (feeds → catálogo pela API → fila por regra → envio);
   `canary` (precisa de `CANARY_CHAT_ID`); `GET /status` com o header `X-Status-Token`.

## Shopee
API oficial de afiliados (GraphQL assinado), sem navegador: `productOfferV2` já devolve o link comissionado.
Preencha `SHOPEE_APP_ID` e `SHOPEE_APP_SECRET` no `.env` e marque Shopee na regra do grupo; o ciclo consulta
os termos da regra no máximo uma vez por hora. A API não traz o preço original: o "De" é derivado do
`priceDiscountRate`, então só entra produto com desconto de 5% a 70%, nota ≥ 4,5 e ≥ 50 vendas.

## Amazon
Sem API utilizável (a Creators API exige 10 vendas qualificadas em 30 dias): a coleta lê a busca pública da
Amazon BR, uma consulta por termo da regra por hora, e usa o preço riscado do próprio anúncio como "De"
(desconto de 5% a 60%, nota ≥ 4,0, foto original). O link é `/dp/ASIN?tag=<tag da org>`; sem tag, não sai.
Captcha ou 503 pausa a loja por 1 h. Raspar viola os termos da Amazon: `AMAZON_COLLECT=0` desliga.

## Link Builder (links `meli.la` do dono)
Oferta sem link do dono não sai. O runner gera o que falta **só para o que as regras ativas vão publicar**,
pares cupom+produto primeiro, com a conta do próprio afiliado:

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

## Verificação
`python tools/verify.py` (ruff + pytest). Testes rodam em SQLite; com `DATABASE_URL` rodam em Postgres.
Os testes do driver abrem um Chromium de verdade contra um portal falso (pulam se o Playwright não estiver
instalado).
