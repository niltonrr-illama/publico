#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
WHEEL="${1:-}"

[[ "$(uname -s)" == "Linux" ]] || {
  printf '%s\n' 'runtime smoke requires Linux' >&2
  exit 2
}
[[ -f "${WHEEL}" && "${WHEEL}" == *.whl ]] || {
  printf '%s\n' 'usage: smoke-runtime-targets.sh PACKAGE.whl' >&2
  exit 2
}
WHEEL="$(realpath -- "${WHEEL}")"

SMOKE_PARENT="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
mkdir -p -- "${SMOKE_PARENT}"
SMOKE_PARENT="$(realpath -- "${SMOKE_PARENT}")"
SMOKE_ROOT="$(mktemp -d "${SMOKE_PARENT}/espelho-zap-runtime-smoke.XXXXXX")"

cleanup() {
  local status="$1" resolved log_file
  if (( status != 0 )) && [[ -d "${SMOKE_ROOT}" ]]; then
    printf 'runtime smoke failed with status %s; sanitized logs follow\n' "${status}" >&2
    while IFS= read -r -d '' log_file; do
      printf '\n== %s ==\n' "${log_file#"${SMOKE_ROOT}/"}" >&2
      sed -n '1,240p' -- "${log_file}" >&2
    done < <(find "${SMOKE_ROOT}" -type f -name '*.log' -print0 | sort -z)
  fi
  resolved="$(realpath -m -- "${SMOKE_ROOT}")"
  case "${resolved}" in
    "${SMOKE_PARENT}"/espelho-zap-runtime-smoke.*) rm -rf -- "${resolved}" ;;
    *) printf '%s\n' 'refusing unsafe runtime smoke cleanup' >&2 ;;
  esac
}
trap 'status=$?; trap - EXIT; cleanup "${status}"; exit "${status}"' EXIT

FAKE_BIN="${SMOKE_ROOT}/fake-bin"
SYSTEMCTL_LOG="${SMOKE_ROOT}/systemctl.log"
RUNTIME_CLI_LOG="${SMOKE_ROOT}/runtime-cli.log"
mkdir -p -- "${FAKE_BIN}"
export SYSTEMCTL_LOG RUNTIME_CLI_LOG
cat >"${FAKE_BIN}/systemctl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${SYSTEMCTL_LOG:?}"
if [[ "${SYSTEMCTL_FAIL_DAEMON_RELOAD:-0}" == "1" && "$*" == "--user daemon-reload" ]]; then
  exit 1
fi
EOF
chmod 0700 "${FAKE_BIN}/systemctl"

cat >"${FAKE_BIN}/hermes" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
home="${HERMES_HOME:?}"
printf 'hermes %s\n' "$*" >>"${RUNTIME_CLI_LOG:?}"
mkdir -p -- "${home}"
case "${1:-} ${2:-}" in
  "plugins enable")
    config="${home}/config.yaml"
    if [[ ! -f "${config}" ]] || ! grep -Fq 'espelho-zap-portable' "${config}"; then
      printf '%s\n' 'plugins:' '  enabled:' '    - espelho-zap-portable' >>"${config}"
    fi
    ;;
  "plugins disable") sed -i '/espelho-zap-portable/d' "${home}/config.yaml" 2>/dev/null || true ;;
  "plugins list")
    [[ ! -d "${home}/plugins/espelho-zap-portable" ]] \
      || printf '%s\n' 'espelho-zap-portable enabled'
    ;;
  "gateway stop") printf '%s\n' stopped >"${home}/.gateway-state" ;;
  "gateway restart") printf '%s\n' running >"${home}/.gateway-state" ;;
  "gateway status")
    [[ ! -f "${home}/.gateway-state" || "$(<"${home}/.gateway-state")" == running ]] || exit 1
    printf '%s\n' 'gateway: running'
    ;;
  *) printf 'unsupported fake Hermes command: %s\n' "$*" >&2; exit 2 ;;
esac
EOF
chmod 0700 "${FAKE_BIN}/hermes"

cat >"${FAKE_BIN}/openclaw" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
state="${OPENCLAW_STATE_DIR:?}"
config="${OPENCLAW_CONFIG_PATH:?}"
printf 'openclaw %s\n' "$*" >>"${RUNTIME_CLI_LOG:?}"
mkdir -p -- "${state}" "$(dirname -- "${config}")"

