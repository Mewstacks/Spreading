#!/usr/bin/env bash
set -euo pipefail

# Liga/desliga a infraestrutura de produção em uma ordem que preserva o banco.
# As credenciais são tokens Fly app-scoped, armazenados como GitHub Actions
# secrets. Nenhum token é gravado neste repositório.

readonly ACTION="${1:-}"
# 5 minutos por app. O boot mais lento (worker WhatsApp) leva ~60s; máquina que
# não ficou sã em 5min não fica em 10. O teto importa porque agora as três
# esperas podem acontecer na mesma execução, e a soma tem de caber no
# timeout-minutes do workflow.
readonly CHECK_ATTEMPTS=30
readonly CHECK_INTERVAL_SECONDS=10

if [[ "$ACTION" != "start" && "$ACTION" != "stop" ]]; then
  echo "Uso: $0 start|stop" >&2
  exit 2
fi

for dependency in flyctl jq; do
  if ! command -v "$dependency" >/dev/null 2>&1; then
    echo "Dependência ausente: $dependency" >&2
    exit 1
  fi
done

for secret_name in FLY_TOKEN_WEB FLY_TOKEN_WA FLY_TOKEN_DB; do
  if [[ -z "${!secret_name:-}" ]]; then
    echo "Secret ausente: $secret_name" >&2
    exit 1
  fi
done

token_for_app() {
  case "$1" in
    spreading-web) printf '%s' "$FLY_TOKEN_WEB" ;;
    spreading-wa) printf '%s' "$FLY_TOKEN_WA" ;;
    spreading-db) printf '%s' "$FLY_TOKEN_DB" ;;
    *) echo "App não permitido: $1" >&2; return 1 ;;
  esac
}

fly_for_app() {
  local app="$1"
  shift
  FLY_API_TOKEN="$(token_for_app "$app")" flyctl "$@"
}

# Terceira coluna: o process group. `spreading-web` tem dois (web e worker) e
# eles têm destinos diferentes na parada noturna — ver o comentário no ramo de
# stop. Quem lê estas linhas precisa consumir os três campos: com IFS de tab,
# `read -r id estado` jogaria o grupo para dentro de `estado` e nenhuma
# comparação de estado funcionaria.
machine_rows() {
  local app="$1"
  fly_for_app "$app" machine list --app "$app" --json \
    | jq -r '.[] | [.id, .state, (.config.metadata.fly_process_group // "")] | @tsv'
}

# `stop_app app [grupo]` — com grupo, para só as máquinas daquele process group.
stop_app() {
  local app="$1"
  local somente="${2:-}"
  local machine_id state grupo found=0

  while IFS=$'\t' read -r machine_id state grupo; do
    [[ -n "$machine_id" ]] || continue
    if [[ -n "$somente" && "$grupo" != "$somente" ]]; then
      echo "$app/$machine_id ($grupo) fica de pé: só $somente dorme."
      continue
    fi
    found=1
    if [[ "$state" == "stopped" || "$state" == "suspended" ]]; then
      echo "$app/$machine_id já está $state."
      continue
    fi
    if [[ "$state" != "started" ]]; then
      echo "$app/$machine_id está em transição ($state); abortando a parada segura." >&2
      return 1
    fi
    echo "Parando $app/$machine_id (estado atual: $state)..."
    fly_for_app "$app" machine stop "$machine_id" --app "$app" \
      --timeout 120 --wait-timeout 3m
  done < <(machine_rows "$app")

  if [[ "$found" -eq 0 ]]; then
    echo "Nenhuma máquina${somente:+ do grupo $somente} encontrada em $app;" \
         "recusando continuar." >&2
    return 1
  fi
}

# Quais checks REPRESENTAM a dependência. Lista vazia = exigir todos.
#
# O spreading-db é a exceção e a razão desta função existir: o postgres-flex
# publica três checks e só o `pg` mede o que os consumidores precisam
# (conexões, locks, disco). O `vm` é o agente da máquina e o `role` só é
# reescrito no boot — em 16, 17 e 18/08 o `vm` ficou em critical
# ("connect: connection refused") com o Postgres servindo normalmente, e como a
# regra antiga exigia TODOS os checks, o start noturno morria aqui e o WhatsApp
# passava o dia inteiro fora do ar. Estado da máquina já é conferido à parte.
checks_required_for() {
  case "$1" in
    spreading-db) printf '%s' '["pg"]' ;;
    *) printf '%s' '[]' ;;
  esac
}

