#!/usr/bin/env bash
#
# local-test.sh — bring up a local Telex test environment for hermes-telex.
#
# It reuses Voyager's own make targets:
#   - `make deploy tunnel-test with-server`  chisel-forwards the REMOTE test
#     server's ports to localhost, including the Voyager API on 127.0.0.1:8000.
#   - `make web dev`                          runs the local Next.js frontend
#     (http://localhost:3000) against that API.
#
# Telex ships inside the Voyager instance, so once the tunnel is up the Telex
# Open API is reachable at  http://127.0.0.1:8000/voyager/v1/openapi/telex/...
# and hermes-telex should be configured with TELEX_BASE_URL=http://127.0.0.1:8000.
#
# Usage:
#   scripts/local-test.sh up            # start tunnel + web (background), wait until ready
#   scripts/local-test.sh down          # stop web + tunnel
#   scripts/local-test.sh status        # show process + port state
#   scripts/local-test.sh logs [tunnel|web]
#   scripts/local-test.sh env           # print the hermes-telex env block for this env
#   scripts/local-test.sh register-bot  # register a Telex bot on the tunneled server (needs a session token)
#
# Config (env vars):
#   VOYAGER_DIR       Path to the Voyager repo (default: auto-detected sibling checkout)
#   API_PORT          Local Voyager API port (default: 8000)
#   WEB_PORT          Local web dev port (default: 3000)
#   WAIT_TIMEOUT      Seconds to wait for each service (default: 120)
#   VOYAGER_SESSION   Session JWT for `register-bot` (or pass --token <jwt>)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN_DIR="$REPO_DIR/.local-test"          # pid/log scratch (gitignored)
API_PORT="${API_PORT:-8000}"
WEB_PORT="${WEB_PORT:-3000}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-120}"
API_BASE="http://127.0.0.1:${API_PORT}"
WEB_BASE="http://localhost:${WEB_PORT}"

# --- Locate the Voyager repo (has the deploy/ + web/ make targets) ------------
resolve_voyager_dir() {
  if [[ -n "${VOYAGER_DIR:-}" ]]; then
    echo "$VOYAGER_DIR"; return
  fi
  local c
  for c in "$SCRIPT_DIR/../../../voyager" "$SCRIPT_DIR/../../voyager"; do
    if [[ -f "$c/Makefile" && -d "$c/deploy" && -d "$c/web" ]]; then
      (cd "$c" && pwd); return
    fi
  done
  echo ""  # not found
}
VOYAGER_DIR="$(resolve_voyager_dir)"

log()  { printf '\033[36m[local-test]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[local-test]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[local-test]\033[0m %s\n' "$*" >&2; exit 1; }

require_voyager() {
  [[ -n "$VOYAGER_DIR" ]] || die "Voyager repo not found. Set VOYAGER_DIR=/path/to/voyager (must contain Makefile, deploy/, web/)."
  log "Voyager repo: $VOYAGER_DIR"
}

port_open() { # host port
  (exec 3<>"/dev/tcp/$1/$2") 2>/dev/null && { exec 3>&- 3<&-; return 0; } || return 1
}

wait_for_port() { # host port label
  local host="$1" port="$2" label="$3" waited=0
  log "waiting for $label ($host:$port) ..."
  while ! port_open "$host" "$port"; do
    sleep 1; waited=$((waited+1))
    if (( waited >= WAIT_TIMEOUT )); then
      warn "$label did not come up within ${WAIT_TIMEOUT}s — check: $0 logs"
      return 1
    fi
  done
  log "$label is up."
}

preflight() {
  require_voyager
  command -v make  >/dev/null || die "make not found"
  command -v chisel >/dev/null || die "chisel not found (tunnel-test needs it; go install github.com/jpillora/chisel@latest)"
  command -v pnpm  >/dev/null || die "pnpm not found (needed by 'make web dev')"
  if [[ ! -d "$VOYAGER_DIR/web/node_modules" ]]; then
    warn "web/node_modules missing — run 'cd $VOYAGER_DIR/web && pnpm install' first (or 'make -C $VOYAGER_DIR init')."
  fi
  mkdir -p "$RUN_DIR"
}

start_proc() { # name  "command..."
  local name="$1"; shift
  local pidfile="$RUN_DIR/$name.pid" logfile="$RUN_DIR/$name.log"
  if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    log "$name already running (pid $(cat "$pidfile"))."; return
  fi
  log "starting $name -> $logfile"
  # New session so we can signal the whole group on teardown.
  nohup bash -c "exec $*" >"$logfile" 2>&1 &
  echo $! >"$pidfile"
}

stop_proc() { # name  [pkill-pattern]
  local name="$1" pattern="${2:-}"
  local pidfile="$RUN_DIR/$name.pid"
  if [[ -f "$pidfile" ]]; then
    local pid; pid="$(cat "$pidfile")"
    if kill -0 "$pid" 2>/dev/null; then
      log "stopping $name (pid $pid)"
      kill "$pid" 2>/dev/null || true
      sleep 1
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$pidfile"
  fi
  # Fallback: reap detached children the make target spawned.
  [[ -n "$pattern" ]] && pkill -f "$pattern" 2>/dev/null || true
}

