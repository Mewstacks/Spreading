#!/usr/bin/env sh
# Supervisor local do worker de WhatsApp. Use `npm start` (nao `node index.js`).
#
# Por que existe: o watchdog (index.js) derruba o worker com SIGKILL quando o
# event loop trava por WATCHDOG_TIMEOUT_MS. Isso e por desenho, e em producao e
# inofensivo — o Fly tem `[[restart]] policy = "always"` (fly.toml) e o compose
# tem `restart: unless-stopped`, entao o processo volta em segundos. Um
# `node index.js` solto no terminal nao tem supervisor nenhum: o watchdog mata e
# o servico fica fora do ar ate alguem perceber, e a tela do painel mostra
# "Servico indisponivel" sem nada explicando por que. Este loop e o equivalente
# local daquelas duas politicas.
#
# Ctrl+C encerra de vez, em vez de disparar mais um restart.

cd "$(dirname "$0")" || exit 1

parar() {
    echo ""
    echo "Supervisor encerrado."
    exit 0
}
trap parar INT TERM

while true; do
    node index.js
    codigo=$?

    # Saida limpa (shutdown() concluiu) = pedido deliberado de parada: nao reergue.
    if [ "$codigo" -eq 0 ]; then
        parar
    fi

    echo "" >&2
    echo "Worker caiu (codigo $codigo). Reiniciando em 2s — Ctrl+C para parar." >&2
    sleep 2
done