checks_are_passing() {
  local app="$1"
  local required
  required="$(checks_required_for "$app")"
  fly_for_app "$app" checks list --app "$app" --json \
    | jq -e --argjson req "$required" '
      [.[] | .[]] as $todos
      | ($todos | length) > 0
      and (
        if ($req | length) == 0
        then all($todos[]; .status == "passing")
        # Exige presença E passagem: check exigido que sumiu não pode virar
        # "saudável por omissão".
        else all($req[]; . as $nome
                 | any($todos[]; .name == $nome and .status == "passing"))
        end
      )
    ' >/dev/null
}

wait_until_healthy() {
  local app="$1"
  local attempt states_ok

  for ((attempt = 1; attempt <= CHECK_ATTEMPTS; attempt++)); do
    states_ok="$({ machine_rows "$app" || true; } \
      | jq -Rsc 'split("\n") | map(select(length > 0) | split("\t"))
        | length > 0 and all(.[]; .[1] == "started")')"

    if [[ "$states_ok" == "true" ]] && checks_are_passing "$app"; then
      echo "$app está iniciado e saudável."
      return 0
    fi

    echo "Aguardando saúde de $app ($attempt/$CHECK_ATTEMPTS)..."
    sleep "$CHECK_INTERVAL_SECONDS"
  done

  echo "$app não ficou saudável dentro do prazo." >&2
  fly_for_app "$app" machine list --app "$app"
  fly_for_app "$app" checks list --app "$app" || true
  return 1
}

start_app() {
  local app="$1"
  local machine_id state grupo found=0 started_any=0

  while IFS=$'\t' read -r machine_id state grupo; do
    [[ -n "$machine_id" ]] || continue
    found=1
    if [[ "$state" == "started" ]]; then
      echo "$app/$machine_id já está iniciado."
      continue
    fi
    if [[ "$state" == "starting" || "$state" == "replacing" || "$state" == "created" ]]; then
      echo "$app/$machine_id está em transição ($state); aguardando saúde sem reiniciar."
      continue
    fi
    if [[ "$state" != "stopped" && "$state" != "suspended" ]]; then
      echo "$app/$machine_id está em estado inesperado ($state); abortando." >&2
      return 1
    fi
    echo "Iniciando $app/$machine_id (estado atual: $state)..."
    fly_for_app "$app" machine start "$machine_id" --app "$app"
    started_any=1
  done < <(machine_rows "$app")

  if [[ "$found" -eq 0 ]]; then
    echo "Nenhuma máquina encontrada em $app; recusando continuar." >&2
    return 1
  fi

  # Evita aceitar imediatamente o resultado antigo de um health check anterior
  # à parada. Depois desta pausa, o Fly já terá começado a publicar checks do boot.
  if [[ "$started_any" -eq 1 ]]; then
    sleep "$CHECK_INTERVAL_SECONDS"
  fi

  wait_until_healthy "$app"
}