case "${1:-} ${2:-}" in
  "plugins install")
    source_dir="${3:?}"
    destination="${state}/extensions/espelho-zap-portable"
    case "${destination}" in
      "${state}"/extensions/espelho-zap-portable) rm -rf -- "${destination}" ;;
      *) printf '%s\n' 'unsafe fake OpenClaw destination' >&2; exit 2 ;;
    esac
    mkdir -p -- "${destination}" "${state}/state"
    cp -R -- "${source_dir}/." "${destination}/"
    python3 - "${config}" "${state}/state/openclaw.sqlite" <<'PY'
import json
from pathlib import Path
import sqlite3
import sys

config_path = Path(sys.argv[1])
value = json.loads(config_path.read_text()) if config_path.exists() else {}
plugins = value.setdefault("plugins", {})
allow = plugins.setdefault("allow", [])
if "espelho-zap-portable" not in allow:
    allow.append("espelho-zap-portable")
deny = plugins.setdefault("deny", [])
plugins["deny"] = [item for item in deny if item != "espelho-zap-portable"]
config_path.write_text(json.dumps(value, sort_keys=True) + "\n")
with sqlite3.connect(sys.argv[2]) as db:
    db.execute("CREATE TABLE IF NOT EXISTS installed_plugin_index (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    db.execute("INSERT OR REPLACE INTO installed_plugin_index VALUES (?, ?)", ("singleton", "espelho-zap-portable"))
    db.commit()
PY
    ;;
  "plugins enable")
    python3 - "${config}" <<'PY'
import json
from pathlib import Path
import sys
path = Path(sys.argv[1])
value = json.loads(path.read_text())
value.setdefault("plugins", {}).setdefault("entries", {}).setdefault(
    "espelho-zap-portable", {}
)["enabled"] = True
path.write_text(json.dumps(value, sort_keys=True) + "\n")
PY
    ;;
  "plugins uninstall")
    destination="${state}/extensions/espelho-zap-portable"
    rm -rf -- "${destination}"
    python3 - "${config}" "${state}/state/openclaw.sqlite" <<'PY'
import json
from pathlib import Path
import sqlite3
import sys

path = Path(sys.argv[1])
value = json.loads(path.read_text()) if path.exists() else {}
plugins = value.setdefault("plugins", {})
plugins["allow"] = [item for item in plugins.get("allow", []) if item != "espelho-zap-portable"]
plugins["deny"] = [item for item in plugins.get("deny", []) if item != "espelho-zap-portable"]
plugins.get("entries", {}).pop("espelho-zap-portable", None)
path.write_text(json.dumps(value, sort_keys=True) + "\n")
db_path = Path(sys.argv[2])
if db_path.exists():
    with sqlite3.connect(db_path) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='installed_plugin_index'").fetchone():
            db.execute("DELETE FROM installed_plugin_index WHERE payload = ?", ("espelho-zap-portable",))
            db.commit()
PY
    ;;
  "plugins list")
    if [[ -d "${state}/extensions/espelho-zap-portable" ]]; then
      printf '%s\n' '{"plugins":[{"id":"espelho-zap-portable"}]}'
    else
      printf '%s\n' '{"plugins":[]}'
    fi
    ;;
  "plugins doctor") printf '%s\n' '{"ok":true,"plugin":"espelho-zap-portable"}' ;;
  "plugins inspect")
    if [[ "${RUNTIME_CANARY_FAIL:-0}" == "1" ]]; then exit 9; fi
    printf '%s\n' '{"plugin":{"id":"espelho-zap-portable"},"hooks":["message_received","before_agent_reply","message_sending","reply_payload_sending"]}'
    ;;
  "config set")
    python3 - "${config}" "${3:?}" "${4:?}" <<'PY'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
key_path = sys.argv[2].split(".")
raw = sys.argv[3]
try:
    assigned = json.loads(raw)
except json.JSONDecodeError:
    assigned = raw
value = json.loads(path.read_text()) if path.exists() else {}
cursor = value
for key in key_path[:-1]:
    cursor = cursor.setdefault(key, {})
cursor[key_path[-1]] = assigned
path.write_text(json.dumps(value, sort_keys=True) + "\n")
PY
    ;;
  "config get")
    python3 - "${config}" "${3:?}" <<'PY'
