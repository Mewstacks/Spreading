'use strict';

// Politica do watchdog do worker, isolada para teste. Ate 08/08 o watchdog so
// media um timer do processo pai (heartbeat por stdin): se o event loop parava
// de escrever por 45s, matava. Mas o sinal que o Fly usa para declarar a
// maquina doente — o /health responder por HTTP — nunca era medido, e foi
// exatamente ele que ficou mudo por 20 minutos no incidente: o TCP era aceito
// e nenhuma resposta saia, com o heartbeat correndo solto. Agora o filho sonda
// o proprio /health por HTTP alem do heartbeat, e a decisao de matar sai daqui.

// Quantas sondas HTTP seguidas falhando antes de matar. Uma falha isolada pode
// ser so o event loop ocupado por alguns segundos num recycle pesado.
const SONDAS_HTTP_PARA_MATAR = 3;

// Janela de graca do boot. O app.listen sobe ~2s depois do processo e ANTES de
// restaurar as sessoes, entao qualquer sonda antes disso falharia por motivo
// errado. Espelha o grace_period = "60s" do check do Fly.
const GRACA_BOOT_MS = 60000;

// Decide entre 'matar' e 'esperar'.
//
// - Dentro da janela de graca nao se conta falha nenhuma: o servidor ainda
//   pode nao ter subido e o boot pesado (dois Chromiums restaurando centenas
//   de grupos) pode segurar o event loop por trechos longos.
// - Passada a graca, mata quando o heartbeat vencer (event loop mudo) OU
//   quando as sondas HTTP falharem N vezes seguidas (HTTP mudo — o caso que o
//   watchdog antigo nao enxergava).
// - Sonda null (nao houve sonda neste ciclo) nao conta para nenhum lado.
const decisaoWatchdog = ({
    msDesdeBoot,
    msSemHeartbeat,
    sondasHttpFalhasSeguidas,
}, {
    gracaMs = GRACA_BOOT_MS,
    heartbeatTimeoutMs,
    sondasParaMatar = SONDAS_HTTP_PARA_MATAR,
} = {}) => {
    if ((Number(msDesdeBoot) || 0) < gracaMs) return 'esperar';
    if ((Number(msSemHeartbeat) || 0) >= heartbeatTimeoutMs) return 'matar';
    if ((Number(sondasHttpFalhasSeguidas) || 0) >= sondasParaMatar) return 'matar';
    return 'esperar';
};

module.exports = { decisaoWatchdog, SONDAS_HTTP_PARA_MATAR, GRACA_BOOT_MS };
