"""Hermes pre-dispatch observer for Espelho Zap Portable.

The plugin is intentionally standard-library-only.  Hermes does not need the
product package installed in its gateway interpreter: it invokes the absolute
CLI installed by the product, over bounded JSON stdin with ``shell=False``.
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import http.client
import importlib.util
from importlib import metadata as importlib_metadata
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Mapping


_SCOPES = frozenset({"area_shared", "partnership_restricted", "owner_private"})
_LOGGER = logging.getLogger("espelho_zap.hermes")
_FAILURES: Counter[str] = Counter()
_MAX_MEDIA = 8
_MAX_MEDIA_BYTES = 128 * 1024 * 1024
_MAX_TOTAL_MEDIA_BYTES = 256 * 1024 * 1024
_MAX_OUTBOUND_ATTEMPTS = 3
_HUMAN_OUTBOUND_HOST = "127.0.0.1"
_HUMAN_OUTBOUND_PORT = 3011
_HUMAN_OUTBOUND_PATH = "/mirror-human-send"
_OUTBOUND_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="espelho-zap-human-outbound",
)
_OUTBOUND_WAKE_LOCK = threading.Lock()
_OUTBOUND_WAKE_PENDING: set[str] = set()
_OUTBOUND_REARM_WATCH_LOCK = threading.Lock()
_OUTBOUND_REARM_WATCHERS: dict[
    str, tuple[threading.Thread, threading.Event]
] = {}
_OUTBOUND_REARM_WATCH_INTERVAL_SECONDS = 1.0
_PLUGIN_VERSION = "0.3.1"
_HUMAN_OUTBOUND_ARM_KEYS = frozenset(
    {
        "schema_version",
        "release_commit",
        "plugin_sha256",
        "hermes_runtime_fingerprint",
        "armed",
    }
)
_MAX_HUMAN_OUTBOUND_ARM_BYTES = 4096
_HERMES_RUNTIME_COMPONENTS = (
    ("gateway_entry", "gateway.run", "gateway/run.py"),
    ("gateway_dispatch", "gateway.platforms.base", "gateway/platforms/base.py"),
    ("plugin_loader", "hermes_cli.plugins", "hermes_cli/plugins.py"),
)
_MAX_HERMES_MANIFEST_FILES = 4096
_MAX_HERMES_MANIFEST_BYTES = 64 * 1024 * 1024
_MAX_HERMES_MANIFEST_FILE_BYTES = 8 * 1024 * 1024
_MAX_RUNTIME_IDENTITY_BYTES = 256 * 1024 * 1024


def hook_health() -> dict[str, int]:
    return dict(_FAILURES)


def _signal_failure(code: str) -> None:
    safe = code if code.replace("_", "").isalnum() else "adapter_error"
    _FAILURES[safe] += 1
    _LOGGER.warning("espelho_zap_capture_failed code=%s", safe)


def _field(value: object, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _text(value: object) -> str:
    if isinstance(value, Enum):
        return _text(value.value)
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _timestamp(value: object) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp_timezone_required")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value) / 1000 if abs(float(value)) >= 100_000_000_000 else float(value)
        try:
            return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return ""
    return value.strip() if isinstance(value, str) else ""


def _kind(media_type: str, media_path: str) -> str:
    value = media_type.split(";", 1)[0].strip().lower()
    suffix = Path(media_path).suffix.lower()
    if value == "image" or value == "photo" or value.startswith("image/"):
        return "image"
    if value in {"voice", "ptt"}:
        return "voice"
    if suffix in {".ogg", ".oga", ".opus"} or value in {
        "audio/ogg",
        "audio/opus",
    }:
        return "voice"
    if value == "audio" or value.startswith("audio/"):
        return "audio"
    if value == "video" or value.startswith("video/"):
        return "video"
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return "image"
    if suffix in {".mp3", ".m4a", ".wav", ".aac"}:
        return "audio"
    if suffix in {".mp4", ".mov", ".webm"}:
        return "video"
    return "document"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _open_verified_regular(
    path: Path,
    *,
    code: str,
    maximum_bytes: int | None = None,
    expected_identity: tuple[int, int, int, int] | None = None,
) -> tuple[int, os.stat_result]:
    """Open one stable regular file without following a replaced symlink."""

    if not path.is_absolute():
        raise RuntimeError(code)
    try:
        named = path.lstat()
    except OSError:
        raise RuntimeError(code) from None
    if stat.S_ISLNK(named.st_mode) or not stat.S_ISREG(named.st_mode):
        raise RuntimeError(code)
    if maximum_bytes is not None and named.st_size > maximum_bytes:
        raise RuntimeError(code)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise RuntimeError(code) from None
    try:
        opened = os.fstat(descriptor)
        identity = (
            int(opened.st_dev),
            int(opened.st_ino),
            int(opened.st_size),
            int(opened.st_mtime_ns),
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            or opened.st_size != named.st_size
            or opened.st_mtime_ns != named.st_mtime_ns
            or (maximum_bytes is not None and opened.st_size > maximum_bytes)
            or (expected_identity is not None and identity != expected_identity)
        ):
            raise RuntimeError(code)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, opened


def _verify_opened_regular_unchanged(
    path: Path,
    descriptor: int,
    opened: os.stat_result,
    *,
    code: str,
) -> None:
    try:
        current = os.fstat(descriptor)
        named = path.lstat()
    except OSError:
        raise RuntimeError(code) from None
    if (
        not stat.S_ISREG(current.st_mode)
        or stat.S_ISLNK(named.st_mode)
        or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
        or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
        or current.st_size != opened.st_size
        or named.st_size != opened.st_size
        or current.st_mtime_ns != opened.st_mtime_ns
        or named.st_mtime_ns != opened.st_mtime_ns
    ):
        raise RuntimeError(code)


def _read_stable_regular_file(
    path: Path,
    *,
    code: str,
    maximum_bytes: int,
) -> bytes:
    descriptor, opened = _open_verified_regular(
        path, code=code, maximum_bytes=maximum_bytes
    )
    try:
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(descriptor, min(1024 * 1024, maximum_bytes + 1)):
            size += len(chunk)
            if size > maximum_bytes:
                raise RuntimeError(code)
            chunks.append(chunk)
        _verify_opened_regular_unchanged(path, descriptor, opened, code=code)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _hash_stable_regular_file(
    path: Path,
    *,
    code: str,
    maximum_bytes: int,
    expected_identity: tuple[int, int, int, int] | None = None,
) -> tuple[str, int, tuple[int, int, int, int]]:
    descriptor, opened = _open_verified_regular(
        path,
        code=code,
        maximum_bytes=maximum_bytes,
        expected_identity=expected_identity,
    )
    digest = hashlib.sha256()
    size = 0
    try:
        while chunk := os.read(descriptor, 1024 * 1024):
            size += len(chunk)
            if size > maximum_bytes:
                raise RuntimeError(code)
            digest.update(chunk)
        _verify_opened_regular_unchanged(path, descriptor, opened, code=code)
    finally:
        os.close(descriptor)
    return (
        digest.hexdigest(),
        size,
        (
            int(opened.st_dev),
            int(opened.st_ino),
            int(opened.st_size),
            int(opened.st_mtime_ns),
        ),
    )


def _runtime_file_identity(path: Path) -> dict[str, object]:
    """Return a stable identity for one existing regular runtime file."""

    try:
        resolved = path.resolve(strict=True)
        digest, size, _ = _hash_stable_regular_file(
            resolved,
            code="hermes_runtime_fingerprint_invalid",
            maximum_bytes=_MAX_RUNTIME_IDENTITY_BYTES,
        )
    except (OSError, RuntimeError):
        raise RuntimeError("hermes_runtime_fingerprint_invalid") from None
    return {
        "resolved_path": str(resolved),
        "size_bytes": size,
        "sha256": digest,
    }


def _runtime_package_root(selected: Path, relative_path: str) -> Path:
    parts = Path(relative_path).parts
    if len(parts) < 2:
        raise RuntimeError("hermes_runtime_fingerprint_invalid")
    root = selected
    for _ in range(len(parts) - 1):
        root = root.parent
    try:
        root = root.resolve(strict=True)
        details = root.lstat()
    except OSError:
        raise RuntimeError("hermes_runtime_fingerprint_invalid") from None
    if (
        root.name != parts[0]
        or stat.S_ISLNK(details.st_mode)
        or not stat.S_ISDIR(details.st_mode)
    ):
        raise RuntimeError("hermes_runtime_fingerprint_invalid")
    return root


def _runtime_package_manifest(root: Path, package: str) -> dict[str, object]:
    """Hash every regular Python source below one Hermes package root."""

    pending = [root]
    files: list[dict[str, object]] = []
    total_bytes = 0
    while pending:
        directory = pending.pop()
        try:
            before = directory.lstat()
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                raise OSError("directory_invalid")
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError:
            raise RuntimeError("hermes_runtime_fingerprint_invalid") from None
        for entry in entries:
            candidate = directory / entry.name
            try:
                details = candidate.lstat()
            except OSError:
                raise RuntimeError("hermes_runtime_fingerprint_invalid") from None
            if stat.S_ISLNK(details.st_mode):
                raise RuntimeError("hermes_runtime_fingerprint_invalid")
            if stat.S_ISDIR(details.st_mode):
                pending.append(candidate)
                continue
            if candidate.suffix != ".py":
                continue
            if not stat.S_ISREG(details.st_mode):
                raise RuntimeError("hermes_runtime_fingerprint_invalid")
            if len(files) >= _MAX_HERMES_MANIFEST_FILES:
                raise RuntimeError("hermes_runtime_fingerprint_invalid")
            digest, size, _ = _hash_stable_regular_file(
                candidate.resolve(strict=True),
                code="hermes_runtime_fingerprint_invalid",
                maximum_bytes=_MAX_HERMES_MANIFEST_FILE_BYTES,
            )
            total_bytes += size
            if total_bytes > _MAX_HERMES_MANIFEST_BYTES:
                raise RuntimeError("hermes_runtime_fingerprint_invalid")
            files.append(
                {
                    "path": candidate.relative_to(root).as_posix(),
                    "size_bytes": size,
                    "sha256": digest,
                }
            )
        try:
            after = directory.lstat()
        except OSError:
            raise RuntimeError("hermes_runtime_fingerprint_invalid") from None
        if (
            stat.S_ISLNK(after.st_mode)
            or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
            or after.st_mtime_ns != before.st_mtime_ns
        ):
            raise RuntimeError("hermes_runtime_fingerprint_invalid")
    files.sort(key=lambda item: str(item["path"]))
    if not files:
        raise RuntimeError("hermes_runtime_fingerprint_invalid")
    return {
        "package": package,
        "root": str(root),
        "file_count": len(files),
        "total_bytes": total_bytes,
        "files": files,
    }


def _runtime_distribution_versions(packages: tuple[str, ...]) -> list[dict[str, str]]:
    """Return installed distribution metadata when the runtime exposes it."""

    try:
        mapping = importlib_metadata.packages_distributions()
    except Exception:
        return []
    names = sorted(
        {
            distribution
            for package in packages
            for distribution in (mapping.get(package) or [])
            if isinstance(distribution, str) and distribution
        },
        key=str.casefold,
    )
    result: list[dict[str, str]] = []
    for name in names:
        try:
            distribution = importlib_metadata.distribution(name)
            canonical_name = str(distribution.metadata.get("Name") or name)
            version = str(distribution.version)
        except Exception:
            continue
        if len(canonical_name) <= 256 and len(version) <= 256:
            result.append({"name": canonical_name, "version": version})
    return result


def _runtime_component_path(module_name: str, relative_path: str) -> Path | None:
    """Resolve Hermes code without retaining an imported module for discovery.

    The guard runs as a separate oneshot process, so Hermes' modules are not
    present in ``sys.modules`` even though the gateway has the same runtime.
    The editable Hermes installation exposes its source through an import
    finder rather than a plain ``sys.path`` directory.  After the direct path
    scan, use ``find_spec`` only to resolve that source file, then remove any
    transient parent modules it had to load.  The plugin registry is never
    invoked and the network audit hook remains active during this probe.
    """

    loaded = sys.modules.get(module_name)
    loaded_path = getattr(loaded, "__file__", "") if loaded is not None else ""
    if isinstance(loaded_path, str) and loaded_path:
        candidate = Path(loaded_path)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            pass
        else:
            if resolved.is_file():
                return resolved

    seen: set[Path] = set()
    for raw_root in sys.path:
        if not isinstance(raw_root, str):
            continue
        try:
            root = Path(raw_root or os.getcwd()).resolve(strict=True)
        except OSError:
            continue
        if root in seen or not root.is_dir():
            continue
        seen.add(root)
        candidate = root / relative_path
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file():
            return resolved
    try:
        executable_root = Path(os.path.abspath(sys.executable)).resolve(strict=True)
        executable_roots = [executable_root.parent]
        executable_roots.extend(executable_root.parents[:8])
    except OSError:
        executable_roots = []
    for root in executable_roots:
        candidate = root / relative_path
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file():
            return resolved
    before_modules = set(sys.modules)
    try:
        specification = importlib.util.find_spec(module_name)
    except BaseException:
        specification = None
    finally:
        for transient_name in set(sys.modules).difference(before_modules):
            sys.modules.pop(transient_name, None)
    origin = getattr(specification, "origin", None) if specification else None
    if isinstance(origin, str) and origin not in {"built-in", "frozen"}:
        try:
            resolved = Path(origin).resolve(strict=True)
        except OSError:
            return None
        if resolved.is_file():
            return resolved
    return None


def _hermes_runtime_fingerprint() -> str:
    """Bind outbound authority to the exact Hermes interpreter and hook path.

    Discovery never imports Hermes modules. It prefers ``__file__`` for modules
    already loaded by the gateway and otherwise mirrors import search order over
    ``sys.path``. The fixed selection covers gateway startup, pre-dispatch and
    plugin hook loading; a missing required component invalidates the
    fingerprint and keeps outbound disarmed.
    """

    executable_raw = Path(os.path.abspath(sys.executable))
    executable = _runtime_file_identity(executable_raw)
    components: list[dict[str, object]] = []
    package_roots: dict[str, Path] = {}
    for name, module_name, relative_path in _HERMES_RUNTIME_COMPONENTS:
        selected = _runtime_component_path(module_name, relative_path)
        if selected is None:
            raise RuntimeError("hermes_runtime_fingerprint_invalid")
        item: dict[str, object] = {
            "name": name,
            "module": module_name,
            "relative_path": relative_path,
        }
        item.update(_runtime_file_identity(selected))
        item["status"] = "present"
        components.append(item)
        package_name = Path(relative_path).parts[0]
        package_root = _runtime_package_root(selected, relative_path)
        previous = package_roots.setdefault(package_name, package_root)
        if previous != package_root:
            raise RuntimeError("hermes_runtime_fingerprint_invalid")

    package_manifests = [
        _runtime_package_manifest(package_roots[name], name)
        for name in sorted(package_roots)
    ]

    prefix = Path(os.path.abspath(sys.prefix))
    base_prefix = Path(os.path.abspath(sys.base_prefix))
    venv_config = prefix / "pyvenv.cfg"
    payload: dict[str, object] = {
        "schema_version": 1,
        "executable_configured_path": str(executable_raw),
        "executable": executable,
        "prefix": str(prefix),
        "base_prefix": str(base_prefix),
        "components": components,
        "package_manifests": package_manifests,
        "distributions": _runtime_distribution_versions(tuple(sorted(package_roots))),
    }
    if venv_config.is_file():
        payload["pyvenv_cfg"] = _runtime_file_identity(venv_config)
    else:
        payload["pyvenv_cfg"] = {"status": "missing"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _HumanOutboundSettings:
    allowed_users: frozenset[str]
    route_map: Path
    token_file: Path
    ledger_file: Path
    managed_media_root: Path
    timeout_seconds: float
    arm_file: Path | None = None
    release_commit: str = ""
    plugin_sha256: str = ""
    hermes_runtime_fingerprint: str = ""
    mirror_ledger_file: Path | None = None


@dataclass(frozen=True, slots=True)
class _OutboundMedia:
    source_path: Path
    source_device: int
    source_inode: int
    source_mtime_ns: int
    media_type: str
    mime_type: str
    sha256: str
    size_bytes: int
    caption: str
    file_name: str


@dataclass(frozen=True, slots=True)
class _OutboundJob:
    request_id: str
    destination: str
    text: str
    media: tuple[dict[str, object], ...]
    attempt_count: int = 1


@dataclass(frozen=True, slots=True)
class _Settings:
    cli: Path
    config_path: Path
    source_profile_id: str
    health_path: Path
    privacy_scope: str
    media_roots: tuple[Path, ...]
    maximum_payload_bytes: int
    timeout_seconds: float
    telegram_forum_chat_id: str = ""
    native_whatsapp_capture: bool = True
    human_outbound: _HumanOutboundSettings | None = None
    hermes_runtime_fingerprint: str = ""

    @classmethod
    def from_environment(cls) -> "_Settings":
        human_outbound_raw = os.environ.get(
            "ESPELHO_ZAP_HUMAN_OUTBOUND_ENABLED", "disabled"
        ).strip().lower()
        marker_requested = bool(
            os.environ.get(
                "ESPELHO_ZAP_HUMAN_OUTBOUND_STARTUP_MARKER", ""
            ).strip()
        )
        hermes_runtime_fingerprint = (
            _hermes_runtime_fingerprint()
            if human_outbound_raw == "enabled" or marker_requested
            else ""
        )
        cli = Path(os.environ.get("ESPELHO_ZAP_CLI", ""))
        config = Path(os.environ.get("ESPELHO_ZAP_CONFIG", ""))
        source_profile = os.environ.get("ESPELHO_ZAP_SOURCE_PROFILE_ID", "").strip()
        health_path = Path(os.environ.get("ESPELHO_ZAP_HOOK_HEALTH_FILE", ""))
        scope = os.environ.get("ESPELHO_ZAP_PRIVACY_SCOPE", "owner_private")
        forum_chat_id = os.environ.get(
            "ESPELHO_ZAP_TELEGRAM_FORUM_CHAT_ID", ""
        ).strip()
        native_capture_raw = os.environ.get(
            "ESPELHO_ZAP_HERMES_NATIVE_WHATSAPP_CAPTURE", "enabled"
        ).strip().lower()
        if not cli.is_absolute() or not cli.is_file():
            raise RuntimeError("ingest_cli_required")
        if not config.is_absolute() or not config.is_file():
            raise RuntimeError("config_path_required")
        if not source_profile:
            raise RuntimeError("source_profile_required")
        if not health_path.is_absolute():
            raise RuntimeError("hook_health_file_required")
        if scope not in _SCOPES:
            raise RuntimeError("privacy_scope_required")
        if forum_chat_id and (
            not re.fullmatch(r"-[0-9]+", forum_chat_id)
            or int(forum_chat_id) >= 0
        ):
            raise RuntimeError("telegram_forum_chat_id_invalid")
        if native_capture_raw not in {"enabled", "disabled"}:
            raise RuntimeError("native_whatsapp_capture_invalid")
        if human_outbound_raw not in {"enabled", "disabled"}:
            raise RuntimeError("human_outbound_invalid")
        roots = tuple(
            Path(item).resolve(strict=True)
            for item in os.environ.get("ESPELHO_ZAP_MEDIA_ROOTS", "").split(os.pathsep)
            if item
        )
        try:
            maximum = int(os.environ.get("ESPELHO_ZAP_MAX_HOOK_BYTES", "1048576"))
            timeout = float(os.environ.get("ESPELHO_ZAP_HOOK_TIMEOUT_SECONDS", "15"))
        except ValueError:
            raise RuntimeError("hook_limits_invalid") from None
        if maximum <= 0 or timeout <= 0:
            raise RuntimeError("hook_limits_invalid")
        human_outbound: _HumanOutboundSettings | None = None
        if human_outbound_raw == "enabled":
            if not forum_chat_id:
                raise RuntimeError("human_outbound_forum_required")
            allowed_users = frozenset(
                value
                for value in re.split(
                    r"[,;\s]+",
                    os.environ.get("ESPELHO_ZAP_HUMAN_OUTBOUND_ALLOWED_USERS", ""),
                )
                if value
            )
            if not allowed_users:
                raise RuntimeError("human_outbound_allowed_users_required")
            route_map_raw = Path(
                os.environ.get("ESPELHO_ZAP_HUMAN_OUTBOUND_ROUTE_MAP", "")
            )
            token_file_raw = Path(
                os.environ.get("ESPELHO_ZAP_HUMAN_OUTBOUND_TOKEN_FILE", "")
            )
            ledger_file_raw = Path(
                os.environ.get("ESPELHO_ZAP_HUMAN_OUTBOUND_LEDGER", "")
            )
            mirror_ledger_file_raw = Path(
                os.environ.get("ESPELHO_ZAP_HUMAN_OUTBOUND_MIRROR_LEDGER", "")
            )
            managed_root_raw = Path(
                os.environ.get("ESPELHO_ZAP_HUMAN_OUTBOUND_MANAGED_MEDIA_ROOT", "")
            )
            arm_file_raw = Path(
                os.environ.get("ESPELHO_ZAP_HUMAN_OUTBOUND_ARM_FILE", "")
            )
            release_commit = os.environ.get(
                "ESPELHO_ZAP_RELEASE_COMMIT", ""
            ).strip()
            if (
                not route_map_raw.is_absolute()
                or route_map_raw.is_symlink()
                or not route_map_raw.is_file()
            ):
                raise RuntimeError("human_outbound_route_map_required")
            if (
                not token_file_raw.is_absolute()
                or token_file_raw.is_symlink()
                or not token_file_raw.is_file()
            ):
                raise RuntimeError("human_outbound_token_file_required")
            if not ledger_file_raw.is_absolute():
                raise RuntimeError("human_outbound_ledger_required")
            if (
                not mirror_ledger_file_raw.is_absolute()
                or mirror_ledger_file_raw.is_symlink()
                or not mirror_ledger_file_raw.is_file()
            ):
                raise RuntimeError("human_outbound_mirror_ledger_required")
            if not managed_root_raw.is_absolute():
                raise RuntimeError("human_outbound_managed_media_root_required")
            if not arm_file_raw.is_absolute():
                raise RuntimeError("human_outbound_arm_file_required")
            if not re.fullmatch(r"[0-9a-f]{40}", release_commit):
                raise RuntimeError("human_outbound_release_commit_required")
            managed_root_raw.mkdir(parents=True, exist_ok=True, mode=0o700)
            if managed_root_raw.is_symlink() or not managed_root_raw.is_dir():
                raise RuntimeError("human_outbound_managed_media_root_invalid")
            try:
                outbound_timeout = float(
                    os.environ.get(
                        "ESPELHO_ZAP_HUMAN_OUTBOUND_TIMEOUT_SECONDS", "70"
                    )
                )
            except ValueError:
                raise RuntimeError("human_outbound_timeout_invalid") from None
            if outbound_timeout <= 0 or outbound_timeout > 120:
                raise RuntimeError("human_outbound_timeout_invalid")
            ledger_file = Path(os.path.abspath(ledger_file_raw))
            _prepare_outbound_ledger_path(ledger_file)
            arm_file = Path(os.path.abspath(arm_file_raw))
            _ensure_private_directory(arm_file.parent)
            plugin_source = Path(__file__).resolve(strict=True)
            plugin_sha256 = hashlib.sha256(plugin_source.read_bytes()).hexdigest()
            human_outbound = _HumanOutboundSettings(
                allowed_users=allowed_users,
                route_map=route_map_raw.resolve(strict=True),
                token_file=token_file_raw.resolve(strict=True),
                ledger_file=ledger_file,
                mirror_ledger_file=mirror_ledger_file_raw.resolve(strict=True),
                managed_media_root=managed_root_raw.resolve(strict=True),
                timeout_seconds=outbound_timeout,
                arm_file=arm_file,
                release_commit=release_commit,
                plugin_sha256=plugin_sha256,
                hermes_runtime_fingerprint=hermes_runtime_fingerprint,
            )
        return cls(
            cli.resolve(),
            config.resolve(),
            source_profile,
            health_path.resolve(),
            scope,
            roots,
            maximum,
            timeout,
            forum_chat_id,
            native_capture_raw == "enabled",
            human_outbound,
            hermes_runtime_fingerprint,
        )


def _persist_health(
    settings: _Settings,
    *,
    success: bool,
    error_code: str = "",
) -> None:
    path = settings.health_path
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    value: dict[str, object] = {
        "schema_version": 1,
        "successes": 0,
        "failures": {},
        "last_success_at": "",
        "last_failure_at": "",
        "last_error_code": "",
    }
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            value.update(loaded)
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if success:
        value["successes"] = int(value.get("successes", 0)) + 1
        value["last_success_at"] = now
    else:
        safe = error_code if error_code.replace("_", "").isalnum() else "adapter_error"
        failures = value.get("failures")
        if not isinstance(failures, dict):
            failures = {}
        failures[safe] = int(failures.get(safe, 0)) + 1
        value["failures"] = failures
        value["last_failure_at"] = now
        value["last_error_code"] = safe
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _contained(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _normalize_message_event(event: object, settings: _Settings) -> dict[str, object] | None:
    source = _field(event, "source")
    if _text(_field(source, "platform")).lower() != "whatsapp":
        return None
    raw_conversation = _text(_field(source, "chat_id"))
    raw_message = _field(event, "raw_message")
    is_group = any(
        _truthy(_field(container, key))
        for container in (event, source, raw_message)
        if container is not None
        for key in ("is_group", "isGroup")
    ) or raw_conversation.endswith("@g.us")
    raw_actor = next(
        (
            value
            for container in (event, source, raw_message)
            if container is not None
            for key in ("participant_id", "participant", "sender_id", "user_id")
            if (value := _text(_field(container, key)))
        ),
        "",
    )
    raw_message_id = _text(_field(event, "message_id")) or _text(
        _field(event, "platform_update_id")
    )
    if not raw_conversation or not raw_actor or not raw_message_id:
        return None
    source_profile_id = (
        _text(_field(source, "profile"))
        or _text(_field(source, "scope_id"))
        or settings.source_profile_id
    )
    profile_ref = "profile:" + _sha256(source_profile_id)
    raw_urls = _field(event, "media_urls", [])
    raw_types = _field(event, "media_types", [])
    if not isinstance(raw_urls, list) or not isinstance(raw_types, list):
        return None
    if len(raw_urls) > _MAX_MEDIA:
        raise ValueError("media_count_exceeded")
    media: list[dict[str, object]] = []
    for index, raw_url in enumerate(raw_urls):
        if not isinstance(raw_url, str) or not raw_url or not settings.media_roots:
            raise ValueError("media_roots_required")
        candidate = Path(raw_url)
        if candidate.is_symlink():
            raise ValueError("media_symlink_rejected")
        media_path = candidate.resolve(strict=True)
        if not _contained(media_path, settings.media_roots) or not media_path.is_file():
            raise ValueError("media_path_rejected")
        raw_type = _text(raw_types[index]) if index < len(raw_types) else ""
        mime_type = raw_type.split(";", 1)[0].strip() if "/" in raw_type else ""
        media.append(
            {
                "media_id": "media:"
                + _sha256(
                    f"{profile_ref}\x1f{raw_conversation}\x1f{raw_message_id}\x1f{index}"
                ),
                "kind": _kind(raw_type, raw_url),
                "path": str(media_path),
                "mime_type": mime_type,
                "sha256": "",
                "size_bytes": media_path.stat().st_size,
                "caption": "",
                "managed_temp": False,
            }
        )
    body = _field(event, "text", "")
    if not isinstance(body, str) or (not body and not media):
        return None
    occurred_at = next(
        (
            rendered
            for value in (
                _field(event, "occurred_at"),
                _field(event, "timestamp"),
                _field(raw_message, "timestamp"),
                _field(raw_message, "messageTimestamp"),
            )
            if (rendered := _timestamp(value))
        ),
        "",
    )
    if not occurred_at:
        raise ValueError("timestamp_required")
    actor_display_label = next(
        (
            value
            for container in (event, source, raw_message)
            if container is not None
            for key in ("sender_name", "senderName", "sender_label", "pushName", "display_name")
            if (value := _text(_field(container, key)).strip())
        ),
        "",
    )
    audio_only = bool(media) and all(item["kind"] in {"audio", "voice"} for item in media)
    return {
        "schema_version": 3,
        "event_id": "event:"
        + _sha256(
            f"whatsapp\x1f{profile_ref}\x1f{raw_conversation}\x1f{raw_message_id}"
        ),
        "source": "whatsapp",
        "source_profile_id": source_profile_id,
        "conversation_id": raw_conversation,
        "actor_ref": raw_actor,
        "occurred_at": occurred_at,
        "privacy_scope": settings.privacy_scope,
        "text": body,
        "context_text": body if audio_only else "",
        "conversation_kind": "group" if is_group else "direct",
        "actor_display_label": actor_display_label,
        "media": media,
    }


def _run_ingest(payload: Mapping[str, object], settings: _Settings) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > settings.maximum_payload_bytes:
        raise RuntimeError("ingest_payload_too_large")
    try:
        result = subprocess.run(
            [str(settings.cli), "--config", str(settings.config_path), "ingest", "-"],
            input=body,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            timeout=settings.timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("ingest_process_failed") from None
    if result.returncode != 0:
        raise RuntimeError("ingest_rejected")


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return _text(value).strip().lower() in {"1", "true", "yes", "bot"}


def _is_bot_or_assistant(event: object, source: object) -> bool:
    role = (
        _text(_field(event, "role"))
        or _text(_field(_field(event, "raw_message"), "role"))
    ).lower()
    if role in {"assistant", "bot", "system", "tool"}:
        return True
    return any(
        _truthy(value)
        for value in (
            _field(event, "is_bot"),
            _field(event, "sender_is_bot"),
            _field(event, "author_is_bot"),
            _field(source, "is_bot"),
            _field(source, "sender_is_bot"),
        )
    )


def _is_service_or_automation(event: object, source: object) -> bool:
    """Reject non-human Telegram events without relying on one Hermes shape."""

    raw_message = _field(event, "raw_message")
    event_kinds = {
        _text(_field(container, field)).strip().lower()
        for container in (event, source, raw_message)
        if container is not None
        for field in ("event_type", "message_type", "type", "kind")
    }
    if event_kinds & {
        "assistant",
        "automation",
        "automatic",
        "bot",
        "service",
        "service_message",
        "system",
        "tool",
    }:
        return True
    return any(
        _truthy(_field(container, field))
        for container in (event, source, raw_message)
        if container is not None
        for field in (
            "is_automatic",
            "is_automation",
            "is_service",
            "service_message",
        )
    )


def _canonical_whatsapp_identity(value: object) -> str:
    candidate = _text(value).strip()
    if not candidate or candidate.endswith("@lid"):
        return ""
    if "@" in candidate:
        local, suffix = candidate.rsplit("@", 1)
        if suffix == "s.whatsapp.net":
            local = local.lstrip("+")
            if local.isdigit() and 6 <= len(local) <= 20:
                return f"{local}@s.whatsapp.net"
            return ""
        if suffix == "g.us":
            if re.fullmatch(r"[0-9][0-9-]{5,39}", local):
                return f"{local}@g.us"
            return ""
        return ""
    local = candidate.lstrip("+")
    if local.isdigit() and 6 <= len(local) <= 20:
        return f"{local}@s.whatsapp.net"
    return ""


def _reverse_route(
    settings: _Settings,
    outbound: _HumanOutboundSettings,
    thread_id: str,
) -> str:
    raw = _read_stable_regular_file(
        outbound.route_map,
        code="route_map_invalid",
        maximum_bytes=4 * 1024 * 1024,
    )
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("route_map_invalid") from None
    if not isinstance(value, Mapping):
        raise RuntimeError("route_map_invalid")
    mapped_forum = _text(
        value.get("forum_chat_id")
        or value.get("groupChatId")
        or value.get("telegramForumChatId")
    )
    if mapped_forum and mapped_forum != settings.telegram_forum_chat_id:
        raise RuntimeError("route_map_forum_mismatch")
    routes = value.get("routes")
    if not isinstance(routes, Mapping):
        routes = value.get("contactTopics")
    if not isinstance(routes, Mapping):
        raise RuntimeError("route_map_routes_required")
    candidates: set[str] = set()
    matching_routes = 0
    for raw_identity, raw_route in routes.items():
        if not isinstance(raw_route, Mapping) or raw_route.get("enabled", True) is False:
            continue
        route_thread = _text(
            raw_route.get("thread_id")
            or raw_route.get("topic_id")
            or raw_route.get("topicId")
        )
        if route_thread != thread_id:
            continue
        matching_routes += 1
        # Aliases (notably ``@lid``) exist only to reconcile inbound identity.
        # They must never become an outbound destination.  The explicit target,
        # or otherwise the canonical route key, is the sole authority.
        identity = (
            raw_route.get("whatsapp_target")
            or raw_route.get("whatsappTarget")
            or raw_identity
        )
        route_candidate = _canonical_whatsapp_identity(identity)
        if not route_candidate:
            raise RuntimeError("route_identity_ambiguous")
        candidates.add(route_candidate)
    if matching_routes != 1 or len(candidates) != 1:
        raise RuntimeError("route_missing_or_ambiguous")
    destination = next(iter(candidates))
    if destination.endswith("@g.us"):
        _require_approved_group_outbound(settings, outbound, destination)
    return destination


def _require_approved_group_outbound(
    settings: _Settings,
    outbound: _HumanOutboundSettings,
    destination: str,
) -> None:
    """Require the same exact group admission used by the inbound ledger.

    The topic map alone is not authority for a WhatsApp group.  A group route
    becomes bidirectional only after the portable ledger contains an exact
    ``group_approved`` admission for the active Hermes profile and JID.
    """

    path = outbound.mirror_ledger_file
    if path is None:
        raise RuntimeError("group_outbound_admission_unavailable")
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("group_outbound_admission_unavailable")
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise RuntimeError("group_outbound_admission_unavailable") from None
    if resolved != path:
        raise RuntimeError("group_outbound_admission_unavailable")
    profile_ref = (
        "profile:"
        + hashlib.sha256(settings.source_profile_id.encode("utf-8")).hexdigest()
    )
    conversation_ref = (
        "conversation:"
        + hashlib.sha256(
            f"{profile_ref}\x1f{destination}".encode("utf-8")
        ).hexdigest()
    )
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{resolved.as_uri()}?mode=ro",
            uri=True,
            timeout=2,
        )
        connection.execute("PRAGMA query_only=ON")
        row = connection.execute(
            """SELECT source_profile_id, conversation_kind, approval_state
               FROM mirror_conversation_admission
               WHERE conversation_id=?""",
            (conversation_ref,),
        ).fetchone()
    except sqlite3.Error:
        raise RuntimeError("group_outbound_admission_unavailable") from None
    finally:
        if connection is not None:
            connection.close()
    if not row or tuple(str(value) for value in row) != (
        profile_ref,
        "group",
        "group_approved",
    ):
        raise RuntimeError("group_outbound_not_approved")


def _hash_file(path: Path, *, maximum_bytes: int | None = None) -> tuple[str, int]:
    digest, size, _ = _hash_stable_regular_file(
        path,
        code="media_too_large" if maximum_bytes is not None else "file_invalid",
        maximum_bytes=(
            maximum_bytes
            if maximum_bytes is not None
            else _MAX_RUNTIME_IDENTITY_BYTES
        ),
    )
    return digest, size


def _bounded_file_name(value: object, fallback: str) -> str:
    candidate = _text(value).replace("\x00", "").replace("\\", "/").rsplit("/", 1)[-1]
    fallback_name = fallback.replace("\x00", "").replace("\\", "/").rsplit("/", 1)[-1]
    if candidate in {"", ".", ".."}:
        candidate = fallback_name
    suffix = Path(candidate).suffix[:16]
    stem = candidate[: -len(suffix)] if suffix else candidate
    while stem and len(f"{stem}{suffix}".encode("utf-8")) > 180:
        stem = stem[:-1]
    if not stem:
        return f"media{suffix}"
    return f"{stem}{suffix}"


def _official_media_file_names(event: object) -> tuple[str, ...]:
    """Recover Telegram's original file name from the official raw message."""

    raw = _field(event, "raw_message")
    nested = _field(raw, "message")
    containers = (raw, nested) if nested is not None else (raw,)
    for container in containers:
        for field_name in ("document", "audio", "video", "animation"):
            original = _text(_field(_field(container, field_name), "file_name"))
            if original:
                return (original,)
    return ()


