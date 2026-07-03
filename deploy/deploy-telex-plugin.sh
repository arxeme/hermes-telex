#!/usr/bin/env bash
set -euo pipefail

# Publish the local hermes-telex plugin into a remote Hermes installation
# (an Incus VM on the Voyager test server), mirroring hermes-seatalk's deploy.
#
# Connection defaults are read from deploy/env.local:
#   SMC_PROFILE=...
#   SERVER_HOST=...                 # test server reachable via smc toc
#   VM_NAME=...                     # Incus VM running hermes-agent
#   REMOTE_USER=...                 # VM user that owns the hermes install
#   REMOTE_HERMES_HOME=/home/<user>/.hermes
#   REMOTE_HERMES_INSTALL_DIR=/home/<user>/hermes-agent
#   REMOTE_PLUGIN_DIR=<HERMES_HOME>/plugins/telex-platform
#
# Runtime config (TELEX_API_KEY, TELEX_BASE_URL, policies, ...) is uploaded to
# HERMES_HOME/.env only when --runtime-env-file is passed or deploy/.env exists.
# TELEX_BASE_URL must point at the Voyager API reachable *from inside the VM*
# (e.g. the Incus host gateway or the internal voyager.ingarena.net), NOT the
# local-test.sh tunnel address.
#
# Only the plugin tree under HERMES_HOME/plugins/telex-platform is replaced;
# other Hermes runtime state is preserved.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ENV_LOCAL="$SCRIPT_DIR/env.local"
RUNTIME_ENV_FILE="AUTO"
SMC_PROFILE_OVERRIDE=""
SERVER_HOST_OVERRIDE=""
VM_NAME_OVERRIDE=""
REMOTE_USER_OVERRIDE=""
REMOTE_HERMES_HOME_OVERRIDE=""
REMOTE_HERMES_INSTALL_DIR_OVERRIDE=""
REMOTE_PLUGIN_DIR_OVERRIDE=""
PLUGIN_ID="telex-platform"
KEEP_LOCAL_ARCHIVE=0
KEEP_REMOTE_ARCHIVE=0

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Options:
  --env-local PATH          connection file (default: deploy/env.local)
  --runtime-env-file PATH   upload PATH to HERMES_HOME/.env before restart
  --no-runtime-env          do not upload deploy/.env even if it exists
  --smc-profile NAME        SMC profile override
  --server HOST             remote server host/IP override
  --vm NAME                 Incus VM name override
  --remote-user USER        VM user override
  --hermes-home PATH        VM HERMES_HOME (default: /home/<user>/.hermes)
  --install-dir PATH        VM Hermes install dir (default: /home/<user>/hermes-agent)
  --plugin-dir PATH         VM plugin dir (default: <HERMES_HOME>/plugins/<plugin-id>)
  --plugin-id NAME          Hermes plugin id to enable (default: telex-platform)
  --keep-local-archive      keep generated archive directory
  --keep-remote-archive     keep VM /tmp archive
  -h, --help                show this help

Examples:
  $(basename "$0")
  $(basename "$0") --runtime-env-file ./deploy/.env
EOF
}

die() { echo "ERROR: $1" >&2; exit 1; }
info() { echo "== $1"; }

