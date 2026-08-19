# Guia de ativação — o que só você pode fazer

Este arquivo cobre os três passos que exigem uma credencial sua. Tudo o mais já está
no código. Cada passo é independente: dá para fazer um hoje e outro semana que vem.

Regra que vale para os três: **as credenciais são geradas por você, no seu terminal
ou no painel da empresa, e viram secret da Fly.** Nada de segredo passa por chat, por
arquivo do repositório ou por log.

---

## 1. Shopee — ofertas e campanhas por API (10 minutos)

A Shopee é a única loja do sistema em que o link de afiliado sai por HTTP, sem
navegador. É por isso que ela é a que escala.

1. Entre no painel de afiliados da Shopee → **Open API**
   (`https://affiliate.shopee.com.br/open_api`)
2. Copie o **App ID** e o **Secret**
3. No Spreading: **Conta → Shopee → Validar e conectar**

A tela valida a credencial contra a API **antes de gravar**. Se a Shopee recusar,
nada é salvo e o motivo aparece na hora.

Depois de conectar, as campanhas entram no ciclo de cupons (a cada 15 min) e as
ofertas no de raspagem. Não precisa configurar mais nada.

> Existe fallback por `SHOPEE_APP_ID`/`SHOPEE_APP_SECRET` em settings, mas ele é para
> desenvolvimento. Em produção prefira uma conta por organização: a comissão vai para
> quem fez o trabalho.

---

## 2. Awin — o feed de cupom que já está pronto e subutilizado

É a fonte de cupom mais barata do sistema hoje: já implementada
(`awin.coletar_ofertas`), já detecta campanha relâmpago, não consome navegador.

O que limita o volume não é o código — é a adesão. O feed só devolve cupom de
anunciante em que **seu publisher está aprovado** (`membership: joined`) **e** que
esteja habilitado no painel do Spreading.

1. Em `https://ui.awin.com` → **Advertisers → Join Programme**, adira aos anunciantes
   brasileiros do seu nicho. Cada adesão aprovada vira cupom no feed
2. No Spreading: **Conta → Awin → Sincronizar agora**
3. Confira em **Conta → Awin** os programas que apareceram e ative os que interessam

É o caminho oficial pelo qual Cuponomia e Méliuz recebem cupom, e não custa
infraestrutura nenhuma.

---

## 3. Telegram — canais como sinal de descoberta

⚠️ **Leia antes de ligar.** Canal-fonte é pista, não verdade: o que ele publica é a
alegação de um terceiro sobre preço e estoque. Por isso o worker agora **confere cada
link no destino antes de qualquer envio** (`canais/validacao.py`). Mensagem com item
reprovado não sai; mensagem cujo destino não respondeu fica para o próximo ciclo em
vez de ser descartada.

### 3.1 Gerar a StringSession (você, no seu terminal)

Precisa de uma conta comum de Telegram — ela só vai **ler** canais públicos.

1. Vá em `https://my.telegram.org` → **API development tools** → crie um app
2. Anote `api_id` e `api_hash`
3. Rode, no seu terminal:

```bash
cd C:\Users\gege\Documents\Spreading\python && venv\Scripts\python.exe -c "from telethon.sync import TelegramClient; from telethon.sessions import StringSession; a=int(input('api_id: ')); h=input('api_hash: '); print('\nSESSION:', TelegramClient(StringSession(), a, h).start().session.save())"
```

Ele pede seu telefone e o código que chega no Telegram, e imprime a string da sessão.
**Ela equivale à sua conta — não cole em lugar nenhum além dos secrets.**

### 3.2 Publicar como secret

```bash
fly secrets set --app spreading-web TELEGRAM_API_ID=SEU_ID TELEGRAM_API_HASH=SEU_HASH TELEGRAM_SESSION=SUA_STRING TELETHON_RELINK_ENABLED=1
```

O worker `canais` já roda em produção — hoje fica ocioso porque esses valores não
existem. Assim que existirem, ele acorda sozinho.

### 3.3 Escolher os canais

Primeiro só liste, sem criar nada:

```bash
cd C:\Users\gege\Documents\Spreading-shopee\python && venv\Scripts\python.exe django\manage.py semear_canais --usuario SEU_USUARIO
```

Para registrar os monitoramentos apontando ao seu grupo:

```bash
cd C:\Users\gege\Documents\Spreading-shopee\python && venv\Scripts\python.exe django\manage.py semear_canais --usuario SEU_USUARIO --destino-canal whatsapp --destino-grupo SEU_GRUPO@g.us --criar
```

Eles nascem **desligados**. Ative um de cada vez e acompanhe alguns ciclos antes de
ligar o próximo — canal do Telegram muda de dono e de qualidade sem aviso.

---

## 4. Alerta de incidente — faça este primeiro (5 minutos)

Sem isto, um problema em produção espera alguém abrir a tela de Saúde. Foi assim que
a produção passou 16, 17 e 18/08 fora do ar.

Quando um incidente de nível `error` **abre** (ou reabre depois de resolvido), o
sistema manda uma mensagem. Ocorrência repetida do mesmo problema não repete o aviso,
e cada chave fica em silêncio por `ALERTA_SILENCIO_MIN` minutos.

**Por Telegram** (usa o `TELEGRAM_BOT_TOKEN` que você já tem):

1. Fale com o seu bot no Telegram e mande qualquer mensagem
2. Pegue o `chat_id` em `https://api.telegram.org/bot<SEU_TOKEN>/getUpdates`
3. Publique:

```bash
fly secrets set --app spreading-web ALERTA_TELEGRAM_CHAT_ID=SEU_CHAT_ID
```

**Ou por e-mail** (o SMTP já está configurado):

```bash
fly secrets set --app spreading-web ALERTA_EMAILS=voce@dominio.com,socio@dominio.com
```

Pode usar os dois ao mesmo tempo. Sem nenhum dos dois, o canal fica desligado e os
incidentes continuam só na tela de Saúde — que é o comportamento de hoje.

---

## Ordem sugerida

1. **Alerta de incidente** — 5 minutos, e é o que impede a próxima queda silenciosa
2. **Shopee** — resultado imediato, risco zero
3. **Awin** — maior ganho de cupom por hora investida
4. **Telegram (canais)** — mais volume, mas é o que exige mais acompanhamento

---

## O que não fazer

- **Não deployar do checkout principal.** Ele fica frequentemente atrás de
  `origin/main`. O trabalho novo está em `feat/shopee-e-confiabilidade`, worktree
  `C:\Users\gege\Documents\Spreading-shopee`
- **Não mexer no `spreading-db`** sem decidir antes o que muda: é Postgres não
  gerenciado e não tem botão de desfazer
- **Não ligar todos os canais de uma vez.** Um canal ruim gasta verificação e enche o
  log; cinco ao mesmo tempo escondem qual é o problema