import json
from pathlib import Path
import sys
value = json.loads(Path(sys.argv[1]).read_text())
cursor = value
for key in sys.argv[2].split("."):
    if not isinstance(cursor, dict) or key not in cursor:
        raise SystemExit(1)
    cursor = cursor[key]
print(json.dumps(cursor, separators=(",", ":")))
PY
    ;;
  "config unset")
    python3 - "${config}" "${3:?}" <<'PY'
import json
from pathlib import Path
import sys
path = Path(sys.argv[1])
value = json.loads(path.read_text())
parts = sys.argv[2].split(".")
cursor = value
for key in parts[:-1]:
    cursor = cursor.get(key, {}) if isinstance(cursor, dict) else {}
if isinstance(cursor, dict):
    cursor.pop(parts[-1], None)
path.write_text(json.dumps(value, sort_keys=True) + "\n")
PY
    ;;
  "config validate") python3 -m json.tool "${config}" >/dev/null ;;
  "gateway stop") printf '%s\n' stopped >"${state}/.gateway-state" ;;
  "gateway restart") printf '%s\n' running >"${state}/.gateway-state" ;;
  "gateway status")
    [[ ! -f "${state}/.gateway-state" || "$(<"${state}/.gateway-state")" == running ]] || exit 1
    printf '%s\n' 'gateway rpc: ok'
    ;;
  *) printf 'unsupported fake OpenClaw command: %s\n' "$*" >&2; exit 2 ;;
esac
EOF
chmod 0700 "${FAKE_BIN}/openclaw"

runtime_install() {
  local target="$1" action="$2" runtime="$3" runtime_home="$4" enable_runtime="$5"
  shift 5
  local home="${target}/home"
  mkdir -p -- "${home}" "${target}/run"
  local -a command=(
    bash "${PROJECT_DIR}/installer/install.sh" "${action}"
    --source "${WHEEL}" --runtime "${runtime}" --source-profile smoke-profile
  )
  if [[ -n "${runtime_home}" ]]; then
    command+=(--runtime-home "${runtime_home}")
  fi
  if [[ "${enable_runtime}" == "1" ]]; then
    command+=(--enable-runtime)
  fi
  local media_root
  for media_root in "$@"; do
    if [[ "${media_root}" == "__CLEAR__" ]]; then
      command+=(--clear-media-roots)
    else
      command+=(--media-root "${media_root}")
    fi
  done
  HOME="${home}" \
  XDG_DATA_HOME="${target}/data" \
  XDG_CONFIG_HOME="${target}/config" \
  XDG_STATE_HOME="${target}/state" \
  XDG_RUNTIME_DIR="${target}/run" \
  PATH="${FAKE_BIN}:${PATH}" \
  SYSTEMCTL_FAIL_DAEMON_RELOAD="${SYSTEMCTL_FAIL_DAEMON_RELOAD:-0}" \
  RUNTIME_CANARY_FAIL="${RUNTIME_CANARY_FAIL:-0}" \
    "${command[@]}" >"${target}/${action}.log" 2>&1
}

runtime_uninstall() {
  local target="$1" runtime="$2" runtime_home="$3"
  local home="${target}/home"
  mkdir -p -- "${target}/run"
  local -a command=(
    bash "${PROJECT_DIR}/installer/install.sh" uninstall --runtime "${runtime}"
  )
  if [[ -n "${runtime_home}" ]]; then
    command+=(--runtime-home "${runtime_home}")
  fi
  HOME="${home}" \
  XDG_DATA_HOME="${target}/data" \
  XDG_CONFIG_HOME="${target}/config" \
  XDG_STATE_HOME="${target}/state" \
  XDG_RUNTIME_DIR="${target}/run" \
  PATH="${FAKE_BIN}:${PATH}" \
    "${command[@]}" >"${target}/uninstall.log" 2>&1
}

