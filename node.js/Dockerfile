FROM node:20-slim

# Instala o Chromium do sistema (necessário para o puppeteer/whatsapp-web.js)
# e fontes básicas para o navegador headless funcionar corretamente
RUN apt-get update && apt-get install -y \
    chromium \
    fonts-freefont-ttf \
    --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Diz ao puppeteer para NÃO baixar o próprio Chromium durante o npm install
# e que ele deve usar o Chromium que instalamos acima
ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

WORKDIR /app

# Copia só os arquivos de dependências primeiro (melhor cache de build)
COPY package*.json ./
RUN npm ci --omit=dev

# Copia o código da aplicação. Contexto inteiro de propósito: um allowlist por
# arquivo deixou payloads.js e auth_store.js de fora e derrubou o boot em
# produção, sem quebrar nada em dev. O que fica fora da imagem é decidido pelo
# .dockerignore.
COPY . .

EXPOSE 3000

CMD ["node", "index.js"]