# Janela de sono, em hora de Brasília. O agendamento do GitHub Actions é
# best-effort e ATRASA — medido neste repositório: o cron das 04:00 UTC disparou
# às 09:11 em 07/09/2026 (cinco horas depois) e às 08:33 em 06/09. Uma parada que
# chega atrasada cai no meio do expediente, e o religamento, que vem no mesmo
# atraso, só acontece horas depois: em 07/09 a produção ficou fora do ar das 06:12
# às ~11:00, incluindo a janela de envio das 08:00.
#
# Por isso o gesto não obedece à hora em que o agendador acordou: ele confere o
# relógio. Parar só vale dentro da janela; ligar vale sempre — ligar fora de hora
# é, no máximo, gastar um pouco antes.
#
# A janela de PARAR e mais estreita que a janela de dormir, e a diferenca e o que
# consertou 08/09/2026. Naquele dia o guard funcionou como escrito: a parada das
# 04:00 UTC chegou as 08:46 UTC (05:46 BRT), 5h46 atrasada, e 05:46 estava dentro
# de 1h-8h, entao ela foi aceita -- corretamente, pela regra antiga. So que os
# dois religamentos seguintes (10:45 e 11:20 UTC) foram DESCARTADOS pelo
# agendador, e a producao passou a manha inteira parada, incluindo a janela de
# envio das 08:00.
#
# A conta mostra que aceitar aquela parada nunca valeu a pena. Uma hora de sono
# poupa cerca de R$0,37 (29% do custo de maquinas, ~R$278/mes, repartido nas ~7h
# de sono). A parada das 05:46 compraria menos de duas horas -- menos de R$0,75 --
# arriscando a manha de envio inteira, que e o produto.
#
# Entao a regra passa a ser economica, nao so horaria: so para se ainda restar
# sono que pague o risco. Ligar continua valendo sempre e a qualquer hora --
# ligar fora de hora custa, no maximo, um pouco de maquina ligada a mais.
readonly SONO_INICIO_H=1
readonly RELIGAMENTO_H=8
# Minimo de sono que justifica o risco de a parada ser a ultima coisa que o
# agendador entrega no dia. Abaixo disso, ficar de pe e mais barato que o buraco.
readonly SONO_MINIMO_H=4

dentro_da_janela_de_sono() {
  local hora
  hora=$(TZ="America/Sao_Paulo" date +%-H)
  (( hora >= SONO_INICIO_H && hora <= RELIGAMENTO_H - SONO_MINIMO_H ))
}

if [[ "$ACTION" == "stop" ]]; then
  if ! dentro_da_janela_de_sono; then
    echo "Parada ignorada: agora são $(TZ=America/Sao_Paulo date '+%H:%M') em"          "Brasília, fora da janela de parada (${SONO_INICIO_H}h-$(( RELIGAMENTO_H - SONO_MINIMO_H ))h)."          "O agendador atrasou; desligar agora tiraria a produção do ar em"          "horário de operação." >&2
    exit 0
  fi
  # Só o que REALMENTE dorme. Medido nos event logs das máquinas em 08/09/2026:
  #
  #   web     stop 05:46:20 -> start 05:47:06 por `proxy`   (45s parado)
  #   db      stop 05:46:40 -> start 05:47:13 por `proxy`   (33s parado)
  #   worker  stop 05:46:24 -> so voltou as 09:02, a mao     (3h16)
  #   wa      stop 05:46:36 -> so voltou as 09:02, a mao     (3h16)
  #
  # `spreading-web` tem `min_machines_running = 1` com `auto_start_machines`, e o
  # `spreading-db` responde por flycast: o proxy da Fly levanta os dois de volta
  # em menos de um minuto. Pará-los não economizava nada — trocava zero centavo
  # por um encerramento do gunicorn e, pior, um bounce do Postgres toda
  # madrugada. Só o worker e o WhatsApp ficam de fato parados, e são eles que a
  # conta do desligamento noturno sempre pagou.
  #
  # Efeito colateral que vale registrar: como web e db não dormem, existe algo de
  # pé a noite inteira. É o candidato natural a vigia do religamento, que hoje
  # depende só do agendador do GitHub — o mesmo que descartou as duas tentativas
  # de 08/09.
  stop_app spreading-wa
  stop_app spreading-web worker
else
  # Dependências sobem antes dos consumidores, e cada etapa espera health checks
  # — mas a espera é uma PRECAUÇÃO, não um portão. Uma dependência que demora (ou
  # cujo check está mentindo) não pode deixar o resto da produção desligada: o
  # WhatsApp parado é uma falha pior do que subir com o banco ainda assentando,
  # porque nada mais no sistema consegue levantá-lo depois (à noite não há web
  # rodando, e portanto não há vigia externo). Cada etapa é tentada; o job só
  # reporta o fracasso no fim, com tudo o que deu para ligar já ligado.
  falhas=0
  start_app spreading-db || { echo "spreading-db não confirmou saúde; seguindo assim mesmo." >&2; falhas=1; }
  start_app spreading-wa || { echo "spreading-wa não confirmou saúde." >&2; falhas=1; }
  start_app spreading-web || { echo "spreading-web não confirmou saúde." >&2; falhas=1; }
  exit "$falhas"
fi