validate_env_template() {
  local template="$1" expected_cli="$2" expected_config="$3" expected_profile="$4" expected_health="$5" expected_media="$6"
  local expected_line
  [[ -f "${template}" ]]
  grep -Fxq "ESPELHO_ZAP_CLI=${expected_cli}" "${template}"
  grep -Fxq "ESPELHO_ZAP_CONFIG=${expected_config}" "${template}"
  grep -Fxq "ESPELHO_ZAP_SOURCE_PROFILE_ID=${expected_profile}" "${template}"
  grep -Fxq "ESPELHO_ZAP_HOOK_HEALTH_FILE=${expected_health}" "${template}"
  grep -Fxq 'ESPELHO_ZAP_PRIVACY_SCOPE=owner_private' "${template}"
  if grep -Eq '^[A-Za-z_][A-Za-z0-9_]*(TOKEN|SECRET|PASSWORD)=' "${template}"; then
    printf '%s\n' 'runtime env template contains a secret-bearing assignment' >&2
    return 1
  fi
  if [[ -n "${expected_media}" ]]; then
    printf -v expected_line 'ESPELHO_ZAP_MEDIA_ROOTS=%q' "${expected_media}"
    grep -Fxq -- "${expected_line}" "${template}"
  elif grep -q '^ESPELHO_ZAP_MEDIA_ROOTS=' "${template}"; then
    printf '%s\n' 'runtime env template unexpectedly enables media' >&2
    return 1
  fi
  [[ "$(stat -c '%a' "${template}")" == "600" ]]
}

validate_hook_health() {
  local target="$1"
  local health="${target}/state/espelho-zap/capture-health.json"
  [[ -f "${health}" && "$(stat -c '%a' "${health}")" == "600" ]]
  python3 - "${health}" <<'PY'
import json
from pathlib import Path
import sys
value = json.loads(Path(sys.argv[1]).read_text())
assert value == {
    "schema_version": 1,
    "successes": 0,
    "failures": {},
    "last_success_at": "",
    "last_failure_at": "",
    "last_error_code": "",
}
PY
}

validate_media_config() {
  local target="$1"
  shift
  "${target}/data/espelho-zap/venv/bin/python" - \
    "${target}/config/espelho-zap/config.toml" "$@" <<'PY'
from pathlib import Path
import sys
import tomllib

config = Path(sys.argv[1])
expected = sys.argv[2:]
with config.open("rb") as handle:
    actual = tomllib.load(handle)["worker"]["source_media_roots"]
if actual != expected:
    raise SystemExit(f"unexpected source_media_roots: {actual!r}")
for raw_root in expected:
    root = Path(raw_root).resolve(strict=True)
    fixture = (root / "smoke-media.bin").resolve(strict=True)
    fixture.relative_to(root)
    if not fixture.is_file() or fixture.stat().st_size == 0:
        raise SystemExit("media smoke fixture is missing or empty")
PY
}

validate_worker_profile() {
  local target="$1" expected="$2"
  "${target}/data/espelho-zap/venv/bin/python" - \
    "${target}/config/espelho-zap/config.toml" "${expected}" <<'PY'
from pathlib import Path
import sys
import tomllib
with Path(sys.argv[1]).open("rb") as handle:
    worker = tomllib.load(handle)["worker"]
assert worker["profile_id"] == sys.argv[2]
assert worker["maximum_spool_bytes"] == 1073741824
PY
}

HERMES_TARGET="${SMOKE_ROOT}/hermes"
runtime_install "${HERMES_TARGET}" install hermes "" 0
HERMES_STAGE="${HERMES_TARGET}/data/espelho-zap/runtime-staging/hermes/espelho-zap-portable"
[[ -f "${HERMES_STAGE}/__init__.py" && -f "${HERMES_STAGE}/plugin.yaml" ]]
grep -Fxq 'runtime=hermes' "${HERMES_STAGE}/.espelho-zap-runtime-managed"
grep -Fxq 'state=prepared' "${HERMES_STAGE}/.espelho-zap-runtime-managed"
[[ ! -e "${HERMES_TARGET}/home/.hermes" ]]
validate_env_template \
  "${HERMES_STAGE}/espelho-zap.env.example" \
  "${HERMES_TARGET}/data/espelho-zap/venv/bin/espelho-zap" \
  "${HERMES_TARGET}/config/espelho-zap/config.toml" \
  "smoke-profile" \
  "${HERMES_TARGET}/state/espelho-zap/capture-health.json" \
  ""
validate_media_config "${HERMES_TARGET}"
validate_worker_profile "${HERMES_TARGET}" smoke-profile
validate_hook_health "${HERMES_TARGET}"
grep -Fq 'media capture: disabled/fail-closed' \
  "${HERMES_TARGET}/install.log"
