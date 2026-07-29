#!/usr/bin/env bash
set -euo pipefail

export DISPLAY="${DISPLAY:-:99}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/chrome-runtime}"
PROFILE_DIR="${SGCC_BROWSER_PROFILE:-/data/chrome-profile}"
BROWSER_WINDOW_SIZE="${BROWSER_WINDOW_SIZE:-1280,900}"
if [[ "$BROWSER_WINDOW_SIZE" =~ ^([0-9]{3,5}),([0-9]{3,5})$ ]]; then
  XVFB_WIDTH="${BASH_REMATCH[1]}"
  XVFB_HEIGHT="${BASH_REMATCH[2]}"
else
  echo "invalid BROWSER_WINDOW_SIZE=${BROWSER_WINDOW_SIZE}; fallback to 1280,900" >&2
  XVFB_WIDTH=1280
  XVFB_HEIGHT=900
fi
mkdir -p /data/errors "$PROFILE_DIR" "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
rm -f /tmp/.X99-lock
rm -f "$PROFILE_DIR"/SingletonLock "$PROFILE_DIR"/SingletonSocket "$PROFILE_DIR"/SingletonCookie

PIDS=()
cleanup() {
  for pid in "${PIDS[@]}"; do
    kill "$pid" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT INT TERM

Xvfb "$DISPLAY" -screen 0 "${XVFB_WIDTH}x${XVFB_HEIGHT}x24" -ac +extension RANDR -nolisten tcp -nolisten local -nolock >/tmp/xvfb.log 2>&1 &
PIDS+=("$!")
sleep 1
if ! kill -0 "${PIDS[0]}" >/dev/null 2>&1; then
  cat /tmp/xvfb.log >&2 || true
  exit 1
fi

if command -v fluxbox >/dev/null 2>&1; then
  fluxbox >/tmp/fluxbox.log 2>&1 &
  PIDS+=("$!")
fi

if command -v x11vnc >/dev/null 2>&1 && command -v websockify >/dev/null 2>&1; then
  x11vnc -display "$DISPLAY" -forever -shared -nopw -localhost -rfbport 5900 >/tmp/x11vnc.log 2>&1 &
  PIDS+=("$!")
  NOVNC_LISTEN="${SGCC_NOVNC_LISTEN:-127.0.0.1:36080}"
  websockify --web=/usr/share/novnc/ "$NOVNC_LISTEN" localhost:5900 >/tmp/novnc.log 2>&1 &
  PIDS+=("$!")
  echo "noVNC ready on ${NOVNC_LISTEN}; Chrome will launch on demand" >&2
fi

python3 -u -m sgcc_ha_bridge.browser_service &
SERVICE_PID="$!"
PIDS+=("$SERVICE_PID")

# Keep sgcc_ha_bridge.browser_service as the primary process. Optional desktop helpers
# (fluxbox/x11vnc/websockify) may exit on minimal hosts; they must not bring
# down the browser manager used by Docker Compose.
wait "$SERVICE_PID"
exit $?
