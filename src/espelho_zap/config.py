"""Secret-free configuration for the portable mirror.

Configuration files may point at an environment variable or a private token
file.  A literal token is deliberately not part of the schema.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import tomllib
from typing import Mapping


SCHEMA_VERSION = 1
DEFAULT_CONFIG_PATH = Path("~/.config/espelho-zap/config.toml").expanduser()
DEFAULT_DATA_DIR = Path("~/.local/share/espelho-zap").expanduser()
DEFAULT_STATE_DIR = Path("~/.local/state/espelho-zap").expanduser()
DEFAULT_TOKEN_FILE = Path("~/.config/espelho-zap/telegram.token").expanduser()
DEFAULT_TOKEN_ENV = "ESPELHO_ZAP_TELEGRAM_BOT_TOKEN"

_TOP_LEVEL_KEYS = {"schema_version", "paths", "telegram", "worker", "routing", "legacy"}
_PATH_KEYS = {"data_dir", "state_dir", "ledger_path", "minimum_free_bytes"}
_TELEGRAM_KEYS = {"api_base", "token_env", "token_file", "timeout_seconds"}
_WORKER_KEYS = {
    "worker_id",
    "profile_id",
    "runtime_lock_seconds",
    "lease_seconds",
    "max_attempts",
    "base_backoff_seconds",
    "allowed_temp_root",
    "source_media_roots",
    "maximum_spool_bytes",
    "media_retention_hours",
}
_LEGACY_KEYS = {"default_chat_id"}
_ROUTING_KEYS = {
    "auto_create_direct_contact_topics",
    "auto_create_whatsapp_group_topics",
    "approved_groups_only",
    "telegram_forum_chat_id",
}
_SECRET_KEY = re.compile(r"(?:password|passwd|secret|api[_-]?key|access[_-]?token|bot[_-]?token|^token$)", re.I)
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ConfigError(ValueError):
    """Raised when configuration is missing, unsafe, or malformed."""


@dataclass(frozen=True, slots=True)
class PathsConfig:
    data_dir: Path
    state_dir: Path
    ledger_path: Path
    minimum_free_bytes: int


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    api_base: str
    token_env: str
    token_file: Path | None
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    worker_id: str | None
    profile_id: str
    runtime_lock_seconds: int
    lease_seconds: int
    max_attempts: int
    base_backoff_seconds: int
    allowed_temp_root: Path | None
    source_media_roots: tuple[Path, ...]
    maximum_spool_bytes: int
    media_retention_hours: int


@dataclass(frozen=True, slots=True)
class LegacyConfig:
    default_chat_id: str | None


@dataclass(frozen=True, slots=True)
class RoutingConfig:
    auto_create_direct_contact_topics: bool
    auto_create_whatsapp_group_topics: bool
    approved_groups_only: bool
    telegram_forum_chat_id: str | None


@dataclass(frozen=True, slots=True)
class AppConfig:
    config_path: Path
    paths: PathsConfig
    telegram: TelegramConfig
    worker: WorkerConfig
    routing: RoutingConfig
    legacy: LegacyConfig

    def public_summary(self, *, environ: Mapping[str, str] | None = None) -> dict[str, object]:
        """Return operational metadata without secret values or message content."""

        env = os.environ if environ is None else environ
        token_source = "none"
        if self.telegram.token_env and env.get(self.telegram.token_env, "").strip():
            token_source = "environment"
        elif self.telegram.token_file and self.telegram.token_file.is_file():
            try:
                if self.telegram.token_file.read_text(encoding="utf-8").strip():
                    token_source = "file"
            except (OSError, UnicodeError):
                pass
        return {
            "token_source": token_source,
            "worker_id": self.worker.worker_id,
            "profile_id": self.worker.profile_id,
        }


def default_config_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    configured = env.get("ESPELHO_ZAP_CONFIG", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    config_base = env.get("XDG_CONFIG_HOME", "").strip()
    if config_base:
        return (Path(config_base).expanduser() / "espelho-zap" / "config.toml").resolve()
    return DEFAULT_CONFIG_PATH.resolve()


def _runtime_defaults(environ: Mapping[str, str] | None = None) -> tuple[Path, Path, Path]:
    env = os.environ if environ is None else environ
    data_base = env.get("XDG_DATA_HOME", "").strip()
    state_base = env.get("XDG_STATE_HOME", "").strip()
    config_base = env.get("XDG_CONFIG_HOME", "").strip()
    data = (
        Path(data_base).expanduser() / "espelho-zap"
        if data_base
        else DEFAULT_DATA_DIR
    )
    state = (
        Path(state_base).expanduser() / "espelho-zap"
        if state_base
        else DEFAULT_STATE_DIR
    )
    token = (
        Path(config_base).expanduser() / "espelho-zap" / "telegram.token"
        if config_base
        else DEFAULT_TOKEN_FILE
    )
    return data.resolve(), state.resolve(), token.resolve()


def _table(value: object, name: str) -> Mapping[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a TOML table")
    return value


def _check_unknown(mapping: Mapping[str, object], allowed: set[str], name: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ConfigError(f"unknown keys in {name}: {', '.join(unknown)}")


def _reject_literal_secrets(mapping: Mapping[str, object], prefix: str = "") -> None:
    for key, value in mapping.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if key not in {"token_env", "token_file"} and _SECRET_KEY.search(key):
            raise ConfigError(f"literal secret field is forbidden: {dotted}; use an env or file reference")
        if isinstance(value, dict):
            _reject_literal_secrets(value, dotted)


def _string(mapping: Mapping[str, object], key: str, default: str) -> str:
    value = mapping.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string")
    return value.strip()


def _integer(mapping: Mapping[str, object], key: str, default: int, *, minimum: int = 1) -> int:
    value = mapping.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{key} must be an integer >= {minimum}")
    return value


def _boolean(mapping: Mapping[str, object], key: str, default: bool) -> bool:
    value = mapping.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{key} must be a boolean")
    return value


def _number(mapping: Mapping[str, object], key: str, default: float, *, minimum: float = 0.1) -> float:
    value = mapping.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) < minimum:
        raise ConfigError(f"{key} must be a number >= {minimum}")
    return float(value)


def _path(value: str, *, base: Path) -> Path:
    expanded = Path(os.path.expandvars(value)).expanduser()
    if not expanded.is_absolute():
        expanded = base / expanded
    return expanded.resolve()


def _optional_path(mapping: Mapping[str, object], key: str, default: str, *, base: Path) -> Path | None:
    value = _string(mapping, key, default)
    return _path(value, base=base) if value else None


def _path_list(mapping: Mapping[str, object], key: str, *, base: Path) -> tuple[Path, ...]:
    value = mapping.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ConfigError(f"{key} must be an array of non-empty paths")
    return tuple(dict.fromkeys(_path(item, base=base) for item in value))


def load_config(
    path: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> AppConfig:
    """Load and strictly validate a secret-free TOML configuration."""

    config_path = Path(path).expanduser().resolve() if path is not None else default_config_path(environ)
    default_data, default_state, default_token = _runtime_defaults(environ)
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError("configuration file not found") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in configuration file: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("configuration root must be a TOML table")
    _reject_literal_secrets(raw)
    _check_unknown(raw, _TOP_LEVEL_KEYS, "root")

    schema_version = raw.get("schema_version", SCHEMA_VERSION)
    if schema_version != SCHEMA_VERSION:
        raise ConfigError(f"unsupported schema_version: {schema_version!r}")

    paths = _table(raw.get("paths"), "paths")
    telegram = _table(raw.get("telegram"), "telegram")
    worker = _table(raw.get("worker"), "worker")
    routing = _table(raw.get("routing"), "routing")
    legacy = _table(raw.get("legacy"), "legacy")
    _check_unknown(paths, _PATH_KEYS, "paths")
    _check_unknown(telegram, _TELEGRAM_KEYS, "telegram")
    _check_unknown(worker, _WORKER_KEYS, "worker")
    _check_unknown(routing, _ROUTING_KEYS, "routing")
    _check_unknown(legacy, _LEGACY_KEYS, "legacy")

    base = config_path.parent
    data_dir = _path(_string(paths, "data_dir", str(default_data)), base=base)
    state_dir = _path(_string(paths, "state_dir", str(default_state)), base=base)
    ledger_path = _path(_string(paths, "ledger_path", str(data_dir / "mirror.sqlite3")), base=base)
    minimum_free_bytes = _integer(paths, "minimum_free_bytes", 268_435_456, minimum=1)

    api_base = _string(telegram, "api_base", "https://api.telegram.org").rstrip("/")
    if not api_base.startswith("https://"):
        raise ConfigError("telegram.api_base must use https")
    token_env = _string(telegram, "token_env", DEFAULT_TOKEN_ENV)
    if token_env and not _ENV_NAME.fullmatch(token_env):
        raise ConfigError("telegram.token_env is not a valid environment variable name")
    token_file = _optional_path(telegram, "token_file", str(default_token), base=base)

    worker_id = _string(worker, "worker_id", "") or None
    profile_id = _string(worker, "profile_id", "default")
    if not profile_id:
        raise ConfigError("worker.profile_id must not be empty")
    allowed_temp_root = _optional_path(
        worker, "allowed_temp_root", str(data_dir / "media"), base=base
    )
    source_media_roots = _path_list(worker, "source_media_roots", base=base)
    default_chat_id = _string(legacy, "default_chat_id", "") or None
    auto_groups = _boolean(routing, "auto_create_whatsapp_group_topics", False)
    approved_only = _boolean(routing, "approved_groups_only", True)
    telegram_forum_chat_id = _string(routing, "telegram_forum_chat_id", "") or None
    if telegram_forum_chat_id and (
        not telegram_forum_chat_id.startswith("-")
        or not telegram_forum_chat_id[1:].isdigit()
    ):
        raise ConfigError("routing.telegram_forum_chat_id must be a negative Telegram id")
    if auto_groups:
        raise ConfigError("routing.auto_create_whatsapp_group_topics must remain false")
    if not approved_only:
        raise ConfigError("routing.approved_groups_only must remain true")

    media_retention_hours = _integer(
        worker, "media_retention_hours", 48, minimum=1
    )
    if media_retention_hours > 48:
        raise ConfigError("worker.media_retention_hours must be <= 48")

    return AppConfig(
        config_path=config_path,
        paths=PathsConfig(
            data_dir=data_dir,
            state_dir=state_dir,
            ledger_path=ledger_path,
            minimum_free_bytes=minimum_free_bytes,
        ),
        telegram=TelegramConfig(
            api_base=api_base,
            token_env=token_env,
            token_file=token_file,
            timeout_seconds=_number(telegram, "timeout_seconds", 30.0),
        ),
        worker=WorkerConfig(
            worker_id=worker_id,
            profile_id=profile_id,
            runtime_lock_seconds=_integer(worker, "runtime_lock_seconds", 120),
            lease_seconds=_integer(worker, "lease_seconds", 60),
            max_attempts=_integer(worker, "max_attempts", 5),
            base_backoff_seconds=_integer(worker, "base_backoff_seconds", 5),
            allowed_temp_root=allowed_temp_root,
            source_media_roots=source_media_roots,
            maximum_spool_bytes=_integer(
                worker, "maximum_spool_bytes", 1_073_741_824
            ),
            media_retention_hours=media_retention_hours,
        ),
        routing=RoutingConfig(
            auto_create_direct_contact_topics=_boolean(
                routing, "auto_create_direct_contact_topics", True
            ),
            auto_create_whatsapp_group_topics=auto_groups,
            approved_groups_only=approved_only,
            telegram_forum_chat_id=telegram_forum_chat_id,
        ),
        legacy=LegacyConfig(default_chat_id=default_chat_id),
    )


def ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        path.chmod(0o700)


def ensure_runtime_paths(config: AppConfig) -> None:
    ensure_private_directory(config.config_path.parent)
    ensure_private_directory(config.paths.data_dir)
    ensure_private_directory(config.paths.state_dir)
    ensure_private_directory(config.paths.ledger_path.parent)
    if config.telegram.token_file:
        ensure_private_directory(config.telegram.token_file.parent)
    if config.worker.allowed_temp_root:
        ensure_private_directory(config.worker.allowed_temp_root)


def private_mode(path: Path) -> tuple[bool, str]:
    """Return whether a file or directory excludes group/other access."""

    if not path.exists():
        return False, "missing"
    if os.name != "posix":
        return True, "not-applicable"
    mode = stat.S_IMODE(path.stat().st_mode)
    expected = 0o700 if path.is_dir() else 0o600
    return (mode & 0o077) == 0, f"{mode:04o}; expected {expected:04o} or stricter"


def resolve_telegram_token(
    config: AppConfig,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve the Telegram token without ever returning it in diagnostics."""

    env = os.environ if environ is None else environ
    if config.telegram.token_env:
        value = env.get(config.telegram.token_env, "").strip()
        if value:
            return value
    token_file = config.telegram.token_file
    if token_file and token_file.is_file():
        private, detail = private_mode(token_file)
        if not private:
            raise ConfigError(f"telegram token file permissions are unsafe: {detail}")
        try:
            value = token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError("telegram token file cannot be read") from exc
        if value:
            return value
    raise ConfigError("telegram token is unavailable; set the configured environment variable or token file")