python3 -I -S -c '
import importlib.util
from pathlib import Path
import sys
path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("espelho_zap_portable_plugin", path)
if spec is None or spec.loader is None:
    raise SystemExit(1)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
raise SystemExit(0 if callable(getattr(module, "register", None)) else 1)
' "${HERMES_STAGE}/__init__.py"

# Hermes lacks an official gateway-runtime hook inspection. Automated enablement
# therefore fails closed without touching discovery/config or deleting the CLI.
if runtime_install "${HERMES_TARGET}" upgrade hermes "" 1; then
  printf '%s\n' 'Hermes activation unexpectedly bypassed the human load-canary gate' >&2
  exit 1
fi
grep -Fq 'activation is fail-closed' "${HERMES_TARGET}/upgrade.log"
[[ -x "${HERMES_TARGET}/data/espelho-zap/venv/bin/espelho-zap" ]]
[[ -f "${HERMES_STAGE}/__init__.py" && ! -e "${HERMES_TARGET}/home/.hermes" ]]
runtime_uninstall "${HERMES_TARGET}" hermes ""
[[ ! -e "${HERMES_STAGE}" ]]
[[ -f "${HERMES_TARGET}/config/espelho-zap/config.toml" ]]

OPENCLAW_TARGET="${SMOKE_ROOT}/openclaw"
OPENCLAW_HOME="${OPENCLAW_TARGET}/explicit-openclaw-home"
OPENCLAW_MEDIA_A="${OPENCLAW_TARGET}/approved-media-a"
OPENCLAW_MEDIA_B="${OPENCLAW_TARGET}/approved-media-b"
mkdir -p -- "${OPENCLAW_MEDIA_A}" "${OPENCLAW_MEDIA_B}" \
  "${OPENCLAW_HOME}/state" "${OPENCLAW_TARGET}/config/systemd/user"
printf '%s\n' 'synthetic-media-a' >"${OPENCLAW_MEDIA_A}/smoke-media.bin"
printf '%s\n' 'synthetic-media-b' >"${OPENCLAW_MEDIA_B}/smoke-media.bin"
printf '%s\n' '{"plugins":{"allow":["existing-plugin"],"deny":["espelho-zap-portable"]},"sentinel":{"keep":true}}' \
  >"${OPENCLAW_HOME}/openclaw.json"
printf '%s\n' 'pre-existing service bytes' >"${OPENCLAW_TARGET}/config/systemd/user/espelho-zap@.service"
printf '%s\n' 'pre-existing timer bytes' >"${OPENCLAW_TARGET}/config/systemd/user/espelho-zap@.timer"
cp -- "${OPENCLAW_TARGET}/config/systemd/user/espelho-zap@.service" "${OPENCLAW_TARGET}/original.service"
cp -- "${OPENCLAW_TARGET}/config/systemd/user/espelho-zap@.timer" "${OPENCLAW_TARGET}/original.timer"
python3 - "${OPENCLAW_HOME}/state/openclaw.sqlite" <<'PY'
import sqlite3
import sys
with sqlite3.connect(sys.argv[1]) as db:
    db.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
    db.execute("INSERT INTO sentinel VALUES ('keep-me')")
    db.commit()
PY
cp -- "${OPENCLAW_HOME}/openclaw.json" "${OPENCLAW_TARGET}/openclaw.before.json"
runtime_install "${OPENCLAW_TARGET}" install openclaw "${OPENCLAW_HOME}" 0 \
  "${OPENCLAW_MEDIA_A}" "${OPENCLAW_MEDIA_B}"
OPENCLAW_PLUGIN="${OPENCLAW_HOME}/extensions/espelho-zap-portable"
OPENCLAW_STAGE="${OPENCLAW_TARGET}/data/espelho-zap/runtime-staging/openclaw/espelho-zap-portable"
[[ -f "${OPENCLAW_STAGE}/openclaw.plugin.json" ]]
[[ -f "${OPENCLAW_STAGE}/package.json" && -f "${OPENCLAW_STAGE}/dist/index.js" ]]
grep -Fxq 'runtime=openclaw' "${OPENCLAW_STAGE}/.espelho-zap-runtime-managed"
grep -Fxq 'state=prepared' "${OPENCLAW_STAGE}/.espelho-zap-runtime-managed"
node --check "${OPENCLAW_STAGE}/dist/index.js" >/dev/null
validate_env_template \
  "${OPENCLAW_STAGE}/espelho-zap.env.example" \
  "${OPENCLAW_TARGET}/data/espelho-zap/venv/bin/espelho-zap" \
  "${OPENCLAW_TARGET}/config/espelho-zap/config.toml" \
  "smoke-profile" \
  "${OPENCLAW_TARGET}/state/espelho-zap/capture-health.json" \
  "${OPENCLAW_MEDIA_A}:${OPENCLAW_MEDIA_B}"