# First pass: pick up --env-local before sourcing.
ARGS=("$@")
while [[ $# -gt 0 ]]; do
    case "$1" in
        --env-local) [[ $# -ge 2 ]] || die "--env-local requires a path"; ENV_LOCAL="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) shift ;;
    esac
done
set -- ${ARGS[@]+"${ARGS[@]}"}

[[ -f "$ENV_LOCAL" ]] || die "connection file not found: $ENV_LOCAL (copy deploy/env.example.local)"
set -a
# shellcheck source=/dev/null
. "$ENV_LOCAL"
set +a

# Second pass: real options.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --env-local) shift 2 ;;
        --runtime-env-file) [[ $# -ge 2 ]] || die "--runtime-env-file requires a path"; RUNTIME_ENV_FILE="$2"; shift 2 ;;
        --no-runtime-env) RUNTIME_ENV_FILE=""; shift ;;
        --smc-profile) [[ $# -ge 2 ]] || die "--smc-profile requires a name"; SMC_PROFILE_OVERRIDE="$2"; shift 2 ;;
        --server) [[ $# -ge 2 ]] || die "--server requires a host"; SERVER_HOST_OVERRIDE="$2"; shift 2 ;;
        --vm) [[ $# -ge 2 ]] || die "--vm requires a VM name"; VM_NAME_OVERRIDE="$2"; shift 2 ;;
        --remote-user) [[ $# -ge 2 ]] || die "--remote-user requires a user"; REMOTE_USER_OVERRIDE="$2"; shift 2 ;;
        --hermes-home) [[ $# -ge 2 ]] || die "--hermes-home requires a path"; REMOTE_HERMES_HOME_OVERRIDE="$2"; shift 2 ;;
        --install-dir) [[ $# -ge 2 ]] || die "--install-dir requires a path"; REMOTE_HERMES_INSTALL_DIR_OVERRIDE="$2"; shift 2 ;;
        --plugin-dir) [[ $# -ge 2 ]] || die "--plugin-dir requires a path"; REMOTE_PLUGIN_DIR_OVERRIDE="$2"; shift 2 ;;
        --plugin-id) [[ $# -ge 2 ]] || die "--plugin-id requires a name"; PLUGIN_ID="$2"; shift 2 ;;
        --keep-local-archive) KEEP_LOCAL_ARCHIVE=1; shift ;;
        --keep-remote-archive) KEEP_REMOTE_ARCHIVE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

SMC_PROFILE="${SMC_PROFILE_OVERRIDE:-${SMC_PROFILE:-}}"
SERVER_HOST="${SERVER_HOST_OVERRIDE:-${SERVER_HOST:-}}"
VM_NAME="${VM_NAME_OVERRIDE:-${VM_NAME:-}}"
REMOTE_USER="${REMOTE_USER_OVERRIDE:-${REMOTE_USER:-}}"

[[ -n "$SMC_PROFILE" ]] || die "missing SMC_PROFILE"
[[ -n "$SERVER_HOST" ]] || die "missing SERVER_HOST"
[[ -n "$VM_NAME" ]] || die "missing VM_NAME"
[[ -n "$REMOTE_USER" ]] || die "missing REMOTE_USER"

REMOTE_HERMES_HOME="${REMOTE_HERMES_HOME_OVERRIDE:-${REMOTE_HERMES_HOME:-/home/${REMOTE_USER}/.hermes}}"
REMOTE_HERMES_INSTALL_DIR="${REMOTE_HERMES_INSTALL_DIR_OVERRIDE:-${REMOTE_HERMES_INSTALL_DIR:-/home/${REMOTE_USER}/hermes-agent}}"
REMOTE_PLUGIN_DIR="${REMOTE_PLUGIN_DIR_OVERRIDE:-${REMOTE_PLUGIN_DIR:-${REMOTE_HERMES_HOME}/plugins/${PLUGIN_ID}}}"

if [[ "$RUNTIME_ENV_FILE" == "AUTO" ]]; then
    [[ -f "$SCRIPT_DIR/.env" ]] && RUNTIME_ENV_FILE="$SCRIPT_DIR/.env" || RUNTIME_ENV_FILE=""
fi
if [[ -n "$RUNTIME_ENV_FILE" ]]; then
    [[ -f "$RUNTIME_ENV_FILE" ]] || die "runtime env file not found: $RUNTIME_ENV_FILE"
    RUNTIME_ENV_FILE="$(cd "$(dirname "$RUNTIME_ENV_FILE")" && pwd)/$(basename "$RUNTIME_ENV_FILE")"
fi

[[ -f "$PLUGIN_ROOT/plugin.yaml" ]] || die "plugin.yaml not found under $PLUGIN_ROOT"
[[ -f "$PLUGIN_ROOT/adapter.py" ]] || die "adapter.py not found under $PLUGIN_ROOT"
[[ -d "$PLUGIN_ROOT/hermes_telex" ]] || die "hermes_telex package not found under $PLUGIN_ROOT"

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/telex-plugin-deploy.XXXXXX")"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE_BASENAME="hermes-telex-plugin_${TIMESTAMP}.tar.gz"
ARCHIVE="$TMP_DIR/$ARCHIVE_BASENAME"
REMOTE_ARCHIVE="/tmp/$ARCHIVE_BASENAME"
ENV_BASENAME=""

cleanup() {
    if (( KEEP_LOCAL_ARCHIVE == 0 )); then rm -rf "$TMP_DIR"; else echo "Kept local archive: $TMP_DIR" >&2; fi
}
trap cleanup EXIT

shell_quote() { printf '%q' "$1"; }
smc_toc() { smc -c "$SMC_PROFILE" toc "$SERVER_HOST" -- "$1"; }
server() { smc_toc "$1"; }
vm_root() { smc_toc "sudo incus exec ${VM_NAME} -- bash -lc $(shell_quote "$1")"; }
vm_user() { smc_toc "sudo incus exec ${VM_NAME} -- su - ${REMOTE_USER} -c $(shell_quote "$1")"; }
vm_user_checked() {
    local out rc
    out="$(vm_user "$1
remote_rc=\$?
echo __REMOTE_EXIT__:\$remote_rc
exit \$remote_rc")"
    printf '%s\n' "$out"
    rc="$(printf '%s\n' "$out" | sed -n 's/^__REMOTE_EXIT__://p' | tail -n 1)"
    [[ "$rc" == "0" ]] || die "remote command failed (exit ${rc:-unknown})"
}

info "Package local Telex plugin"
tar -czf "$ARCHIVE" \
    --exclude="./.git" \
    --exclude="./.venv" \
    --exclude="./.pytest_cache" \
    --exclude="./__pycache__" \
    --exclude="./*/__pycache__" \
    --exclude="./*.pyc" \
    --exclude="./deploy" \
    --exclude="./docs" \
    --exclude="./tests" \
    --exclude="./scripts" \
    --exclude="./.local-test" \
    --exclude="./uv.lock" \
    --exclude="./*.local" \
    --exclude="./.env" \
    -C "$PLUGIN_ROOT" .

info "Upload plugin archive to ${SERVER_HOST}/${VM_NAME}"
smc -c "$SMC_PROFILE" scp "$ARCHIVE" "${SERVER_HOST}:/tmp/${ARCHIVE_BASENAME}"
server "sudo incus file push /tmp/${ARCHIVE_BASENAME} ${VM_NAME}${REMOTE_ARCHIVE}"
smc_toc "rm -f /tmp/${ARCHIVE_BASENAME}"

if [[ -n "$RUNTIME_ENV_FILE" ]]; then
    ENV_BASENAME="hermes-telex-runtime_${TIMESTAMP}.env"
    info "Upload runtime env file"
    smc -c "$SMC_PROFILE" scp "$RUNTIME_ENV_FILE" "${SERVER_HOST}:/tmp/${ENV_BASENAME}"
    server "sudo incus file push /tmp/${ENV_BASENAME} ${VM_NAME}/tmp/${ENV_BASENAME}"
    smc_toc "rm -f /tmp/${ENV_BASENAME}"
fi

info "Install plugin, verify register(ctx), and restart gateway"
INSTALL_CMD=$(cat <<EOF
set -eu
export HERMES_HOME="${REMOTE_HERMES_HOME}"
export REMOTE_PLUGIN_DIR="${REMOTE_PLUGIN_DIR}"
export PLUGIN_ID="${PLUGIN_ID}"
export PATH="\$HOME/.local/bin:\$HOME/.cargo/bin:\$PATH"

command -v hermes >/dev/null
test -d "${REMOTE_HERMES_INSTALL_DIR}"
test -x "${REMOTE_HERMES_INSTALL_DIR}/venv/bin/python"

service=hermes-gateway.service
systemctl --user stop "\$service" 2>/dev/null || true
systemctl --user reset-failed "\$service" 2>/dev/null || true

mkdir -p "${REMOTE_HERMES_HOME}/plugins"
rm -rf "${REMOTE_PLUGIN_DIR}"
mkdir -p "${REMOTE_PLUGIN_DIR}"
tar -xzf "${REMOTE_ARCHIVE}" -C "${REMOTE_PLUGIN_DIR}"
chmod -R u+rwX,go+rX "${REMOTE_PLUGIN_DIR}"
test -f "${REMOTE_PLUGIN_DIR}/plugin.yaml"
test -f "${REMOTE_PLUGIN_DIR}/adapter.py"

if [ -f "${REMOTE_PLUGIN_DIR}/requirements.txt" ]; then
    if "${REMOTE_HERMES_INSTALL_DIR}/venv/bin/python" -m pip --version >/dev/null 2>&1; then
        "${REMOTE_HERMES_INSTALL_DIR}/venv/bin/python" -m pip install -r "${REMOTE_PLUGIN_DIR}/requirements.txt"
    elif command -v uv >/dev/null 2>&1; then
        uv pip install --python "${REMOTE_HERMES_INSTALL_DIR}/venv/bin/python" -r "${REMOTE_PLUGIN_DIR}/requirements.txt"
    else
        "${REMOTE_HERMES_INSTALL_DIR}/venv/bin/python" - <<'PY'
import importlib.util
missing = [m for m in ("aiohttp",) if importlib.util.find_spec(m) is None]
if missing:
    raise SystemExit("pip and uv unavailable, and missing modules: " + ", ".join(missing))
print("pip unavailable; plugin requirements already satisfied")
PY
    fi
fi

if [ -n "${ENV_BASENAME}" ] && [ -f "/tmp/${ENV_BASENAME}" ]; then
    install -m 600 "/tmp/${ENV_BASENAME}" "${REMOTE_HERMES_HOME}/.env"
    rm -f "/tmp/${ENV_BASENAME}"
fi

hermes plugins enable "\$PLUGIN_ID"

"${REMOTE_HERMES_INSTALL_DIR}/venv/bin/python" - <<'PY'
import importlib.util, os, pathlib, sys, types
plugin_dir = pathlib.Path(os.environ["REMOTE_PLUGIN_DIR"])
parent = "hermes_plugins"
if parent not in sys.modules:
    ns = types.ModuleType(parent); ns.__path__ = []; ns.__package__ = parent
    sys.modules[parent] = ns
module_name = f"{parent}.telex"
spec = importlib.util.spec_from_file_location(
    module_name, plugin_dir / "__init__.py", submodule_search_locations=[str(plugin_dir)])
if spec is None or spec.loader is None:
    raise SystemExit("cannot load Telex plugin package")
module = importlib.util.module_from_spec(spec)
module.__package__ = module_name; module.__path__ = [str(plugin_dir)]
sys.modules[module_name] = module
spec.loader.exec_module(module)
if not hasattr(module, "register"):
    raise SystemExit("Telex plugin package does not expose register(ctx)")
print("register(ctx) OK")
PY

# Non-interactive install: feed "y" to the "Start the gateway now?" prompt.
yes | hermes gateway install --force
systemctl --user daemon-reload
systemctl --user restart "\$service"
sleep 3
systemctl --user --no-pager -l status "\$service" | sed -n '1,18p' || true
hermes gateway status || true
EOF
)

info "Normalize remote ownership of plugin and log dirs (as root)"
vm_root "chown -R ${REMOTE_USER}:${REMOTE_USER} ${REMOTE_PLUGIN_DIR} ${REMOTE_HERMES_HOME}/logs 2>/dev/null || true"

vm_user_checked "$INSTALL_CMD"

if (( KEEP_REMOTE_ARCHIVE == 0 )); then
    vm_root "rm -f ${REMOTE_ARCHIVE}"
fi

cat <<EOF

Telex plugin deployed.
  server:      ${SERVER_HOST}
  vm:          ${VM_NAME}
  user:        ${REMOTE_USER}
  plugin dir:  ${REMOTE_PLUGIN_DIR}
  plugin id:   ${PLUGIN_ID}
  HERMES_HOME: ${REMOTE_HERMES_HOME}
EOF
