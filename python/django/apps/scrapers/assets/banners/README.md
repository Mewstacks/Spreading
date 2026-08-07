# Banners do aviso de cupons

Imagens que vão no topo da mensagem "NOVOS CUPONS" (`ofertas.enviar_aviso_cupons`).
São fixas no repositório e valem para todos os usuários; o sistema **sorteia uma a
cada mensagem** para o grupo não ver sempre a mesma arte.

## Como adicionar

Solte os arquivos em:

- `mercadolivre/` — usados nas mensagens de cupom do Mercado Livre
- `amazon/` — usados nas mensagens de cupom da Amazon

Formatos aceitos: `.jpg`, `.jpeg`, `.png`, `.webp`.

## Formato recomendado

- Proporção larga, tipo banner (ex.: 1200×630). O WhatsApp corta as bordas de
  imagens muito altas.
- Lado maior até 1600px — acima disso o `preparar_jpeg_b64` reduz sozinho, mas
  entregar já no tamanho evita recompressão desnecessária.
- Sem texto pequeno: a miniatura do WhatsApp é bem menor que o arquivo.

## Pasta vazia

Não é erro. Sem nenhum arquivo, a mensagem sai **só em texto** e o envio segue
normalmente — nenhum aviso deixa de ir por falta de banner.

## Direitos de uso

As artes usam marcas de terceiros (Mercado Livre, Amazon). Confira as diretrizes de
marca de cada programa de afiliados antes de publicar; nada aqui é fornecido junto
com o código.