validate_media_config "${OPENCLAW_TARGET}" "${OPENCLAW_MEDIA_A}" "${OPENCLAW_MEDIA_B}"
validate_worker_profile "${OPENCLAW_TARGET}" smoke-profile
validate_hook_health "${OPENCLAW_TARGET}"
cmp -s -- "${OPENCLAW_TARGET}/openclaw.before.json" "${OPENCLAW_HOME}/openclaw.json"
[[ ! -e "${OPENCLAW_HOME}/extensions" ]]

# Omission preserves approved roots; clearing them requires the explicit flag.
runtime_install "${OPENCLAW_TARGET}" upgrade openclaw "${OPENCLAW_HOME}" 0 __CLEAR__
validate_media_config "${OPENCLAW_TARGET}"
runtime_install "${OPENCLAW_TARGET}" upgrade openclaw "${OPENCLAW_HOME}" 0 \
  "${OPENCLAW_MEDIA_A}" "${OPENCLAW_MEDIA_B}"
validate_media_config "${OPENCLAW_TARGET}" "${OPENCLAW_MEDIA_A}" "${OPENCLAW_MEDIA_B}"

# A post-install failure must restore the exact prior systemd unit files.
UNIT_DIR="${OPENCLAW_TARGET}/config/systemd/user"
UNIT_SNAPSHOT="${OPENCLAW_TARGET}/unit-snapshot"
mkdir -p -- "${UNIT_SNAPSHOT}"
cp -- "${UNIT_DIR}/espelho-zap@.service" "${UNIT_SNAPSHOT}/espelho-zap@.service"
cp -- "${UNIT_DIR}/espelho-zap@.timer" "${UNIT_SNAPSHOT}/espelho-zap@.timer"
cp -- "${OPENCLAW_TARGET}/config/espelho-zap/config.toml" "${OPENCLAW_TARGET}/config.before-failure.toml"
if RUNTIME_CANARY_FAIL=1 runtime_install \
  "${OPENCLAW_TARGET}" upgrade openclaw "${OPENCLAW_HOME}" 1; then
  printf '%s\n' 'induced post-install failure unexpectedly succeeded' >&2
  exit 1
fi
cmp -s -- "${UNIT_SNAPSHOT}/espelho-zap@.service" "${UNIT_DIR}/espelho-zap@.service"
cmp -s -- "${UNIT_SNAPSHOT}/espelho-zap@.timer" "${UNIT_DIR}/espelho-zap@.timer"
cmp -s -- "${OPENCLAW_TARGET}/openclaw.before.json" "${OPENCLAW_HOME}/openclaw.json"
cmp -s -- "${OPENCLAW_TARGET}/config.before-failure.toml" \
  "${OPENCLAW_TARGET}/config/espelho-zap/config.toml"
python3 - "${OPENCLAW_HOME}/state/openclaw.sqlite" <<'PY'
import sqlite3
import sys
with sqlite3.connect(sys.argv[1]) as db:
    assert db.execute("SELECT value FROM sentinel").fetchone() == ("keep-me",)
    assert db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='installed_plugin_index'"
    ).fetchone() is None
PY
[[ ! -e "${OPENCLAW_PLUGIN}" ]]
[[ -f "${OPENCLAW_STAGE}/dist/index.js" ]]
[[ ! -e "${OPENCLAW_TARGET}/data/espelho-zap/runtime-activation.state" ]]

# A successful upgrade retains the previous plugin outside the discovery root.
runtime_install "${OPENCLAW_TARGET}" upgrade openclaw "${OPENCLAW_HOME}" 1
[[ -f "${OPENCLAW_PLUGIN}/dist/index.js" ]]
python3 - "${OPENCLAW_HOME}/openclaw.json" \
  "${OPENCLAW_MEDIA_A}:${OPENCLAW_MEDIA_B}" <<'PY'
import json
from pathlib import Path
import sys

