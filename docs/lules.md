# O que falta da lules

Tudo no programa que não depende dela está pronto. Sobra uma coisa só, que só a conta de afiliado do
Mercado Livre pode fazer: **entrar no Link Builder uma vez**, para o sistema gerar os links `meli.la`
dos cupons de **ativação** (121 pares conferíveis hoje) e dos produtos sem link.

Sem isso o sistema já funciona: manda ofertas que têm link, **cupons por código** (o link do produto
basta) e tudo da Shopee e da Amazon. O que fica de fora são os cupons de ativação, que precisam de um
link que carregue a campanha.

## O que ela faz (uns 5 minutos, no computador dela)

1. Ter Windows, Google Chrome e Python 3.12 instalados.
2. Baixar o projeto (a pasta `Spreading`, branch `v2`) e, dentro dela, no terminal:

   ```bash
   python -m venv .venv
   .venv\Scripts\pip install -r requirements-linkbuilder.txt
   .venv\Scripts\python -m playwright install chromium
   ```

3. Rodar o login:

   ```bash
   .venv\Scripts\python tools\ml_login.py
   ```

   Abre uma janela do Chrome comum (sem automação, senão o ML trava a página). Ela entra na conta
   de afiliado (com a verificação em duas etapas, se pedir), **deixa o Link Builder aberto** e
   **fecha a janela**. O script reabre o mesmo perfil por trás, confere que o Link Builder abre
   logado e grava a sessão em `.secrets\ml-state.json`.

4. Mandar esse arquivo para o dono **por um canal privado** (nunca por git, e-mail aberto ou grupo):
   ele equivale ao login dela.

## O que o dono faz com o arquivo

1. Coloca em `.secrets/ml-state.json` na raiz do projeto, no servidor (ou no computador, em teste).
2. Sobe o gerador: `docker compose --profile linkbuilder up -d --build linkbuilder`.
3. Confere: Painel → Grupos mostra **Link Builder · Pronto**; depois de um ciclo (5 minutos), a
   tela **Links** tem menos pendentes e a tela **Cupons** passa a mostrar "Com link" nos pares de
   ativação.

Limites que já vêm ligados (termos do programa de afiliados): até 8 links por rodada, 120 por dia,
passo humano entre um e outro, e o gerador **para sozinho** na primeira verificação ou captcha do ML.

## Se o ML bloquear o servidor

O ML é duro com IP de datacenter. Se o estado do Link Builder ficar em **Verificação** ou
**Fora do ar** mesmo com a sessão boa, o servidor não serve para gerar link. Dois caminhos:

- **Tela Links (funciona hoje, sem login no servidor):** lista as URLs que faltam, pares primeiro.
  A lules gera cada uma no Link Builder da conta dela e o dono cola `url link` de volta; só entra
  link `meli.la` e só para a URL que o painel pediu.
- **Rodar o gerador num computador com IP residencial** (o dela ou o do dono), apontando para o
  banco do servidor por túnel SSH. **Não foi testado** e exige expor o banco por túnel e ajustar
  `DATABASE_URL` do serviço `linkbuilder`; só vale se o volume de cupons de ativação compensar.

## Como saber que deu certo

- `docker compose logs --tail 50 linkbuilder` sem erro de sessão.
- Painel → Cupons → filtro **Com link**: sobem os pares de ativação.
- Em 24 horas, pares de ativação aparecendo nas mensagens do grupo com a linha
  `🎟️ CUPOM: ative no link`.

## Quando refazer

A sessão do ML expira (semanas a meses) ou cai se ela sair da conta. O painel avisa: estado
**Sem sessão** ou **Entrar de novo**. É só repetir o passo 3.