def _toml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def default_config_text(
    *,
    data_dir: Path | None = None,
    minimum_free_bytes: int = 268_435_456,
    environ: Mapping[str, str] | None = None,
) -> str:
    if isinstance(minimum_free_bytes, bool) or not isinstance(minimum_free_bytes, int) or minimum_free_bytes < 1:
        raise ConfigError("minimum_free_bytes must be a positive integer")
    default_data, state, token_file = _runtime_defaults(environ)
    root = (data_dir or default_data).expanduser().resolve()
    return "\n".join(
        [
            f"schema_version = {SCHEMA_VERSION}",
            "",
            "[paths]",
            f"data_dir = {_toml_quote(str(root))}",
            f"state_dir = {_toml_quote(str(state))}",
            f"ledger_path = {_toml_quote(str(root / 'mirror.sqlite3'))}",
            f"minimum_free_bytes = {minimum_free_bytes}",
            "",
            "[telegram]",
            'api_base = "https://api.telegram.org"',
            f"token_env = {_toml_quote(DEFAULT_TOKEN_ENV)}",
            f"token_file = {_toml_quote(str(token_file))}",
            "timeout_seconds = 30",
            "",
            "[worker]",
            'worker_id = ""',
            'profile_id = "default"',
            "runtime_lock_seconds = 120",
            "lease_seconds = 60",
            "max_attempts = 5",
            "base_backoff_seconds = 5",
            "maximum_spool_bytes = 1073741824",
            "media_retention_hours = 48",
            f"allowed_temp_root = {_toml_quote(str(root / 'media'))}",
            "source_media_roots = []",
            "",
            "[routing]",
            "auto_create_direct_contact_topics = true",
            "auto_create_whatsapp_group_topics = false",
            "approved_groups_only = true",
            'telegram_forum_chat_id = ""',
            "",
            "[legacy]",
            'default_chat_id = ""',
            "",
        ]
    )


def write_default_config(
    path: str | os.PathLike[str] | None = None,
    *,
    data_dir: Path | None = None,
    minimum_free_bytes: int = 268_435_456,
    force: bool = False,
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, bool]:
    """Create a private, secret-free config; return ``(path, created)``."""

    target = Path(path).expanduser().resolve() if path is not None else default_config_path(environ)
    ensure_private_directory(target.parent)
    if target.exists() and not force:
        return target, False

    payload = default_config_text(
        data_dir=data_dir,
        minimum_free_bytes=minimum_free_bytes,
        environ=environ,
    )
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent, text=True)
    temporary_path = Path(temporary)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if target.exists() and not force:
            temporary_path.unlink(missing_ok=True)
            return target, False
        os.replace(temporary_path, target)
        if os.name == "posix":
            target.chmod(0o600)
    finally:
        temporary_path.unlink(missing_ok=True)
    return target, True