def _telegram_human_payload(event: object) -> tuple[str, bool]:
    """Return only the content the human authored in the current Telegram update.

    Hermes may enrich ``MessageEvent.text`` with vision, document contents or
    replied-to media notes before hooks run.  The official ``raw_message`` is
    therefore the authority for outbound intent; normalized fields are used
    only for the staged bytes of a current, explicitly supported attachment.
    """

    raw = _field(event, "raw_message")
    if raw is None:
        raise RuntimeError("telegram_raw_message_required")
    current_media = tuple(
        name
        for name in ("photo", "voice", "audio", "video", "document", "animation")
        if _field(raw, name)
    )
    media_urls = _field(event, "media_urls", [])
    has_staged_media = isinstance(media_urls, (list, tuple)) and bool(media_urls)
    if current_media:
        if len(current_media) != 1:
            raise RuntimeError("telegram_current_media_ambiguous")
        if not has_staged_media:
            raise RuntimeError("telegram_current_media_unavailable")
        caption = _field(raw, "caption", "")
        if caption is not None and not isinstance(caption, str):
            raise RuntimeError("outbound_text_invalid")
        return caption or "", True
    raw_text = _field(raw, "text", "")
    if isinstance(raw_text, str) and raw_text:
        if has_staged_media:
            # Hermes caches media from the message being replied to and appends
            # a technical note to event.text.  That cited attachment is context,
            # not a new outbound attachment; send only the current raw text.
            return raw_text, False
        normalized_text = _field(event, "text", "")
        if not isinstance(normalized_text, str) or not normalized_text:
            raise RuntimeError("outbound_text_invalid")
        # Text batching concatenates multiple human Telegram updates while
        # retaining raw_message from the first one. With no staged media this
        # normalized text is the complete human batch, not media enrichment.
        return normalized_text, False
    # Sticker, location, contact, poll, dice, game and service updates are not
    # human text/media transport. In particular, never forward a vision or
    # extracted-content enrichment from MessageEvent.text.
    raise RuntimeError("telegram_outbound_type_unsupported")


