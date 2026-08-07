#!/usr/bin/env python3
"""Launch one paired bridge with passive capture and bounded human outbound."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import sys
import tomllib
from typing import Mapping

try:
    import fcntl
except ImportError:  # pragma: no cover - the launcher is Linux-only
    fcntl = None  # type: ignore[assignment]

try:
    from .bridge_guard import check_bridge
except ImportError:  # direct script execution
    from bridge_guard import check_bridge


class LauncherError(ValueError):
    """Raised when launching would weaken isolation or create a new session."""


@dataclass(frozen=True)
class ObserverSettings:
    node: Path
    bridge_js: Path
    session_dir: Path
    spool_file: Path
    cache_root: Path
    lock_file: Path
    port: int
    mode: str
    dm_policy: str
    group_policy: str
    allowed_users: str
    forward_owner_messages: bool
    human_outbound_token_file: Path | None
    human_outbound_media_root: Path | None
    debug: bool


_PATH_ENV = {
    "node": "ESPELHO_ZAP_BRIDGE_NODE",
    "bridge_js": "ESPELHO_ZAP_BRIDGE_JS",
    "session_dir": "ESPELHO_ZAP_BRIDGE_SESSION_DIR",
    "spool_file": "ESPELHO_ZAP_BRIDGE_SPOOL_FILE",
    "cache_root": "ESPELHO_ZAP_BRIDGE_CACHE_ROOT",
    "lock_file": "ESPELHO_ZAP_BRIDGE_LOCK_FILE",
    "human_outbound_token_file": "ESPELHO_ZAP_BRIDGE_HUMAN_OUTBOUND_TOKEN_FILE",
    "human_outbound_media_root": "ESPELHO_ZAP_BRIDGE_HUMAN_OUTBOUND_MEDIA_ROOT",
}


def _boolean(value: object, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise LauncherError(f"{field} must be a boolean")


def _value(section: Mapping[str, object], environ: Mapping[str, str], key: str) -> object:
    environment_name = _PATH_ENV.get(key, f"ESPELHO_ZAP_BRIDGE_{key.upper()}")
    return environ.get(environment_name, section.get(key, ""))


def _absolute(value: object, *, field: str) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        raise LauncherError(f"{field} must be an absolute path")
    return path


def _optional_absolute(value: object, *, field: str) -> Path | None:
    rendered = str(value or "").strip()
    return _absolute(rendered, field=field) if rendered else None


def load_settings(config_path: Path, environ: Mapping[str, str] | None = None) -> ObserverSettings:
    environ = os.environ if environ is None else environ
    if not config_path.is_absolute() or config_path.is_symlink() or not config_path.is_file():
        raise LauncherError("config must be an absolute regular non-symlink file")
    _assert_no_symlink_components(config_path)
    if os.name == "posix" and config_path.stat().st_mode & 0o022:
        raise LauncherError("config must not be group/world writable")
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    if raw.get("schema_version") != 1 or not isinstance(raw.get("bridge"), dict):
        raise LauncherError("unsupported direct bridge configuration")
    section = raw["bridge"]

    raw_port = _value(section, environ, "port")
    try:
        port = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise LauncherError("port must be an integer") from exc
    if not 1024 <= port <= 65535:
        raise LauncherError("port must be between 1024 and 65535")

    mode = str(_value(section, environ, "mode") or "bot").strip().lower()
    if mode != "bot":
        raise LauncherError("the observer launcher only accepts bot mode")
    dm_policy = str(_value(section, environ, "dm_policy") or "open").strip().lower()
    if dm_policy not in {"open", "pairing"}:
        raise LauncherError("unsupported DM policy")
    group_policy = str(_value(section, environ, "group_policy") or "disabled").strip().lower()
    if group_policy not in {"enabled", "disabled"}:
        raise LauncherError("unsupported group policy")
    allowed_users = str(_value(section, environ, "allowed_users") or "*").strip()
    if not allowed_users:
        raise LauncherError("allowed_users must be explicit")

    token_file = _optional_absolute(
        _value(section, environ, "human_outbound_token_file"),
        field="human_outbound_token_file",
    )
    media_root = _optional_absolute(
        _value(section, environ, "human_outbound_media_root"),
        field="human_outbound_media_root",
    )
    if (token_file is None) != (media_root is None):
        raise LauncherError(
            "human outbound token file and media root must be configured together"
        )
    if token_file is not None and port != 3011:
        raise LauncherError("human outbound requires the fixed loopback port 3011")

    return ObserverSettings(
        node=_absolute(_value(section, environ, "node"), field="node"),
        bridge_js=_absolute(_value(section, environ, "bridge_js"), field="bridge_js"),
        session_dir=_absolute(_value(section, environ, "session_dir"), field="session_dir"),
        spool_file=_absolute(_value(section, environ, "spool_file"), field="spool_file"),
        cache_root=_absolute(_value(section, environ, "cache_root"), field="cache_root"),
        lock_file=_absolute(_value(section, environ, "lock_file"), field="lock_file"),
        port=port,
        mode=mode,
        dm_policy=dm_policy,
        group_policy=group_policy,
        allowed_users=allowed_users,
        forward_owner_messages=_boolean(
            _value(section, environ, "forward_owner_messages") or False,
            field="forward_owner_messages",
        ),
        human_outbound_token_file=token_file,
        human_outbound_media_root=media_root,
        debug=_boolean(_value(section, environ, "debug") or False, field="debug"),
    )


def _assert_no_symlink_components(path: Path, *, allow_missing_leaf: bool = False) -> None:
    candidate = path
    if allow_missing_leaf and not candidate.exists() and not candidate.is_symlink():
        candidate = candidate.parent
    while True:
        if candidate.is_symlink():
            raise LauncherError("managed paths must not contain symlinks")
        if candidate == candidate.parent:
            return
        candidate = candidate.parent


def _assert_private_tree(root: Path) -> None:
    if not root.is_dir() or root.is_symlink():
        raise LauncherError("paired session must be a regular directory")
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        details = current_path.lstat()
        if stat.S_ISLNK(details.st_mode) or details.st_mode & 0o077:
            raise LauncherError("paired session directories must be private")
        if os.name == "posix" and details.st_uid != os.geteuid():
            raise LauncherError("paired session must be owned by the service user")
        for name in [*directories, *files]:
            item = current_path / name
            item_details = item.lstat()
            if stat.S_ISLNK(item_details.st_mode) or item_details.st_mode & 0o077:
                raise LauncherError("paired session entries must be private and non-symlink")
            if os.name == "posix" and item_details.st_uid != os.geteuid():
                raise LauncherError("paired session entries must be owned by the service user")


def _private_directory(path: Path) -> None:
    _assert_no_symlink_components(path, allow_missing_leaf=True)
    if not path.exists():
        path.mkdir(mode=0o700, parents=True)
    if not path.is_dir() or path.is_symlink() or path.stat().st_mode & 0o077:
        raise LauncherError("managed directories must be private and non-symlink")
    if os.name == "posix" and path.stat().st_uid != os.geteuid():
        raise LauncherError("managed directories must be owned by the service user")


def _assert_private_file_if_present(path: Path) -> None:
    _assert_no_symlink_components(path, allow_missing_leaf=True)
    if not path.exists():
        return
    details = path.lstat()
    if not stat.S_ISREG(details.st_mode) or details.st_mode & 0o077:
        raise LauncherError("managed files must be private regular files")
    if os.name == "posix" and details.st_uid != os.geteuid():
        raise LauncherError("managed files must be owned by the service user")


def validate_settings(settings: ObserverSettings) -> None:
    node = settings.node.resolve(strict=True)
    if (
        not node.is_file()
        or not os.access(node, os.X_OK)
        or (os.name == "posix" and node.stat().st_mode & 0o022)
    ):
        raise LauncherError("node must resolve to a trusted executable")
    _assert_no_symlink_components(settings.bridge_js)
    if settings.bridge_js.is_symlink() or not settings.bridge_js.is_file():
        raise LauncherError("bridge_js must be a regular non-symlink file")
    if os.name == "posix" and settings.bridge_js.stat().st_mode & 0o022:
        raise LauncherError("bridge_js must not be group/world writable")
    check_bridge(settings.bridge_js)

    _assert_private_tree(settings.session_dir)
    credentials = settings.session_dir / "creds.json"
    if not credentials.is_file() or credentials.is_symlink():
        raise LauncherError("an existing paired session is required")
    for directory in (
        settings.spool_file.parent,
        settings.cache_root,
        settings.cache_root / "images",
        settings.cache_root / "documents",
        settings.cache_root / "audio",
        settings.lock_file.parent,
    ):
        _private_directory(directory)
    _assert_private_file_if_present(settings.spool_file)
    _assert_private_file_if_present(settings.lock_file)
    if settings.human_outbound_token_file is not None:
        token_file = settings.human_outbound_token_file
        _assert_private_file_if_present(token_file)
        if not token_file.is_file() or token_file.is_symlink():
            raise LauncherError("human outbound token file is required")
        try:
            if len(token_file.read_text(encoding="utf-8").strip()) < 43:
                raise LauncherError("human outbound token is invalid")
        except UnicodeError as exc:
            raise LauncherError("human outbound token is invalid") from exc
        assert settings.human_outbound_media_root is not None
        _private_directory(settings.human_outbound_media_root)


def acquire_lock(path: Path) -> int:
    if fcntl is None:
        raise LauncherError("observer launcher requires POSIX flock support")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise LauncherError("the paired session is already owned by another observer") from exc
    os.set_inheritable(descriptor, True)
    return descriptor


def build_environment(
    settings: ObserverSettings, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    result = dict(os.environ if environ is None else environ)
    # Node preload hooks can execute before bridge.js and would invalidate the
    # compatibility proof.  Production dependencies are resolved relative to
    # the bridge itself, so neither variable is needed here.
    result.pop("NODE_OPTIONS", None)
    result.pop("NODE_PATH", None)
    result.update(
        {
            "WHATSAPP_OBSERVE_ONLY": "true",
            "ESPELHO_ZAP_HUMAN_OUTBOUND_TOKEN_FILE": (
                str(settings.human_outbound_token_file)
                if settings.human_outbound_token_file is not None
                else ""
            ),
            "ESPELHO_ZAP_HUMAN_OUTBOUND_MEDIA_ROOT": (
                str(settings.human_outbound_media_root)
                if settings.human_outbound_media_root is not None
                else ""
            ),
            "WHATSAPP_MODE": settings.mode,
            "WHATSAPP_DM_POLICY": settings.dm_policy,
            "WHATSAPP_GROUP_POLICY": settings.group_policy,
            # A previous group-pilot bridge used this process-wide switch to
            # discard every direct conversation.  Portable routing supports
            # direct contacts plus explicitly approved groups, so inherited
            # service environments must never turn the observer group-only.
            "WHATSAPP_GROUP_ONLY_CAPTURE": "false",
            "WHATSAPP_ALLOWED_USERS": settings.allowed_users,
            "WHATSAPP_FORWARD_OWNER_MESSAGES": (
                "true" if settings.forward_owner_messages else "false"
            ),
            "WHATSAPP_DEBUG": "true" if settings.debug else "false",
            "WHATSAPP_MIRROR_SPOOL": str(settings.spool_file),
            "HERMES_IMAGE_CACHE_DIR": str(settings.cache_root / "images"),
            "HERMES_DOCUMENT_CACHE_DIR": str(settings.cache_root / "documents"),
            "HERMES_AUDIO_CACHE_DIR": str(settings.cache_root / "audio"),
        }
    )
    return result


def command(settings: ObserverSettings) -> list[str]:
    return [
        str(settings.node.resolve(strict=True)),
        str(settings.bridge_js),
        "--port",
        str(settings.port),
        "--session",
        str(settings.session_dir),
        "--mode",
        settings.mode,
    ]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="validate without launching")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = load_settings(args.config)
        validate_settings(settings)
        if args.check:
            state = "true" if settings.human_outbound_token_file is not None else "false"
            print(
                "HERMES_DIRECT_BRIDGE_PREFLIGHT=PASS "
                f"observe_only=true human_outbound={state} automatic_outbound=false"
            )
            return 0
        lock_descriptor = acquire_lock(settings.lock_file)
        environment = build_environment(settings)
        environment["ESPELHO_ZAP_OBSERVER_LOCK_FD"] = str(lock_descriptor)
        os.execve(command(settings)[0], command(settings), environment)
    except (LauncherError, OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        print(f"HERMES_DIRECT_BRIDGE_PREFLIGHT=FAIL reason={type(exc).__name__}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
