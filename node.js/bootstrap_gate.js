'use strict';

// Portão de bootstrap: UM Chromium subindo o WhatsApp Web de cada vez.
//
// Por que isto existe: subir o WhatsApp Web é a operação mais cara do worker —
// Chromium frio + o bundle inteiro do WA + o boot do app. Medido na VM de
// produção (shared-cpu-2x/4gb, 18/08/2026), com o MESMO script e o mesmo pin:
//
//   1 bootstrap sozinho ....... QR em 37s, loadavg ~1
//   2 bootstraps simultâneos .. NENHUM QR em 150s, loadavg 16
//
// Não é 2× mais lento: é indeterminado. O shared-cpu cobra por cota de baseline
// e aplica throttle quando ela acaba, então os dois Chromiums ficam prontos e
// não são escalonados. Como o orçamento de bootstrap é de 90s, os dois estouram,
// os dois são reciclados, e cada reciclagem sobe um Chromium novo — o segundo
// bootstrap não atrasa o primeiro, ele cria uma fome de CPU que se realimenta.
// Foi assim que a tela ficou em "preparando o leitor" sem nunca gerar QR.
//
// Serializar não custa nada no caso normal (a fila só existe quando há dois
// pedidos ao mesmo tempo) e transforma "os dois falham para sempre" em "o
// segundo espera 40s". Trocar a VM não substitui isto: com mais vCPU o teto
// sobe, mas nada impede N bootstraps concorrentes de estourá-lo de novo.
//
// Módulo puro: sem fs, sem rede, sem console. Recebe temporizadores por
// parâmetro para os testes rodarem sem relógio real.

const criarPortao = ({
    tetoMs = 120000,
    agendar = setTimeout,
    cancelar = clearTimeout,
    aoEstourarTeto = () => {},
} = {}) => {
    let dono = null;          // { id } de quem segura a vez
    let timerTeto = null;
    const fila = [];          // [{ id, resolve }] em ordem de chegada

    const entregar = (pedido) => {
        dono = { id: pedido.id };
        let liberado = false;
        // O teto é a rede de segurança: um bootstrap que trave sem emitir
        // 'qr' nem 'ready' nem estourar o próprio timeout não pode deixar a
        // fila parada para sempre. Só existe para esse caso.
        timerTeto = agendar(() => {
            aoEstourarTeto(pedido.id);
            liberar('teto');
        }, tetoMs);
        if (timerTeto && timerTeto.unref) timerTeto.unref();

        // Idempotente de propósito: os pontos de liberação são vários ('qr',
        // 'ready', reciclagem, falha) e mais de um pode acontecer na mesma
        // tentativa. Liberar duas vezes não pode adiantar a vez de ninguém.
        function liberar() {
            if (liberado) return false;
            liberado = true;
            if (timerTeto) { cancelar(timerTeto); timerTeto = null; }
            dono = null;
            const proximo = fila.shift();
            if (proximo) entregar(proximo);
            return true;
        }

        pedido.resolve(liberar);
    };

    return {
        // Devolve uma promise que resolve com a função de liberar. Nunca rejeita:
        // quem espera a vez não pode quebrar por causa do portão.
        adquirir(id) {
            return new Promise((resolve) => {
                const pedido = { id, resolve };
                if (!dono) entregar(pedido);
                else fila.push(pedido);
            });
        },
        // Só para log e /health — nunca para decidir.
        estado() {
            return { dono: dono ? dono.id : null, fila: fila.map((p) => p.id) };
        },
    };
};

module.exports = { criarPortao };