def _outbound_media(event: object, settings: _Settings, text: str) -> tuple[_OutboundMedia, ...]:
    raw_urls = _field(event, "media_urls", [])
    raw_types = _field(event, "media_types", [])
    raw_names = _field(event, "media_file_names", [])
    raw_captions = _field(event, "media_captions", [])
    if not raw_urls:
        single = _text(_field(event, "media_path")) or _text(
            _field(event, "media_url")
        )
        raw_urls = [single] if single else []
        raw_types = [_text(_field(event, "media_type"))] if single else []
    if not isinstance(raw_urls, (list, tuple)) or not isinstance(
        raw_types, (list, tuple)
    ):
        raise RuntimeError("outbound_media_invalid")
    if len(raw_urls) > _MAX_MEDIA:
        raise RuntimeError("media_count_exceeded")
    if raw_urls and not settings.media_roots:
        raise RuntimeError("media_roots_required")
    names = raw_names if isinstance(raw_names, (list, tuple)) else []
    if not names:
        names = _official_media_file_names(event)
    captions = raw_captions if isinstance(raw_captions, (list, tuple)) else []
    result: list[_OutboundMedia] = []
    total_size = 0
    for index, raw_url in enumerate(raw_urls):
        if not isinstance(raw_url, str) or not raw_url:
            raise RuntimeError("media_path_required")
        candidate = Path(raw_url)
        if candidate.is_symlink():
            raise RuntimeError("media_symlink_rejected")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_file() or not _contained(resolved, settings.media_roots):
            raise RuntimeError("media_path_rejected")
        digest, size, identity = _hash_stable_regular_file(
            resolved,
            code="media_path_rejected",
            maximum_bytes=_MAX_MEDIA_BYTES,
        )
        total_size += size
        if total_size > _MAX_TOTAL_MEDIA_BYTES:
            raise RuntimeError("media_total_too_large")
        media_hint = _text(raw_types[index]) if index < len(raw_types) else ""
        mime_type = media_hint.split(";", 1)[0].strip() if "/" in media_hint else ""
        supplied_name = _text(names[index]) if index < len(names) else ""
        safe_name = _bounded_file_name(supplied_name, resolved.name)
        caption = _text(captions[index]) if index < len(captions) else ""
        if not caption and index == 0:
            caption = text
        result.append(
            _OutboundMedia(
                source_path=resolved,
                source_device=identity[0],
                source_inode=identity[1],
                source_mtime_ns=identity[3],
                media_type=_kind(media_hint, str(resolved)),
                mime_type=mime_type,
                sha256=digest,
                size_bytes=size,
                caption=caption,
                file_name=safe_name,
            )
        )
    return tuple(result)


