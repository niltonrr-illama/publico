#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Transactional installer. The portable worker remains per-user. The optional
# Hermes observer is a root-only, prepared system service: it is installed
# disabled/inactive and this script never starts, enables, or restarts it.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"

ACTION="install"
DRY_RUN=0
SOURCE="${PROJECT_DIR}"
SOURCE_EXPLICIT=0
PYTHON_BIN="${PYTHON_BIN:-python3}"
MIN_FREE_BYTES="${ESPELHO_ZAP_MIN_FREE_BYTES:-268435456}"
RUNTIME="none"
RUNTIME_HOME=""
RUNTIME_HOME_EXPLICIT=0
ENABLE_RUNTIME=0
SOURCE_PROFILE_ID=""
SOURCE_PROFILE_ID_EXPLICIT=0
MEDIA_ROOTS_SPECIFIED=0
CLEAR_MEDIA_ROOTS=0
declare -a MEDIA_ROOTS=()
PREPARE_HERMES_OBSERVER=0
HERMES_OBSERVER_ARGS_SEEN=0
HERMES_OBSERVER_PROFILE=""
HERMES_BRIDGE_CONFIG_SOURCE=""
HERMES_BRIDGE_JS=""
HERMES_HUMAN_OUTBOUND_TOKEN_FILE=""
HERMES_HUMAN_OUTBOUND_MEDIA_ROOT=""
HERMES_SERVICE_USER=""
HERMES_SERVICE_GROUP=""
HERMES_SERVICE_UID=""
HERMES_SERVICE_GID=""

DATA_BASE="${XDG_DATA_HOME:-${HOME}/.local/share}"
CONFIG_BASE="${XDG_CONFIG_HOME:-${HOME}/.config}"
STATE_BASE="${XDG_STATE_HOME:-${HOME}/.local/state}"
APP_ROOT="${ESPELHO_ZAP_APP_ROOT:-${DATA_BASE}/espelho-zap}"
CONFIG_DIR="${ESPELHO_ZAP_CONFIG_DIR:-${CONFIG_BASE}/espelho-zap}"
STATE_DIR="${ESPELHO_ZAP_STATE_DIR:-${STATE_BASE}/espelho-zap}"
CONFIG_FILE="${CONFIG_DIR}/config.toml"
HOOK_HEALTH_FILE="${STATE_DIR}/capture-health.json"
INSTALL_RECORD="${APP_ROOT}/install.state"
ACTIVATION_RECORD="${APP_ROOT}/runtime-activation.state"
LOCK_DIR="${XDG_RUNTIME_DIR:-/tmp}/espelho-zap-portable-installer-${UID}"
LOCK_FILE="${LOCK_DIR}/transaction.lock"
LOCK_FD=""
HERMES_OBSERVER_LOCK_DIR="/tmp/espelho-zap-hermes-observer-installer"
VENV="${APP_ROOT}/venv"
BACKUP_DIR="${APP_ROOT}/backups"
UNIT_DIR="${CONFIG_BASE}/systemd/user"
SERVICE_UNIT="${UNIT_DIR}/espelho-zap@.service"
TIMER_UNIT="${UNIT_DIR}/espelho-zap@.timer"

SKILL_ROOT="${ESPELHO_ZAP_SKILL_ROOT:-${HOME}/.agents/skills}"
SKILL_SOURCE="${PROJECT_DIR}/skills/espelho-zap-portable"
SKILL_DEST="${SKILL_ROOT}/espelho-zap-portable"
SKILL_MARKER="${SKILL_DEST}/.espelho-zap-managed"
MANAGED_MARKER="managed_by=espelho-zap-portable-installer-v1"

HERMES_DIRECT_BRIDGE_SOURCE="${PROJECT_DIR}/integrations/hermes/direct_bridge"
HERMES_OBSERVER_BASE="${ESPELHO_ZAP_HERMES_OBSERVER_ROOT:-/opt/espelho-zap/hermes-observer}"
HERMES_SYSTEM_UNIT_DIR="${ESPELHO_ZAP_HERMES_SYSTEMD_DIR:-/etc/systemd/system}"
HERMES_OBSERVER_ROOT=""
HERMES_OBSERVER_MARKER=""
HERMES_OBSERVER_UNIT=""
HERMES_OBSERVER_UNIT_NAME=""
HERMES_OBSERVER_CANDIDATE=""
HERMES_OBSERVER_BACKUP_DIR=""
HERMES_OBSERVER_BACKUP=""
HERMES_OBSERVER_UNIT_CANDIDATE_DIR=""
HERMES_OBSERVER_UNIT_CANDIDATE=""
HERMES_OBSERVER_UNIT_BACKUP=""
HERMES_OBSERVER_ROOT_HAD=0
HERMES_OBSERVER_UNIT_HAD=0
HERMES_OBSERVER_BASE_HAD=0
HERMES_OBSERVER_ACTIVATED=0
HERMES_OBSERVER_INSTALLED=0
HERMES_OBSERVER_TRANSACTION_ACTIVE=0
HERMES_OBSERVER_UNIT_LOAD_STATE=""
HERMES_OBSERVER_UNIT_ACTIVE_STATE=""
HERMES_OBSERVER_UNIT_ENABLED_STATE=""

HERMES_PLUGIN_SOURCE="${PROJECT_DIR}/integrations/hermes"
OPENCLAW_PLUGIN_SOURCE="${PROJECT_DIR}/integrations/openclaw"
RUNTIME_PLUGIN_SOURCE=""
RUNTIME_PLUGIN_ROOT=""
RUNTIME_PLUGIN_DEST=""
RUNTIME_PLUGIN_MARKER=""
RUNTIME_ENV_TEMPLATE=""
RUNTIME_STAGING_ROOT=""
RUNTIME_STAGING_DEST=""
RUNTIME_STAGING_MARKER=""
RUNTIME_STAGING_ENV_TEMPLATE=""
RUNTIME_STAGING_BACKUP_ROOT=""
RUNTIME_BACKUP_ROOT=""
RUNTIME_ACTIVATION_BACKUP_ROOT=""
RUNTIME_CLI=""
RUNTIME_ACTIVATION_BACKUP_DIR=""
HERMES_CONFIG_HAD=0
HERMES_ENV_HAD=0
OPENCLAW_CONFIG_HAD=0
OPENCLAW_STATE_DB_HAD=0
OPENCLAW_LEGACY_INDEX_HAD=0
RUNTIME_GATEWAY_STOP_ATTEMPTED=0
RUNTIME_GATEWAY_WAS_RUNNING=0
RUNTIME_DEACTIVATION=0
ACTIVE_RUNTIME_FOUND=0
ACTIVE_RUNTIME=""
ACTIVE_RUNTIME_HOME=""
ACTIVE_OPENCLAW_HOOK_CHANGED=0
ACTIVE_ACTIVATION_BASELINE_DIR=""
ACTIVATION_BASELINE_DIR=""
OPENCLAW_HOOK_CHANGED=0
ACTIVE_PLUGIN_HAD=0
ACTIVE_PLUGIN_BACKUP=""
UNIT_BACKUP_DIR=""
SERVICE_UNIT_HAD=0
TIMER_UNIT_HAD=0
TRANSACTION_BACKUP_DIR=""
CONFIG_HAD=0
LEDGER_HAD=0
HOOK_HEALTH_HAD=0
INSTALL_RECORD_HAD=0
ACTIVATION_RECORD_HAD=0
LEDGER_PATH=""
WORKER_STATE_CAPTURED=0
RUNTIME_STATE_SNAPSHOTTED=0
ACTIVE_PLUGIN_STAGED=0
TRANSACTION_DATA_SNAPSHOTTED=0
INSTALL_RECORD_FOUND=0
ORIGINAL_UNIT_BACKUP_DIR=""
ORIGINAL_SERVICE_UNIT_HAD=0
ORIGINAL_TIMER_UNIT_HAD=0

usage() {
  cat <<'EOF'
Usage: install.sh [install|upgrade|preflight|uninstall] [options]

Options:
  --source PATH   Prebuilt wheel or local project directory
  --python PATH   Python 3.11+ executable (default: python3)
  --runtime NAME  Plugin target: hermes, openclaw, or none (default: none)
  --runtime-home PATH
                  Runtime state root. Hermes defaults to ~/.hermes;
                  OpenClaw requires an explicit root and uses PATH/extensions.
  --source-profile ID
                  Stable source profile identity. Defaults to worker.profile_id
                  from an existing config, or default on a clean install.
  --media-root PATH
                  Approved existing media directory. Repeat for multiple roots.
                  Omission preserves existing approved roots; on a clean install
                  media capture stays disabled and fail-closed.
  --clear-media-roots
                  Explicitly remove every approved media root (mutually exclusive
                  with --media-root).
  --enable-runtime
                  Explicit opt-in: persist plugin env/config, enable the plugin,
                  restart the selected managed gateway, and run a load canary.
  --prepare-hermes-observer
                  Independent root-only transaction for one reviewed external
                  Hermes bridge. It never installs the per-user CLI/plugin,
                  skill or worker units and never starts/restarts the observer.
  --hermes-observer-profile ID
                  Safe systemd instance/profile ID (required by the opt-in).
  --hermes-bridge-config PATH
                  Reviewed direct-bridge TOML used as the render source.
  --hermes-bridge-js PATH
                  Absolute guarded bridge.js deployment path.
  --hermes-human-outbound-token-file PATH
                  Existing private token file for the loopback endpoint.
  --hermes-human-outbound-media-root PATH
                  Existing private managed outbound-media directory.
  --hermes-service-user NAME
                  Existing unprivileged observer service account.
  --hermes-service-group NAME
                  Existing unprivileged observer service group.
  --dry-run       Check and print mutations without executing them
  -h, --help      Show this help

Environment:
  ESPELHO_ZAP_MIN_FREE_BYTES  Install/doctor free-space floor (default 256 MiB)
  ESPELHO_ZAP_SKILL_ROOT      AgentSkills root (default ~/.agents/skills)
  ESPELHO_ZAP_HERMES_OBSERVER_ROOT
                              System observer root (default /opt/espelho-zap/hermes-observer)
  ESPELHO_ZAP_HERMES_SYSTEMD_DIR
                              System unit directory (default /etc/systemd/system)

Exit codes: 0 success; 2 usage/preflight failure; 5 lifecycle failure.
The user timer is installed disabled. Enable a profile explicitly with:
  systemctl --user enable --now espelho-zap@default.timer
EOF
}

log() { printf '%s\n' "$*"; }
die() { log "ERROR: $1" >&2; exit "${2:-5}"; }

quote_command() {
  printf 'DRY-RUN:'
  printf ' %q' "$@"
  printf '\n'
}

run() {
  if (( DRY_RUN )); then quote_command "$@"; else "$@"; fi
}