cmd_up() {
  preflight
  # 1) Tunnel: forwards remote Voyager API to 127.0.0.1:8000 (+ mysql/redis/gitlab/s3).
  start_proc tunnel "make -C '$VOYAGER_DIR' deploy tunnel-test with-server"
  wait_for_port 127.0.0.1 "$API_PORT" "Voyager API (tunnel)" || true
  # 2) Local frontend against the tunneled API.
  #    Use `make -C` (a single external command) — NOT `cd ... && make`, because
  #    start_proc runs the command under `exec`, and `exec cd` would replace the
  #    shell with the `cd` builtin and die before make ever runs.
  start_proc web "make -C '$VOYAGER_DIR' web dev"
  wait_for_port 127.0.0.1 "$WEB_PORT" "Web frontend" || true

  echo
  log "Local Telex test environment is up:"
  echo "    Voyager API : $API_BASE   (Telex Open API under /voyager/v1/openapi/telex/)"
  echo "    Web UI      : $WEB_BASE   (log in with your real account, DM/@mention the bot)"
  echo "    Logs        : $0 logs [tunnel|web]"
  echo "    Stop        : $0 down"
  echo
  log "hermes-telex config for this env:  $0 env"
}

cmd_down() {
  stop_proc web  "next dev"
  pkill -f "next-server" 2>/dev/null || true   # turbopack child outlives `make`
  stop_proc tunnel "chisel client --auth"
  log "stopped."
}

cmd_status() {
  local p
  for p in tunnel web; do
    local pidfile="$RUN_DIR/$p.pid"
    if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
      echo "  $p: running (pid $(cat "$pidfile"))"
    else
      echo "  $p: stopped"
    fi
  done
  port_open 127.0.0.1 "$API_PORT" && echo "  api  :$API_PORT open" || echo "  api  :$API_PORT closed"
  port_open 127.0.0.1 "$WEB_PORT" && echo "  web  :$WEB_PORT open" || echo "  web  :$WEB_PORT closed"
}

cmd_logs() {
  local which="${1:-}"
  case "$which" in
    tunnel|web) tail -n 200 -f "$RUN_DIR/$which.log" ;;
    "")         tail -n 100 -f "$RUN_DIR"/tunnel.log "$RUN_DIR"/web.log ;;
    *) die "logs: expected 'tunnel' or 'web'";;
  esac
}

cmd_env() {
  cat <<EOF
# hermes-telex env quickstart for the local (tunneled) Telex test server.
# Register a bot first ($0 register-bot) and paste its key + id below.
# (Full config incl. multi-account lives in ~/.hermes/config.yaml under platforms.telex.extra — see TD 5.)
TELEX_API_KEY=<plaintext_key from register-bot>
TELEX_BASE_URL=$API_BASE
TELEX_BOT_ID=<bot.id from register-bot>
TELEX_DM_POLICY=allowlist                 # open | allowlist | pairing
TELEX_ALLOW_FROM=<your-account-email>      # comma-separated ids/emails; "*" for all (open)
TELEX_GROUP_POLICY=disabled                # disabled | allowlist | open
TELEX_GROUP_REQUIRE_MENTION=true
EOF
}

cmd_register_bot() {
  local token="${VOYAGER_SESSION:-}" name="hermes-test-bot" desc="hermes-telex local test" vis=1
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --token) token="$2"; shift 2;;
      --name)  name="$2";  shift 2;;
      --visibility) vis="$2"; shift 2;;
      *) die "register-bot: unknown arg '$1'";;
    esac
  done
  [[ -n "$token" ]] || die "register-bot needs a session JWT: pass --token <jwt> or set VOYAGER_SESSION.
  Get it from the browser after logging into $WEB_BASE: DevTools > Application > Local Storage > 'voyager_session'."
  command -v curl >/dev/null || die "curl not found"
  port_open 127.0.0.1 "$API_PORT" || die "API $API_BASE not reachable — run '$0 up' first."
  log "registering bot '$name' (visibility=$vis) on $API_BASE ..."
  curl -fsS -X POST "$API_BASE/voyager/v1/telex/register-bot" \
    -H "Authorization: Bearer $token" \
    -H "Content-Type: application/json" \
    -d "{\"display_name\":\"$name\",\"description\":\"$desc\",\"visibility\":$vis}"
  echo
  warn "Save 'plaintext_key' now — it is shown only once. Put it in TELEX_API_KEY and bot.id in TELEX_BOT_IDENTITY_ID."
}

usage() {
  sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

case "${1:-up}" in
  up)            cmd_up ;;
  down|stop)     cmd_down ;;
  status)        cmd_status ;;
  logs)          shift; cmd_logs "${1:-}" ;;
  env)           cmd_env ;;
  register-bot)  shift; cmd_register_bot "$@" ;;
  -h|--help|help) usage ;;
  *) die "unknown command '$1' (try: up | down | status | logs | env | register-bot | help)";;
esac