_OUTBOUND_SCHEMA = """
CREATE TABLE IF NOT EXISTS hermes_human_outbound (
  request_id TEXT PRIMARY KEY,
  telegram_message_id TEXT NOT NULL,
  forum_chat_id TEXT NOT NULL,
  telegram_thread_id TEXT NOT NULL,
  destination TEXT NOT NULL,
  destination_hash TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  text TEXT NOT NULL,
  media_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('prepared','sending','sent','failed','uncertain')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
  next_attempt_at INTEGER NOT NULL DEFAULT 0 CHECK(next_attempt_at >= 0),
  remote_message_ids_json TEXT NOT NULL DEFAULT '[]',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
)
"""


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    details = path.lstat()
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise RuntimeError("human_outbound_ledger_parent_invalid")
    resolved = os.path.normcase(str(path.resolve(strict=True)))
    absolute = os.path.normcase(os.path.abspath(path))
    if resolved != absolute:
        raise RuntimeError("human_outbound_ledger_parent_invalid")
    if os.name == "posix":
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            named = path.lstat()
            if (
                not stat.S_ISDIR(opened.st_mode)
                or stat.S_ISLNK(named.st_mode)
                or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            ):
                raise RuntimeError("human_outbound_ledger_parent_invalid")
            os.fchmod(descriptor, 0o700)
        finally:
            os.close(descriptor)
    else:
        os.chmod(path, 0o700)


