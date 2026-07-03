#!/usr/bin/env bash
set -euo pipefail

# Disable and remove the remote hermes-telex plugin, then restart Hermes.
# Mirrors hermes-seatalk's cleanup. Connection defaults from deploy/env.local.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ENV_LOCAL="$SCRIPT_DIR/env.local"
SMC_PROFILE_OVERRIDE=""
SERVER_HOST_OVERRIDE=""
VM_NAME_OVERRIDE=""
REMOTE_USER_OVERRIDE=""
REMOTE_HERMES_HOME_OVERRIDE=""
REMOTE_HERMES_INSTALL_DIR_OVERRIDE=""
REMOTE_PLUGIN_DIR_OVERRIDE=""
PLUGIN_ID="telex-platform"

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Options:
  --env-local PATH      connection file (default: deploy/env.local)
  --smc-profile NAME    SMC profile override
  --server HOST         remote server host/IP override
  --vm NAME             Incus VM name override
  --remote-user USER    VM user override
  --hermes-home PATH    VM HERMES_HOME (default: /home/<user>/.hermes)
  --install-dir PATH    VM Hermes install dir (default: /home/<user>/hermes-agent)
  --plugin-dir PATH     VM plugin dir (default: <HERMES_HOME>/plugins/<plugin-id>)
  --plugin-id NAME      Hermes plugin id (default: telex-platform)
  -h, --help            show this help
EOF
}

die() { echo "ERROR: $1" >&2; exit 1; }
info() { echo "== $1"; }

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

while [[ $# -gt 0 ]]; do
    case "$1" in
        --env-local) shift 2 ;;
        --smc-profile) [[ $# -ge 2 ]] || die "--smc-profile requires a name"; SMC_PROFILE_OVERRIDE="$2"; shift 2 ;;
        --server) [[ $# -ge 2 ]] || die "--server requires a host"; SERVER_HOST_OVERRIDE="$2"; shift 2 ;;
        --vm) [[ $# -ge 2 ]] || die "--vm requires a VM name"; VM_NAME_OVERRIDE="$2"; shift 2 ;;
        --remote-user) [[ $# -ge 2 ]] || die "--remote-user requires a user"; REMOTE_USER_OVERRIDE="$2"; shift 2 ;;
        --hermes-home) [[ $# -ge 2 ]] || die "--hermes-home requires a path"; REMOTE_HERMES_HOME_OVERRIDE="$2"; shift 2 ;;
        --install-dir) [[ $# -ge 2 ]] || die "--install-dir requires a path"; REMOTE_HERMES_INSTALL_DIR_OVERRIDE="$2"; shift 2 ;;
        --plugin-dir) [[ $# -ge 2 ]] || die "--plugin-dir requires a path"; REMOTE_PLUGIN_DIR_OVERRIDE="$2"; shift 2 ;;
        --plugin-id) [[ $# -ge 2 ]] || die "--plugin-id requires a name"; PLUGIN_ID="$2"; shift 2 ;;
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
REMOTE_CANONICAL_PLUGIN_DIR="${REMOTE_HERMES_HOME}/plugins/${PLUGIN_ID}"
REMOTE_LEGACY_PLUGIN_DIR="${REMOTE_HERMES_HOME}/plugins/telex"

shell_quote() { printf '%q' "$1"; }
smc_toc() { smc -c "$SMC_PROFILE" toc "$SERVER_HOST" -- "$1"; }
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

info "Disable and remove Telex plugin, then restart gateway"
CLEANUP_CMD=$(cat <<EOF
set -eu
export HERMES_HOME="${REMOTE_HERMES_HOME}"
export PLUGIN_ID="${PLUGIN_ID}"
export PATH="\$HOME/.local/bin:\$HOME/.cargo/bin:\$PATH"

command -v hermes >/dev/null
test -d "${REMOTE_HERMES_INSTALL_DIR}"

service=hermes-gateway.service
systemctl --user stop "\$service" 2>/dev/null || true
systemctl --user reset-failed "\$service" 2>/dev/null || true

if [ -d "${REMOTE_PLUGIN_DIR}" ] || [ -d "${REMOTE_CANONICAL_PLUGIN_DIR}" ] || [ -d "${REMOTE_LEGACY_PLUGIN_DIR}" ]; then
    hermes plugins disable "\$PLUGIN_ID" || true
fi

"${REMOTE_HERMES_INSTALL_DIR}/venv/bin/python" - <<'PY'
import os
from pathlib import Path
import yaml
home = Path(os.environ["HERMES_HOME"])
config_path = home / "config.yaml"
data = yaml.safe_load(config_path.read_text()) if config_path.exists() else {}
data = data or {}
plugins = data.setdefault("plugins", {})
enabled = plugins.get("enabled") or []
disabled = plugins.get("disabled") or []
remove = {"telex", "telex-platform", os.environ["PLUGIN_ID"]}
plugins["enabled"] = sorted(x for x in enabled if x not in remove)
plugins["disabled"] = sorted(set(disabled) | {os.environ["PLUGIN_ID"]})
config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
PY

rm -rf "${REMOTE_PLUGIN_DIR}" "${REMOTE_CANONICAL_PLUGIN_DIR}" "${REMOTE_LEGACY_PLUGIN_DIR}"

yes | hermes gateway install --force
systemctl --user daemon-reload
systemctl --user restart "\$service"
sleep 3
systemctl --user --no-pager -l status "\$service" | sed -n '1,18p' || true
hermes gateway status || true
EOF
)

vm_user_checked "$CLEANUP_CMD"

cat <<EOF

Telex plugin removed.
  server:      ${SERVER_HOST}
  vm:          ${VM_NAME}
  user:        ${REMOTE_USER}
  plugin dirs: ${REMOTE_PLUGIN_DIR}
               ${REMOTE_CANONICAL_PLUGIN_DIR}
               ${REMOTE_LEGACY_PLUGIN_DIR}
  plugin id:   ${PLUGIN_ID}
  HERMES_HOME: ${REMOTE_HERMES_HOME}
EOF
