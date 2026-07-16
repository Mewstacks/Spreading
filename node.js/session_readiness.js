'use strict';

// whatsapp-web.js 1.34 expõe as operações de chat em window.WWebJS. `Store`
// era uma API interna de versões anteriores e não é requisito para enviar nem
// para ler o estado do socket; usá-lo como gate deixava a sessão em falso
// "carregando" mesmo depois do evento ready.
const runtimePronto = (win) => Boolean(
    win && win.WWebJS && typeof win.WWebJS.getChat === 'function'
);

module.exports = { runtimePronto };