class _OutboundDisarmed(RuntimeError):
    """The durable release-bound human outbound authority is not active."""


def _human_outbound_arm_identity(
    outbound: _HumanOutboundSettings,
) -> tuple[object, ...] | None:
    """Read and verify the private ARM file without ever repairing it.

    Absence is an ordinary, persistent disarmed state.  Every other malformed
    state is fail-closed.  The file binds authority to the release commit, the
    exact plugin bytes and the Hermes runtime loaded by this gateway process.
    """

    path = outbound.arm_file
    if path is None:
        return None
    if (
        not path.is_absolute()
        or not re.fullmatch(r"[0-9a-f]{40}", outbound.release_commit)
        or not re.fullmatch(r"[0-9a-f]{64}", outbound.plugin_sha256)
        or not re.fullmatch(
            r"[0-9a-f]{64}", outbound.hermes_runtime_fingerprint
        )
    ):
        raise RuntimeError("human_outbound_arm_invalid")
    try:
        _ensure_private_directory(path.parent)
        parent_details = path.parent.lstat()
    except (OSError, RuntimeError):
        raise RuntimeError("human_outbound_arm_parent_invalid") from None
    if os.name == "posix" and (
        parent_details.st_uid != os.geteuid()
        or stat.S_IMODE(parent_details.st_mode) != 0o700
    ):
        raise RuntimeError("human_outbound_arm_parent_invalid")
    try:
        named = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise RuntimeError("human_outbound_arm_invalid") from None
    if stat.S_ISLNK(named.st_mode) or not stat.S_ISREG(named.st_mode):
        raise RuntimeError("human_outbound_arm_invalid")
    if os.name == "posix" and (
        named.st_uid != os.geteuid() or (named.st_mode & 0o7777) != 0o600
    ):
        raise RuntimeError("human_outbound_arm_permissions_invalid")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise RuntimeError("human_outbound_arm_invalid") from None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            or opened.st_size > _MAX_HUMAN_OUTBOUND_ARM_BYTES
            or (
                os.name == "posix"
                and (
                    opened.st_uid != os.geteuid()
                    or (opened.st_mode & 0o7777) != 0o600
                )
            )
        ):
            raise RuntimeError("human_outbound_arm_invalid")
        raw = os.read(descriptor, _MAX_HUMAN_OUTBOUND_ARM_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > _MAX_HUMAN_OUTBOUND_ARM_BYTES:
        raise RuntimeError("human_outbound_arm_invalid")
    try:
        after = path.lstat()
    except OSError:
        raise RuntimeError("human_outbound_arm_invalid") from None
    if (
        stat.S_ISLNK(after.st_mode)
        or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
        or after.st_size != opened.st_size
        or after.st_mtime_ns != opened.st_mtime_ns
        or (
            os.name == "posix"
            and (
                after.st_ctime_ns != opened.st_ctime_ns
                or after.st_uid != os.geteuid()
                or (after.st_mode & 0o7777) != 0o600
            )
        )
    ):
        raise RuntimeError("human_outbound_arm_invalid")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("human_outbound_arm_invalid") from None
    if (
        not isinstance(value, dict)
        or frozenset(value) != _HUMAN_OUTBOUND_ARM_KEYS
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or value.get("armed") is not True
    ):
        raise RuntimeError("human_outbound_arm_invalid")
    if (
        value.get("release_commit") != outbound.release_commit
        or value.get("plugin_sha256") != outbound.plugin_sha256
        or value.get("hermes_runtime_fingerprint")
        != outbound.hermes_runtime_fingerprint
    ):
        raise RuntimeError("human_outbound_arm_mismatch")
    return (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
        outbound.release_commit,
        outbound.plugin_sha256,
        outbound.hermes_runtime_fingerprint,
    )


def _activate_human_outbound(outbound: _HumanOutboundSettings) -> bool:
    """Validate the persistent ARM file for this exact call."""

    try:
        identity = _human_outbound_arm_identity(outbound)
    except Exception as exc:
        code = (
            str(exc)
            if isinstance(exc, RuntimeError)
            else "human_outbound_arm_invalid"
        )
        _signal_failure(code)
        return False
    return identity is not None


def _require_human_outbound_arm(outbound: _HumanOutboundSettings) -> None:
    try:
        identity = _human_outbound_arm_identity(outbound)
    except Exception as exc:
        code = (
            str(exc)
            if isinstance(exc, RuntimeError)
            else "human_outbound_arm_invalid"
        )
        raise _OutboundDisarmed(code) from None
    if identity is None:
        raise _OutboundDisarmed("human_outbound_disarmed")


def _harden_existing_regular_file(path: Path) -> None:
    details = path.lstat()
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise RuntimeError("human_outbound_ledger_invalid")
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        named = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(named.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise RuntimeError("human_outbound_ledger_invalid")
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def _prepare_outbound_ledger_path(ledger_file: Path) -> None:
    parent = ledger_file.parent
    _ensure_private_directory(parent)
    try:
        details = ledger_file.lstat()
    except FileNotFoundError:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(ledger_file, flags, 0o600)
        except FileExistsError:
            # Another local initializer won the exclusive create.  It must
            # still pass the same no-symlink/regular-file checks below.
            pass
        else:
            try:
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _fsync_directory(parent)
        details = ledger_file.lstat()
    _harden_existing_regular_file(ledger_file)


def _harden_outbound_sqlite_files(ledger_file: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(f"{ledger_file}{suffix}")
        try:
            details = candidate.lstat()
        except FileNotFoundError:
            continue
        _harden_existing_regular_file(candidate)


def _outbound_connection(outbound: _HumanOutboundSettings) -> sqlite3.Connection:
    _prepare_outbound_ledger_path(outbound.ledger_file)
    # Reject hostile or non-regular pre-existing SQLite sidecars before SQLite
    # is allowed to resolve/open them by name.
    _harden_outbound_sqlite_files(outbound.ledger_file)
    connection = sqlite3.connect(outbound.ledger_file, timeout=10)
    try:
        journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
        if not journal_mode or str(journal_mode[0]).lower() != "wal":
            raise RuntimeError("human_outbound_ledger_wal_required")
        _harden_outbound_sqlite_files(outbound.ledger_file)
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(_OUTBOUND_SCHEMA)
        connection.commit()
        # SQLite derives WAL/journal modes from the already-private main DB.
        # Revalidate and harden every sidecar before this connection is handed
        # to code that writes destinations, text, or media manifests.
        _harden_outbound_sqlite_files(outbound.ledger_file)
    except Exception:
        connection.close()
        raise
    return connection


def _request_media_root(outbound: _HumanOutboundSettings, request_id: str) -> Path:
    return outbound.managed_media_root / _sha256(request_id)


def _cleanup_request_media(outbound: _HumanOutboundSettings, request_id: str) -> None:
    request_root = _request_media_root(outbound, request_id)
    try:
        details = request_root.lstat()
    except FileNotFoundError:
        return
    if request_root.is_symlink() or not request_root.is_dir():
        raise RuntimeError("managed_media_request_root_invalid")
    if request_root.parent.resolve(strict=True) != outbound.managed_media_root:
        raise RuntimeError("managed_media_request_root_invalid")
    for item in request_root.iterdir():
        if item.is_symlink() or not item.is_file():
            raise RuntimeError("managed_media_request_entry_invalid")
        item.unlink()
    request_root.rmdir()


def _stage_outbound_media(
    outbound: _HumanOutboundSettings,
    request_id: str,
    media: tuple[_OutboundMedia, ...],
) -> tuple[dict[str, object], ...]:
    if not media:
        return ()
    if any(item.size_bytes > _MAX_MEDIA_BYTES for item in media):
        raise RuntimeError("media_too_large")
    if sum(item.size_bytes for item in media) > _MAX_TOTAL_MEDIA_BYTES:
        raise RuntimeError("media_total_too_large")
    request_root = _request_media_root(outbound, request_id)
    if request_root.exists() or request_root.is_symlink():
        raise RuntimeError("managed_media_collision")
    request_root.mkdir(mode=0o700)
    staged: list[dict[str, object]] = []
    try:
        for index, item in enumerate(media):
            suffix = item.source_path.suffix.lower()[:16]
            final = request_root / f"{index:02d}{suffix}"
            temporary = request_root / f".{index:02d}{suffix}.tmp"
            source_identity = (
                item.source_device,
                item.source_inode,
                item.size_bytes,
                item.source_mtime_ns,
            )
            source_descriptor, opened = _open_verified_regular(
                item.source_path,
                code="media_path_rejected",
                maximum_bytes=_MAX_MEDIA_BYTES,
                expected_identity=source_identity,
            )
            target_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            target_flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(
                os, "O_CLOEXEC", 0
            )
            try:
                target_descriptor = os.open(temporary, target_flags, 0o600)
            except Exception:
                os.close(source_descriptor)
                raise
            digest = hashlib.sha256()
            copied = 0
            try:
                with os.fdopen(source_descriptor, "rb", closefd=True) as source, os.fdopen(
                    target_descriptor, "wb", closefd=True
                ) as target:
                    if os.name == "posix":
                        os.fchmod(target.fileno(), 0o600)
                    else:
                        os.chmod(temporary, 0o600)
                    while chunk := source.read(1024 * 1024):
                        copied += len(chunk)
                        if copied > _MAX_MEDIA_BYTES:
                            raise RuntimeError("media_too_large")
                        digest.update(chunk)
                        target.write(chunk)
                    _verify_opened_regular_unchanged(
                        item.source_path,
                        source.fileno(),
                        opened,
                        code="media_changed_during_copy",
                    )
                    target.flush()
                    os.fsync(target.fileno())
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
            if digest.hexdigest() != item.sha256 or copied != item.size_bytes:
                raise RuntimeError("managed_media_hash_mismatch")
            os.replace(temporary, final)
            os.chmod(final, 0o600)
            staged.append(
                {
                    "filePath": str(final),
                    "mediaType": item.media_type,
                    "mimeType": item.mime_type,
                    "sha256": item.sha256,
                    "sizeBytes": item.size_bytes,
                    "caption": item.caption,
                    "fileName": item.file_name,
                }
            )
        if os.name == "posix":
            directory_fd = os.open(request_root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return tuple(staged)
    except Exception:
        _cleanup_request_media(outbound, request_id)
        raise


def _prepare_outbound(
    outbound: _HumanOutboundSettings,
    *,
    request_id: str,
    message_id: str,
    forum_chat_id: str,
    thread_id: str,
    destination: str,
    text: str,
    payload_sha256: str,
    media: tuple[_OutboundMedia, ...],
) -> bool:
    _require_human_outbound_arm(outbound)
    connection = _outbound_connection(outbound)
    staged_created = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing = connection.execute(
                "SELECT payload_sha256 FROM hermes_human_outbound WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if existing is not None:
                connection.rollback()
                if str(existing[0]) != payload_sha256:
                    raise RuntimeError("outbound_replay_conflict")
                return False
            # A crash before the transaction committed may have left only the
            # private request directory.  There is no durable job in that case,
            # so it is safe to remove and rebuild it from the current event.
            _cleanup_request_media(outbound, request_id)
            staged = _stage_outbound_media(outbound, request_id, media)
            staged_created = bool(media)
            # Staging can be comparatively slow.  Revocation before the
            # durable reservation must leave no prepared row or media copy.
            _require_human_outbound_arm(outbound)
            now = int(time.time())
            connection.execute(
                """INSERT INTO hermes_human_outbound(
                request_id,telegram_message_id,forum_chat_id,telegram_thread_id,
                destination,destination_hash,payload_sha256,text,media_json,status,
                created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,'prepared',?,?)""",
                (
                    request_id,
                    message_id,
                    forum_chat_id,
                    thread_id,
                    destination,
                    _sha256(destination),
                    payload_sha256,
                    text,
                    json.dumps(staged, sort_keys=True, separators=(",", ":")),
                    now,
                    now,
                ),
            )
        except Exception:
            connection.rollback()
            if staged_created:
                _cleanup_request_media(outbound, request_id)
            raise
        connection.commit()
        return True
    finally:
        connection.close()


def _decode_outbound_media(value: object) -> tuple[dict[str, object], ...]:
    try:
        raw = json.loads(_text(value))
    except json.JSONDecodeError:
        raise RuntimeError("outbound_media_manifest_invalid") from None
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise RuntimeError("outbound_media_manifest_invalid")
    return tuple(raw)


def _claim_prepared_outbound(outbound: _HumanOutboundSettings) -> _OutboundJob | None:
    _require_human_outbound_arm(outbound)
    connection = _outbound_connection(outbound)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT request_id,destination,text,media_json,attempt_count
               FROM hermes_human_outbound
               WHERE status='prepared' AND next_attempt_at<=?
               ORDER BY created_at,request_id
               LIMIT 1""",
            (int(time.time()),),
        ).fetchone()
        if row is None:
            connection.rollback()
            return None
        request_id = str(row[0])
        media = _decode_outbound_media(row[3])
        attempt_count = int(row[4]) + 1
        updated = connection.execute(
            """UPDATE hermes_human_outbound
               SET status='sending',attempt_count=?,next_attempt_at=0,updated_at=?
               WHERE request_id=? AND status='prepared'""",
            (attempt_count, int(time.time()), request_id),
        ).rowcount
        if updated != 1:
            connection.rollback()
            raise RuntimeError("outbound_claim_rejected")
        _require_human_outbound_arm(outbound)
        connection.commit()
        return _OutboundJob(
            request_id=request_id,
            destination=str(row[1]),
            text=str(row[2]),
            media=media,
            attempt_count=attempt_count,
        )
    finally:
        connection.close()


def _recover_outbound(outbound: _HumanOutboundSettings) -> int:
    """Conservatively quarantine jobs interrupted after the send claim."""

    connection = _outbound_connection(outbound)
    try:
        updated = connection.execute(
            """UPDATE hermes_human_outbound
               SET status='uncertain',updated_at=?
               WHERE status='sending'""",
            (int(time.time()),),
        ).rowcount
        connection.commit()
        return updated
    finally:
        connection.close()


def _release_disarmed_outbound_claim(
    outbound: _HumanOutboundSettings,
    job: _OutboundJob,
) -> None:
    """Return a proven pre-send claim to the durable prepared queue."""

    connection = _outbound_connection(outbound)
    try:
        updated = connection.execute(
            """UPDATE hermes_human_outbound
               SET status='prepared',attempt_count=CASE
                     WHEN attempt_count > 0 THEN attempt_count - 1 ELSE 0 END,
                   next_attempt_at=0,updated_at=?
               WHERE request_id=? AND status='sending'""",
            (int(time.time()), job.request_id),
        ).rowcount
        connection.commit()
        if updated != 1:
            raise RuntimeError("outbound_disarmed_release_rejected")
    finally:
        connection.close()


def _finish_outbound(
    outbound: _HumanOutboundSettings,
    request_id: str,
    status: str,
    remote_message_ids: tuple[str, ...] = (),
) -> None:
    if status not in {"sent", "failed", "uncertain"}:
        raise ValueError("outbound_status_invalid")
    connection = _outbound_connection(outbound)
    try:
        updated = connection.execute(
            """UPDATE hermes_human_outbound
               SET status=?,remote_message_ids_json=?,updated_at=?
               WHERE request_id=? AND status='sending'""",
            (
                status,
                json.dumps(remote_message_ids, separators=(",", ":")),
                int(time.time()),
                request_id,
            ),
        ).rowcount
        connection.commit()
        if updated != 1:
            raise RuntimeError("outbound_state_transition_rejected")
    finally:
        connection.close()


def _append_topic_context(
    session_store: object,
    source: object,
    message_id: str,
    text: str,
    media: tuple[_OutboundMedia, ...],
) -> None:
    if session_store is None:
        raise RuntimeError("session_store_required")
    entry = session_store.get_or_create_session(source)
    session_id = _field(entry, "session_id")
    if not session_id:
        raise RuntimeError("session_id_required")
    platform_message_id = (
        f"telegram:{_text(_field(source, 'chat_id'))}:"
        f"{_text(_field(source, 'thread_id'))}:{message_id}"
    )
    if session_store.has_platform_message_id(session_id, platform_message_id):
        return
    session_store.append_to_transcript(
        session_id,
        {
            "role": "user",
            "content": text,
            "platform_message_id": platform_message_id,
            "observed": True,
            "timestamp": int(time.time()),
            "transport_intent": "whatsapp_human_outbound",
            "media": [
                {
                    "media_type": item.media_type,
                    "mime_type": item.mime_type,
                    "sha256": item.sha256,
                    "size_bytes": item.size_bytes,
                    "file_name": item.file_name,
                }
                for item in media
            ],
        },
    )


class _BridgeRejected(RuntimeError):
    pass


class _BridgeRetryable(RuntimeError):
    """The bridge proved that no WhatsApp send was attempted."""


class _BridgeUncertain(RuntimeError):
    def __init__(self, message_ids: tuple[str, ...] = ()) -> None:
        super().__init__("bridge_delivery_uncertain")
        self.message_ids = message_ids


def _post_human_outbound(
    outbound: _HumanOutboundSettings,
    job: _OutboundJob,
) -> tuple[str, ...]:
    _require_human_outbound_arm(outbound)
    token = outbound.token_file.read_text(encoding="utf-8").strip()
    if not token or len(token.encode("utf-8")) > 4096:
        raise _BridgeRejected("bridge_token_invalid")
    body = json.dumps(
        {
            "requestId": job.request_id,
            "chatId": job.destination,
            "text": job.text,
            "media": list(job.media),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(body) > 2 * 1024 * 1024:
        raise _BridgeRejected("bridge_payload_too_large")
    connection = http.client.HTTPConnection(
        _HUMAN_OUTBOUND_HOST,
        _HUMAN_OUTBOUND_PORT,
        timeout=outbound.timeout_seconds,
    )
    try:
        try:
            # Revalidate immediately before the only operation that can cross
            # the loopback transport boundary.
            _require_human_outbound_arm(outbound)
            connection.request(
                "POST",
                _HUMAN_OUTBOUND_PATH,
                body=body,
                headers={
                    "content-type": "application/json",
                    "x-espelho-token": token,
                },
            )
        except ConnectionRefusedError:
            # The loopback TCP connection never opened, so the bridge could
            # not have attempted a WhatsApp send. This is the same proven
            # pre-send condition as an explicit 503 {attempted:false}.
            raise _BridgeRetryable("bridge_pre_send_unavailable") from None
        response = connection.getresponse()
        response_body = response.read(65537)
        if len(response_body) > 65536:
            raise RuntimeError("bridge_response_too_large")
        try:
            result = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            if response.status >= 500:
                raise _BridgeUncertain() from None
            raise _BridgeRejected("bridge_response_invalid") from None
        if not isinstance(result, Mapping):
            if response.status >= 500:
                raise _BridgeUncertain()
            raise _BridgeRejected("bridge_response_invalid")
        raw_ids = result.get("messageIds")
        if isinstance(raw_ids, list):
            message_ids = tuple(_text(value) for value in raw_ids if _text(value))
        else:
            message_id = _text(result.get("messageId"))
            message_ids = (message_id,) if message_id else ()
        if response.status >= 500 and result.get("attempted") is False:
            raise _BridgeRetryable("bridge_pre_send_unavailable")
        if result.get("uncertain") is True or response.status >= 500:
            raise _BridgeUncertain(message_ids)
        if response.status < 200 or response.status >= 300:
            raise _BridgeRejected("bridge_rejected")
        if not isinstance(result, Mapping) or result.get("success") is not True:
            raise _BridgeRejected("bridge_rejected")
        return message_ids
    finally:
        connection.close()


def _retry_or_fail_outbound(
    outbound: _HumanOutboundSettings,
    job: _OutboundJob,
) -> str:
    if job.attempt_count >= _MAX_OUTBOUND_ATTEMPTS:
        _finish_outbound(outbound, job.request_id, "failed")
        return "failed"
    # Short deterministic backoff: 2s after the first attempt and 4s after the
    # second. The timestamp is durable; the executor merely waits/wakes it.
    delay = min(2**job.attempt_count, 10)
    connection = _outbound_connection(outbound)
    try:
        updated = connection.execute(
            """UPDATE hermes_human_outbound
               SET status='prepared',next_attempt_at=?,updated_at=?
               WHERE request_id=? AND status='sending'""",
            (
                int(time.time()) + delay,
                int(time.time()),
                job.request_id,
            ),
        ).rowcount
        connection.commit()
        if updated != 1:
            raise RuntimeError("outbound_retry_transition_rejected")
    finally:
        connection.close()
    return "prepared"


def _dispatch_human_outbound(
    outbound: _HumanOutboundSettings,
    job: _OutboundJob,
) -> None:
    try:
        message_ids = _post_human_outbound(outbound, job)
    except _OutboundDisarmed:
        _release_disarmed_outbound_claim(outbound, job)
        return
    except _BridgeRetryable as exc:
        _signal_failure(type(exc).__name__)
        result = _retry_or_fail_outbound(outbound, job)
        if result == "failed":
            _cleanup_request_media(outbound, job.request_id)
        return
    except _BridgeUncertain as exc:
        _signal_failure(type(exc).__name__)
        _finish_outbound(outbound, job.request_id, "uncertain", exc.message_ids)
        return
    except _BridgeRejected as exc:
        _signal_failure(type(exc).__name__)
        _finish_outbound(outbound, job.request_id, "failed")
        _cleanup_request_media(outbound, job.request_id)
        return
    except Exception as exc:
        # Once the HTTP request begins, a timeout or broken response is
        # ambiguous: reserve it as uncertain and never replay automatically.
        _signal_failure(type(exc).__name__)
        _finish_outbound(outbound, job.request_id, "uncertain")
        return
    try:
        _finish_outbound(outbound, job.request_id, "sent", message_ids)
    except Exception as exc:
        # The bridge confirmed the send, but local acknowledgement did not
        # commit. Preserve the managed copy and the sending row; restart will
        # quarantine it as uncertain and never resend.
        _signal_failure(type(exc).__name__)
        return
    try:
        _cleanup_request_media(outbound, job.request_id)
    except OSError as exc:
        _signal_failure(type(exc).__name__)


def _seconds_until_prepared(outbound: _HumanOutboundSettings) -> float | None:
    connection = _outbound_connection(outbound)
    try:
        row = connection.execute(
            """SELECT MIN(next_attempt_at)
               FROM hermes_human_outbound
               WHERE status='prepared'"""
        ).fetchone()
    finally:
        connection.close()
    if row is None or row[0] is None:
        return None
    return max(0.0, float(int(row[0]) - int(time.time())))


def _drain_human_outbound(outbound: _HumanOutboundSettings) -> None:
    while True:
        if not _activate_human_outbound(outbound):
            return
        job = _claim_prepared_outbound(outbound)
        if job is None:
            wait_seconds = _seconds_until_prepared(outbound)
            if wait_seconds is None:
                return
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            continue
        try:
            _require_human_outbound_arm(outbound)
            _dispatch_human_outbound(outbound, job)
        except _OutboundDisarmed:
            try:
                _release_disarmed_outbound_claim(outbound, job)
            except Exception as release_exc:
                _signal_failure(type(release_exc).__name__)
            return
        except Exception as exc:
            # A claimed job can no longer be assumed unsent. Quarantine it
            # conservatively and leave subsequent prepared jobs for the next
            # wake-up instead of losing the worker thread silently.
            _signal_failure(type(exc).__name__)
            try:
                _finish_outbound(outbound, job.request_id, "uncertain")
            except Exception as finish_exc:
                _signal_failure(type(finish_exc).__name__)
            return


def _has_prepared_outbound(outbound: _HumanOutboundSettings) -> bool:
    connection = _outbound_connection(outbound)
    try:
        return (
            connection.execute(
                "SELECT 1 FROM hermes_human_outbound WHERE status='prepared' LIMIT 1"
            ).fetchone()
            is not None
        )
    finally:
        connection.close()


def _outbound_wake_runner(outbound: _HumanOutboundSettings, key: str) -> None:
    try:
        while True:
            _drain_human_outbound(outbound)
            # Keep ownership while checking the queue. A producer commits its
            # row before calling `_submit_human_outbound`; if it races here it
            # either becomes visible to this query or blocks on the wake lock
            # until ownership is released and then schedules a new runner.
            with _OUTBOUND_WAKE_LOCK:
                try:
                    has_more = _activate_human_outbound(
                        outbound
                    ) and _has_prepared_outbound(outbound)
                except Exception as exc:
                    _signal_failure(type(exc).__name__)
                    _OUTBOUND_WAKE_PENDING.discard(key)
                    return
                if not has_more:
                    _OUTBOUND_WAKE_PENDING.discard(key)
                    return
    except Exception as exc:
        _signal_failure(type(exc).__name__)
        with _OUTBOUND_WAKE_LOCK:
            _OUTBOUND_WAKE_PENDING.discard(key)


def _submit_human_outbound(outbound: _HumanOutboundSettings) -> None:
    if not _activate_human_outbound(outbound):
        return
    key = str(outbound.ledger_file)
    with _OUTBOUND_WAKE_LOCK:
        if key in _OUTBOUND_WAKE_PENDING:
            return
        _OUTBOUND_WAKE_PENDING.add(key)
    try:
        _OUTBOUND_EXECUTOR.submit(_outbound_wake_runner, outbound, key)
    except Exception:
        with _OUTBOUND_WAKE_LOCK:
            _OUTBOUND_WAKE_PENDING.discard(key)
        raise


def _outbound_rearm_watcher_key(outbound: _HumanOutboundSettings) -> str:
    arm = outbound.arm_file
    return "\x1f".join(
        (
            os.path.abspath(outbound.ledger_file),
            os.path.abspath(arm) if arm is not None else "",
        )
    )


def _outbound_rearm_watch_loop(
    outbound: _HumanOutboundSettings,
    key: str,
    stop: threading.Event,
    poll_seconds: float,
) -> None:
    """Wake durable work once an externally recreated ARM becomes valid."""

    last_arm_identity: tuple[object, ...] | None = None
    last_error = ""
    try:
        while not stop.is_set():
            if not outbound.ledger_file.parent.exists():
                return
            try:
                arm_identity = _human_outbound_arm_identity(outbound)
                if arm_identity is None:
                    last_arm_identity = None
                elif arm_identity != last_arm_identity:
                    if _has_prepared_outbound(outbound):
                        _submit_human_outbound(outbound)
                    last_arm_identity = arm_identity
                last_error = ""
            except Exception as exc:
                code = (
                    str(exc)
                    if isinstance(exc, RuntimeError)
                    else type(exc).__name__
                )
                if code != last_error:
                    _signal_failure(code)
                    last_error = code
            if stop.wait(poll_seconds):
                return
    finally:
        with _OUTBOUND_REARM_WATCH_LOCK:
            current = _OUTBOUND_REARM_WATCHERS.get(key)
            if current is not None and current[0] is threading.current_thread():
                _OUTBOUND_REARM_WATCHERS.pop(key, None)


def _start_human_outbound_rearm_watcher(
    outbound: _HumanOutboundSettings,
    *,
    poll_seconds: float = _OUTBOUND_REARM_WATCH_INTERVAL_SECONDS,
) -> tuple[threading.Thread, threading.Event]:
    """Start at most one daemon watcher for an ARM/ledger pair."""

    if poll_seconds <= 0:
        raise ValueError("outbound_rearm_watch_interval_invalid")
    key = _outbound_rearm_watcher_key(outbound)
    with _OUTBOUND_REARM_WATCH_LOCK:
        current = _OUTBOUND_REARM_WATCHERS.get(key)
        if current is not None and current[0].is_alive():
            return current
        stop = threading.Event()
        thread = threading.Thread(
            target=_outbound_rearm_watch_loop,
            args=(outbound, key, stop, poll_seconds),
            name=f"espelho-zap-rearm-{_sha256(key)[:12]}",
            daemon=True,
        )
        _OUTBOUND_REARM_WATCHERS[key] = (thread, stop)
        try:
            thread.start()
        except Exception:
            _OUTBOUND_REARM_WATCHERS.pop(key, None)
            stop.set()
            raise
        return thread, stop


def _stop_human_outbound_rearm_watcher(
    outbound: _HumanOutboundSettings,
    *,
    timeout: float = 2.0,
) -> None:
    """Bounded local lifecycle hook used by tests and controlled shutdowns."""

    key = _outbound_rearm_watcher_key(outbound)
    with _OUTBOUND_REARM_WATCH_LOCK:
        current = _OUTBOUND_REARM_WATCHERS.get(key)
    if current is None:
        return
    thread, stop = current
    stop.set()
    if thread is not threading.current_thread():
        thread.join(timeout=max(0.0, timeout))


def _write_startup_marker(settings: _Settings) -> None:
    """Prove that the gateway registered this exact plugin generation."""

    raw_path = os.environ.get(
        "ESPELHO_ZAP_HUMAN_OUTBOUND_STARTUP_MARKER", ""
    ).strip()
    if not raw_path:
        return
    path = Path(raw_path)
    release_commit = os.environ.get("ESPELHO_ZAP_RELEASE_COMMIT", "").strip()
    if not path.is_absolute() or not re.fullmatch(r"[0-9a-f]{40}", release_commit):
        raise RuntimeError("human_outbound_startup_marker_invalid")
    _ensure_private_directory(path.parent)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise RuntimeError("human_outbound_startup_marker_invalid")
    source = Path(__file__).resolve(strict=True)
    plugin_sha256 = (
        settings.human_outbound.plugin_sha256
        if settings.human_outbound is not None
        else hashlib.sha256(source.read_bytes()).hexdigest()
    )
    hermes_runtime_fingerprint = settings.hermes_runtime_fingerprint
    if (
        settings.human_outbound is not None
        and settings.human_outbound.hermes_runtime_fingerprint
        != hermes_runtime_fingerprint
    ):
        raise RuntimeError("human_outbound_startup_marker_invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", hermes_runtime_fingerprint):
        raise RuntimeError("human_outbound_startup_marker_invalid")
    payload = {
        "schema_version": 1,
        "plugin_version": _PLUGIN_VERSION,
        "release_commit": release_commit,
        "plugin_sha256": plugin_sha256,
        "hermes_runtime_fingerprint": hermes_runtime_fingerprint,
        "human_outbound_enabled": settings.human_outbound is not None,
        "human_outbound_armed": False,
        "gateway_pid": os.getpid(),
        "registered_at": int(time.time()),
    }
    if settings.human_outbound is not None:
        try:
            payload["human_outbound_armed"] = (
                _human_outbound_arm_identity(settings.human_outbound) is not None
            )
        except Exception:
            payload["human_outbound_armed"] = False
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _build_hook(
    settings: _Settings,
    ready: threading.Event | None = None,
):
    def pre_gateway_dispatch(event, gateway, session_store, **kwargs):
        del gateway, kwargs
        source = _field(event, "source")
        platform = _text(_field(source, "platform")).lower()
        if ready is not None and not ready.is_set():
            chat_id = _text(_field(source, "chat_id"))
            if platform == "whatsapp" or (
                platform == "telegram"
                and settings.telegram_forum_chat_id
                and chat_id == settings.telegram_forum_chat_id
            ):
                return {
                    "action": "skip",
                    "reason": "espelho-zap-plugin-not-ready",
                }
            return None
        if platform == "telegram":
            chat_id = _text(_field(source, "chat_id"))
            if (
                settings.telegram_forum_chat_id
                and chat_id == settings.telegram_forum_chat_id
            ):
                outbound = settings.human_outbound
                if outbound is None:
                    return {
                        "action": "skip",
                        "reason": "espelho-zap-forum-data-plane",
                    }
                user_id = _text(_field(source, "user_id")) or _text(
                    _field(source, "sender_id")
                )
                if (
                    user_id not in outbound.allowed_users
                    or _is_bot_or_assistant(event, source)
                    or _is_service_or_automation(event, source)
                ):
                    return {
                        "action": "skip",
                        "reason": "espelho-zap-human-outbound-not-authorized",
                    }
                if not _activate_human_outbound(outbound):
                    return {
                        "action": "skip",
                        "reason": "espelho-zap-human-outbound-disarmed",
                    }
                try:
                    thread_id = _text(_field(source, "thread_id")) or _text(
                        _field(event, "message_thread_id")
                    )
                    message_id = (
                        _text(_field(source, "message_id"))
                        or _text(_field(event, "message_id"))
                        or _text(_field(event, "platform_update_id"))
                    )
                    if not thread_id or not message_id:
                        raise RuntimeError("telegram_topic_identity_required")
                    body, expects_media = _telegram_human_payload(event)
                    media = _outbound_media(event, settings, body) if expects_media else ()
                    if expects_media != bool(media):
                        raise RuntimeError("telegram_current_media_mismatch")
                    if not body.strip() and not media:
                        raise RuntimeError("outbound_content_required")
                    destination = _reverse_route(settings, outbound, thread_id)
                    request_id = f"telegram:{chat_id}:{thread_id}:{message_id}"
                    digest_material = {
                        "requestId": request_id,
                        "destination": _sha256(destination),
                        "text": body,
                        "media": [
                            {
                                "mediaType": item.media_type,
                                "mimeType": item.mime_type,
                                "sha256": item.sha256,
                                "sizeBytes": item.size_bytes,
                                "caption": item.caption,
                                "fileName": item.file_name,
                            }
                            for item in media
                        ],
                    }
                    payload_sha256 = hashlib.sha256(
                        json.dumps(
                            digest_material,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                    prepared = _prepare_outbound(
                        outbound,
                        request_id=request_id,
                        message_id=message_id,
                        forum_chat_id=chat_id,
                        thread_id=thread_id,
                        destination=destination,
                        text=body,
                        payload_sha256=payload_sha256,
                        media=media,
                    )
                    if not prepared:
                        return {
                            "action": "skip",
                            "reason": "espelho-zap-human-outbound-replay-blocked",
                        }
                    try:
                        _append_topic_context(
                            session_store, source, message_id, body, media
                        )
                    except Exception as exc:
                        _signal_failure(type(exc).__name__)
                        # Context projection is a consumer of the durable send
                        # intent, not an authority gate. Delivery must continue
                        # even when the Hermes transcript store is unavailable.
                    try:
                        _submit_human_outbound(outbound)
                    except Exception as exc:
                        _signal_failure(type(exc).__name__)
                        # The prepared job is already durable. A later wake or
                        # process restart will drain it; never downgrade it to
                        # failed merely because the in-memory wake-up failed.
                    return {
                        "action": "skip",
                        "reason": "espelho-zap-human-outbound",
                    }
                except Exception as exc:
                    _signal_failure(type(exc).__name__)
                    return {
                        "action": "skip",
                        "reason": "espelho-zap-human-outbound-blocked",
                    }
            return None
        if platform != "whatsapp":
            return None
        if not settings.native_whatsapp_capture:
            return {
                "action": "skip",
                "reason": "espelho-zap-native-whatsapp-blocked",
            }
        try:
            payload = _normalize_message_event(event, settings)
            if payload is None:
                raise RuntimeError("capture_payload_missing")
            _run_ingest(payload, settings)
            _persist_health(settings, success=True)
        except Exception as exc:
            # Passive fail-closed semantics: a WhatsApp inbound is never
            # released to an agent, even when local capture fails.  Only a
            # sanitized aggregate code is retained locally.
            code = str(getattr(exc, "code", "") or type(exc).__name__)
            _signal_failure(code)
            try:
                _persist_health(settings, success=False, error_code=code)
            except Exception:
                _signal_failure("health_persist_failed")
        return {"action": "skip", "reason": "espelho-zap-passive"}

    return pre_gateway_dispatch


def register(ctx):
    settings = _Settings.from_environment()
    outbound = settings.human_outbound
    if outbound is not None:
        # Loading the plugin always initializes/verifies the durable ledger,
        # even while the persistent release-bound ARM file is absent.
        connection = _outbound_connection(outbound)
        connection.close()
        # Recovery only quarantines a pre-crash `sending` row; it performs no
        # outbound and is therefore safe and necessary while disarmed.
        _recover_outbound(outbound)
    ready = threading.Event()
    ctx.register_hook("pre_gateway_dispatch", _build_hook(settings, ready))
    _write_startup_marker(settings)
    if outbound is not None:
        _start_human_outbound_rearm_watcher(outbound)
    ready.set()
    # The marker proves this exact plugin generation registered before a
    # release-bound ARM is allowed to wake the durable queue.
    if outbound is not None and _activate_human_outbound(outbound):
        _submit_human_outbound(outbound)
