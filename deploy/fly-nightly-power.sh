#!/usr/bin/env bash
set -euo pipefail

# Liga/desliga a infraestrutura de produção em uma ordem que preserva o banco.
# As credenciais são tokens Fly app-scoped, armazenados como GitHub Actions
# secrets. Nenhum token é gravado neste repositório.

readonly ACTION="${1:-}"
readonly CHECK_ATTEMPTS=60
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

machine_rows() {
  local app="$1"
  fly_for_app "$app" machine list --app "$app" --json \
    | jq -r '.[] | [.id, .state] | @tsv'
}

stop_app() {
  local app="$1"
  local machine_id state found=0

  while IFS=$'\t' read -r machine_id state; do
    [[ -n "$machine_id" ]] || continue
    found=1
    if [[ "$state" == "stopped" || "$state" == "suspended" ]]; then
      echo "$app/$machine_id já está $state."
      continue
    fi
    echo "Parando $app/$machine_id (estado atual: $state)..."
    fly_for_app "$app" machine stop "$machine_id" --app "$app" \
      --timeout 120 --wait-timeout 3m
  done < <(machine_rows "$app")

  if [[ "$found" -eq 0 ]]; then
    echo "Nenhuma máquina encontrada em $app; recusando continuar." >&2
    return 1
  fi
}

checks_are_passing() {
  local app="$1"
  fly_for_app "$app" checks list --app "$app" --json | jq -e '
    length > 0
    and ([.[] | .[]] | length > 0)
    and all(.[] | .[]; .status == "passing")
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
  local machine_id state found=0 started_any=0

  while IFS=$'\t' read -r machine_id state; do
    [[ -n "$machine_id" ]] || continue
    found=1
    if [[ "$state" == "started" ]]; then
      echo "$app/$machine_id já está iniciado."
      continue
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

if [[ "$ACTION" == "stop" ]]; then
  # Primeiro remove tráfego e workers; o banco é sempre o último a parar.
  stop_app spreading-web
  stop_app spreading-wa
  stop_app spreading-db
else
  # Dependências sobem antes dos consumidores. Cada etapa espera health checks.
  start_app spreading-db
  start_app spreading-wa
  start_app spreading-web
fi
