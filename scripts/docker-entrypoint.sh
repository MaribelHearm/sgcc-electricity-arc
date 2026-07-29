#!/usr/bin/env bash
set -euo pipefail

export DISPLAY="${DISPLAY:-:99}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/chrome-runtime}"
mkdir -p /data/errors "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR" >/dev/null 2>&1 || true

# Home Assistant Add-on passes user options via /data/options.json, not env.
# Load them before deciding whether this container should run local Chromium,
# embedded browser-service, or attach to an external CDP endpoint.
ADDON_OPTIONS_FILE="${SGCC_ADDON_OPTIONS_FILE:-/data/options.json}"
LOAD_ADDON_OPTIONS="${SGCC_LOAD_ADDON_OPTIONS:-auto}"

addon_options_active() {
  [ -f "$ADDON_OPTIONS_FILE" ] || return 1
  case "$(printf '%s' "$LOAD_ADDON_OPTIONS" | tr '[:upper:]' '[:lower:]')" in
    0|false|no|off|disabled) return 1 ;;
  esac
  return 0
}

load_addon_options() {
  addon_options_active || return 0
  command -v jq >/dev/null 2>&1 || return 0
  while IFS=$'\t' read -r key value; do
    case "$key" in
      ''|*[!A-Za-z0-9_]*|[0-9]*) continue ;;
    esac
    # Existing environment wins. This prevents Docker Compose hosts that also
    # happen to have /data/options.json from being reconfigured as Add-ons.
    if [ -z "${!key+x}" ]; then
      export "$key=$value"
    fi
  done < <(jq -r 'to_entries[] | "\(.key)\t\(.value | tostring)"' "$ADDON_OPTIONS_FILE")
}

load_addon_options

# Compose sets SGCC_BROWSER_MODE explicitly in docker-compose.yml. For Add-on,
# prefer official Chrome browser-service even when an older options.json does
# not yet contain the new browser fields.
DEFAULT_BROWSER_MODE="local"
if addon_options_active; then
  DEFAULT_BROWSER_MODE="browser-service"
fi
BROWSER_MODE="${SGCC_BROWSER_MODE:-$DEFAULT_BROWSER_MODE}"
export SGCC_BROWSER_MODE="$BROWSER_MODE"
PROFILE_DIR="${SGCC_BROWSER_PROFILE:-/data/chrome-profile}"
# Compose uses an external sgcc_browser sidecar; Add-on is a single container
# and therefore embeds the lightweight browser manager in this entrypoint.
if [ -z "${SGCC_BROWSER_SERVICE_EMBEDDED:-}" ]; then
  if addon_options_active; then
    SGCC_BROWSER_SERVICE_EMBEDDED="true"
  else
    SGCC_BROWSER_SERVICE_EMBEDDED="false"
  fi
fi
PIDS=()

cleanup() {
  for pid in "${PIDS[@]}"; do
    kill "$pid" >/dev/null 2>&1 || true
  done
  wait >/dev/null 2>&1 || true
}

on_term() {
  cleanup
  exit 143
}

trap cleanup EXIT
trap on_term INT TERM

run_main() {
  python3 -u -m sgcc_ha_bridge.main &
  local main_pid="$!"
  PIDS+=("$main_pid")
  wait "$main_pid"
}

start_xvfb() {
  local browser_window_size="${BROWSER_WINDOW_SIZE:-1280,900}"
  local xvfb_width=1280
  local xvfb_height=900
  if [[ "$browser_window_size" =~ ^([0-9]{3,5}),([0-9]{3,5})$ ]]; then
    xvfb_width="${BASH_REMATCH[1]}"
    xvfb_height="${BASH_REMATCH[2]}"
  else
    echo "invalid BROWSER_WINDOW_SIZE=${browser_window_size}; fallback to 1280,900" >&2
  fi
  rm -f /tmp/.X99-lock
  Xvfb "$DISPLAY" -screen 0 "${xvfb_width}x${xvfb_height}x24" -ac +extension RANDR -nolisten tcp -nolisten local -nolock >/tmp/xvfb.log 2>&1 &
  local xvfb_pid="$!"
  PIDS+=("$xvfb_pid")
  sleep 1
  if ! kill -0 "$xvfb_pid" >/dev/null 2>&1; then
    cat /tmp/xvfb.log >&2 || true
    exit 1
  fi
}

start_embedded_browser_service() {
  export SGCC_BROWSER_SERVICE_HOST="${SGCC_BROWSER_SERVICE_HOST:-127.0.0.1}"
  export SGCC_BROWSER_SERVICE_PORT="${SGCC_BROWSER_SERVICE_PORT:-39222}"
  export SGCC_BROWSER_CDP_HOST="${SGCC_BROWSER_CDP_HOST:-127.0.0.1}"
  export SGCC_BROWSER_CDP_PORT="${SGCC_BROWSER_CDP_PORT:-19222}"
  export SGCC_CDP_ADDRESS="${SGCC_CDP_ADDRESS:-127.0.0.1:${SGCC_BROWSER_CDP_PORT}}"
  export SGCC_BROWSER_SERVICE_URL="${SGCC_BROWSER_SERVICE_URL:-http://127.0.0.1:${SGCC_BROWSER_SERVICE_PORT}}"
  export SGCC_BROWSER_PROFILE="${SGCC_BROWSER_SERVICE_PROFILE:-/data/sgcc-browser-profile}"

  mkdir -p "$SGCC_BROWSER_PROFILE"
  rm -f "$SGCC_BROWSER_PROFILE"/SingletonLock "$SGCC_BROWSER_PROFILE"/SingletonSocket "$SGCC_BROWSER_PROFILE"/SingletonCookie

  python3 -u -m sgcc_ha_bridge.browser_service &
  local service_pid="$!"
  PIDS+=("$service_pid")

  for _ in $(seq 1 30); do
    if curl -fsS "${SGCC_BROWSER_SERVICE_URL}/health" >/dev/null 2>&1; then
      echo "embedded browser-service ready on ${SGCC_BROWSER_SERVICE_URL}; Chrome will launch on demand" >&2
      return 0
    fi
    if ! kill -0 "$service_pid" >/dev/null 2>&1; then
      echo "embedded browser-service exited before becoming ready" >&2
      exit 1
    fi
    sleep 1
  done

  echo "embedded browser-service did not become ready" >&2
  exit 1
}

case "$BROWSER_MODE" in
  browser-service|browser_service|sidecar|container-google-cdp|container_google_cdp)
    if [ "$(printf '%s' "$SGCC_BROWSER_SERVICE_EMBEDDED" | tr '[:upper:]' '[:lower:]')" = "true" ]; then
      # Single-container Add-on path: keep only Xvfb + a lightweight manager
      # resident; official Google Chrome itself is launched on demand by
      # sgcc_ha_bridge.browser_service and stopped after each Selenium session.
      start_xvfb
      start_embedded_browser_service
    fi
    # Docker Compose path keeps using the external sgcc_browser sidecar.
    run_main
    ;;
  cdp|cdp_attach|host_cdp|host-cdp|remote_debugging|remote-debugging)
    # External Chrome/CDP is managed outside this container.
    run_main
    ;;
  *)
    # Compatibility mode: Debian Chromium + ChromeDriver inside app container.
    mkdir -p "$PROFILE_DIR"
    rm -f "$PROFILE_DIR"/SingletonLock "$PROFILE_DIR"/SingletonSocket "$PROFILE_DIR"/SingletonCookie
    start_xvfb
    run_main
    ;;
esac