parse_args() {
  if (($#)) && [[ "$1" != -* ]]; then ACTION="$1"; shift; fi
  case "${ACTION}" in install|upgrade|preflight|uninstall) ;; *) usage >&2; exit 2 ;; esac
  while (($#)); do
    case "$1" in
      --source)
        (($# >= 2)) || die "--source needs a value" 2
        SOURCE="$2"
        SOURCE_EXPLICIT=1
        shift 2
        ;;
      --python) (($# >= 2)) || die "--python needs a value" 2; PYTHON_BIN="$2"; shift 2 ;;
      --runtime) (($# >= 2)) || die "--runtime needs a value" 2; RUNTIME="$2"; shift 2 ;;
      --runtime-home)
        (($# >= 2)) || die "--runtime-home needs a value" 2
        RUNTIME_HOME="$2"
        RUNTIME_HOME_EXPLICIT=1
        shift 2
        ;;
      --source-profile)
        (($# >= 2)) || die "--source-profile needs a value" 2
        SOURCE_PROFILE_ID="$2"
        SOURCE_PROFILE_ID_EXPLICIT=1
        shift 2
        ;;
      --media-root)
        (($# >= 2)) || die "--media-root needs a value" 2
        MEDIA_ROOTS_SPECIFIED=1
        MEDIA_ROOTS+=("$2")
        shift 2
        ;;
      --clear-media-roots) CLEAR_MEDIA_ROOTS=1; shift ;;
      --enable-runtime) ENABLE_RUNTIME=1; shift ;;
      --prepare-hermes-observer) PREPARE_HERMES_OBSERVER=1; shift ;;
      --hermes-observer-profile)
        (($# >= 2)) || die "--hermes-observer-profile needs a value" 2
        HERMES_OBSERVER_ARGS_SEEN=1
        HERMES_OBSERVER_PROFILE="$2"
        shift 2
        ;;
      --hermes-bridge-config)
        (($# >= 2)) || die "--hermes-bridge-config needs a value" 2
        HERMES_OBSERVER_ARGS_SEEN=1
        HERMES_BRIDGE_CONFIG_SOURCE="$2"
        shift 2
        ;;
      --hermes-bridge-js)
        (($# >= 2)) || die "--hermes-bridge-js needs a value" 2
        HERMES_OBSERVER_ARGS_SEEN=1
        HERMES_BRIDGE_JS="$2"
        shift 2
        ;;
      --hermes-human-outbound-token-file)
        (($# >= 2)) || die "--hermes-human-outbound-token-file needs a value" 2
        HERMES_OBSERVER_ARGS_SEEN=1
        HERMES_HUMAN_OUTBOUND_TOKEN_FILE="$2"
        shift 2
        ;;
      --hermes-human-outbound-media-root)
        (($# >= 2)) || die "--hermes-human-outbound-media-root needs a value" 2
        HERMES_OBSERVER_ARGS_SEEN=1
        HERMES_HUMAN_OUTBOUND_MEDIA_ROOT="$2"
        shift 2
        ;;
      --hermes-service-user)
        (($# >= 2)) || die "--hermes-service-user needs a value" 2
        HERMES_OBSERVER_ARGS_SEEN=1
        HERMES_SERVICE_USER="$2"
        shift 2
        ;;
      --hermes-service-group)
        (($# >= 2)) || die "--hermes-service-group needs a value" 2
        HERMES_OBSERVER_ARGS_SEEN=1
        HERMES_SERVICE_GROUP="$2"
        shift 2
        ;;
      --dry-run) DRY_RUN=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die "unknown option: $1" 2 ;;
    esac
  done
  (( ! CLEAR_MEDIA_ROOTS || ! MEDIA_ROOTS_SPECIFIED )) \
    || die "--clear-media-roots and --media-root are mutually exclusive" 2
  (( PREPARE_HERMES_OBSERVER || ! HERMES_OBSERVER_ARGS_SEEN )) \
    || die "Hermes observer parameters require --prepare-hermes-observer" 2
}

canonical() { realpath -m -- "$1"; }

contained_by() {
  local root target
  root="$(canonical "$1")"
  target="$(canonical "$2")"
  [[ "${target}" == "${root}" || "${target}" == "${root}/"* ]]
}

reject_symlink() {
  [[ ! -L "$1" ]] || die "$2 must not be a symlink" 2
}

root_owned_not_writable_by_others() {
  local mode path="$1"
  [[ "$(stat -c '%u:%g' -- "${path}")" == "0:0" ]] || return 1
  mode="$(stat -c '%a' -- "${path}")" || return 1
  (( (8#${mode} & 8#022) == 0 ))
}

runtime_marker_matches() {
  local marker="$1"
  [[ -f "${marker}" ]] \
    && grep -Fxq -- "${MANAGED_MARKER}" "${marker}" \
    && grep -Fxq -- "runtime=${RUNTIME}" "${marker}"
}

write_runtime_marker() {
  local destination="$1" state="$2"
  {
    printf '%s\n' "${MANAGED_MARKER}"
    printf 'runtime=%s\n' "${RUNTIME}"
    printf 'state=%s\n' "${state}"
  } >"${destination}" || return 1
  chmod 0600 -- "${destination}" || return 1
}

load_activation_record() {
  local key value managed="" runtime="" runtime_home="" hook_changed="" baseline=""
  ACTIVE_RUNTIME_FOUND=0
  [[ -e "${ACTIVATION_RECORD}" ]] || return 0
  [[ -f "${ACTIVATION_RECORD}" && ! -L "${ACTIVATION_RECORD}" ]] \
    || die "runtime activation record is not a regular non-symlink file" 2
  while IFS='=' read -r key value; do
    case "${key}" in
      managed_by) managed="${value}" ;;
      runtime) runtime="${value}" ;;
      runtime_home) runtime_home="${value}" ;;
      openclaw_hook_changed) hook_changed="${value}" ;;
      activation_baseline_dir) baseline="${value}" ;;
      '') ;;
      *) die "runtime activation record contains an unknown field" 2 ;;
    esac
  done <"${ACTIVATION_RECORD}"
  [[ "managed_by=${managed}" == "${MANAGED_MARKER}" ]] \
    || die "runtime activation record is not installer-managed" 2
  [[ "${runtime}" == "hermes" || "${runtime}" == "openclaw" ]] \
    || die "runtime activation record has an invalid runtime" 2
  [[ "${runtime_home}" == /* && "${hook_changed}" =~ ^[01]$ && "${baseline}" == /* ]] \
    || die "runtime activation record is incomplete" 2
  ACTIVE_RUNTIME_FOUND=1
  ACTIVE_RUNTIME="${runtime}"
  ACTIVE_RUNTIME_HOME="$(canonical "${runtime_home}")"
  ACTIVE_OPENCLAW_HOOK_CHANGED="${hook_changed}"
  ACTIVE_ACTIVATION_BASELINE_DIR="$(canonical "${baseline}")"
}

activation_selection_matches() {
  (( ACTIVE_RUNTIME_FOUND )) \
    && [[ "${RUNTIME}" == "${ACTIVE_RUNTIME}" ]] \
    && [[ "${RUNTIME_HOME}" == "${ACTIVE_RUNTIME_HOME}" ]]
}

write_activation_record() {
  local temporary="${ACTIVATION_RECORD}.tmp.$$"
  {
    printf '%s\n' "${MANAGED_MARKER}"
    printf 'runtime=%s\n' "${RUNTIME}"
    printf 'runtime_home=%s\n' "${RUNTIME_HOME}"
    printf 'openclaw_hook_changed=%s\n' "${OPENCLAW_HOOK_CHANGED}"
    printf 'activation_baseline_dir=%s\n' "${ACTIVATION_BASELINE_DIR}"
  } >"${temporary}" || return 1
  chmod 0600 -- "${temporary}" || return 1
  mv -f -- "${temporary}" "${ACTIVATION_RECORD}" || return 1
}

load_install_record() {
  local key value managed="" backup="" service_had="" timer_had=""
  INSTALL_RECORD_FOUND=0
  [[ -e "${INSTALL_RECORD}" ]] || return 0
  [[ -f "${INSTALL_RECORD}" && ! -L "${INSTALL_RECORD}" ]] \
    || die "install ownership record is not a regular non-symlink file" 2
  while IFS='=' read -r key value; do
    case "${key}" in
      managed_by) managed="${value}" ;;
      original_unit_backup_dir) backup="${value}" ;;
      original_service_unit_had) service_had="${value}" ;;
      original_timer_unit_had) timer_had="${value}" ;;
      '') ;;
      *) die "install ownership record contains an unknown field" 2 ;;
    esac
  done <"${INSTALL_RECORD}"
  [[ "managed_by=${managed}" == "${MANAGED_MARKER}" ]] \
    || die "install ownership record is not installer-managed" 2
  [[ "${backup}" == /* && "${service_had}" =~ ^[01]$ && "${timer_had}" =~ ^[01]$ ]] \
    || die "install ownership record is incomplete" 2
  contained_by "${BACKUP_DIR}" "${backup}" \
    || die "install ownership record points outside the backup directory" 2
  INSTALL_RECORD_FOUND=1
  ORIGINAL_UNIT_BACKUP_DIR="${backup}"
  ORIGINAL_SERVICE_UNIT_HAD="${service_had}"
  ORIGINAL_TIMER_UNIT_HAD="${timer_had}"
}

write_install_record() {
  local temporary="${INSTALL_RECORD}.tmp.$$"
  {
    printf '%s\n' "${MANAGED_MARKER}"
    printf 'original_unit_backup_dir=%s\n' "${ORIGINAL_UNIT_BACKUP_DIR}"
    printf 'original_service_unit_had=%s\n' "${ORIGINAL_SERVICE_UNIT_HAD}"
    printf 'original_timer_unit_had=%s\n' "${ORIGINAL_TIMER_UNIT_HAD}"
  } >"${temporary}" || return 1
  chmod 0600 -- "${temporary}" || return 1
  mv -f -- "${temporary}" "${INSTALL_RECORD}" || return 1
}

acquire_global_lock() {
  command -v flock >/dev/null 2>&1 || die "required command not found: flock" 2
  command -v stat >/dev/null 2>&1 || die "required command not found: stat" 2
  if [[ ! -e "${LOCK_DIR}" ]]; then
    mkdir -m 0700 -- "${LOCK_DIR}" 2>/dev/null || true
  fi
  [[ -d "${LOCK_DIR}" && ! -L "${LOCK_DIR}" ]] \
    || die "installer lock directory is unsafe" 2
  [[ "$(stat -c '%u' -- "${LOCK_DIR}")" == "${UID}" ]] \
    || die "installer lock directory is not owned by the current user" 2
  reject_symlink "${LOCK_FILE}" "installer lock file"
  if [[ ! -e "${LOCK_FILE}" ]]; then
    (set -o noclobber; : >"${LOCK_FILE}") 2>/dev/null || true
  fi
  [[ -f "${LOCK_FILE}" && ! -L "${LOCK_FILE}" ]] \
    || die "installer lock file is unsafe" 2
  chmod 0600 -- "${LOCK_FILE}" || die "installer lock file permissions could not be hardened" 2
  exec {LOCK_FD}<>"${LOCK_FILE}"
  flock -n "${LOCK_FD}" || die "another Espelho Zap install/upgrade/uninstall transaction is active" 5
  log "installer transaction lock: acquired"
}

release_global_lock() {
  [[ -n "${LOCK_FD}" ]] || return 0
  flock -u "${LOCK_FD}" 2>/dev/null || true
  eval "exec ${LOCK_FD}>&-"
  LOCK_FD=""
}

configure_runtime_target() {
  if (( ENABLE_RUNTIME )) && [[ "${ACTION}" == "uninstall" ]]; then
    die "--enable-runtime is valid only for install, upgrade, or preflight" 2
  fi
  case "${RUNTIME}" in
    none)
      (( ! RUNTIME_HOME_EXPLICIT )) \
        || die "--runtime-home is not valid with --runtime none" 2
      (( ! ENABLE_RUNTIME )) \
        || die "--enable-runtime requires --runtime hermes or openclaw" 2
      return 0
      ;;
    hermes)
      if (( ! RUNTIME_HOME_EXPLICIT )); then RUNTIME_HOME="${HOME}/.hermes"; fi
      RUNTIME_PLUGIN_SOURCE="${HERMES_PLUGIN_SOURCE}"
      ;;
    openclaw)
      (( RUNTIME_HOME_EXPLICIT )) \
        || die "--runtime-home is required with --runtime openclaw" 2
      RUNTIME_PLUGIN_SOURCE="${OPENCLAW_PLUGIN_SOURCE}"
      ;;
    *) die "--runtime must be hermes, openclaw, or none" 2 ;;
  esac

  [[ "${RUNTIME_HOME}" == /* ]] || die "--runtime-home must be an absolute path" 2
  reject_symlink "${RUNTIME_HOME}" "runtime home"
  RUNTIME_HOME="$(canonical "${RUNTIME_HOME}")"
  [[ "${RUNTIME_HOME}" != "/" && "${RUNTIME_HOME}" != "$(canonical "${HOME}")" ]] \
    || die "unsafe runtime home" 2

  if [[ "${RUNTIME}" == "hermes" ]]; then
    RUNTIME_PLUGIN_ROOT="${RUNTIME_HOME}/plugins"
  else
    RUNTIME_PLUGIN_ROOT="${RUNTIME_HOME}/extensions"
  fi
  RUNTIME_PLUGIN_DEST="${RUNTIME_PLUGIN_ROOT}/espelho-zap-portable"
  RUNTIME_PLUGIN_MARKER="${RUNTIME_PLUGIN_DEST}/.espelho-zap-runtime-managed"
  RUNTIME_ENV_TEMPLATE="${RUNTIME_PLUGIN_DEST}/espelho-zap.env.example"
  RUNTIME_STAGING_ROOT="${APP_ROOT}/runtime-staging/${RUNTIME}"
  RUNTIME_STAGING_DEST="${RUNTIME_STAGING_ROOT}/espelho-zap-portable"
  RUNTIME_STAGING_MARKER="${RUNTIME_STAGING_DEST}/.espelho-zap-runtime-managed"
  RUNTIME_STAGING_ENV_TEMPLATE="${RUNTIME_STAGING_DEST}/espelho-zap.env.example"
  RUNTIME_STAGING_BACKUP_ROOT="${APP_ROOT}/runtime-staging-backups/${RUNTIME}"
  RUNTIME_BACKUP_ROOT="${RUNTIME_HOME}/.espelho-zap-plugin-backups/${RUNTIME}"
  RUNTIME_ACTIVATION_BACKUP_ROOT="${RUNTIME_HOME}/.espelho-zap-activation-backups/${RUNTIME}"

  contained_by "${RUNTIME_HOME}" "${RUNTIME_PLUGIN_ROOT}" \
    && contained_by "${RUNTIME_PLUGIN_ROOT}" "${RUNTIME_PLUGIN_DEST}" \
    && contained_by "${RUNTIME_HOME}" "${RUNTIME_BACKUP_ROOT}" \
    && contained_by "${RUNTIME_HOME}" "${RUNTIME_ACTIVATION_BACKUP_ROOT}" \
    && contained_by "${APP_ROOT}" "${RUNTIME_STAGING_ROOT}" \
    && contained_by "${RUNTIME_STAGING_ROOT}" "${RUNTIME_STAGING_DEST}" \
    && contained_by "${APP_ROOT}" "${RUNTIME_STAGING_BACKUP_ROOT}" \
    || die "runtime plugin or staging destination escapes its managed root" 2
  reject_symlink "${RUNTIME_PLUGIN_ROOT}" "runtime plugin root"
  reject_symlink "${RUNTIME_PLUGIN_DEST}" "runtime plugin destination"
  reject_symlink "${RUNTIME_BACKUP_ROOT}" "runtime plugin backup root"
  reject_symlink "${RUNTIME_ACTIVATION_BACKUP_ROOT}" "runtime activation backup root"
  reject_symlink "${RUNTIME_STAGING_ROOT}" "runtime staging root"
  reject_symlink "${RUNTIME_STAGING_DEST}" "runtime staging destination"
  reject_symlink "${RUNTIME_STAGING_BACKUP_ROOT}" "runtime staging backup root"
}

set_hermes_observer_target() {
  HERMES_OBSERVER_ROOT="$(canonical "${HERMES_OBSERVER_BASE}/${HERMES_OBSERVER_PROFILE}")"
  HERMES_OBSERVER_MARKER="${HERMES_OBSERVER_ROOT}/.espelho-zap-hermes-observer-managed"
  HERMES_OBSERVER_UNIT_NAME="espelho-zap-hermes-observer@${HERMES_OBSERVER_PROFILE}.service"
  HERMES_OBSERVER_UNIT="$(canonical "${HERMES_SYSTEM_UNIT_DIR}/${HERMES_OBSERVER_UNIT_NAME}")"
}

hermes_observer_root_marker_matches() {
  local root="$1" profile="$2"
  [[ -d "${root}" && ! -L "${root}" ]] \
    && [[ -f "${root}/.espelho-zap-hermes-observer-managed" \
      && ! -L "${root}/.espelho-zap-hermes-observer-managed" ]] \
    && grep -Fxq -- "${MANAGED_MARKER}" "${root}/.espelho-zap-hermes-observer-managed" \
    && grep -Fxq -- "profile=${profile}" "${root}/.espelho-zap-hermes-observer-managed"
}

hermes_observer_unit_marker_matches() {
  local unit="$1" profile="$2"
  [[ -f "${unit}" && ! -L "${unit}" ]] \
    && grep -Fxq -- "# ${MANAGED_MARKER}" "${unit}" \
    && grep -Fxq -- "# profile=${profile}" "${unit}"
}

read_hermes_observer_unit_state() {
  local enabled_status=0
  HERMES_OBSERVER_UNIT_LOAD_STATE="$(
    systemctl show "${HERMES_OBSERVER_UNIT_NAME}" --property=LoadState --value 2>/dev/null
  )" || return 1
  HERMES_OBSERVER_UNIT_ACTIVE_STATE="$(
    systemctl show "${HERMES_OBSERVER_UNIT_NAME}" --property=ActiveState --value 2>/dev/null
  )" || return 1
  HERMES_OBSERVER_UNIT_ENABLED_STATE="$(
    systemctl is-enabled "${HERMES_OBSERVER_UNIT_NAME}" 2>/dev/null
  )" || enabled_status=$?
  if (( enabled_status )) && [[ -z "${HERMES_OBSERVER_UNIT_ENABLED_STATE}" ]]; then
    return 1
  fi
  [[ -n "${HERMES_OBSERVER_UNIT_LOAD_STATE}" \
    && -n "${HERMES_OBSERVER_UNIT_ACTIVE_STATE}" \
    && -n "${HERMES_OBSERVER_UNIT_ENABLED_STATE}" ]] || return 1
}

validate_hermes_observer_managed_directories() {
  "${PYTHON_BIN}" - "${HERMES_BRIDGE_CONFIG_SOURCE}" \
    "${HERMES_HUMAN_OUTBOUND_MEDIA_ROOT}" "${HERMES_SERVICE_UID}" \
    "${HERMES_SERVICE_GID}" <<'PY'
from pathlib import Path
import stat
import sys
import tomllib

config_path, media_root = map(Path, sys.argv[1:3])
service_uid, service_gid = map(int, sys.argv[3:5])
with config_path.open("rb") as handle:
    bridge = tomllib.load(handle).get("bridge")
if not isinstance(bridge, dict):
    raise SystemExit("direct bridge configuration is missing [bridge]")
required = ("node", "session_dir", "spool_file", "cache_root", "lock_file")
if any(not isinstance(bridge.get(key), str) for key in required):
    raise SystemExit("direct bridge managed directory fields are incomplete")
for key in required:
    value = bridge[key]
    if (
        not Path(value).is_absolute()
        or any(character.isspace() for character in value)
        or "%" in value
        or "@" in value
    ):
        raise SystemExit(f"unsafe direct bridge path: {key}")
spool_file = Path(bridge["spool_file"])
cache_root = Path(bridge["cache_root"])
lock_file = Path(bridge["lock_file"])
directories = sorted({
    spool_file.parent,
    cache_root,
    cache_root / "images",
    cache_root / "documents",
    cache_root / "audio",
    lock_file.parent,
    media_root,
}, key=str)
for path in directories:
    if (
        not path.is_absolute()
        or any(character.isspace() for character in str(path))
        or "%" in str(path)
        or "@" in str(path)
    ):
        raise SystemExit(f"managed directory path must be absolute without whitespace: {path}")
    cursor = path
    while True:
        try:
            details = cursor.lstat()
        except FileNotFoundError:
            raise SystemExit(f"managed directory must already exist: {path}")
        if stat.S_ISLNK(details.st_mode):
            raise SystemExit(f"managed directory path must not contain symlinks: {path}")
        if cursor == cursor.parent:
            break
        cursor = cursor.parent
    details = path.lstat()
    if (
        not stat.S_ISDIR(details.st_mode)
        or stat.S_IMODE(details.st_mode) != 0o700
        or details.st_uid != service_uid
        or details.st_gid != service_gid
    ):
        raise SystemExit(
            f"managed directory must be service-owned with mode 0700: {path}"
        )
PY
}

configure_hermes_observer_target() {
  (( PREPARE_HERMES_OBSERVER )) || return 0
  [[ "${RUNTIME}" == "hermes" ]] \
    || die "--prepare-hermes-observer requires --runtime hermes" 2
  (( ! ENABLE_RUNTIME )) \
    || die "the prepared Hermes observer cannot be combined with --enable-runtime" 2
  [[ "${HERMES_OBSERVER_PROFILE}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ ]] \
    || die "--hermes-observer-profile must be a safe 1-64 character systemd instance" 2
  local value option
  for option in HERMES_BRIDGE_CONFIG_SOURCE HERMES_BRIDGE_JS \
    HERMES_HUMAN_OUTBOUND_TOKEN_FILE HERMES_HUMAN_OUTBOUND_MEDIA_ROOT \
    HERMES_SERVICE_USER HERMES_SERVICE_GROUP; do
    value="${!option}"
    [[ -n "${value}" ]] || die "all Hermes observer parameters are required" 2
  done
  [[ "${HERMES_OBSERVER_BASE}" == /* && "${HERMES_SYSTEM_UNIT_DIR}" == /* ]] \
    || die "Hermes observer install roots must be absolute" 2
  [[ "${HERMES_OBSERVER_BASE}" != "/" && "${HERMES_SYSTEM_UNIT_DIR}" != "/" ]] \
    || die "unsafe Hermes observer install root" 2
  reject_symlink "${HERMES_OBSERVER_BASE}" "Hermes observer managed root"
  reject_symlink "${HERMES_SYSTEM_UNIT_DIR}" "Hermes system unit directory"
  (( EUID == 0 )) \
    || die "--prepare-hermes-observer installs a system unit and requires root" 2
  for option in getent id systemctl runuser; do
    command -v "${option}" >/dev/null 2>&1 \
      || die "required Hermes observer command not found: ${option}" 2
  done
  for option in HERMES_BRIDGE_CONFIG_SOURCE HERMES_BRIDGE_JS \
    HERMES_HUMAN_OUTBOUND_TOKEN_FILE HERMES_HUMAN_OUTBOUND_MEDIA_ROOT; do
    value="${!option}"
    [[ "${value}" == /* && "${value}" != *[[:space:]]* ]] \
      || die "Hermes observer paths must be absolute and contain no whitespace" 2
    [[ "${value}" != *"%"* && "${value}" != *"@"* ]] \
      || die "Hermes observer paths must not contain systemd/template metacharacters" 2
    [[ ! -L "${value}" ]] \
      || die "Hermes observer source paths must not be symlinks" 2
  done
  HERMES_BRIDGE_CONFIG_SOURCE="$(canonical "${HERMES_BRIDGE_CONFIG_SOURCE}")"
  HERMES_BRIDGE_JS="$(canonical "${HERMES_BRIDGE_JS}")"
  HERMES_HUMAN_OUTBOUND_TOKEN_FILE="$(canonical "${HERMES_HUMAN_OUTBOUND_TOKEN_FILE}")"
  HERMES_HUMAN_OUTBOUND_MEDIA_ROOT="$(canonical "${HERMES_HUMAN_OUTBOUND_MEDIA_ROOT}")"
  [[ -f "${HERMES_BRIDGE_CONFIG_SOURCE}" && ! -L "${HERMES_BRIDGE_CONFIG_SOURCE}" ]] \
    || die "--hermes-bridge-config must be an existing regular non-symlink file" 2
  [[ "$(stat -c '%a' -- "${HERMES_BRIDGE_CONFIG_SOURCE}")" == "600" ]] \
    || die "Hermes direct-bridge config source must have mode 0600" 2
  [[ -f "${HERMES_BRIDGE_JS}" && ! -L "${HERMES_BRIDGE_JS}" ]] \
    || die "--hermes-bridge-js must be an existing regular non-symlink file" 2
  [[ -f "${HERMES_HUMAN_OUTBOUND_TOKEN_FILE}" && ! -L "${HERMES_HUMAN_OUTBOUND_TOKEN_FILE}" ]] \
    || die "--hermes-human-outbound-token-file must be an existing regular non-symlink file" 2
  [[ -d "${HERMES_HUMAN_OUTBOUND_MEDIA_ROOT}" && ! -L "${HERMES_HUMAN_OUTBOUND_MEDIA_ROOT}" ]] \
    || die "--hermes-human-outbound-media-root must be an existing non-symlink directory" 2
  getent passwd "${HERMES_SERVICE_USER}" >/dev/null \
    || die "Hermes observer service user does not exist" 2
  getent group "${HERMES_SERVICE_GROUP}" >/dev/null \
    || die "Hermes observer service group does not exist" 2
  [[ "$(id -u "${HERMES_SERVICE_USER}")" != "0" ]] \
    && [[ "$(getent group "${HERMES_SERVICE_GROUP}" | awk -F: '{print $3}')" != "0" ]] \
    || die "Hermes observer service user/group must be unprivileged" 2
  HERMES_SERVICE_UID="$(id -u "${HERMES_SERVICE_USER}")"
  HERMES_SERVICE_GID="$(getent group "${HERMES_SERVICE_GROUP}" | awk -F: '{print $3}')"
  [[ "$(stat -c '%u:%g:%a' -- "${HERMES_HUMAN_OUTBOUND_TOKEN_FILE}")" == "${HERMES_SERVICE_UID}:${HERMES_SERVICE_GID}:600" ]] \
    || die "Hermes human-outbound token must be owned by the service user/group with mode 0600" 2
  [[ "$(stat -c '%u:%g:%a' -- "${HERMES_HUMAN_OUTBOUND_MEDIA_ROOT}")" == "${HERMES_SERVICE_UID}:${HERMES_SERVICE_GID}:700" ]] \
    || die "Hermes human-outbound media root must be owned by the service user/group with mode 0700" 2
  validate_hermes_observer_managed_directories \
    || die "Hermes launcher directories failed read-only prevalidation" 2
  set_hermes_observer_target
  contained_by "${HERMES_OBSERVER_BASE}" "${HERMES_OBSERVER_ROOT}" \
    && [[ "$(canonical "$(dirname -- "${HERMES_OBSERVER_UNIT}")")" == "$(canonical "${HERMES_SYSTEM_UNIT_DIR}")" ]] \
    || die "Hermes observer destination escapes its managed root" 2
  if [[ -e "${HERMES_OBSERVER_ROOT}" || -L "${HERMES_OBSERVER_ROOT}" \
    || -e "${HERMES_OBSERVER_UNIT}" || -L "${HERMES_OBSERVER_UNIT}" ]]; then
    hermes_observer_root_marker_matches "${HERMES_OBSERVER_ROOT}" "${HERMES_OBSERVER_PROFILE}" \
      && hermes_observer_unit_marker_matches "${HERMES_OBSERVER_UNIT}" "${HERMES_OBSERVER_PROFILE}" \
      || die "existing Hermes observer artifacts are incomplete or unmanaged" 2
    HERMES_OBSERVER_INSTALLED=1
  else
    HERMES_OBSERVER_INSTALLED=0
  fi
}

check_hermes_observer_competitors() {
  (( PREPARE_HERMES_OBSERVER )) || return 0
  command -v systemctl >/dev/null 2>&1 \
    || { log "ERROR: systemctl is required for the Hermes observer safety gate" >&2; return 1; }
  read_hermes_observer_unit_state \
    || { log "ERROR: Hermes observer unit state could not be read" >&2; return 1; }
  if [[ "${HERMES_OBSERVER_UNIT_ACTIVE_STATE}" != "inactive" ]]; then
    log "ERROR: Hermes observer ActiveState must be inactive (${HERMES_OBSERVER_UNIT_ACTIVE_STATE})" >&2
    return 1
  fi
  case "${HERMES_OBSERVER_UNIT_LOAD_STATE}" in
    loaded|not-found) ;;
    *) log "ERROR: Hermes observer LoadState is unsafe (${HERMES_OBSERVER_UNIT_LOAD_STATE})" >&2; return 1 ;;
  esac
  case "${HERMES_OBSERVER_UNIT_ENABLED_STATE}" in
    disabled|not-found) ;;
    *) log "ERROR: Hermes observer unit is not disabled (${HERMES_OBSERVER_UNIT_ENABLED_STATE})" >&2; return 1 ;;
  esac
  local inventory loaded_units unit_name unit_state unit_files
  loaded_units="$(systemctl list-units --all --type=service --no-legend --plain)" \
    || { log "ERROR: the system service manager is unavailable" >&2; return 1; }
  unit_files="$(systemctl list-unit-files --type=service --no-legend --plain)" \
    || { log "ERROR: system unit inventory could not be read" >&2; return 1; }
  for inventory in "${loaded_units}" "${unit_files}"; do
    while read -r unit_name unit_state _; do
      [[ -n "${unit_name}" ]] || continue
      case "${unit_name}" in
        espelho-zap-hermes-observer.service|espelho-zap-hermes-observer@*.service)
          if [[ "${unit_name}" != "${HERMES_OBSERVER_UNIT_NAME}" ]]; then
            log "ERROR: competing Hermes observer unit found: ${unit_name}" >&2
            return 1
          fi
          ;;
      esac
    done <<<"${inventory}"
  done
  if (( HERMES_OBSERVER_INSTALLED )); then
    hermes_observer_root_marker_matches "${HERMES_OBSERVER_ROOT}" "${HERMES_OBSERVER_PROFILE}" \
      && hermes_observer_unit_marker_matches "${HERMES_OBSERVER_UNIT}" "${HERMES_OBSERVER_PROFILE}" \
      || { log "ERROR: recorded Hermes observer artifacts are missing or no longer installer-managed" >&2; return 1; }
  fi
  if [[ -e "${HERMES_OBSERVER_ROOT}" || -L "${HERMES_OBSERVER_ROOT}" ]]; then
    (( HERMES_OBSERVER_INSTALLED )) \
      && hermes_observer_root_marker_matches "${HERMES_OBSERVER_ROOT}" "${HERMES_OBSERVER_PROFILE}" \
      || { log "ERROR: existing Hermes observer root is not owned by this installer" >&2; return 1; }
  fi
  if [[ -e "${HERMES_OBSERVER_UNIT}" || -L "${HERMES_OBSERVER_UNIT}" ]]; then
    (( HERMES_OBSERVER_INSTALLED )) \
      && hermes_observer_unit_marker_matches "${HERMES_OBSERVER_UNIT}" "${HERMES_OBSERVER_PROFILE}" \
      || { log "ERROR: existing Hermes observer unit is not owned by this installer" >&2; return 1; }
  fi
  if [[ -n "${HERMES_BRIDGE_JS}" ]] && "${PYTHON_BIN}" - "${HERMES_BRIDGE_JS}" <<'PY'
import os
from pathlib import Path
import sys

target = str(Path(sys.argv[1]).resolve(strict=False))
ancestors = {os.getpid()}
cursor = os.getppid()
while cursor > 1 and cursor not in ancestors:
    ancestors.add(cursor)
    try:
        cursor = int(Path(f"/proc/{cursor}/stat").read_text().split()[3])
    except (OSError, ValueError, IndexError):
        break
found = []
for entry in Path("/proc").iterdir():
    if not entry.name.isdigit() or int(entry.name) in ancestors:
        continue
    try:
        args = (entry / "cmdline").read_bytes().split(b"\0")
    except OSError:
        continue
    try:
        cwd = (entry / "cwd").resolve(strict=True)
    except OSError:
        continue
    resolved_args = []
    for arg in args:
        if not arg:
            continue
        candidate = Path(os.fsdecode(arg))
        if not candidate.is_absolute():
            candidate = cwd / candidate
        try:
            resolved_args.append(str(candidate.resolve(strict=False)))
        except OSError:
            continue
    if target in resolved_args:
        found.append(entry.name)
raise SystemExit(0 if found else 1)
PY
  then
    log "ERROR: the selected bridge.js is already running outside this disabled installer flow" >&2
    return 1
  fi
}

resolve_source_profile_id() {
  if (( ! SOURCE_PROFILE_ID_EXPLICIT )); then
    if [[ -f "${CONFIG_FILE}" ]]; then
      SOURCE_PROFILE_ID="$("${PYTHON_BIN}" - "${CONFIG_FILE}" <<'PY'
from pathlib import Path
import sys
import tomllib

with Path(sys.argv[1]).open("rb") as handle:
    value = tomllib.load(handle).get("worker", {}).get("profile_id", "default")
if not isinstance(value, str):
    raise SystemExit(1)
print(value, end="")
PY
)" || die "could not read worker.profile_id from the existing config" 2
    else
      SOURCE_PROFILE_ID="default"
    fi
  fi
  [[ -n "${SOURCE_PROFILE_ID}" && ${#SOURCE_PROFILE_ID} -le 128 ]] \
    || die "--source-profile must contain 1 to 128 characters" 2
  [[ "${SOURCE_PROFILE_ID}" != *$'\n'* && "${SOURCE_PROFILE_ID}" != *$'\r'* ]] \
    || die "--source-profile must not contain control newlines" 2
}

load_preserved_media_roots() {
  local line roots_output
  [[ -f "${CONFIG_FILE}" ]] || return 0
  roots_output="$("${PYTHON_BIN}" - "${CONFIG_FILE}" <<'PY'
from pathlib import Path
import sys
import tomllib

with Path(sys.argv[1]).open("rb") as handle:
    roots = tomllib.load(handle).get("worker", {}).get("source_media_roots", [])
if not isinstance(roots, list) or any(not isinstance(value, str) for value in roots):
    raise SystemExit("worker.source_media_roots must be a list of strings")
for value in roots:
    if "\n" in value or "\r" in value:
        raise SystemExit("worker.source_media_roots contains a newline")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(sys.argv[1]).parent / path
    print(path.resolve(strict=False))
PY
  )" || die "could not read worker.source_media_roots from the existing config" 2
  if [[ -n "${roots_output}" ]]; then
    mapfile -t MEDIA_ROOTS <<<"${roots_output}"
  else
    MEDIA_ROOTS=()
  fi
  for line in "${MEDIA_ROOTS[@]}"; do
    [[ -n "${line}" ]] || die "existing worker.source_media_roots contains an empty path" 2
  done
}

normalize_media_roots() {
  local raw resolved existing duplicate preserving=0
  local -a normalized=()
  if (( CLEAR_MEDIA_ROOTS )); then
    MEDIA_ROOTS=()
  elif (( ! MEDIA_ROOTS_SPECIFIED )); then
    load_preserved_media_roots
    preserving=1
  fi
  for raw in "${MEDIA_ROOTS[@]}"; do
    [[ -n "${raw}" ]] || die "--media-root must not be empty" 2
    [[ "${raw}" == /* ]] || die "--media-root must be an absolute path" 2
    [[ "${raw}" != *:* && "${raw}" != *$'\n'* && "${raw}" != *$'\r'* ]] \
      || die "--media-root contains a character that cannot be represented safely" 2
    if (( preserving )); then
      resolved="$(canonical "${raw}")"
    else
      reject_symlink "${raw}" "media root"
      [[ -d "${raw}" && -r "${raw}" && -x "${raw}" ]] \
        || die "--media-root must be an existing readable directory" 2
      resolved="$(realpath -- "${raw}")"
    fi
    [[ "${resolved}" != "/" && "${resolved}" != "$(canonical "${HOME}")" ]] \
      || die "--media-root is too broad" 2
    duplicate=0
    for existing in "${normalized[@]}"; do
      if [[ "${existing}" == "${resolved}" ]]; then duplicate=1; break; fi
    done
    (( duplicate )) || normalized+=("${resolved}")
  done
  MEDIA_ROOTS=("${normalized[@]}")

  if ((${#MEDIA_ROOTS[@]})); then
    if (( preserving )); then
      log "media capture: existing approved roots preserved (count=${#MEDIA_ROOTS[@]})"
    else
      log "media capture: approved roots configured (count=${#MEDIA_ROOTS[@]})"
    fi
  elif (( CLEAR_MEDIA_ROOTS )); then
    log "media capture: approved roots explicitly cleared; capture is fail-closed"
  else
    log "media capture: disabled/fail-closed (no approved root in existing or fresh config)"
  fi
}

joined_media_roots() {
  local IFS=:
  printf '%s' "${MEDIA_ROOTS[*]}"
}

managed_runtime_name() {
  local parent name
  [[ "${RUNTIME}" != "none" ]] || return 1
  parent="$(dirname -- "$1")"
  name="$(basename -- "$1")"
  [[ "$(canonical "${parent}")" == "$(canonical "${RUNTIME_PLUGIN_ROOT}")" \
    || "$(canonical "${parent}")" == "$(canonical "${RUNTIME_STAGING_ROOT}")" ]] || return 1
  case "${name}" in
    espelho-zap-portable|.espelho-zap-portable.new-*) return 0 ;;
    *) return 1 ;;
  esac
}

remove_managed_runtime() {
  local target="$1" root
  managed_runtime_name "${target}" || die "refusing to remove an unmanaged runtime path"
  if contained_by "${RUNTIME_PLUGIN_ROOT}" "${target}"; then
    root="${RUNTIME_PLUGIN_ROOT}"
  else
    root="${RUNTIME_STAGING_ROOT}"
  fi
  contained_by "${root}" "${target}" \
    || die "runtime plugin path escapes the managed root"
  reject_symlink "${target}" "managed runtime plugin"
  [[ -e "${target}" ]] || return 0
  run rm -rf -- "${target}"
}

managed_venv_name() {
  local parent name
  parent="$(dirname -- "$1")"
  name="$(basename -- "$1")"
  [[ "$(canonical "${parent}")" == "$(canonical "${APP_ROOT}")" ]] || return 1
  case "${name}" in venv|.venv.new-*|venv.backup-*) return 0 ;; *) return 1 ;; esac
}

remove_managed_venv() {
  local target="$1"
  managed_venv_name "${target}" || die "refusing to remove an unmanaged venv path"
  contained_by "${APP_ROOT}" "${target}" || die "venv path escapes the application root"
  reject_symlink "${target}" "managed venv"
  [[ -e "${target}" ]] || return 0
  run rm -rf -- "${target}"
}

managed_skill_name() {
  local parent name
  parent="$(dirname -- "$1")"
  name="$(basename -- "$1")"
  [[ "$(canonical "${parent}")" == "$(canonical "${SKILL_ROOT}")" ]] || return 1
  case "${name}" in espelho-zap-portable|.espelho-zap-portable.new-*|espelho-zap-portable.backup-*) return 0 ;; *) return 1 ;; esac
}

remove_managed_skill() {
  local target="$1"
  managed_skill_name "${target}" || die "refusing to remove an unmanaged skill path"
  contained_by "${SKILL_ROOT}" "${target}" || die "skill path escapes the skill root"
  reject_symlink "${target}" "managed skill"
  [[ -e "${target}" ]] || return 0
  run rm -rf -- "${target}"
}

nearest_existing_parent() {
  local candidate="$1"
  while [[ ! -e "${candidate}" && "${candidate}" != "/" ]]; do
    candidate="$(dirname -- "${candidate}")"
  done
  printf '%s\n' "${candidate}"
}

available_bytes() {
  local probe
  probe="$(nearest_existing_parent "$1")"
  df -Pk -- "${probe}" | awk 'END { printf "%.0f\n", $4 * 1024 }'
}

check_space() {
  local free_bytes
  [[ "${MIN_FREE_BYTES}" =~ ^[0-9]+$ ]] && (( MIN_FREE_BYTES > 0 )) \
    || die "ESPELHO_ZAP_MIN_FREE_BYTES must be a positive integer" 2
  if ! free_bytes="$(available_bytes "${APP_ROOT}")"; then
    die "free disk space could not be measured" 2
  fi
  [[ "${free_bytes}" =~ ^[0-9]+$ ]] || die "free disk space could not be measured" 2
  (( free_bytes >= MIN_FREE_BYTES )) || die "free disk space is below the configured threshold" 2
  log "disk-space: ok (free_bytes=${free_bytes}, required_bytes=${MIN_FREE_BYTES})"
}

preflight() {
  [[ "$(uname -s)" == "Linux" ]] || die "Linux is required" 2
  for command_name in realpath install sed df awk cp find chmod mv rm head grep stat; do
    command -v "${command_name}" >/dev/null 2>&1 || die "required command not found: ${command_name}" 2
  done
  configure_runtime_target
  [[ "${APP_ROOT}" != "/" && "${APP_ROOT}" != "${HOME}" ]] || die "unsafe app root" 2
  [[ "${CONFIG_DIR}" != "/" && "${STATE_DIR}" != "/" && "${SKILL_ROOT}" != "/" ]] \
    || die "unsafe runtime path" 2
  reject_symlink "${APP_ROOT}" "application root"
  reject_symlink "${VENV}" "active venv"
  reject_symlink "${BACKUP_DIR}" "backup directory"
  reject_symlink "${CONFIG_DIR}" "configuration directory"
  reject_symlink "${STATE_DIR}" "state directory"
  reject_symlink "${HOOK_HEALTH_FILE}" "hook health file"
  reject_symlink "${INSTALL_RECORD}" "install ownership record"
  reject_symlink "${ACTIVATION_RECORD}" "runtime activation record"
  reject_symlink "${SKILL_ROOT}" "skill root"
  reject_symlink "${SKILL_DEST}" "managed skill destination"
  reject_symlink "${UNIT_DIR}" "systemd user unit directory"
  reject_symlink "${SERVICE_UNIT}" "systemd service unit"
  reject_symlink "${TIMER_UNIT}" "systemd timer unit"

  load_install_record
  load_activation_record
  if [[ "${ACTION}" == "uninstall" ]]; then
    (( ! SOURCE_PROFILE_ID_EXPLICIT && ! MEDIA_ROOTS_SPECIFIED && ! CLEAR_MEDIA_ROOTS )) \
      || die "profile/media selection options are not valid for uninstall" 2
    if (( ACTIVE_RUNTIME_FOUND )); then
      [[ "${RUNTIME}" != "none" ]] \
        || die "an enabled runtime integration is present; uninstall requires explicit matching --runtime and --runtime-home" 2
      activation_selection_matches \
        || die "selected runtime/home does not match the enabled integration" 2
      contained_by "${RUNTIME_ACTIVATION_BACKUP_ROOT}" "${ACTIVE_ACTIVATION_BASELINE_DIR}" \
        && [[ -d "${ACTIVE_ACTIVATION_BASELINE_DIR}" && ! -L "${ACTIVE_ACTIVATION_BASELINE_DIR}" ]] \
        || die "runtime activation baseline is missing or outside the managed backup root" 2
      ACTIVATION_BASELINE_DIR="${ACTIVE_ACTIVATION_BASELINE_DIR}"
      RUNTIME_DEACTIVATION=1
      [[ -x "${VENV}/bin/python" ]] \
        || die "the managed CLI is required to transactionally remove an enabled runtime integration" 2
    fi
    if (( RUNTIME_DEACTIVATION )); then
      if [[ "${RUNTIME}" == "hermes" ]]; then
        command -v hermes >/dev/null 2>&1 || die "Hermes CLI is required for active uninstall" 2
        RUNTIME_CLI="$(canonical "$(command -v hermes)")"
      else
        command -v openclaw >/dev/null 2>&1 || die "OpenClaw CLI is required for active uninstall" 2
        RUNTIME_CLI="$(canonical "$(command -v openclaw)")"
      fi
    fi
    log "preflight: ok (uninstall)"
    return 0
  fi

  command -v "${PYTHON_BIN}" >/dev/null 2>&1 || die "Python executable not found" 2
  "${PYTHON_BIN}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "Python 3.11 or newer is required" 2
  "${PYTHON_BIN}" -m venv --help >/dev/null 2>&1 || die "Python venv support is required" 2
  PYTHON_BIN="$(canonical "$(command -v "${PYTHON_BIN}")")"
  resolve_source_profile_id
  normalize_media_roots

  if (( ACTIVE_RUNTIME_FOUND )); then
    activation_selection_matches \
      || die "an enabled runtime integration exists; upgrade requires its explicit matching --runtime and --runtime-home" 2
    contained_by "${RUNTIME_ACTIVATION_BACKUP_ROOT}" "${ACTIVE_ACTIVATION_BASELINE_DIR}" \
      && [[ -d "${ACTIVE_ACTIVATION_BASELINE_DIR}" && ! -L "${ACTIVE_ACTIVATION_BASELINE_DIR}" ]] \
      || die "runtime activation baseline is missing or outside the managed backup root" 2
    ACTIVATION_BASELINE_DIR="${ACTIVE_ACTIVATION_BASELINE_DIR}"
    (( ENABLE_RUNTIME )) \
      || die "an enabled runtime integration exists; upgrade requires --enable-runtime for quiescence and load verification" 2
    OPENCLAW_HOOK_CHANGED="${ACTIVE_OPENCLAW_HOOK_CHANGED}"
  fi

  if [[ -d "${SOURCE}" ]]; then
    [[ -f "${SOURCE}/pyproject.toml" ]] || die "local source has no pyproject.toml" 2
  elif [[ -f "${SOURCE}" && "${SOURCE}" == *.whl ]]; then
    :
  else
    die "--source must be a prebuilt wheel or local project directory" 2
  fi
  [[ -f "${PROJECT_DIR}/packaging/systemd/espelho-zap@.service" ]] \
    || die "service unit template is missing" 2
  [[ -f "${PROJECT_DIR}/packaging/systemd/espelho-zap@.timer" ]] \
    || die "timer unit template is missing" 2
  [[ -f "${SKILL_SOURCE}/SKILL.md" ]] || die "portable skill source is missing" 2
  if [[ "${RUNTIME}" != "none" ]]; then
    [[ -d "${RUNTIME_PLUGIN_SOURCE}" ]] || die "selected runtime plugin source is missing" 2
    if [[ "${RUNTIME}" == "hermes" ]]; then
      [[ -f "${RUNTIME_PLUGIN_SOURCE}/__init__.py" ]] \
        && [[ -f "${RUNTIME_PLUGIN_SOURCE}/plugin.yaml" ]] \
        || die "Hermes plugin package is incomplete" 2
    else
      command -v node >/dev/null 2>&1 || die "Node.js is required for OpenClaw plugin validation" 2
      [[ -f "${RUNTIME_PLUGIN_SOURCE}/openclaw.plugin.json" ]] \
        && [[ -f "${RUNTIME_PLUGIN_SOURCE}/package.json" ]] \
        && [[ -f "${RUNTIME_PLUGIN_SOURCE}/dist/index.js" ]] \
        || die "OpenClaw plugin package is incomplete" 2
      if (( ENABLE_RUNTIME )); then
        grep -Fq 'api.on("before_agent_reply"' "${RUNTIME_PLUGIN_SOURCE}/dist/index.js" \
          && grep -Fq 'api.on("message_sending"' "${RUNTIME_PLUGIN_SOURCE}/dist/index.js" \
          && grep -Fq 'api.on("reply_payload_sending"' "${RUNTIME_PLUGIN_SOURCE}/dist/index.js" \
          || die "OpenClaw activation is fail-closed until the adapter proves model short-circuit plus both outbound-cancellation hooks" 2
      fi
    fi
    if [[ -e "${RUNTIME_STAGING_DEST}" ]]; then
      runtime_marker_matches "${RUNTIME_STAGING_MARKER}" \
        || die "existing runtime staging destination is not installer-managed" 2
    fi
    if (( ENABLE_RUNTIME )) && [[ -e "${RUNTIME_PLUGIN_DEST}" ]]; then
      runtime_marker_matches "${RUNTIME_PLUGIN_MARKER}" \
        || die "existing active runtime plugin destination is not installer-managed" 2
    fi
    if (( ENABLE_RUNTIME )); then
      if [[ "${RUNTIME}" == "hermes" ]]; then
        command -v hermes >/dev/null 2>&1 \
          || die "Hermes CLI is required by --enable-runtime" 2
        RUNTIME_CLI="$(canonical "$(command -v hermes)")"
        die "Hermes CLI cannot prove a hook was loaded inside the running gateway; activation is fail-closed at the documented human load-canary gate" 2
      else
        command -v openclaw >/dev/null 2>&1 \
          || die "OpenClaw CLI is required by --enable-runtime" 2
        RUNTIME_CLI="$(canonical "$(command -v openclaw)")"
      fi
      [[ -x "${RUNTIME_CLI}" ]] || die "selected runtime CLI is not executable" 2
      if [[ "${RUNTIME}" == "openclaw" ]] && (( ! ACTIVE_RUNTIME_FOUND )); then
        local plugin_listing
        plugin_listing="$(runtime_command plugins list --json)" \
          || die "OpenClaw plugin inventory could not be inspected before activation" 2
        [[ "${plugin_listing}" != *'espelho-zap-portable'* ]] \
          || die "OpenClaw already knows this plugin without an installer activation record; refusing to take ownership" 2
      fi
    fi
  fi
  if [[ -e "${SKILL_DEST}" ]]; then
    [[ -f "${SKILL_MARKER}" ]] \
      && [[ "$(head -n 1 -- "${SKILL_MARKER}")" == "${MANAGED_MARKER}" ]] \
      || die "existing skill destination is not installer-managed" 2
  fi
  check_space
  log "preflight: ok (python=$(${PYTHON_BIN} -c 'import sys; print(".".join(map(str, sys.version_info[:3])))'))"
}

private_directories() {
  run install -d -m 0700 -- \
    "${APP_ROOT}" "${BACKUP_DIR}" "${CONFIG_DIR}" "${STATE_DIR}" "${UNIT_DIR}" "${SKILL_ROOT}"
  if [[ "${RUNTIME}" != "none" ]]; then
    run install -d -m 0700 -- "${RUNTIME_STAGING_ROOT}" "${RUNTIME_STAGING_BACKUP_ROOT}"
    if (( ENABLE_RUNTIME || RUNTIME_DEACTIVATION )); then
      run install -d -m 0700 -- \
        "${RUNTIME_HOME}" "${RUNTIME_PLUGIN_ROOT}" "${RUNTIME_BACKUP_ROOT}" \
        "${RUNTIME_ACTIVATION_BACKUP_ROOT}"
    fi
  fi
}

local_build_backend_ready() {
  "$1" -c 'import importlib.metadata as m, re, sys
try: raw=m.version("setuptools")
except m.PackageNotFoundError: raise SystemExit(1)
parts=[]
for item in raw.split("."):
    match=re.match(r"[0-9]+", item)
    if not match: break
    parts.append(int(match.group(0)))
raise SystemExit(0 if tuple(parts) >= (68,) else 1)'
}

install_candidate() {
  local candidate="$1"
  run "${PYTHON_BIN}" -m venv "${candidate}" || return 1
  if (( DRY_RUN )); then
    if [[ -d "${SOURCE}" ]]; then log "DRY-RUN: verify candidate setuptools >= 68"; fi
  elif [[ -d "${SOURCE}" ]] && ! local_build_backend_ready "${candidate}/bin/python"; then
    log "local installation requires setuptools>=68 in the new venv; supply a prebuilt wheel" >&2
    return 1
  fi
  if [[ -d "${SOURCE}" ]]; then
    run "${candidate}/bin/python" -m pip install \
      --disable-pip-version-check --no-deps --no-build-isolation "${SOURCE}" || return 1
  else
    run "${candidate}/bin/python" -m pip install \
      --disable-pip-version-check --no-deps "${SOURCE}" || return 1
  fi
  run "${candidate}/bin/espelho-zap" --version || return 1
}

relocate_candidate_venv() {
  local previous_root="$1" active_root="$2"
  if (( DRY_RUN )); then
    log "DRY-RUN: rewrite staged virtualenv text launchers for their final path"
    return 0
  fi
  "${PYTHON_BIN}" - "${previous_root}" "${active_root}" <<'PY'
import os
from pathlib import Path
import stat
import sys
import tempfile

previous = Path(sys.argv[1]).resolve(strict=False)
active = Path(sys.argv[2]).resolve(strict=True)
old = os.fsencode(previous)
new = os.fsencode(active)
if old == new:
    raise SystemExit(0)

targets = [active / "pyvenv.cfg"]
bin_dir = active / "bin"
if not bin_dir.is_dir():
    raise SystemExit("relocated virtualenv has no bin directory")
targets.extend(bin_dir.iterdir())

for path in targets:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        continue
    payload = path.read_bytes()
    if old not in payload or b"\0" in payload:
        continue
    updated = payload.replace(old, new)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, stat.S_IMODE(metadata.st_mode))
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

launcher = active / "bin" / "espelho-zap"
if old in launcher.read_bytes():
    raise SystemExit("console launcher still references the staging virtualenv")
PY
  run "${active_root}/bin/python" -c 'import espelho_zap' || return 1
  run "${active_root}/bin/espelho-zap" --version || return 1
  log "virtualenv launchers: rebound to final installation path"
}

prepare_skill_candidate() {
  local candidate="$1"
  run install -d -m 0700 -- "${candidate}" || return 1
  run cp -R -- "${SKILL_SOURCE}/." "${candidate}/" || return 1
  if (( DRY_RUN )); then
    log "DRY-RUN: write managed skill manifest (mode 0600)"
    log "DRY-RUN: harden skill directories to 0700 and files to 0600"
    return 0
  fi
  printf '%s\n' "${MANAGED_MARKER}" >"${candidate}/.espelho-zap-managed" || return 1
  find "${candidate}" -type d -exec chmod 0700 {} + || return 1
  find "${candidate}" -type f -exec chmod 0600 {} + || return 1
}

write_runtime_env_template() {
  local destination="$1" runtime_cli runtime_config media_roots
  runtime_cli="$(canonical "${VENV}/bin/espelho-zap")"
  runtime_config="$(canonical "${CONFIG_FILE}")"
  if (( DRY_RUN )); then
    log "DRY-RUN: write inert secret-free runtime env template (mode 0600)"
    return 0
  fi
  {
    printf '%s\n' '# Inert template: review and load through the runtime service configuration.'
    printf 'ESPELHO_ZAP_CLI=%q\n' "${runtime_cli}"
    printf 'ESPELHO_ZAP_CONFIG=%q\n' "${runtime_config}"
    printf 'ESPELHO_ZAP_SOURCE_PROFILE_ID=%q\n' "${SOURCE_PROFILE_ID}"
    printf 'ESPELHO_ZAP_HOOK_HEALTH_FILE=%q\n' "$(canonical "${HOOK_HEALTH_FILE}")"
    printf '%s\n' 'ESPELHO_ZAP_PRIVACY_SCOPE=owner_private'
    if ((${#MEDIA_ROOTS[@]})); then
      media_roots="$(joined_media_roots)"
      printf 'ESPELHO_ZAP_MEDIA_ROOTS=%q\n' "${media_roots}"
    else
      printf '%s\n' '# ESPELHO_ZAP_MEDIA_ROOTS intentionally unset: media capture is fail-closed.'
    fi
    printf '%s\n' 'ESPELHO_ZAP_MAX_HOOK_BYTES=1048576'
    printf '%s\n' 'ESPELHO_ZAP_HOOK_TIMEOUT_SECONDS=15'
    printf '%s\n' 'ESPELHO_ZAP_HOOK_TIMEOUT_MS=15000'
  } >"${destination}" || return 1
  chmod 0600 "${destination}" || return 1
}

validate_runtime_candidate() {
  local candidate="$1" template expected_media expected_line
  [[ "${RUNTIME}" != "none" ]] || return 0
  if (( DRY_RUN )); then
    log "DRY-RUN: validate ${RUNTIME} plugin discovery shape without loading the runtime"
    return 0
  fi
  template="${candidate}/espelho-zap.env.example"
  [[ -f "${template}" ]] \
    && grep -q '^ESPELHO_ZAP_CLI=' "${template}" \
    && grep -q '^ESPELHO_ZAP_CONFIG=' "${template}" \
    && grep -q '^ESPELHO_ZAP_SOURCE_PROFILE_ID=' "${template}" \
    && grep -q '^ESPELHO_ZAP_HOOK_HEALTH_FILE=' "${template}" \
    || return 1
  if grep -Eq '^[A-Za-z_][A-Za-z0-9_]*(TOKEN|SECRET|PASSWORD)=' "${template}"; then
    return 1
  fi
  if ((${#MEDIA_ROOTS[@]})); then
    expected_media="$(joined_media_roots)"
    printf -v expected_line 'ESPELHO_ZAP_MEDIA_ROOTS=%q' "${expected_media}"
    grep -Fxq -- "${expected_line}" "${template}" || return 1
  elif grep -q '^ESPELHO_ZAP_MEDIA_ROOTS=' "${template}"; then
    return 1
  fi

  if [[ "${RUNTIME}" == "hermes" ]]; then
    grep -Fxq 'name: espelho-zap-portable' "${candidate}/plugin.yaml" || return 1
    grep -Fxq '  - pre_gateway_dispatch' "${candidate}/plugin.yaml" || return 1
    "${PYTHON_BIN}" -I -S -c '
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
' "${candidate}/__init__.py" || return 1
  else
    grep -Fq '"id": "espelho-zap-portable"' "${candidate}/openclaw.plugin.json" || return 1
    grep -Fq '"./dist/index.js"' "${candidate}/package.json" || return 1
    node --check "${candidate}/dist/index.js" >/dev/null || return 1
  fi
}

prepare_runtime_candidate() {
  local candidate="$1"
  [[ "${RUNTIME}" != "none" ]] || return 0
  run install -d -m 0700 -- "${candidate}" || return 1
  run cp -R -- "${RUNTIME_PLUGIN_SOURCE}/." "${candidate}/" || return 1
  if (( DRY_RUN )); then
    log "DRY-RUN: write managed runtime manifest (mode 0600)"
  else
    write_runtime_marker "${candidate}/.espelho-zap-runtime-managed" prepared || return 1
  fi
  write_runtime_env_template "${candidate}/espelho-zap.env.example" || return 1
  if (( ! DRY_RUN )); then
    find "${candidate}" -type d -exec chmod 0700 {} + || return 1
    find "${candidate}" -type f -exec chmod 0600 {} + || return 1
  fi
  validate_runtime_candidate "${candidate}"
}

prepare_hermes_observer_candidate() {
  local candidate="$1" config="${candidate}/direct-bridge.toml"
  (( PREPARE_HERMES_OBSERVER )) || return 0
  if (( DRY_RUN )); then
    log "DRY-RUN: render and validate managed Hermes observer candidate"
    return 0
  fi
  install -d -o root -g "${HERMES_SERVICE_GROUP}" -m 0750 -- "${candidate}" || return 1
  install -o root -g "${HERMES_SERVICE_GROUP}" -m 0550 \
    -- "${HERMES_DIRECT_BRIDGE_SOURCE}/bridge_guard.py" \
    "${candidate}/bridge_guard.py" || return 1
  install -o root -g "${HERMES_SERVICE_GROUP}" -m 0550 \
    -- "${HERMES_DIRECT_BRIDGE_SOURCE}/observer_launcher.py" \
    "${candidate}/observer_launcher.py" || return 1
  {
    printf '%s\n' "${MANAGED_MARKER}"
    printf 'profile=%s\n' "${HERMES_OBSERVER_PROFILE}"
  } >"${candidate}/.espelho-zap-hermes-observer-managed" || return 1
  chown root:"${HERMES_SERVICE_GROUP}" \
    -- "${candidate}/.espelho-zap-hermes-observer-managed" || return 1
  chmod 0440 -- "${candidate}/.espelho-zap-hermes-observer-managed" || return 1
  "${PYTHON_BIN}" - "${HERMES_BRIDGE_CONFIG_SOURCE}" "${config}" \
    "${HERMES_BRIDGE_JS}" "${HERMES_HUMAN_OUTBOUND_TOKEN_FILE}" \
    "${HERMES_HUMAN_OUTBOUND_MEDIA_ROOT}" <<'PY'
import json
import os
from pathlib import Path
import stat
import sys
import tomllib

source, destination, bridge_js, token_file, media_root = map(Path, sys.argv[1:])
details = source.stat()
if not stat.S_ISREG(details.st_mode) or stat.S_IMODE(details.st_mode) != 0o600:
    raise SystemExit("direct bridge config source must have mode 0600")
with source.open("rb") as handle:
    raw = tomllib.load(handle)
bridge = raw.get("bridge")
required = {
    "node", "session_dir", "spool_file", "cache_root", "lock_file", "port",
    "mode", "dm_policy", "group_policy", "allowed_users",
    "forward_owner_messages", "debug",
}
allowed = required | {"bridge_js", "human_outbound_token_file", "human_outbound_media_root"}
if raw.get("schema_version") != 1 or not isinstance(bridge, dict):
    raise SystemExit("unsupported direct bridge configuration")
if required - bridge.keys() or bridge.keys() - allowed:
    raise SystemExit("direct bridge configuration fields are incomplete or unknown")
values = dict(bridge)
values.update(
    bridge_js=str(bridge_js),
    human_outbound_token_file=str(token_file),
    human_outbound_media_root=str(media_root),
)
for key in (
    "node", "bridge_js", "session_dir", "spool_file", "cache_root", "lock_file",
    "human_outbound_token_file", "human_outbound_media_root",
):
    value = values[key]
    if (
        not isinstance(value, str)
        or not Path(value).is_absolute()
        or any(ch.isspace() for ch in value)
        or "%" in value
        or "@" in value
    ):
        raise SystemExit(f"{key} must be an absolute path without whitespace")
if values["port"] != 3011:
    raise SystemExit("Hermes human outbound requires port 3011")
for key in ("forward_owner_messages", "debug"):
    if not isinstance(values[key], bool):
        raise SystemExit(f"{key} must be a boolean")
for key in ("mode", "dm_policy", "group_policy", "allowed_users"):
    if not isinstance(values[key], str) or not values[key]:
        raise SystemExit(f"{key} must be an explicit string")
order = (
    "node", "bridge_js", "session_dir", "spool_file", "cache_root", "lock_file",
    "port", "mode", "dm_policy", "group_policy", "allowed_users",
    "forward_owner_messages", "human_outbound_token_file",
    "human_outbound_media_root", "debug",
)
lines = ["schema_version = 1", "", "[bridge]"]
for key in order:
    value = values[key]
    if isinstance(value, bool):
        rendered = "true" if value else "false"
    elif isinstance(value, int):
        rendered = str(value)
    else:
        rendered = json.dumps(value, ensure_ascii=False)
    lines.append(f"{key} = {rendered}")
destination.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
os.chmod(destination, 0o600)
PY
  chown "${HERMES_SERVICE_USER}:${HERMES_SERVICE_GROUP}" -- "${config}" || return 1
  chmod 0600 -- "${config}" || return 1
  "${PYTHON_BIN}" "${candidate}/bridge_guard.py" check "${HERMES_BRIDGE_JS}" \
    >/dev/null || return 1
  validate_hermes_observer_managed_directories || return 1
  runuser --user "${HERMES_SERVICE_USER}" --group "${HERMES_SERVICE_GROUP}" -- \
    "${PYTHON_BIN}" "${candidate}/observer_launcher.py" --config "${config}" --check \
    >/dev/null || return 1
}

render_hermes_observer_unit() {
  local destination="$1" config="${HERMES_OBSERVER_ROOT}/direct-bridge.toml"
  "${PYTHON_BIN}" - \
    "${PROJECT_DIR}/packaging/systemd/espelho-zap-hermes-observer@.service.in" \
    "${destination}" "${config}" "${HERMES_OBSERVER_ROOT}" "${PYTHON_BIN}" \
    "${HERMES_SERVICE_USER}" "${HERMES_SERVICE_GROUP}" "${MANAGED_MARKER}" \
    "${HERMES_OBSERVER_PROFILE}" <<'PY'
import os
from pathlib import Path
import re
import sys
import tomllib

(template_path, destination, config_path, observer_root, python_bin,
 service_user, service_group, marker, profile) = sys.argv[1:]
with Path(config_path).open("rb") as handle:
    bridge = tomllib.load(handle)["bridge"]
replacements = {
    "SERVICE_USER": service_user,
    "SERVICE_GROUP": service_group,
    "BRIDGE_ROOT": str(Path(bridge["bridge_js"]).parent),
    "OBSERVER_ENV_FILE": "/dev/null",
    "PYTHON_BIN": python_bin,
    "DIRECT_BRIDGE_GUARD": str(Path(observer_root) / "bridge_guard.py"),
    "DIRECT_BRIDGE_LAUNCHER": str(Path(observer_root) / "observer_launcher.py"),
    "DIRECT_BRIDGE_CONFIG": config_path,
    "BRIDGE_JS": bridge["bridge_js"],
    "SESSION_DIR": bridge["session_dir"],
    "SPOOL_DIR": str(Path(bridge["spool_file"]).parent),
    "CACHE_DIR": bridge["cache_root"],
    "LOCK_DIR": str(Path(bridge["lock_file"]).parent),
    "HUMAN_OUTBOUND_TOKEN_FILE": bridge["human_outbound_token_file"],
    "HUMAN_OUTBOUND_MEDIA_ROOT": bridge["human_outbound_media_root"],
}
body = Path(template_path).read_text(encoding="utf-8")
placeholder_pattern = re.compile(r"@([A-Z][A-Z0-9_]*)@")
template_placeholders = set(placeholder_pattern.findall(body))
if template_placeholders != set(replacements):
    raise SystemExit("unknown or missing systemd placeholder")
for name, value in replacements.items():
    if not isinstance(value, str) or not value or any(ch.isspace() for ch in value):
        raise SystemExit(f"unsafe systemd replacement: {name}")
body = placeholder_pattern.sub(
    lambda match: replacements[match.group(1)].replace("%", "%%"), body
)
body = f"# {marker}\n# profile={profile}\n" + body
Path(destination).write_text(body, encoding="utf-8", newline="\n")
os.chmod(destination, 0o644)
PY
}

cleanup_hermes_observer_candidate() {
  local cleanup_ok=1
  if [[ -n "${HERMES_OBSERVER_CANDIDATE}" \
    && ( -e "${HERMES_OBSERVER_CANDIDATE}" || -L "${HERMES_OBSERVER_CANDIDATE}" ) ]]; then
    rm -rf -- "${HERMES_OBSERVER_CANDIDATE}" || cleanup_ok=0
  fi
  if [[ -n "${HERMES_OBSERVER_UNIT_CANDIDATE_DIR}" \
    && ( -e "${HERMES_OBSERVER_UNIT_CANDIDATE_DIR}" \
      || -L "${HERMES_OBSERVER_UNIT_CANDIDATE_DIR}" ) ]]; then
    rm -rf -- "${HERMES_OBSERVER_UNIT_CANDIDATE_DIR}" || cleanup_ok=0
  fi
  if (( PREPARE_HERMES_OBSERVER && ! HERMES_OBSERVER_BASE_HAD )) \
    && [[ -d "${HERMES_OBSERVER_BASE}" ]] \
    && [[ ! -e "${HERMES_OBSERVER_ROOT}" && ! -L "${HERMES_OBSERVER_ROOT}" ]]; then
    rmdir -- "${HERMES_OBSERVER_BASE}" 2>/dev/null || cleanup_ok=0
  fi
  (( cleanup_ok ))
}

activate_hermes_observer() {
  (( PREPARE_HERMES_OBSERVER )) || return 0
  if (( DRY_RUN )); then
    log "DRY-RUN: atomically publish Hermes observer root and disabled system unit"
    log "DRY-RUN: systemd-analyze verify rendered observer unit when available"
    return 0
  fi
  check_hermes_observer_competitors || return 1
  HERMES_OBSERVER_ROOT_HAD="${HERMES_OBSERVER_INSTALLED}"
  HERMES_OBSERVER_UNIT_HAD="${HERMES_OBSERVER_INSTALLED}"
  HERMES_OBSERVER_ACTIVATED=1
  if (( HERMES_OBSERVER_ROOT_HAD )); then
    install -d -o root -g root -m 0700 -- "${HERMES_OBSERVER_BACKUP_DIR}" || return 1
    mv -- "${HERMES_OBSERVER_ROOT}" "${HERMES_OBSERVER_BACKUP}" || return 1
  fi
  if (( HERMES_OBSERVER_UNIT_HAD )); then
    mv -- "${HERMES_OBSERVER_UNIT}" "${HERMES_OBSERVER_UNIT_BACKUP}" || return 1
  fi
  mv -- "${HERMES_OBSERVER_CANDIDATE}" "${HERMES_OBSERVER_ROOT}" || return 1
  install -d -m 0700 -- "${HERMES_OBSERVER_UNIT_CANDIDATE_DIR}" || return 1
  HERMES_OBSERVER_UNIT_CANDIDATE="${HERMES_OBSERVER_UNIT_CANDIDATE_DIR}/${HERMES_OBSERVER_UNIT_NAME}"
  render_hermes_observer_unit "${HERMES_OBSERVER_UNIT_CANDIDATE}" || return 1
  if command -v systemd-analyze >/dev/null 2>&1; then
    systemd-analyze verify "${HERMES_OBSERVER_UNIT_CANDIDATE}" || return 1
  else
    log "warning: systemd-analyze not found; rendered observer unit received placeholder validation only"
  fi
  mv -- "${HERMES_OBSERVER_UNIT_CANDIDATE}" "${HERMES_OBSERVER_UNIT}" || return 1
  rmdir -- "${HERMES_OBSERVER_UNIT_CANDIDATE_DIR}" || return 1
  systemctl daemon-reload || return 1
  read_hermes_observer_unit_state || return 1
  [[ "${HERMES_OBSERVER_UNIT_LOAD_STATE}" == "loaded" \
    && "${HERMES_OBSERVER_UNIT_ACTIVE_STATE}" == "inactive" \
    && "${HERMES_OBSERVER_UNIT_ENABLED_STATE}" == "disabled" ]] \
    || return 1
  HERMES_OBSERVER_INSTALLED=1
  log "Hermes observer: installed disabled and inactive (no start/enable/restart executed)"
}

rollback_hermes_observer() {
  if (( ! HERMES_OBSERVER_ACTIVATED )); then
    cleanup_hermes_observer_candidate
    return
  fi
  local rollback_ok=1
  if (( HERMES_OBSERVER_ROOT_HAD )); then
    if [[ -e "${HERMES_OBSERVER_BACKUP}" ]]; then
      if [[ -e "${HERMES_OBSERVER_ROOT}" ]]; then
        hermes_observer_root_marker_matches "${HERMES_OBSERVER_ROOT}" "${HERMES_OBSERVER_PROFILE}" \
          && rm -rf -- "${HERMES_OBSERVER_ROOT}" || rollback_ok=0
      fi
      if [[ ! -e "${HERMES_OBSERVER_ROOT}" ]]; then
        mv -- "${HERMES_OBSERVER_BACKUP}" "${HERMES_OBSERVER_ROOT}" || rollback_ok=0
      fi
    elif [[ ! -e "${HERMES_OBSERVER_ROOT}" ]]; then
      rollback_ok=0
    fi
  elif [[ -e "${HERMES_OBSERVER_ROOT}" ]]; then
    hermes_observer_root_marker_matches "${HERMES_OBSERVER_ROOT}" "${HERMES_OBSERVER_PROFILE}" \
      && rm -rf -- "${HERMES_OBSERVER_ROOT}" || rollback_ok=0
  fi
  if (( HERMES_OBSERVER_UNIT_HAD )); then
    if [[ -e "${HERMES_OBSERVER_UNIT_BACKUP}" ]]; then
      if [[ -e "${HERMES_OBSERVER_UNIT}" ]]; then
        hermes_observer_unit_marker_matches "${HERMES_OBSERVER_UNIT}" "${HERMES_OBSERVER_PROFILE}" \
          && rm -f -- "${HERMES_OBSERVER_UNIT}" || rollback_ok=0
      fi
      if [[ ! -e "${HERMES_OBSERVER_UNIT}" ]]; then
        mv -- "${HERMES_OBSERVER_UNIT_BACKUP}" "${HERMES_OBSERVER_UNIT}" || rollback_ok=0
      fi
    elif [[ ! -e "${HERMES_OBSERVER_UNIT}" ]]; then
      rollback_ok=0
    fi
  elif [[ -e "${HERMES_OBSERVER_UNIT}" ]]; then
    hermes_observer_unit_marker_matches "${HERMES_OBSERVER_UNIT}" "${HERMES_OBSERVER_PROFILE}" \
      && rm -f -- "${HERMES_OBSERVER_UNIT}" || rollback_ok=0
  fi
  if [[ -d "${HERMES_OBSERVER_BACKUP_DIR}" ]] \
    && [[ ! -e "${HERMES_OBSERVER_BACKUP}" ]]; then
    rmdir -- "${HERMES_OBSERVER_BACKUP_DIR}" || rollback_ok=0
  fi
  cleanup_hermes_observer_candidate || rollback_ok=0
  systemctl daemon-reload || rollback_ok=0
  (( rollback_ok )) && log "rollback: exact prior Hermes observer root and unit restored"
  (( rollback_ok ))
}

hermes_observer_signal_abort() {
  local signal_name="$1" status=130
  [[ "${signal_name}" == "TERM" ]] && status=143
  trap '' INT TERM
  log "ERROR: Hermes observer preparation interrupted by ${signal_name}; rolling back" >&2
  if (( HERMES_OBSERVER_TRANSACTION_ACTIVE )); then
    rollback_hermes_observer \
      || log "ERROR: Hermes observer signal rollback was incomplete" >&2
  fi
  HERMES_OBSERVER_TRANSACTION_ACTIVE=0
  exit "${status}"
}

preflight_hermes_observer() {
  [[ "$(uname -s)" == "Linux" ]] || die "Linux is required" 2
  [[ "${ACTION}" == "install" || "${ACTION}" == "upgrade" || "${ACTION}" == "preflight" ]] \
    || die "--prepare-hermes-observer supports install, upgrade, or preflight only" 2
  (( ! SOURCE_EXPLICIT && ! RUNTIME_HOME_EXPLICIT && ! SOURCE_PROFILE_ID_EXPLICIT \
    && ! MEDIA_ROOTS_SPECIFIED && ! CLEAR_MEDIA_ROOTS && ! ENABLE_RUNTIME )) \
    || die "Hermes observer preparation is independent; do not pass per-user install/runtime options" 2
  local command_name parent_details
  for command_name in realpath install awk cp chmod chown mv rm rmdir grep stat \
    getent id systemctl runuser date; do
    command -v "${command_name}" >/dev/null 2>&1 \
      || die "required Hermes observer command not found: ${command_name}" 2
  done
  command -v "${PYTHON_BIN}" >/dev/null 2>&1 || die "Python executable not found" 2
  "${PYTHON_BIN}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "Python 3.11 or newer is required" 2
  PYTHON_BIN="$(canonical "$(command -v "${PYTHON_BIN}")")"
  configure_hermes_observer_target
  [[ -f "${HERMES_DIRECT_BRIDGE_SOURCE}/bridge_guard.py" ]] \
    && [[ -f "${HERMES_DIRECT_BRIDGE_SOURCE}/observer_launcher.py" ]] \
    && [[ -f "${PROJECT_DIR}/packaging/systemd/espelho-zap-hermes-observer@.service.in" ]] \
    || die "Hermes observer packaging sources are incomplete" 2
  [[ -d "${HERMES_SYSTEM_UNIT_DIR}" && ! -L "${HERMES_SYSTEM_UNIT_DIR}" ]] \
    || die "Hermes system unit directory must already exist and must not be a symlink" 2
  root_owned_not_writable_by_others "${HERMES_SYSTEM_UNIT_DIR}" \
    || die "Hermes system unit directory must be root-owned and not group/world writable" 2
  [[ -d "$(dirname -- "${HERMES_OBSERVER_BASE}")" \
    && ! -L "$(dirname -- "${HERMES_OBSERVER_BASE}")" ]] \
    || die "Hermes observer parent directory must already exist and must not be a symlink" 2
  root_owned_not_writable_by_others "$(dirname -- "${HERMES_OBSERVER_BASE}")" \
    || die "Hermes observer parent must be root-owned and not group/world writable" 2
  [[ -d /proc ]] || die "a mounted /proc is required for the process safety gate" 2
  if [[ -d "${HERMES_OBSERVER_BASE}" ]]; then
    parent_details="$(stat -c '%u:%g:%a' -- "${HERMES_OBSERVER_BASE}")"
    [[ "${parent_details}" == "0:0:711" ]] \
      || die "existing Hermes observer base must be root-owned with mode 0711" 2
  fi
  check_hermes_observer_competitors \
    || die "Hermes observer safety gate failed" 2
  log "Hermes observer preflight: ok (independent system preparation; no per-user artifacts)"
}

hermes_observer_transaction_body() {
  if (( HERMES_OBSERVER_BASE_HAD )); then
    [[ -d "${HERMES_OBSERVER_BASE}" && ! -L "${HERMES_OBSERVER_BASE}" ]] || return 1
  else
    [[ ! -e "${HERMES_OBSERVER_BASE}" && ! -L "${HERMES_OBSERVER_BASE}" ]] || return 1
    install -d -o root -g root -m 0711 -- "${HERMES_OBSERVER_BASE}" || return 1
  fi
  prepare_hermes_observer_candidate "${HERMES_OBSERVER_CANDIDATE}" || return 1
  activate_hermes_observer || return 1
}

prepare_hermes_observer_transaction() {
  local rollback_status=0 stamp
  if [[ -d "${HERMES_OBSERVER_BASE}" && ! -L "${HERMES_OBSERVER_BASE}" ]]; then
    HERMES_OBSERVER_BASE_HAD=1
  else
    HERMES_OBSERVER_BASE_HAD=0
  fi
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  HERMES_OBSERVER_CANDIDATE="${HERMES_OBSERVER_BASE}/.${HERMES_OBSERVER_PROFILE}.new-${stamp}-$$"
  HERMES_OBSERVER_BACKUP_DIR="${HERMES_OBSERVER_BASE}/.${HERMES_OBSERVER_PROFILE}.backup-${stamp}-$$"
  HERMES_OBSERVER_BACKUP="${HERMES_OBSERVER_BACKUP_DIR}/${HERMES_OBSERVER_PROFILE}"
  HERMES_OBSERVER_UNIT_CANDIDATE_DIR="${HERMES_SYSTEM_UNIT_DIR}/.espelho-zap-hermes-observer-${stamp}-$$"
  HERMES_OBSERVER_UNIT_BACKUP="${HERMES_SYSTEM_UNIT_DIR}/.${HERMES_OBSERVER_UNIT_NAME}.backup-${stamp}-$$"
  local transaction_path
  for transaction_path in "${HERMES_OBSERVER_CANDIDATE}" \
    "${HERMES_OBSERVER_BACKUP_DIR}" "${HERMES_OBSERVER_UNIT_CANDIDATE_DIR}" \
    "${HERMES_OBSERVER_UNIT_BACKUP}"; do
    [[ ! -e "${transaction_path}" && ! -L "${transaction_path}" ]] \
      || die "Hermes observer transaction path already exists: ${transaction_path}"
  done
  HERMES_OBSERVER_TRANSACTION_ACTIVE=1
  trap 'hermes_observer_signal_abort INT' INT
  trap 'hermes_observer_signal_abort TERM' TERM
  if (( DRY_RUN )); then
    quote_command install -d -o root -g root -m 0711 -- "${HERMES_OBSERVER_BASE}"
    prepare_hermes_observer_candidate "${HERMES_OBSERVER_CANDIDATE}"
    activate_hermes_observer
    trap - INT TERM
    HERMES_OBSERVER_TRANSACTION_ACTIVE=0
    log "dry-run complete; no per-user or system mutation was performed"
    return 0
  fi
  if ! hermes_observer_transaction_body; then
    trap '' INT TERM
    rollback_hermes_observer || rollback_status=$?
    trap - INT TERM
    HERMES_OBSERVER_TRANSACTION_ACTIVE=0
    if (( rollback_status )); then
      die "Hermes observer preparation failed and rollback was incomplete; retained backups require review"
    fi
    die "Hermes observer preparation failed; candidate was removed and prior bytes were restored"
  fi
  trap '' INT TERM
  HERMES_OBSERVER_TRANSACTION_ACTIVE=0
  cleanup_hermes_observer_candidate
  trap - INT TERM
  (( ! HERMES_OBSERVER_ROOT_HAD )) || log "rollback Hermes observer root backup retained"
  (( ! HERMES_OBSERVER_UNIT_HAD )) || log "rollback Hermes observer unit backup retained"
  log "Hermes observer preparation: ok; unit is disabled and inactive"
}

escape_sed_replacement() { printf '%s' "$1" | sed 's/[\\&|]/\\&/g'; }

render_service_unit() {
  local source="${PROJECT_DIR}/packaging/systemd/espelho-zap@.service"
  if (( DRY_RUN )); then
    log "DRY-RUN: render parameterized service unit (mode 0600)"
    return 0
  fi
  local venv_bin config_file app_root state_dir config_dir temporary
  venv_bin="$(escape_sed_replacement "${VENV}/bin/espelho-zap")"
  config_file="$(escape_sed_replacement "${CONFIG_FILE}")"
  app_root="$(escape_sed_replacement "${APP_ROOT}")"
  state_dir="$(escape_sed_replacement "${STATE_DIR}")"
  config_dir="$(escape_sed_replacement "${CONFIG_DIR}")"
  temporary="${SERVICE_UNIT}.tmp.$$"
  sed \
    -e "s|@ESPELHO_ZAP_BIN@|${venv_bin}|g" \
    -e "s|@CONFIG_FILE@|${config_file}|g" \
    -e "s|@APP_ROOT@|${app_root}|g" \
    -e "s|@STATE_DIR@|${state_dir}|g" \
    -e "s|@CONFIG_DIR@|${config_dir}|g" \
    "${source}" >"${temporary}" || return 1
  chmod 0600 "${temporary}" || return 1
  mv -f -- "${temporary}" "${SERVICE_UNIT}" || return 1
}

install_units() {
  render_service_unit || return 1
  run install -m 0600 -- "${PROJECT_DIR}/packaging/systemd/espelho-zap@.timer" "${TIMER_UNIT}" \
    || return 1
  if command -v systemctl >/dev/null 2>&1; then
    run systemctl --user daemon-reload || return 1
  else
    log "warning: systemctl not found; unit files were installed but not loaded"
  fi
}

backup_existing_units() {
  SERVICE_UNIT_HAD=0
  TIMER_UNIT_HAD=0
  [[ ! -e "${SERVICE_UNIT}" || -f "${SERVICE_UNIT}" ]] \
    || { log "existing service unit is not a regular file" >&2; return 1; }
  [[ ! -e "${TIMER_UNIT}" || -f "${TIMER_UNIT}" ]] \
    || { log "existing timer unit is not a regular file" >&2; return 1; }
  if [[ ! -e "${SERVICE_UNIT}" && ! -e "${TIMER_UNIT}" ]]; then return 0; fi

  run install -d -m 0700 -- "${UNIT_BACKUP_DIR}" || return 1
  if [[ -f "${SERVICE_UNIT}" ]]; then
    run install -m 0600 -- "${SERVICE_UNIT}" "${UNIT_BACKUP_DIR}/espelho-zap@.service" \
      || return 1
    SERVICE_UNIT_HAD=1
  fi
  if [[ -f "${TIMER_UNIT}" ]]; then
    run install -m 0600 -- "${TIMER_UNIT}" "${UNIT_BACKUP_DIR}/espelho-zap@.timer" \
      || return 1
    TIMER_UNIT_HAD=1
  fi
  log "pre-install systemd unit backup: retained"
}

restore_prior_units() {
  local restore_ok=1
  (( ! DRY_RUN )) || return 0
  rm -f -- "${SERVICE_UNIT}" "${TIMER_UNIT}" || restore_ok=0
  if (( SERVICE_UNIT_HAD )); then
    install -m 0600 -- "${UNIT_BACKUP_DIR}/espelho-zap@.service" "${SERVICE_UNIT}" \
      || restore_ok=0
  fi
  if (( TIMER_UNIT_HAD )); then
    install -m 0600 -- "${UNIT_BACKUP_DIR}/espelho-zap@.timer" "${TIMER_UNIT}" \
      || restore_ok=0
  fi
  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload || restore_ok=0
  fi
  if (( restore_ok )); then
    log "rollback: previous systemd units restored"
    return 0
  fi
  log "ERROR: systemd unit files were restored as far as possible; daemon state needs inspection" >&2
  return 1
}

capture_worker_runtime_state() {
  local destination="$1"
  run install -d -m 0700 -- "${destination}" || return 1
  if ! command -v systemctl >/dev/null 2>&1; then
    if (( ! DRY_RUN )); then
      : >"${destination}/active-units.txt"
      : >"${destination}/enabled-timers.txt"
      chmod 0600 -- "${destination}/active-units.txt" "${destination}/enabled-timers.txt"
    fi
    WORKER_STATE_CAPTURED=1
    return 0
  fi
  if (( DRY_RUN )); then
    log "DRY-RUN: capture active Espelho Zap services/timers and enabled timer instances"
    WORKER_STATE_CAPTURED=1
    return 0
  fi
  systemctl --user list-units --all --plain --no-legend --no-pager \
    'espelho-zap@*.service' 'espelho-zap@*.timer' \
    | awk '$3 == "active" {print $1}' >"${destination}/active-units.txt" || return 1
  systemctl --user list-unit-files --no-legend --no-pager 'espelho-zap@*.timer' \
    | awk '$2 == "enabled" {print $1}' >"${destination}/enabled-timers.txt" || return 1
  chmod 0600 -- "${destination}/active-units.txt" "${destination}/enabled-timers.txt" \
    || return 1
  WORKER_STATE_CAPTURED=1
  log "worker unit prior state: captured"
}

quiesce_worker_units() {
  command -v systemctl >/dev/null 2>&1 || return 0
  run systemctl --user stop 'espelho-zap@*.timer' 'espelho-zap@*.service' || return 1
  if (( DRY_RUN )); then
    log "DRY-RUN: verify no Espelho Zap service/timer remains active"
    return 0
  fi
  if systemctl --user list-units --state=active --plain --no-legend --no-pager \
    'espelho-zap@*.service' 'espelho-zap@*.timer' | grep -q .; then
    log "Espelho Zap services/timers did not quiesce" >&2
    return 1
  fi
  log "worker units: quiesced before ledger/config snapshot and activation"
}

disable_captured_worker_timers() {
  local source="$1" unit
  command -v systemctl >/dev/null 2>&1 || return 0
  if (( DRY_RUN )); then
    log "DRY-RUN: disable captured pre-install timer instances so the new install defaults disabled"
    return 0
  fi
  while IFS= read -r unit; do
    [[ -n "${unit}" ]] || continue
    systemctl --user disable "${unit}" || return 1
  done <"${source}/enabled-timers.txt"
}

restore_worker_runtime_state() {
  local source="$1" unit
  (( ! DRY_RUN )) || return 0
  [[ -f "${source}/active-units.txt" && -f "${source}/enabled-timers.txt" ]] \
    || { log "worker unit state snapshot is incomplete" >&2; return 1; }
  command -v systemctl >/dev/null 2>&1 || return 0
  systemctl --user daemon-reload || return 1
  while IFS= read -r unit; do
    [[ -n "${unit}" ]] || continue
    systemctl --user enable "${unit}" || return 1
  done <"${source}/enabled-timers.txt"
  while IFS= read -r unit; do
    [[ -n "${unit}" ]] || continue
    systemctl --user start "${unit}" || return 1
  done <"${source}/active-units.txt"
  log "worker unit running/enabled state: restored"
}

restore_original_units_for_uninstall() {
  local restore_ok=1
  if (( DRY_RUN )); then
    log "DRY-RUN: remove installer units and restore any pre-install unit bytes plus running/enabled state"
    return 0
  fi
  rm -f -- "${SERVICE_UNIT}" "${TIMER_UNIT}" || return 1
  if (( INSTALL_RECORD_FOUND )); then
    if (( ORIGINAL_SERVICE_UNIT_HAD )); then
      install -m 0600 -- "${ORIGINAL_UNIT_BACKUP_DIR}/espelho-zap@.service" "${SERVICE_UNIT}" \
        || restore_ok=0
    fi
    if (( ORIGINAL_TIMER_UNIT_HAD )); then
      install -m 0600 -- "${ORIGINAL_UNIT_BACKUP_DIR}/espelho-zap@.timer" "${TIMER_UNIT}" \
        || restore_ok=0
    fi
  fi
  if command -v systemctl >/dev/null 2>&1; then systemctl --user daemon-reload || restore_ok=0; fi
  if (( restore_ok && INSTALL_RECORD_FOUND )); then
    restore_worker_runtime_state "${ORIGINAL_UNIT_BACKUP_DIR}" || restore_ok=0
  fi
  (( restore_ok )) && log "uninstall: pre-install systemd units and state restored"
  (( restore_ok ))
}

backup_transaction_data() {
  local candidate="$1"
  CONFIG_HAD=0
  LEDGER_HAD=0
  HOOK_HEALTH_HAD=0
  INSTALL_RECORD_HAD=0
  ACTIVATION_RECORD_HAD=0
  if (( DRY_RUN )); then
    log "DRY-RUN: snapshot exact config, ledger, hook health, and ownership records after quiescence"
    TRANSACTION_DATA_SNAPSHOTTED=1
    return 0
  fi
  install -d -m 0700 -- "${TRANSACTION_BACKUP_DIR}" || return 1
  if [[ -f "${INSTALL_RECORD}" ]]; then
    install -m 0600 -- "${INSTALL_RECORD}" "${TRANSACTION_BACKUP_DIR}/install.state" || return 1
    INSTALL_RECORD_HAD=1
  fi
  if [[ -f "${ACTIVATION_RECORD}" ]]; then
    install -m 0600 -- "${ACTIVATION_RECORD}" "${TRANSACTION_BACKUP_DIR}/runtime-activation.state" \
      || return 1
    ACTIVATION_RECORD_HAD=1
  fi
  if [[ -f "${CONFIG_FILE}" ]]; then
    reject_symlink "${CONFIG_FILE}" "configuration file"
    install -m 0600 -- "${CONFIG_FILE}" "${TRANSACTION_BACKUP_DIR}/config.toml" || return 1
    CONFIG_HAD=1
    LEDGER_PATH="$("${candidate}/bin/python" - "${CONFIG_FILE}" "${APP_ROOT}" <<'PY'
from pathlib import Path
import sys
import tomllib

config = Path(sys.argv[1])
app_root = Path(sys.argv[2])
with config.open("rb") as handle:
    raw = tomllib.load(handle)
value = raw.get("paths", {}).get("ledger_path", str(app_root / "mirror.sqlite3"))
if not isinstance(value, str) or not value:
    raise SystemExit("invalid paths.ledger_path")
path = Path(value).expanduser()
if not path.is_absolute():
    path = config.parent / path
print(path.resolve(strict=False), end="")
PY
)" || return 1
  else
    LEDGER_PATH="$(canonical "${APP_ROOT}/mirror.sqlite3")"
  fi
  reject_symlink "${LEDGER_PATH}" "ledger file"
  if [[ -f "${LEDGER_PATH}" ]]; then
    "${candidate}/bin/espelho-zap" --config "${CONFIG_FILE}" \
      backup "${TRANSACTION_BACKUP_DIR}/ledger.sqlite3" || return 1
    LEDGER_HAD=1
  elif [[ -e "${LEDGER_PATH}" ]]; then
    log "configured ledger path is not a regular file" >&2
    return 1
  fi
  if [[ -f "${HOOK_HEALTH_FILE}" ]]; then
    install -m 0600 -- "${HOOK_HEALTH_FILE}" "${TRANSACTION_BACKUP_DIR}/capture-health.json" \
      || return 1
    HOOK_HEALTH_HAD=1
  elif [[ -e "${HOOK_HEALTH_FILE}" ]]; then
    return 1
  fi
  log "transaction snapshot: config, ledger, and hook health retained"
  TRANSACTION_DATA_SNAPSHOTTED=1
}

restore_transaction_data() {
  local restore_ok=1
  (( ! DRY_RUN )) || return 0
  rm -f -- "${CONFIG_FILE}" || restore_ok=0
  if (( CONFIG_HAD )); then
    install -m 0600 -- "${TRANSACTION_BACKUP_DIR}/config.toml" "${CONFIG_FILE}" || restore_ok=0
  fi
  if [[ -n "${LEDGER_PATH}" ]]; then
    rm -f -- "${LEDGER_PATH}" "${LEDGER_PATH}-wal" "${LEDGER_PATH}-shm" || restore_ok=0
    if (( LEDGER_HAD )); then
      install -d -m 0700 -- "$(dirname -- "${LEDGER_PATH}")" || restore_ok=0
      install -m 0600 -- "${TRANSACTION_BACKUP_DIR}/ledger.sqlite3" "${LEDGER_PATH}" || restore_ok=0
    fi
  fi
  rm -f -- "${HOOK_HEALTH_FILE}" || restore_ok=0
  if (( HOOK_HEALTH_HAD )); then
    install -m 0600 -- "${TRANSACTION_BACKUP_DIR}/capture-health.json" "${HOOK_HEALTH_FILE}" \
      || restore_ok=0
  fi
  rm -f -- "${INSTALL_RECORD}" "${ACTIVATION_RECORD}" || restore_ok=0
  if (( INSTALL_RECORD_HAD )); then
    install -m 0600 -- "${TRANSACTION_BACKUP_DIR}/install.state" "${INSTALL_RECORD}" || restore_ok=0
  fi
  if (( ACTIVATION_RECORD_HAD )); then
    install -m 0600 -- "${TRANSACTION_BACKUP_DIR}/runtime-activation.state" "${ACTIVATION_RECORD}" \
      || restore_ok=0
  fi
  (( restore_ok )) && log "rollback: exact pre-transaction config, ledger, and hook health restored"
  (( restore_ok ))
}

backup_sqlite_online() {
  local source="$1" destination="$2" python="${PYTHON_BIN}"
  [[ ! -x "${VENV}/bin/python" ]] || python="${VENV}/bin/python"
  "${python}" - "${source}" "${destination}" <<'PY'
from pathlib import Path
import sqlite3
import sys

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src:
    with sqlite3.connect(destination) as dst:
        src.backup(dst)
        row = dst.execute("PRAGMA quick_check").fetchone()
        if row is None or row[0] != "ok":
            raise SystemExit("runtime state backup quick_check failed")
destination.chmod(0o600)
PY
}

backup_runtime_activation_state() {
  local hermes_config="${RUNTIME_HOME}/config.yaml"
  local hermes_env="${RUNTIME_HOME}/.env"
  local openclaw_config="${RUNTIME_HOME}/openclaw.json"
  local openclaw_db="${RUNTIME_HOME}/state/openclaw.sqlite"
  local openclaw_legacy="${RUNTIME_HOME}/plugins/installs.json"
  (( ENABLE_RUNTIME || RUNTIME_DEACTIVATION )) || return 0
  if (( DRY_RUN )); then
    log "DRY-RUN: snapshot exact selected runtime config and plugin index after gateway quiescence"
    RUNTIME_STATE_SNAPSHOTTED=1
    return 0
  fi

  install -d -m 0700 -- "${RUNTIME_ACTIVATION_BACKUP_DIR}" || return 1
  if [[ "${RUNTIME}" == "hermes" ]]; then
    HERMES_CONFIG_HAD=0
    HERMES_ENV_HAD=0
    reject_symlink "${hermes_config}" "Hermes config"
    reject_symlink "${hermes_env}" "Hermes env file"
    [[ ! -e "${hermes_config}" || -f "${hermes_config}" ]] || return 1
    [[ ! -e "${hermes_env}" || -f "${hermes_env}" ]] || return 1
    if [[ -f "${hermes_config}" ]]; then
      install -m 0600 -- "${hermes_config}" "${RUNTIME_ACTIVATION_BACKUP_DIR}/config.yaml" \
        || return 1
      HERMES_CONFIG_HAD=1
    fi
    if [[ -f "${hermes_env}" ]]; then
      install -m 0600 -- "${hermes_env}" "${RUNTIME_ACTIVATION_BACKUP_DIR}/hermes.env" \
        || return 1
      HERMES_ENV_HAD=1
    fi
  else
    OPENCLAW_CONFIG_HAD=0
    OPENCLAW_STATE_DB_HAD=0
    OPENCLAW_LEGACY_INDEX_HAD=0
    reject_symlink "${openclaw_config}" "OpenClaw config"
    reject_symlink "${openclaw_db}" "OpenClaw state database"
    reject_symlink "${openclaw_legacy}" "OpenClaw legacy plugin index"
    [[ ! -e "${openclaw_config}" || -f "${openclaw_config}" ]] || return 1
    [[ ! -e "${openclaw_db}" || -f "${openclaw_db}" ]] || return 1
    [[ ! -e "${openclaw_legacy}" || -f "${openclaw_legacy}" ]] || return 1
    if [[ -f "${openclaw_config}" ]]; then
      if grep -Eq '\$include' "${openclaw_config}"; then
        log "OpenClaw config uses includes; prepared-only install is safe, but automated enablement cannot guarantee exact rollback" >&2
        return 1
      fi
      install -m 0600 -- "${openclaw_config}" "${RUNTIME_ACTIVATION_BACKUP_DIR}/openclaw.json" \
        || return 1
      OPENCLAW_CONFIG_HAD=1
    fi
    if [[ -f "${openclaw_db}" ]]; then
      backup_sqlite_online "${openclaw_db}" "${RUNTIME_ACTIVATION_BACKUP_DIR}/openclaw.sqlite" \
        || return 1
      OPENCLAW_STATE_DB_HAD=1
    fi
    if [[ -f "${openclaw_legacy}" ]]; then
      install -m 0600 -- "${openclaw_legacy}" "${RUNTIME_ACTIVATION_BACKUP_DIR}/installs.json" \
        || return 1
      OPENCLAW_LEGACY_INDEX_HAD=1
    fi
  fi
  log "runtime activation backup: retained"
  RUNTIME_STATE_SNAPSHOTTED=1
}

stage_active_runtime_plugin() {
  local mode="${1:-move}"
  ACTIVE_PLUGIN_HAD=0
  ACTIVE_PLUGIN_BACKUP="${RUNTIME_ACTIVATION_BACKUP_DIR}/active-plugin"
  ACTIVE_PLUGIN_STAGED=1
  if (( DRY_RUN )); then
    log "DRY-RUN: stage any active managed runtime plugin outside discovery"
    return 0
  fi
  [[ -e "${RUNTIME_PLUGIN_DEST}" ]] || return 0
  runtime_marker_matches "${RUNTIME_PLUGIN_MARKER}" \
    || { log "active runtime plugin is not installer-managed" >&2; return 1; }
  if [[ "${mode}" == "copy" ]]; then
    cp -R -- "${RUNTIME_PLUGIN_DEST}" "${ACTIVE_PLUGIN_BACKUP}" || return 1
  else
    mv -- "${RUNTIME_PLUGIN_DEST}" "${ACTIVE_PLUGIN_BACKUP}" || return 1
  fi
  ACTIVE_PLUGIN_HAD=1
  if [[ "${mode}" == "copy" ]]; then
    log "active runtime plugin: exact rollback copy retained after gateway quiescence"
  else
    log "active runtime plugin: staged outside discovery after gateway quiescence"
  fi
}

restore_active_runtime_plugin() {
  local restore_ok=1
  (( ! DRY_RUN )) || return 0
  if [[ -e "${RUNTIME_PLUGIN_DEST}" ]]; then
    remove_managed_runtime "${RUNTIME_PLUGIN_DEST}" || restore_ok=0
  fi
  if (( ACTIVE_PLUGIN_HAD )); then
    [[ -d "${ACTIVE_PLUGIN_BACKUP}" ]] || return 1
    mv -- "${ACTIVE_PLUGIN_BACKUP}" "${RUNTIME_PLUGIN_DEST}" || restore_ok=0
  fi
  (( restore_ok )) && log "rollback: previous active runtime plugin restored"
  (( restore_ok ))
}

restore_runtime_activation_state() {
  local hermes_config="${RUNTIME_HOME}/config.yaml"
  local hermes_env="${RUNTIME_HOME}/.env"
  local openclaw_config="${RUNTIME_HOME}/openclaw.json"
  local openclaw_db="${RUNTIME_HOME}/state/openclaw.sqlite"
  local openclaw_legacy="${RUNTIME_HOME}/plugins/installs.json"
  (( ENABLE_RUNTIME || RUNTIME_DEACTIVATION )) || return 0

  if [[ "${RUNTIME}" == "hermes" ]]; then
    rm -f -- "${hermes_config}" "${hermes_env}" || return 1
    if (( HERMES_CONFIG_HAD )); then
      install -m 0600 -- "${RUNTIME_ACTIVATION_BACKUP_DIR}/config.yaml" "${hermes_config}" \
        || return 1
    fi
    if (( HERMES_ENV_HAD )); then
      install -m 0600 -- "${RUNTIME_ACTIVATION_BACKUP_DIR}/hermes.env" "${hermes_env}" \
        || return 1
    fi
  else
    rm -f -- "${openclaw_config}" "${openclaw_db}" "${openclaw_db}-wal" "${openclaw_db}-shm" \
      "${openclaw_legacy}" || return 1
    if (( OPENCLAW_CONFIG_HAD )); then
      install -m 0600 -- "${RUNTIME_ACTIVATION_BACKUP_DIR}/openclaw.json" "${openclaw_config}" \
        || return 1
    fi
    if (( OPENCLAW_STATE_DB_HAD )); then
      install -d -m 0700 -- "$(dirname -- "${openclaw_db}")" || return 1
      install -m 0600 -- "${RUNTIME_ACTIVATION_BACKUP_DIR}/openclaw.sqlite" "${openclaw_db}" \
        || return 1
    fi
    if (( OPENCLAW_LEGACY_INDEX_HAD )); then
      install -d -m 0700 -- "$(dirname -- "${openclaw_legacy}")" || return 1
      install -m 0600 -- "${RUNTIME_ACTIVATION_BACKUP_DIR}/installs.json" "${openclaw_legacy}" \
        || return 1
    fi
  fi
  log "rollback: previous runtime config and plugin index restored"
}

runtime_command() {
  if [[ "${RUNTIME}" == "hermes" ]]; then
    env HERMES_HOME="${RUNTIME_HOME}" "${RUNTIME_CLI}" "$@"
  else
    env OPENCLAW_STATE_DIR="${RUNTIME_HOME}" \
      OPENCLAW_CONFIG_PATH="${RUNTIME_HOME}/openclaw.json" \
      "${RUNTIME_CLI}" "$@"
  fi
}

stop_selected_runtime_gateway() {
  (( ENABLE_RUNTIME || RUNTIME_DEACTIVATION )) || return 0
  RUNTIME_GATEWAY_STOP_ATTEMPTED=1
  if (( DRY_RUN )); then
    log "DRY-RUN: stop selected runtime gateway and verify quiescence"
    return 0
  fi
  runtime_command gateway stop || return 1
  if [[ "${RUNTIME}" == "openclaw" ]]; then
    if runtime_command gateway status --deep --require-rpc >/dev/null 2>&1; then
      log "OpenClaw gateway remained reachable after stop" >&2
      return 1
    fi
  elif runtime_command gateway status >/dev/null 2>&1; then
    log "Hermes gateway still reports running after stop" >&2
    return 1
  fi
  log "runtime gateway: quiesced before config/index/plugin snapshot"
}

capture_selected_runtime_gateway_state() {
  local status_report="${RUNTIME_ACTIVATION_BACKUP_DIR}/gateway-before.txt"
  RUNTIME_GATEWAY_WAS_RUNNING=0
  if (( DRY_RUN )); then
    log "DRY-RUN: capture selected runtime gateway running state"
    return 0
  fi
  if runtime_command gateway status >"${status_report}" 2>&1; then
    RUNTIME_GATEWAY_WAS_RUNNING=1
  fi
  chmod 0600 -- "${status_report}" 2>/dev/null || true
  log "runtime gateway prior state: $([[ ${RUNTIME_GATEWAY_WAS_RUNNING} -eq 1 ]] && printf running || printf stopped)"
}

restart_selected_runtime_gateway_after_rollback() {
  (( RUNTIME_GATEWAY_STOP_ATTEMPTED )) || return 0
  if (( ! RUNTIME_GATEWAY_WAS_RUNNING )); then
    runtime_command gateway stop >/dev/null 2>&1 || true
    log "rollback: selected runtime gateway left stopped as before"
    return 0
  fi
  if runtime_command gateway restart; then
    log "rollback: selected runtime gateway restarted with prior state"
    return 0
  fi
  log "ERROR: prior runtime state was restored but its gateway restart needs operator attention" >&2
  return 1
}

runtime_env_json() {
  local media_roots
  media_roots="$(joined_media_roots)"
  "${VENV}/bin/python" - \
    "$(canonical "${VENV}/bin/espelho-zap")" "$(canonical "${CONFIG_FILE}")" \
    "${SOURCE_PROFILE_ID}" "$(canonical "${HOOK_HEALTH_FILE}")" "${media_roots}" <<'PY'
import json
import sys

cli, config, profile, health, media = sys.argv[1:]
print(json.dumps({
    "ESPELHO_ZAP_CLI": cli,
    "ESPELHO_ZAP_CONFIG": config,
    "ESPELHO_ZAP_SOURCE_PROFILE_ID": profile,
    "ESPELHO_ZAP_HOOK_HEALTH_FILE": health,
    "ESPELHO_ZAP_PRIVACY_SCOPE": "owner_private",
    "ESPELHO_ZAP_MEDIA_ROOTS": media,
    "ESPELHO_ZAP_MAX_HOOK_BYTES": "1048576",
    "ESPELHO_ZAP_HOOK_TIMEOUT_SECONDS": "15",
    "ESPELHO_ZAP_HOOK_TIMEOUT_MS": "15000",
}, separators=(",", ":")))
PY
}

write_hermes_runtime_env() {
  local media_roots
  media_roots="$(joined_media_roots)"
  "${VENV}/bin/python" - "${RUNTIME_HOME}/.env" \
    "$(canonical "${VENV}/bin/espelho-zap")" "$(canonical "${CONFIG_FILE}")" \
    "${SOURCE_PROFILE_ID}" "$(canonical "${HOOK_HEALTH_FILE}")" "${media_roots}" <<'PY'
import json
import os
from pathlib import Path
import re
import sys
import tempfile

destination = Path(sys.argv[1])
cli, config, profile, health, media = sys.argv[2:]
begin = "# BEGIN ESPELHO_ZAP_PORTABLE_MANAGED"
end = "# END ESPELHO_ZAP_PORTABLE_MANAGED"
values = {
    "ESPELHO_ZAP_CLI": cli,
    "ESPELHO_ZAP_CONFIG": config,
    "ESPELHO_ZAP_SOURCE_PROFILE_ID": profile,
    "ESPELHO_ZAP_HOOK_HEALTH_FILE": health,
    "ESPELHO_ZAP_PRIVACY_SCOPE": "owner_private",
    "ESPELHO_ZAP_MEDIA_ROOTS": media,
    "ESPELHO_ZAP_MAX_HOOK_BYTES": "1048576",
    "ESPELHO_ZAP_HOOK_TIMEOUT_SECONDS": "15",
}
original = destination.read_text(encoding="utf-8") if destination.exists() else ""
if (original.count(begin), original.count(end)) not in ((0, 0), (1, 1)):
    raise SystemExit("invalid managed Espelho Zap block in Hermes .env")
if begin not in original:
    for key in values:
        if re.search(rf"(?m)^\s*(?:export\s+)?{re.escape(key)}\s*=", original):
            raise SystemExit(f"unmanaged {key} already exists in Hermes .env")
    retained = original
else:
    start = original.index(begin)
    finish = original.index(end, start) + len(end)
    if original.find(begin, start + len(begin)) != -1 or original.find(end, finish) != -1:
        raise SystemExit("duplicate managed Espelho Zap block in Hermes .env")
    while finish < len(original) and original[finish] in "\r\n":
        finish += 1
    retained = original[:start] + original[finish:]

if retained and not retained.endswith("\n"):
    retained += "\n"
block = [begin]
block.extend(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in values.items())
block.append(end)
updated = retained + "\n".join(block) + "\n"

destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
fd, temporary = tempfile.mkstemp(prefix=".espelho-zap.env.", dir=destination.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(updated)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)
    directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
}

remove_hermes_runtime_env() {
  "${VENV}/bin/python" - "${RUNTIME_HOME}/.env" <<'PY'
import os
from pathlib import Path
import sys
import tempfile

destination = Path(sys.argv[1])
if not destination.exists():
    raise SystemExit(0)
if destination.is_symlink() or not destination.is_file():
    raise SystemExit("Hermes env path is unsafe")
original = destination.read_text(encoding="utf-8")
begin = "# BEGIN ESPELHO_ZAP_PORTABLE_MANAGED"
end = "# END ESPELHO_ZAP_PORTABLE_MANAGED"
if (original.count(begin), original.count(end)) != (1, 1):
    raise SystemExit("managed Espelho Zap block is missing or ambiguous")
start = original.index(begin)
finish = original.index(end, start) + len(end)
while finish < len(original) and original[finish] in "\r\n":
    finish += 1
updated = original[:start] + original[finish:]
if not updated:
    destination.unlink()
    raise SystemExit(0)
fd, temporary = tempfile.mkstemp(prefix=".espelho-zap.env.remove.", dir=destination.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(updated)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)
    directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
}

hermes_registration_canary() {
  local media_roots
  media_roots="$(joined_media_roots)"
  env \
    ESPELHO_ZAP_CLI="$(canonical "${VENV}/bin/espelho-zap")" \
    ESPELHO_ZAP_CONFIG="$(canonical "${CONFIG_FILE}")" \
    ESPELHO_ZAP_SOURCE_PROFILE_ID="${SOURCE_PROFILE_ID}" \
    ESPELHO_ZAP_HOOK_HEALTH_FILE="$(canonical "${HOOK_HEALTH_FILE}")" \
    ESPELHO_ZAP_PRIVACY_SCOPE=owner_private \
    ESPELHO_ZAP_MEDIA_ROOTS="${media_roots}" \
    "${PYTHON_BIN}" -I -S - "${RUNTIME_PLUGIN_DEST}/__init__.py" <<'PY'
import importlib.util
from pathlib import Path
import sys

path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("espelho_zap_portable_plugin_canary", path)
if spec is None or spec.loader is None:
    raise SystemExit(1)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

class CanaryContext:
    def __init__(self):
        self.hooks = []
    def register_hook(self, name, callback):
        if callable(callback):
            self.hooks.append(name)

context = CanaryContext()
module.register(context)
if context.hooks != ["pre_gateway_dispatch"]:
    raise SystemExit("Hermes hook registration canary failed")
PY
}

enable_selected_runtime() {
  local media_roots env_json discovery_report runtime_report doctor_report hook_before
  (( ENABLE_RUNTIME )) || return 0
  if (( DRY_RUN )); then
    log "DRY-RUN: back up selected runtime config/index, stop its managed gateway, persist the secret-free plugin environment, and enable the plugin"
    log "DRY-RUN: restart the selected gateway and verify discovery plus runtime hook loading"
    return 0
  fi

  media_roots="$(joined_media_roots)"
  discovery_report="${RUNTIME_ACTIVATION_BACKUP_DIR}/discovery.txt"
  runtime_report="${RUNTIME_ACTIVATION_BACKUP_DIR}/runtime-canary.json"
  doctor_report="${RUNTIME_ACTIVATION_BACKUP_DIR}/plugin-doctor.json"
  if [[ "${RUNTIME}" == "hermes" ]]; then
    install -d -m 0700 -- "${RUNTIME_PLUGIN_ROOT}" || return 1
    cp -R -- "${RUNTIME_STAGING_DEST}" "${RUNTIME_PLUGIN_DEST}" || return 1
    write_runtime_marker "${RUNTIME_PLUGIN_MARKER}" enabled || return 1
    write_hermes_runtime_env || return 1
    runtime_command plugins enable espelho-zap-portable || return 1
    runtime_command plugins list >"${discovery_report}" || return 1
    grep -Fq 'espelho-zap-portable' "${discovery_report}" || return 1
    hermes_registration_canary || return 1
    # Hermes can fall back to a secondary config when YAML is malformed.  Do
    # not restart into that fallback: validate the effective profile first and
    # let the transaction rollback if the native config is invalid.
    runtime_command config validate || return 1
    runtime_command gateway restart || return 1
    runtime_command gateway status >"${runtime_report}" || return 1
  else
    if (( ! ACTIVE_RUNTIME_FOUND )); then
      if hook_before="$(runtime_command config get \
        channels.whatsapp.pluginHooks.messageReceived --json 2>/dev/null)" \
        && [[ "${hook_before//[[:space:]]/}" == "true" ]]; then
        OPENCLAW_HOOK_CHANGED=0
      else
        OPENCLAW_HOOK_CHANGED=1
      fi
    fi
    runtime_command plugins install "${RUNTIME_STAGING_DEST}" --force || return 1
    write_runtime_marker "${RUNTIME_PLUGIN_MARKER}" enabled || return 1
    write_runtime_env_template "${RUNTIME_ENV_TEMPLATE}" || return 1
    find "${RUNTIME_PLUGIN_DEST}" -type d -exec chmod 0700 {} + || return 1
    find "${RUNTIME_PLUGIN_DEST}" -type f -exec chmod 0600 {} + || return 1
    validate_runtime_candidate "${RUNTIME_PLUGIN_DEST}" || return 1
    env_json="$(runtime_env_json)" || return 1
    runtime_command config set channels.whatsapp.pluginHooks.messageReceived true --strict-json \
      || return 1
    runtime_command config set plugins.entries.espelho-zap-portable.env "${env_json}" --strict-json \
      || return 1
    runtime_command config set \
      plugins.entries.espelho-zap-portable.hooks.allowConversationAccess true --strict-json \
      || return 1
    runtime_command plugins enable espelho-zap-portable || return 1
    runtime_command config validate || return 1
    runtime_command plugins doctor --json >"${doctor_report}" || return 1
    runtime_command gateway restart || return 1
    runtime_command plugins inspect espelho-zap-portable --runtime --json >"${runtime_report}" \
      || return 1
    grep -Fq 'espelho-zap-portable' "${runtime_report}" \
      && grep -Fq 'message_received' "${runtime_report}" \
      && grep -Fq 'before_agent_reply' "${runtime_report}" \
      && grep -Fq 'message_sending' "${runtime_report}" \
      && grep -Fq 'reply_payload_sending' "${runtime_report}" || return 1
    runtime_command gateway status --deep --require-rpc >"${discovery_report}" || return 1
  fi
  chmod 0600 -- "${discovery_report}" "${runtime_report}" 2>/dev/null || true
  [[ ! -e "${doctor_report}" ]] || chmod 0600 -- "${doctor_report}" || return 1
  write_runtime_marker "${RUNTIME_PLUGIN_MARKER}" enabled || return 1
  write_activation_record || return 1
  log "runtime activation: enabled, gateway restarted, and hook load canary passed"
}

restore_openclaw_owned_config_from_baseline() {
  local baseline="${ACTIVATION_BASELINE_DIR}/openclaw.json"
  "${VENV}/bin/python" - "${RUNTIME_HOME}/openclaw.json" "${baseline}" <<'PY'
import json
import os
from pathlib import Path
import sys
import tempfile

current_path = Path(sys.argv[1])
baseline_path = Path(sys.argv[2])
current = json.loads(current_path.read_text()) if current_path.exists() else {}
baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else {}
plugin_id = "espelho-zap-portable"

current_plugins = current.setdefault("plugins", {})
baseline_plugins = baseline.get("plugins", {}) if isinstance(baseline.get("plugins", {}), dict) else {}
for key in ("allow", "deny"):
    values = current_plugins.setdefault(key, [])
    if not isinstance(values, list):
        raise SystemExit(f"plugins.{key} is not a list")
    values[:] = [value for value in values if value != plugin_id]
    baseline_values = baseline_plugins.get(key, [])
    if isinstance(baseline_values, list) and plugin_id in baseline_values:
        values.append(plugin_id)

entries = current_plugins.setdefault("entries", {})
if not isinstance(entries, dict):
    raise SystemExit("plugins.entries is not an object")
baseline_entries = baseline_plugins.get("entries", {})
if isinstance(baseline_entries, dict) and plugin_id in baseline_entries:
    entries[plugin_id] = baseline_entries[plugin_id]
else:
    entries.pop(plugin_id, None)

hook_path = ("channels", "whatsapp", "pluginHooks")
baseline_cursor = baseline
baseline_hook_present = True
for key in hook_path:
    if not isinstance(baseline_cursor, dict) or key not in baseline_cursor:
        baseline_hook_present = False
        break
    baseline_cursor = baseline_cursor[key]
baseline_hook_present = baseline_hook_present and isinstance(baseline_cursor, dict) and "messageReceived" in baseline_cursor

cursor = current
for key in hook_path:
    child = cursor.get(key)
    if not isinstance(child, dict):
        child = {}
        cursor[key] = child
    cursor = child
if baseline_hook_present:
    cursor["messageReceived"] = baseline_cursor["messageReceived"]
else:
    cursor.pop("messageReceived", None)

current_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
fd, temporary = tempfile.mkstemp(prefix=".openclaw.espelho-zap.remove.", dir=current_path.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(current, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, current_path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
}

deactivate_selected_runtime() {
  local report="${RUNTIME_ACTIVATION_BACKUP_DIR}/deactivation-canary.json"
  (( RUNTIME_DEACTIVATION )) || return 0
  if (( DRY_RUN )); then
    log "DRY-RUN: officially disable/uninstall the selected plugin, remove only installer-owned env/config, restore prior gateway state, and prove absence"
    return 0
  fi
  if [[ "${RUNTIME}" == "hermes" ]]; then
    runtime_command plugins disable espelho-zap-portable || return 1
    remove_hermes_runtime_env || return 1
    if [[ -e "${RUNTIME_PLUGIN_DEST}" ]]; then remove_managed_runtime "${RUNTIME_PLUGIN_DEST}" || return 1; fi
  else
    runtime_command plugins uninstall espelho-zap-portable --force || return 1
    if (( ACTIVE_OPENCLAW_HOOK_CHANGED )); then
      runtime_command config unset channels.whatsapp.pluginHooks.messageReceived || return 1
    fi
    restore_openclaw_owned_config_from_baseline || return 1
    runtime_command config validate || return 1
  fi

  if (( RUNTIME_GATEWAY_WAS_RUNNING )); then
    runtime_command gateway restart || return 1
  else
    runtime_command gateway stop >/dev/null 2>&1 || return 1
  fi
  if [[ "${RUNTIME}" == "hermes" ]]; then
    runtime_command plugins list >"${report}" || return 1
  else
    runtime_command plugins list --json >"${report}" || return 1
  fi
  if grep -Fq 'espelho-zap-portable' "${report}"; then return 1; fi
  if (( RUNTIME_GATEWAY_WAS_RUNNING )); then
    if [[ "${RUNTIME}" == "openclaw" ]]; then
      runtime_command gateway status --deep --require-rpc >/dev/null || return 1
    else
      runtime_command gateway status >/dev/null || return 1
    fi
  fi
  chmod 0600 -- "${report}" || return 1
  log "runtime deactivation: official removal, gateway state restore, and absence canary passed"
}

configure_worker_settings() {
  if (( DRY_RUN )); then
    log "DRY-RUN: persist worker.profile_id and effective source_media_roots atomically (mode 0600)"
    return 0
  fi
  "${VENV}/bin/python" - "${CONFIG_FILE}" "${SOURCE_PROFILE_ID}" "${MEDIA_ROOTS[@]}" <<'PY'
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import tomllib

config_path = Path(sys.argv[1])
profile_id = sys.argv[2]
roots = sys.argv[3:]
if config_path.is_symlink() or not config_path.is_file():
    raise SystemExit("config must be a regular non-symlink file")

original = config_path.read_text(encoding="utf-8")
tomllib.loads(original)
newline = "\r\n" if "\r\n" in original else "\n"
lines = original.splitlines(keepends=True)
section = None
worker_header = None
worker_end = len(lines)
matches = []
profile_matches = []
section_re = re.compile(r"^\s*\[([^]]+)]\s*(?:#.*)?$")
root_re = re.compile(r"^\s*source_media_roots\s*=")
profile_re = re.compile(r"^\s*profile_id\s*=")

for index, line in enumerate(lines):
    stripped = line.rstrip("\r\n")
    section_match = section_re.match(stripped)
    if section_match:
        if section == "worker" and worker_end == len(lines):
            worker_end = index
        section = section_match.group(1).strip()
        if section == "worker":
            if worker_header is not None:
                raise SystemExit("duplicate [worker] section")
            worker_header = index
        continue
    if section == "worker" and root_re.match(stripped):
        matches.append(index)
    if section == "worker" and profile_re.match(stripped):
        profile_matches.append(index)

if len(matches) > 1 or len(profile_matches) > 1:
    raise SystemExit("duplicate worker settings")
assignment = "source_media_roots = " + json.dumps(roots, ensure_ascii=False) + newline
profile_assignment = "profile_id = " + json.dumps(profile_id, ensure_ascii=False) + newline
if matches:
    lines[matches[0]] = assignment
elif worker_header is not None:
    lines.insert(worker_end, assignment)
else:
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += newline
    if lines and lines[-1].strip():
        lines.append(newline)
    lines.extend(("[worker]" + newline, assignment))

# Re-scan because inserting source_media_roots may have shifted indexes.
section = None
worker_header = None
worker_end = len(lines)
profile_matches = []
for index, line in enumerate(lines):
    stripped = line.rstrip("\r\n")
    section_match = section_re.match(stripped)
    if section_match:
        if section == "worker" and worker_end == len(lines):
            worker_end = index
        section = section_match.group(1).strip()
        if section == "worker":
            worker_header = index
        continue
    if section == "worker" and profile_re.match(stripped):
        profile_matches.append(index)
if len(profile_matches) > 1:
    raise SystemExit("duplicate worker.profile_id")
if profile_matches:
    lines[profile_matches[0]] = profile_assignment
elif worker_header is not None:
    lines.insert(worker_end, profile_assignment)

updated = "".join(lines)
parsed = tomllib.loads(updated)
worker = parsed.get("worker", {})
if worker.get("source_media_roots") != roots or worker.get("profile_id") != profile_id:
    raise SystemExit("worker settings verification failed")

fd, temporary = tempfile.mkstemp(prefix=".config.toml.", dir=config_path.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(updated)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, config_path)
    directory_fd = os.open(config_path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
}

initialize_hook_health_file() {
  if (( DRY_RUN )); then
    log "DRY-RUN: initialize aggregate hook health file if absent (mode 0600)"
    return 0
  fi
  "${VENV}/bin/python" - "${HOOK_HEALTH_FILE}" <<'PY'
import json
import os
from pathlib import Path
import sys
import tempfile

path = Path(sys.argv[1])
if path.is_symlink():
    raise SystemExit("hook health file must not be a symlink")
path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
os.chmod(path.parent, 0o700)
if path.exists():
    if not path.is_file():
        raise SystemExit("hook health path must be a regular file")
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise SystemExit("hook health file has an unsupported schema")
    path.chmod(0o600)
    raise SystemExit(0)

value = {
    "schema_version": 1,
    "successes": 0,
    "failures": {},
    "last_success_at": "",
    "last_failure_at": "",
    "last_error_code": "",
}
fd, temporary = tempfile.mkstemp(prefix=".capture-health.", dir=path.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    path.chmod(0o600)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
}

initialize_runtime() {
  run "${VENV}/bin/espelho-zap" --config "${CONFIG_FILE}" init \
    --data-dir "${APP_ROOT}" --minimum-free-bytes "${MIN_FREE_BYTES}" || return 1
  if [[ -f "${CONFIG_FILE}" ]]; then run chmod 0600 -- "${CONFIG_FILE}" || return 1; fi
  if [[ -f "${CONFIG_DIR}/telegram.token" ]]; then
    run chmod 0600 -- "${CONFIG_DIR}/telegram.token" || return 1
  fi
  configure_worker_settings || return 1
  initialize_hook_health_file || return 1
  run "${VENV}/bin/espelho-zap" --config "${CONFIG_FILE}" doctor --allow-missing-token \
    || return 1
}

rollback_active() {
  local venv_backup="$1" venv_had="$2" skill_backup="$3" skill_had="$4"
  local runtime_backup="$5" runtime_had="$6"
  remove_managed_venv "${VENV}"
  if (( venv_had )); then mv -- "${venv_backup}" "${VENV}"; fi
  if [[ -e "${SKILL_DEST}" ]]; then remove_managed_skill "${SKILL_DEST}"; fi
  if (( skill_had )); then mv -- "${skill_backup}" "${SKILL_DEST}"; fi
  if [[ "${RUNTIME}" != "none" ]]; then
    if [[ -e "${RUNTIME_STAGING_DEST}" ]]; then
      remove_managed_runtime "${RUNTIME_STAGING_DEST}"
    fi
    if (( runtime_had )); then mv -- "${runtime_backup}" "${RUNTIME_STAGING_DEST}"; fi
  fi
  log "rollback: previous managed application, skill, and selected runtime plugin restored"
}

cleanup_install_candidates() {
  local venv_candidate="$1" skill_candidate="$2" runtime_candidate="$3"
  [[ ! -e "${venv_candidate}" ]] || remove_managed_venv "${venv_candidate}"
  [[ ! -e "${skill_candidate}" ]] || remove_managed_skill "${skill_candidate}"
  if [[ -n "${runtime_candidate}" && -e "${runtime_candidate}" ]]; then
    remove_managed_runtime "${runtime_candidate}"
  fi
}

rollback_install_transaction() {
  local venv_backup="$1" venv_had="$2" skill_backup="$3" skill_had="$4"
  local runtime_backup="$5" runtime_had="$6" rollback_ok=1
  if (( ENABLE_RUNTIME )); then runtime_command gateway stop >/dev/null 2>&1 || true; fi
  if (( RUNTIME_STATE_SNAPSHOTTED )); then restore_runtime_activation_state || rollback_ok=0; fi
  if (( ACTIVE_PLUGIN_STAGED )); then restore_active_runtime_plugin || rollback_ok=0; fi
  restore_prior_units || rollback_ok=0
  rollback_active \
    "${venv_backup}" "${venv_had}" "${skill_backup}" "${skill_had}" \
    "${runtime_backup}" "${runtime_had}" || rollback_ok=0
  if (( TRANSACTION_DATA_SNAPSHOTTED )); then restore_transaction_data || rollback_ok=0; fi
  if (( ENABLE_RUNTIME )); then restart_selected_runtime_gateway_after_rollback || rollback_ok=0; fi
  if (( WORKER_STATE_CAPTURED )); then restore_worker_runtime_state "${UNIT_BACKUP_DIR}" || rollback_ok=0; fi
  (( rollback_ok ))
}

rollback_before_candidate_activation() {
  local venv_backup="$1" venv_had="$2" skill_backup="$3" skill_had="$4"
  local runtime_backup="$5" runtime_had="$6" rollback_ok=1
  if (( runtime_had )); then mv -- "${runtime_backup}" "${RUNTIME_STAGING_DEST}" || rollback_ok=0; fi
  if (( skill_had )); then mv -- "${skill_backup}" "${SKILL_DEST}" || rollback_ok=0; fi
  if (( venv_had )); then mv -- "${venv_backup}" "${VENV}" || rollback_ok=0; fi
  if (( RUNTIME_STATE_SNAPSHOTTED )); then restore_runtime_activation_state || rollback_ok=0; fi
  if (( ACTIVE_PLUGIN_STAGED )); then restore_active_runtime_plugin || rollback_ok=0; fi
  restore_prior_units || rollback_ok=0
  if (( TRANSACTION_DATA_SNAPSHOTTED )); then restore_transaction_data || rollback_ok=0; fi
  if (( ENABLE_RUNTIME )); then restart_selected_runtime_gateway_after_rollback || rollback_ok=0; fi
  restore_worker_runtime_state "${UNIT_BACKUP_DIR}" || rollback_ok=0
  (( rollback_ok ))
}

install_or_upgrade() {
  private_directories
  local stamp venv_candidate venv_backup skill_candidate skill_backup venv_had skill_had
  local runtime_candidate runtime_backup runtime_had activation_ok failure_message
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  venv_candidate="${APP_ROOT}/.venv.new-${stamp}-$$"
  venv_backup="${APP_ROOT}/venv.backup-${stamp}-$$"
  skill_candidate="${SKILL_ROOT}/.espelho-zap-portable.new-${stamp}-$$"
  skill_backup="${SKILL_ROOT}/espelho-zap-portable.backup-${stamp}-$$"
  runtime_candidate=""
  runtime_backup=""
  UNIT_BACKUP_DIR="${BACKUP_DIR}/unit-backups/${stamp}-$$"
  TRANSACTION_BACKUP_DIR="${BACKUP_DIR}/transactions/${stamp}-$$"
  if [[ "${RUNTIME}" != "none" ]]; then
    runtime_candidate="${RUNTIME_STAGING_ROOT}/.espelho-zap-portable.new-${stamp}-$$"
    runtime_backup="${RUNTIME_STAGING_BACKUP_ROOT}/espelho-zap-portable-${stamp}-$$"
    RUNTIME_ACTIVATION_BACKUP_DIR="${RUNTIME_ACTIVATION_BACKUP_ROOT}/activation-${stamp}-$$"
  fi
  venv_had=0
  skill_had=0
  runtime_had=0

  install_candidate "${venv_candidate}" || {
    cleanup_install_candidates "${venv_candidate}" "${skill_candidate}" "${runtime_candidate}"
    die "candidate installation failed; active installation was not changed"
  }
  prepare_skill_candidate "${skill_candidate}" || {
    cleanup_install_candidates "${venv_candidate}" "${skill_candidate}" "${runtime_candidate}"
    die "skill candidate failed; active installation was not changed"
  }
  if [[ "${RUNTIME}" != "none" ]]; then
    prepare_runtime_candidate "${runtime_candidate}" || {
      cleanup_install_candidates "${venv_candidate}" "${skill_candidate}" "${runtime_candidate}"
      die "runtime staging candidate failed; active installation was not changed"
    }
  fi

  capture_worker_runtime_state "${UNIT_BACKUP_DIR}" || {
    cleanup_install_candidates "${venv_candidate}" "${skill_candidate}" "${runtime_candidate}"
    die "worker unit state could not be captured; active installation was not changed"
  }
  quiesce_worker_units || {
    restore_worker_runtime_state "${UNIT_BACKUP_DIR}" || true
    cleanup_install_candidates "${venv_candidate}" "${skill_candidate}" "${runtime_candidate}"
    die "worker units could not be quiesced; active installation was not changed"
  }
  if (( ! INSTALL_RECORD_FOUND )); then
    disable_captured_worker_timers "${UNIT_BACKUP_DIR}" || {
      restore_worker_runtime_state "${UNIT_BACKUP_DIR}" || true
      cleanup_install_candidates "${venv_candidate}" "${skill_candidate}" "${runtime_candidate}"
      die "pre-existing timer instances could not be disabled safely"
    }
  fi

  if (( ENABLE_RUNTIME )); then
    run install -d -m 0700 -- "${RUNTIME_ACTIVATION_BACKUP_DIR}" || {
      restore_worker_runtime_state "${UNIT_BACKUP_DIR}" || true
      cleanup_install_candidates "${venv_candidate}" "${skill_candidate}" "${runtime_candidate}"
      die "runtime activation snapshot directory could not be created"
    }
    capture_selected_runtime_gateway_state
    if ! stop_selected_runtime_gateway \
      || ! backup_runtime_activation_state \
      || ! stage_active_runtime_plugin; then
      if (( ACTIVE_PLUGIN_STAGED )); then restore_active_runtime_plugin || true; fi
      if (( RUNTIME_STATE_SNAPSHOTTED )); then restore_runtime_activation_state || true; fi
      restart_selected_runtime_gateway_after_rollback || true
      restore_worker_runtime_state "${UNIT_BACKUP_DIR}" || true
      cleanup_install_candidates "${venv_candidate}" "${skill_candidate}" "${runtime_candidate}"
      die "selected runtime could not be quiesced and snapshotted; active installation was not changed"
    fi
    if (( ! ACTIVE_RUNTIME_FOUND )); then
      ACTIVATION_BASELINE_DIR="${RUNTIME_ACTIVATION_BACKUP_DIR}"
    fi
  fi

  if ! backup_existing_units || ! backup_transaction_data "${venv_candidate}"; then
    if (( ACTIVE_PLUGIN_STAGED )); then restore_active_runtime_plugin || true; fi
    if (( RUNTIME_STATE_SNAPSHOTTED )); then restore_runtime_activation_state || true; fi
    if (( ENABLE_RUNTIME )); then restart_selected_runtime_gateway_after_rollback || true; fi
    restore_worker_runtime_state "${UNIT_BACKUP_DIR}" || true
    cleanup_install_candidates "${venv_candidate}" "${skill_candidate}" "${runtime_candidate}"
    die "pre-activation unit/config/ledger snapshot failed; active installation was not changed"
  fi
  if (( ! INSTALL_RECORD_FOUND )); then
    ORIGINAL_UNIT_BACKUP_DIR="${UNIT_BACKUP_DIR}"
    ORIGINAL_SERVICE_UNIT_HAD="${SERVICE_UNIT_HAD}"
    ORIGINAL_TIMER_UNIT_HAD="${TIMER_UNIT_HAD}"
  fi

  if (( DRY_RUN )); then
    [[ ! -d "${VENV}" ]] || quote_command mv -- "${VENV}" "${venv_backup}"
    [[ ! -d "${SKILL_DEST}" ]] || quote_command mv -- "${SKILL_DEST}" "${skill_backup}"
    if [[ "${RUNTIME}" != "none" ]]; then
      [[ ! -d "${RUNTIME_STAGING_DEST}" ]] \
        || quote_command mv -- "${RUNTIME_STAGING_DEST}" "${runtime_backup}"
    fi
    quote_command mv -- "${venv_candidate}" "${VENV}"
    quote_command mv -- "${skill_candidate}" "${SKILL_DEST}"
    if [[ "${RUNTIME}" != "none" ]]; then quote_command mv -- "${runtime_candidate}" "${RUNTIME_STAGING_DEST}"; fi
    initialize_runtime
    install_units
    enable_selected_runtime
    log "DRY-RUN: persist install ownership record only after all canaries pass"
    log "dry-run complete; no mutation was performed"
    return 0
  fi

  if [[ -d "${VENV}" ]]; then
    if mv -- "${VENV}" "${venv_backup}"; then venv_had=1; else failure_message="venv staging failed"; fi
  fi
  if [[ -z "${failure_message:-}" && -d "${SKILL_DEST}" ]]; then
    mv -- "${SKILL_DEST}" "${skill_backup}" || failure_message="skill staging failed"
    [[ -n "${failure_message:-}" ]] || skill_had=1
  fi
  if [[ -z "${failure_message:-}" && "${RUNTIME}" != "none" && -d "${RUNTIME_STAGING_DEST}" ]]; then
    mv -- "${RUNTIME_STAGING_DEST}" "${runtime_backup}" || failure_message="runtime staging failed"
    [[ -n "${failure_message:-}" ]] || runtime_had=1
  fi
  if [[ -n "${failure_message:-}" ]]; then
    cleanup_install_candidates "${venv_candidate}" "${skill_candidate}" "${runtime_candidate}"
    rollback_before_candidate_activation \
      "${venv_backup}" "${venv_had}" "${skill_backup}" "${skill_had}" \
      "${runtime_backup}" "${runtime_had}" || true
    die "${failure_message}; prior installation was restored"
  fi

  activation_ok=1
  mv -- "${venv_candidate}" "${VENV}" || activation_ok=0
  if (( activation_ok )); then
    relocate_candidate_venv "${venv_candidate}" "${VENV}" || activation_ok=0
  fi
  if (( activation_ok )); then mv -- "${skill_candidate}" "${SKILL_DEST}" || activation_ok=0; fi
  if (( activation_ok )) && [[ "${RUNTIME}" != "none" ]]; then
    mv -- "${runtime_candidate}" "${RUNTIME_STAGING_DEST}" || activation_ok=0
  fi
  if (( ! activation_ok )); then
    cleanup_install_candidates "${venv_candidate}" "${skill_candidate}" "${runtime_candidate}"
    rollback_install_transaction \
      "${venv_backup}" "${venv_had}" "${skill_backup}" "${skill_had}" \
      "${runtime_backup}" "${runtime_had}" || true
    die "activation failed; prior installation and persistent state were restored"
  fi

  failure_message=""
  initialize_runtime || failure_message="post-install initialization failed"
  if [[ -z "${failure_message}" ]]; then install_units || failure_message="systemd unit installation failed"; fi
  if [[ -z "${failure_message}" && "${RUNTIME}" != "none" ]]; then
    validate_runtime_candidate "${RUNTIME_STAGING_DEST}" || failure_message="runtime staging validation failed"
  fi
  if [[ -z "${failure_message}" && ${ENABLE_RUNTIME} -eq 1 ]]; then
    enable_selected_runtime || failure_message="runtime enable/load canary failed"
  fi
  if [[ -z "${failure_message}" ]]; then write_install_record || failure_message="install ownership record failed"; fi
  if [[ -z "${failure_message}" && ${INSTALL_RECORD_FOUND} -eq 1 ]]; then
    restore_worker_runtime_state "${UNIT_BACKUP_DIR}" || failure_message="worker running state restore failed"
  fi
  if [[ -n "${failure_message}" ]]; then
    rollback_install_transaction \
      "${venv_backup}" "${venv_had}" "${skill_backup}" "${skill_had}" \
      "${runtime_backup}" "${runtime_had}" || \
      log "ERROR: automatic rollback was incomplete; inspect ${TRANSACTION_BACKUP_DIR}" >&2
    die "${failure_message}; prior application, config, ledger, units, and runtime state were rolled back"
  fi

  log "installation: ok"
  (( ! venv_had )) || log "rollback venv backup retained"
  (( ! skill_had )) || log "rollback skill backup retained"
  (( ! runtime_had )) || log "rollback runtime staging backup retained"
  (( ! ENABLE_RUNTIME )) || log "rollback runtime activation backup retained"
  log "transaction snapshot retained: ${TRANSACTION_BACKUP_DIR}"
  log "skill: installed in the AgentSkills root"
  if [[ "${RUNTIME}" == "none" ]]; then
    log "runtime plugin: not requested"
  elif (( ENABLE_RUNTIME )); then
    log "runtime plugin: installed, enabled, and runtime-inspect verified for ${RUNTIME}"
  else
    log "runtime plugin: prepared safely outside every runtime discovery root"
    log "runtime activation: config, discovery roots, plugin enablement, hook consent, and restart were not touched"
  fi
  if ((${#MEDIA_ROOTS[@]})); then
    log "runtime media: effective approved roots persisted in config and env template"
  else
    log "runtime media: disabled/fail-closed; use --media-root to authorize or --clear-media-roots to clear explicitly"
  fi
  if (( INSTALL_RECORD_FOUND )); then
    log "timer state: restored to the pre-upgrade state"
  else
    log "timer status: disabled by default"
  fi
  log "enable explicitly: systemctl --user enable --now espelho-zap@default.timer"
}

uninstall_runtime() {
  local stamp
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  private_directories
  UNIT_BACKUP_DIR="${BACKUP_DIR}/unit-backups/uninstall-${stamp}-$$"
  capture_worker_runtime_state "${UNIT_BACKUP_DIR}" \
    || die "uninstall could not capture worker unit state; nothing was removed"
  quiesce_worker_units || {
    restore_worker_runtime_state "${UNIT_BACKUP_DIR}" || true
    die "uninstall could not quiesce worker units; nothing was removed"
  }
  disable_captured_worker_timers "${UNIT_BACKUP_DIR}" || {
    restore_worker_runtime_state "${UNIT_BACKUP_DIR}" || true
    die "uninstall could not disable managed timer instances; nothing was removed"
  }
  backup_existing_units || {
    restore_worker_runtime_state "${UNIT_BACKUP_DIR}" || true
    die "uninstall could not snapshot current unit bytes; nothing was removed"
  }

  if (( RUNTIME_DEACTIVATION )); then
    RUNTIME_ACTIVATION_BACKUP_DIR="${RUNTIME_ACTIVATION_BACKUP_ROOT}/uninstall-${stamp}-$$"
    run install -d -m 0700 -- "${RUNTIME_ACTIVATION_BACKUP_DIR}" \
      || die "runtime deactivation backup directory could not be created"
    capture_selected_runtime_gateway_state
    if ! stop_selected_runtime_gateway \
      || ! backup_runtime_activation_state \
      || ! stage_active_runtime_plugin copy \
      || ! deactivate_selected_runtime; then
      runtime_command gateway stop >/dev/null 2>&1 || true
      if (( RUNTIME_STATE_SNAPSHOTTED )); then restore_runtime_activation_state || true; fi
      if (( ACTIVE_PLUGIN_STAGED )); then restore_active_runtime_plugin || true; fi
      restart_selected_runtime_gateway_after_rollback || true
      restore_worker_runtime_state "${UNIT_BACKUP_DIR}" || true
      die "runtime deactivation failed; managed CLI, units, activation record, plugin, config, and gateway state were retained/restored"
    fi
  fi

  if ! restore_original_units_for_uninstall; then
    restore_prior_units || true
    restore_worker_runtime_state "${UNIT_BACKUP_DIR}" || true
    if (( RUNTIME_DEACTIVATION )); then
      runtime_command gateway stop >/dev/null 2>&1 || true
      restore_runtime_activation_state || true
      restore_active_runtime_plugin || true
      restart_selected_runtime_gateway_after_rollback || true
    fi
    die "pre-install systemd units could not be restored; uninstall was rolled back before CLI removal"
  fi

  if (( RUNTIME_DEACTIVATION )); then
    run rm -f -- "${ACTIVATION_RECORD}"
    log "runtime activation record: removed after successful deactivation canary"
  fi
  remove_managed_venv "${VENV}"
  if [[ -d "${SKILL_DEST}" ]]; then
    if [[ -f "${SKILL_MARKER}" ]] && [[ "$(head -n 1 -- "${SKILL_MARKER}")" == "${MANAGED_MARKER}" ]]; then
      remove_managed_skill "${SKILL_DEST}"
      log "skill: managed copy removed"
    else
      log "skill: unmanaged destination preserved"
    fi
  fi
  if [[ "${RUNTIME}" != "none" && -d "${RUNTIME_STAGING_DEST}" ]]; then
    if runtime_marker_matches "${RUNTIME_STAGING_MARKER}"; then
      remove_managed_runtime "${RUNTIME_STAGING_DEST}"
      log "runtime staging: managed ${RUNTIME} prepared copy removed"
    else
      log "runtime staging: unmanaged destination preserved"
    fi
  fi
  run rm -f -- "${INSTALL_RECORD}"
  log "uninstall: active application venv and installer-owned unit files removed"
  log "preserved: config, token file, ledger, hook health, state, and rollback backups"
}

main() {
  parse_args "$@"
  if (( PREPARE_HERMES_OBSERVER )); then
    if [[ "${ACTION}" != "preflight" ]] && (( ! DRY_RUN )); then
      LOCK_DIR="${HERMES_OBSERVER_LOCK_DIR}"
      LOCK_FILE="${LOCK_DIR}/transaction.lock"
      acquire_global_lock
      trap release_global_lock EXIT
    fi
    preflight_hermes_observer
    if [[ "${ACTION}" == "preflight" ]]; then
      log "preflight completed without writes"
    else
      prepare_hermes_observer_transaction
    fi
    return 0
  fi
  if [[ "${ACTION}" != "preflight" ]] && (( ! DRY_RUN )); then
    acquire_global_lock
    trap release_global_lock EXIT
  fi
  preflight
  case "${ACTION}" in
    preflight) log "preflight completed without writes" ;;
    install|upgrade) install_or_upgrade ;;
    uninstall) uninstall_runtime ;;
  esac
}

main "$@"