value = json.loads(Path(sys.argv[1]).read_text())
plugins = value["plugins"]
assert plugins["allow"] == ["existing-plugin", "espelho-zap-portable"]
assert "espelho-zap-portable" not in plugins["deny"]
assert plugins["entries"]["espelho-zap-portable"]["env"]["ESPELHO_ZAP_MEDIA_ROOTS"] == sys.argv[2]
assert plugins["entries"]["espelho-zap-portable"]["env"]["ESPELHO_ZAP_SOURCE_PROFILE_ID"] == "smoke-profile"
assert plugins["entries"]["espelho-zap-portable"]["env"]["ESPELHO_ZAP_HOOK_HEALTH_FILE"].endswith("/state/espelho-zap/capture-health.json")
assert plugins["entries"]["espelho-zap-portable"]["hooks"]["allowConversationAccess"] is True
assert value["channels"]["whatsapp"]["pluginHooks"]["messageReceived"] is True
assert value["sentinel"]["keep"] is True
PY
grep -Fq 'runtime plugin: installed, enabled, and runtime-inspect verified for openclaw' \
  "${OPENCLAW_TARGET}/upgrade.log"
[[ "$(find "${OPENCLAW_HOME}/extensions" -mindepth 1 -maxdepth 1 -type d | wc -l)" -eq 1 ]]
[[ -f "${OPENCLAW_TARGET}/data/espelho-zap/runtime-activation.state" ]]

# The default uninstall refuses before deleting the CLI when an active runtime
# record exists. Explicit matching selection performs the full transaction.
if runtime_uninstall "${OPENCLAW_TARGET}" none ""; then
  printf '%s\n' 'default uninstall unexpectedly removed an active integration' >&2
  exit 1
fi
grep -Fq 'uninstall requires explicit matching --runtime' "${OPENCLAW_TARGET}/uninstall.log"
[[ -x "${OPENCLAW_TARGET}/data/espelho-zap/venv/bin/espelho-zap" ]]
[[ -f "${OPENCLAW_PLUGIN}/dist/index.js" ]]
runtime_uninstall "${OPENCLAW_TARGET}" openclaw "${OPENCLAW_HOME}"
[[ ! -e "${OPENCLAW_PLUGIN}" ]]
[[ ! -e "${OPENCLAW_STAGE}" ]]
[[ ! -e "${OPENCLAW_TARGET}/data/espelho-zap/venv" ]]
[[ ! -e "${OPENCLAW_TARGET}/data/espelho-zap/runtime-activation.state" ]]
cmp -s -- "${OPENCLAW_TARGET}/original.service" "${UNIT_DIR}/espelho-zap@.service"
cmp -s -- "${OPENCLAW_TARGET}/original.timer" "${UNIT_DIR}/espelho-zap@.timer"
[[ -f "${OPENCLAW_TARGET}/config/espelho-zap/config.toml" ]]
validate_hook_health "${OPENCLAW_TARGET}"
python3 - "${OPENCLAW_HOME}/openclaw.json" <<'PY'
import json
from pathlib import Path
import sys
value = json.loads(Path(sys.argv[1]).read_text())
assert value["sentinel"]["keep"] is True
assert value["plugins"]["allow"] == ["existing-plugin"]
assert value["plugins"]["deny"] == ["espelho-zap-portable"]
assert "espelho-zap-portable" not in value["plugins"].get("entries", {})
assert "messageReceived" not in value.get("channels", {}).get("whatsapp", {}).get("pluginHooks", {})
PY

grep -Fq 'openclaw plugins install' "${RUNTIME_CLI_LOG}"
grep -Fq 'openclaw plugins inspect espelho-zap-portable --runtime --json' "${RUNTIME_CLI_LOG}"
grep -Fq 'openclaw gateway restart' "${RUNTIME_CLI_LOG}"
grep -Fq 'openclaw plugins uninstall espelho-zap-portable --force' "${RUNTIME_CLI_LOG}"
grep -Fq 'openclaw config unset channels.whatsapp.pluginHooks.messageReceived' "${RUNTIME_CLI_LOG}"

if grep -Eq '(^| )(enable|start|restart)( |$)' "${SYSTEMCTL_LOG}"; then
  printf '%s\n' 'installer attempted runtime activation or restart' >&2
  exit 1
fi

printf '%s\n' 'runtime target smoke: ok'
