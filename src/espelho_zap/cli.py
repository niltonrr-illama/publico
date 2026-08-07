"""Command-line operations for the portable mirror.

Every operational command emits one JSON object. Outputs contain identifiers,
counts, and state only; message text, captions, local media paths, and tokens
are never echoed.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import fields, is_dataclass, replace
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
from typing import Any, Mapping, Sequence
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request
import uuid

from .config import (
    AppConfig,
    ConfigError,
    ensure_runtime_paths,
    load_config,
    private_mode,
    resolve_telegram_token,
    write_default_config,
)


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CONFIG = 3
EXIT_UNHEALTHY = 4
EXIT_OPERATION = 5
EXIT_INTERRUPTED = 130
MAX_EVENT_JSON_BYTES = 4 * 1024 * 1024
MAX_TELEGRAM_RESPONSE_BYTES = 1024 * 1024
_SHA256_CHUNK_BYTES = 1024 * 1024

EXIT_CODE_HELP = """exit codes:
  0 success
  2 invalid command or input
  3 missing or unsafe configuration/secret
  4 doctor or health check failed
  5 operation failed safely
  130 interrupted
"""

_SENSITIVE_KEYS = {
    "text",
    "caption",
    "content",
    "body",
    "payload",
    "payload_json",
    "token",
    "secret",
    "password",
    "media_path",
    "path",
}
_COUNT_FIELDS = {
    "routes_seen",
    "routes_created",
    "routes_updated",
    "routes_unchanged",
    "routes_created_or_updated",
    "recent_ids_seen",
    "recent_ids_added",
    "legacy_ids_seen",
    "legacy_ids_added",
    "tombstones_created",
    "aliases_seen",
    "aliases_created_or_updated",
    "skipped_records",
    "events",
    "routes",
    "active_leases",
}


class CLIError(ValueError):
    def __init__(self, code: str, message: str, *, exit_code: int = EXIT_USAGE):
        self.code = code
        self.safe_message = message
        self.exit_code = exit_code
        super().__init__(message)


class JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise CLIError("invalid_arguments", "invalid command arguments")


def _package_version() -> str:
    try:
        return metadata.version("espelho-zap-portable")
    except metadata.PackageNotFoundError:
        return "0.3.1"


def _core() -> SimpleNamespace:
    try:
        from espelho_zap import (  # type: ignore[attr-defined]
            InboundEvent,
            GroupGrill,
            HumanCanary,
            DryRunTransport,
            MediaAttachment,
            MirrorLedger,
            MirrorConsumers,
            MirrorWorker,
            Route,
            TelegramBotTransport,
            import_legacy_runtime_config,
            opaque_ref,
            sanitize_captured_event,
            stage_event_media,
            remove_managed_media,
            installation_state,
        )
    except (ImportError, AttributeError) as exc:
        raise CLIError(
            "core_unavailable",
            "portable mirror core is not installed completely",
            exit_code=EXIT_OPERATION,
        ) from exc
    return SimpleNamespace(
        InboundEvent=InboundEvent,
        GroupGrill=GroupGrill,
        HumanCanary=HumanCanary,
        DryRunTransport=DryRunTransport,
        MediaAttachment=MediaAttachment,
        MirrorLedger=MirrorLedger,
        MirrorConsumers=MirrorConsumers,
        MirrorWorker=MirrorWorker,
        Route=Route,
        TelegramBotTransport=TelegramBotTransport,
        import_legacy_runtime_config=import_legacy_runtime_config,
        opaque_ref=opaque_ref,
        sanitize_captured_event=sanitize_captured_event,
        stage_event_media=stage_event_media,
        remove_managed_media=remove_managed_media,
        installation_state=installation_state,
    )


def _emit(payload: Mapping[str, object], *, pretty: bool = False) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )
    )


def _config_for(args: argparse.Namespace) -> AppConfig:
    return load_config(args.config)


def _opaque_summary(value: object) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:20]


def _opaque_boundary_ref(core: SimpleNamespace, namespace: str, raw_value: str) -> str:
    if re.fullmatch(r"[a-z][a-z0-9_-]{0,31}:[0-9a-f]{64}", raw_value):
        return raw_value
    return str(core.opaque_ref(namespace, raw_value))


def _source_profile_ref(core: SimpleNamespace, raw_value: str) -> str:
    return _opaque_boundary_ref(core, "profile", raw_value)


def _profile_scoped_ref(
    core: SimpleNamespace,
    namespace: str,
    raw_value: str,
    source_profile: str,
) -> str:
    """Hash a runtime identity inside one explicit source-profile boundary."""

    if re.fullmatch(r"[a-z][a-z0-9_-]{0,31}:[0-9a-f]{64}", raw_value):
        return raw_value
    profile_ref = _source_profile_ref(core, source_profile)
    return str(core.opaque_ref(namespace, f"{profile_ref}\x1f{raw_value}"))


def _profile_for(args: argparse.Namespace, config: AppConfig) -> str:
    configured = getattr(args, "source_profile", None)
    return str(configured or config.worker.profile_id)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("positive integer required") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("positive integer required")
    return parsed


def _negative_chat_id(value: str) -> str:
    if not re.fullmatch(r"-[0-9]+", value) or int(value) >= 0:
        raise argparse.ArgumentTypeError("negative Telegram supergroup id required")
    return value


def _route_summary(route: object, *, show_identifiers: bool = False) -> dict[str, object]:
    conversation_id = str(getattr(route, "conversation_id"))
    chat_id = str(getattr(route, "chat_id"))
    thread_id = str(getattr(route, "thread_id"))
    summary: dict[str, object] = {
        "conversation_ref": _opaque_summary(conversation_id),
        "destination_ref": _opaque_summary(f"{chat_id}\x1f{thread_id}"),
        "enabled": bool(getattr(route, "enabled")),
    }
    if show_identifiers:
        summary.update(
            {
                "conversation_id": conversation_id,
                "chat_id": chat_id,
                "thread_id": thread_id,
            }
        )
    return summary


def _route_block_summary(core: SimpleNamespace, block: object) -> dict[str, object]:
    state = str(getattr(block, "state", "unknown"))
    reason = str(getattr(block, "reason", "unknown"))
    return {
        "event_ref": _opaque_boundary_ref(core, "event", str(getattr(block, "event_ref"))),
        "conversation_ref": _opaque_boundary_ref(
            core, "conversation", str(getattr(block, "conversation_id"))
        ),
        "state": state if state in {"blocked_no_route", "requeued"} else "unknown",
        "reason": reason if reason in {"route_missing", "route_disabled"} else "unknown",
        "blocked_at": int(getattr(block, "blocked_at")),
        "updated_at": int(getattr(block, "updated_at")),
        "requeued_at": (
            int(getattr(block, "requeued_at"))
            if getattr(block, "requeued_at", None) is not None
            else None
        ),
    }


def _delivery_summary(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "delivery_ref": _opaque_summary(row["delivery_id"]),
        "event_id": str(row["event_id"]),
        "conversation_id": str(row["conversation_id"]),
        "state": str(row["state"]),
        "attempts": int(row["attempts"]),
        "error_code": str(row["last_error_code"] or ""),
        "updated_at": int(row["updated_at"]),
    }


def _telegram_get_chat(config: AppConfig, chat_id: str) -> Mapping[str, object]:
    token = resolve_telegram_token(config)
    body = urllib_parse.urlencode({"chat_id": chat_id}).encode("ascii")
    request = urllib_request.Request(
        f"{config.telegram.api_base}/bot{token}/getChat",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=config.telegram.timeout_seconds) as response:
            raw = response.read(MAX_TELEGRAM_RESPONSE_BYTES + 1)
    except (urllib_error.HTTPError, urllib_error.URLError, TimeoutError, OSError) as exc:
        raise CLIError(
            "telegram_verification_failed",
            "Telegram destination verification failed safely",
            exit_code=EXIT_OPERATION,
        ) from exc
    if len(raw) > MAX_TELEGRAM_RESPONSE_BYTES:
        raise CLIError(
            "telegram_response_too_large",
            "Telegram verification response exceeded the size limit",
            exit_code=EXIT_OPERATION,
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CLIError(
            "telegram_response_invalid",
            "Telegram verification response was invalid",
            exit_code=EXIT_OPERATION,
        ) from exc
    if not isinstance(payload, Mapping) or payload.get("ok") is not True:
        raise CLIError(
            "telegram_get_chat_rejected",
            "Telegram rejected the read-only getChat verification",
            exit_code=EXIT_UNHEALTHY,
        )
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise CLIError(
            "telegram_response_invalid",
            "Telegram verification response was invalid",
            exit_code=EXIT_OPERATION,
        )
    return result


def _sqlite_quick_check(connection: sqlite3.Connection) -> str:
    rows = connection.execute("PRAGMA quick_check").fetchall()
    return "ok" if len(rows) == 1 and str(rows[0][0]) == "ok" else "failed"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_SHA256_CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    # Windows requires a writable descriptor for FlushFileBuffers via fsync.
    descriptor = os.open(path, os.O_RDWR)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _disk_space(path: Path) -> tuple[int, int, int]:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    usage = shutil.disk_usage(candidate)
    return int(usage.total), int(usage.used), int(usage.free)


@contextmanager
def _runtime_lock(ledger: object, config: AppConfig):
    owner_id = f"cli-{uuid.uuid4().hex}"
    profile_id = _source_profile_ref(_core(), config.worker.profile_id)
    acquired = ledger.acquire_runtime_lock(
        profile_id,
        owner_id,
        lease_seconds=config.worker.runtime_lock_seconds,
    )
    if not acquired:
        raise CLIError(
            "runtime_lock_unavailable",
            "another writer owns the configured profile",
            exit_code=EXIT_OPERATION,
        )
    try:
        yield
    finally:
        ledger.release_runtime_lock(profile_id, owner_id)


def _health_summary(value: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key in (
        "schema_version",
        "quick_check",
        "events",
        "routes",
        "active_leases",
        "runtime_locks",
        "blocked_no_route",
    ):
        item = value.get(key)
        if isinstance(item, (bool, int, float)) or item is None:
            result[key] = item
        elif key == "quick_check" and isinstance(item, str):
            result[key] = item
    raw_states = value.get("delivery_states")
    if isinstance(raw_states, Mapping):
        result["delivery_states"] = {
            str(key): int(item)
            for key, item in raw_states.items()
            if isinstance(item, int) and not isinstance(item, bool)
        }
    raw_route_blocks = value.get("route_blocks")
    if isinstance(raw_route_blocks, Mapping):
        result["route_blocks"] = {
            str(key): int(item)
            for key, item in raw_route_blocks.items()
            if isinstance(item, int) and not isinstance(item, bool)
        }
    return result


def _capture_health_summary(config: AppConfig) -> dict[str, object]:
    path = config.paths.state_dir / "capture-health.json"
    if not path.is_file():
        return {"status": "not_observed", "successes": 0, "failures": {}}
    try:
        if path.stat().st_size > 64 * 1024:
            raise ValueError("capture_health_too_large")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return {"status": "invalid", "successes": 0, "failures": {}}
    if not isinstance(value, Mapping) or value.get("schema_version") != 1:
        return {"status": "invalid", "successes": 0, "failures": {}}
    raw_failures = value.get("failures")
    failures = {
        str(key): int(item)
        for key, item in raw_failures.items()
        if isinstance(raw_failures, Mapping)
        and isinstance(item, int)
        and not isinstance(item, bool)
        and str(key).replace("_", "").isalnum()
    } if isinstance(raw_failures, Mapping) else {}
    successes = value.get("successes", 0)
    if isinstance(successes, bool) or not isinstance(successes, int) or successes < 0:
        successes = 0
    last_success = str(value.get("last_success_at") or "")
    last_failure = str(value.get("last_failure_at") or "")
    status = "failing" if last_failure and last_failure >= last_success else "healthy"
    return {
        "status": status,
        "successes": successes,
        "failures": failures,
        "last_success_at": last_success,
        "last_failure_at": last_failure,
        "last_error_code": str(value.get("last_error_code") or ""),
    }


def _worker_summary(value: object) -> dict[str, object]:
    return {
        "status": str(getattr(value, "status", "unknown")),
        "attempt_no": int(getattr(value, "attempt_no", 0)),
        "error_code": str(getattr(value, "error_code", "")),
        "media_removed": int(getattr(value, "media_removed", 0)),
    }


def _safe_aggregate(value: object) -> object:
    """Reduce result objects to state and counts without user-authored content."""

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= 160 and "\n" not in value else "redacted"
    if is_dataclass(value) and not isinstance(value, type):
        return _safe_aggregate({field.name: getattr(value, field.name) for field in fields(value)})
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            lowered = key.lower()
            if lowered in _SENSITIVE_KEYS or any(
                marker in lowered for marker in ("token", "secret", "password", "caption", "content")
            ):
                continue
            result[key] = _safe_aggregate(item)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        # Operational results may contain remote IDs, but never input payloads.
        return [_safe_aggregate(item) for item in value]
    return type(value).__name__


def _legacy_summary(result: object) -> dict[str, object]:
    aggregate = _safe_aggregate(result)
    if isinstance(aggregate, dict):
        allowed: dict[str, object] = {}
        for key, value in aggregate.items():
            if key in _COUNT_FIELDS or key in {
                "source_hash", "already_imported", "imported", "dry_run"
            }:
                allowed[key] = value
        if allowed:
            return allowed
    return {"imported": bool(result)}


def _read_json_object(source: str) -> dict[str, Any]:
    try:
        if source == "-":
            raw = sys.stdin.read(MAX_EVENT_JSON_BYTES + 1)
        else:
            event_path = Path(source).expanduser()
            if event_path.stat().st_size > MAX_EVENT_JSON_BYTES:
                raise CLIError("event_input_too_large", "event input exceeds the size limit")
            raw = event_path.read_text(encoding="utf-8")
        if len(raw.encode("utf-8")) > MAX_EVENT_JSON_BYTES:
            raise CLIError("event_input_too_large", "event input exceeds the size limit")
        value = json.loads(raw)
    except CLIError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CLIError("invalid_event_input", "event input is not a readable JSON object") from exc
    if not isinstance(value, dict):
        raise CLIError("invalid_event_input", "event input must be a JSON object")
    return value


def _required_string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise CLIError("invalid_event_input", f"event field {key} must be a non-empty string")
    return item


def _optional_string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key, "")
    if item is None:
        return ""
    if not isinstance(item, str):
        raise CLIError("invalid_event_input", f"event field {key} must be a string")
    return item


def _event_from_json(
    value: Mapping[str, Any],
    core: SimpleNamespace,
    *,
    default_source_profile: str,
) -> object:
    raw_profile = value.get("source_profile_id", default_source_profile)
    if not isinstance(raw_profile, str) or not raw_profile.strip():
        raise CLIError(
            "invalid_event_input",
            "event field source_profile_id must be a non-empty string",
        )
    source_profile_id = _source_profile_ref(core, raw_profile)
    raw_source = _required_string(value, "source")
    raw_conversation = _required_string(value, "conversation_id")
    conversation_id = _profile_scoped_ref(
        core, "conversation", raw_conversation, source_profile_id
    )
    raw_actor = _required_string(value, "actor_ref")
    actor_ref = _profile_scoped_ref(core, "actor", raw_actor, source_profile_id)
    raw_event_id = _required_string(value, "event_id")
    event_id = (
        raw_event_id
        if re.fullmatch(r"event:[0-9a-f]{64}", raw_event_id)
        else str(
            core.opaque_ref(
                "event",
                f"{raw_source}\x1f{source_profile_id}\x1f{raw_conversation}\x1f{raw_event_id}",
            )
        )
    )
    raw_media = value.get("media", [])
    if raw_media is None:
        raw_media = []
    if not isinstance(raw_media, list):
        raise CLIError("invalid_event_input", "event field media must be an array")
    media: list[object] = []
    for index, raw_item in enumerate(raw_media):
        if not isinstance(raw_item, dict):
            raise CLIError("invalid_event_input", f"media item {index} must be an object")
        size = raw_item.get("size_bytes", 0)
        if isinstance(size, bool) or not isinstance(size, int):
            raise CLIError("invalid_event_input", f"media item {index} size_bytes must be an integer")
        managed_temp = raw_item.get("managed_temp", False)
        if not isinstance(managed_temp, bool):
            raise CLIError("invalid_event_input", f"media item {index} managed_temp must be boolean")
        try:
            media.append(
                core.MediaAttachment(
                    media_id=_profile_scoped_ref(
                        core,
                        "media",
                        _required_string(raw_item, "media_id"),
                        source_profile_id,
                    ),
                    kind=_required_string(raw_item, "kind"),
                    path=_required_string(raw_item, "path"),
                    mime_type=_optional_string(raw_item, "mime_type"),
                    sha256=_optional_string(raw_item, "sha256"),
                    size_bytes=size,
                    caption=_optional_string(raw_item, "caption"),
                    managed_temp=managed_temp,
                )
            )
        except ValueError as exc:
            raise CLIError("invalid_event_input", f"media item {index} failed validation") from exc
    try:
        privacy_scope = value.get("privacy_scope", "owner_private")
        if not isinstance(privacy_scope, str):
            raise CLIError("invalid_event_input", "event field privacy_scope must be a string")
        schema_version = value.get("schema_version", 3)
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise CLIError("invalid_event_input", "event field schema_version must be an integer")
        event = core.InboundEvent(
            event_id=event_id,
            source=raw_source,
            source_profile_id=source_profile_id,
            conversation_id=conversation_id,
            occurred_at=_required_string(value, "occurred_at"),
            actor_ref=actor_ref,
            privacy_scope=privacy_scope,
            text=_optional_string(value, "text"),
            context_text=_optional_string(value, "context_text"),
            media=tuple(media),
            conversation_kind=_optional_string(value, "conversation_kind") or "direct",
            actor_display_label=_optional_string(value, "actor_display_label"),
            schema_version=schema_version,
        )
        return core.sanitize_captured_event(event)
    except ValueError as exc:
        raise CLIError("invalid_event_input", "event failed domain validation") from exc


def _replay_compatible(incoming: object, existing: object) -> bool:
    for field in (
        "event_id",
        "source",
        "source_profile_id",
        "conversation_id",
        "occurred_at",
        "actor_ref",
        "privacy_scope",
        "text",
        "context_text",
        "conversation_kind",
        "actor_display_label",
        "schema_version",
    ):
        if getattr(incoming, field) != getattr(existing, field):
            return False
    incoming_media = tuple(getattr(incoming, "media"))
    existing_media = tuple(getattr(existing, "media"))
    if len(incoming_media) != len(existing_media):
        return False
    for left, right in zip(incoming_media, existing_media):
        for field in ("media_id", "kind", "mime_type", "caption"):
            if getattr(left, field) != getattr(right, field):
                return False
        if getattr(left, "size_bytes") and getattr(left, "size_bytes") != getattr(
            right, "size_bytes"
        ):
            return False
        if getattr(left, "sha256") and getattr(left, "sha256") != getattr(right, "sha256"):
            return False
    return True


def cmd_init(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    target, created = write_default_config(
        args.config,
        data_dir=Path(args.data_dir).expanduser().resolve() if args.data_dir else None,
        minimum_free_bytes=args.minimum_free_bytes,
        force=args.force,
    )
    config = load_config(target)
    ensure_runtime_paths(config)
    _, _, free_bytes = _disk_space(config.paths.data_dir)
    if free_bytes < config.paths.minimum_free_bytes:
        raise CLIError(
            "insufficient_disk_space",
            "free disk space is below the configured threshold",
            exit_code=EXIT_UNHEALTHY,
        )

    token_file_created = False
    if config.telegram.token_file and not config.telegram.token_file.exists():
        descriptor = os.open(config.telegram.token_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        token_file_created = True
    if config.telegram.token_file and os.name == "posix":
        config.telegram.token_file.chmod(0o600)

    core = _core()
    with core.MirrorLedger(config.paths.ledger_path) as ledger:
        quick_check = ledger.quick_check()
    return (
        {
            "config_created": created,
            "ledger_initialized": quick_check == "ok",
            "token_file_created": token_file_created,
            "secret_stored_in_config": False,
            "disk_space": {
                "free_bytes": free_bytes,
                "required_bytes": config.paths.minimum_free_bytes,
            },
        },
        EXIT_OK if quick_check == "ok" else EXIT_UNHEALTHY,
    )


def cmd_doctor(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    checks: list[dict[str, object]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    check("python", sys.version_info >= (3, 11), f"{sys.version_info.major}.{sys.version_info.minor}")
    try:
        config = _config_for(args)
        check("config", True, "loaded")
    except ConfigError as exc:
        check("config", False, str(exc))
        return {"ready": False, "checks": checks}, EXIT_UNHEALTHY

    config_private, config_detail = private_mode(config.config_path)
    check("config_permissions", config_private, config_detail)
    for name, path in (
        ("data_directory", config.paths.data_dir),
        ("state_directory", config.paths.state_dir),
        ("ledger_directory", config.paths.ledger_path.parent),
    ):
        ok = path.is_dir() and os.access(path, os.R_OK | os.W_OK | os.X_OK)
        check(name, ok, "ready" if ok else "missing_or_unusable")

    try:
        total_bytes, used_bytes, free_bytes = _disk_space(config.paths.data_dir)
        disk_ok = free_bytes >= config.paths.minimum_free_bytes
        checks.append(
            {
                "name": "disk_space",
                "ok": disk_ok,
                "detail": {
                    "total_bytes": total_bytes,
                    "used_bytes": used_bytes,
                    "free_bytes": free_bytes,
                    "required_bytes": config.paths.minimum_free_bytes,
                },
            }
        )
    except OSError:
        check("disk_space", False, "unavailable")

    try:
        core = _core()
        with core.MirrorLedger(config.paths.ledger_path) as ledger:
            quick_check = ledger.quick_check()
        check("ledger", quick_check == "ok", quick_check)
    except Exception as exc:  # diagnostic boundary: report class, not data-bearing message
        check("ledger", False, type(exc).__name__)

    if args.allow_missing_token:
        token_available = config.public_summary().get("token_source") != "none"
        check("telegram_token", True, "available" if token_available else "not_required")
    else:
        try:
            resolve_telegram_token(config)
            check("telegram_token", True, "available")
        except ConfigError:
            check("telegram_token", False, "unavailable_or_unsafe")

    ready = all(bool(item["ok"]) for item in checks)
    return {"ready": ready, "checks": checks, "runtime": config.public_summary()}, (
        EXIT_OK if ready else EXIT_UNHEALTHY
    )


def cmd_health(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    config = _config_for(args)
    core = _core()
    with core.MirrorLedger(config.paths.ledger_path) as ledger:
        health = ledger.health()
    aggregate = _health_summary(health)
    capture = _capture_health_summary(config)
    ok = (
        isinstance(aggregate, dict)
        and aggregate.get("quick_check") == "ok"
        and capture.get("status") not in {"invalid", "failing"}
    )
    return {
        "healthy": ok,
        "ledger": aggregate,
        "capture": capture,
    }, EXIT_OK if ok else EXIT_UNHEALTHY


def cmd_backup(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    """Create a validated online SQLite backup without replacing any file."""

    config = _config_for(args)
    source = config.paths.ledger_path.expanduser().resolve()
    destination = Path(args.destination).expanduser().resolve()
    if not source.is_file():
        raise CLIError(
            "ledger_missing",
            "configured ledger does not exist",
            exit_code=EXIT_OPERATION,
        )
    if source == destination:
        raise CLIError(
            "invalid_backup_destination",
            "backup destination must differ from the configured ledger",
            exit_code=EXIT_USAGE,
        )
    if os.path.lexists(destination):
        raise CLIError(
            "backup_destination_exists",
            "backup destination already exists; overwrite is not allowed",
            exit_code=EXIT_OPERATION,
        )

    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not destination.parent.is_dir():
        raise CLIError(
            "invalid_backup_destination",
            "backup destination parent is not a directory",
            exit_code=EXIT_USAGE,
        )

    source_connection: sqlite3.Connection | None = None
    temporary_path: Path | None = None
    try:
        source_connection = sqlite3.connect(
            f"{source.as_uri()}?mode=ro",
            uri=True,
            timeout=10.0,
        )
        source_connection.execute("PRAGMA busy_timeout = 10000")
        source_check = _sqlite_quick_check(source_connection)
        if source_check != "ok":
            raise CLIError(
                "ledger_quick_check_failed",
                "configured ledger failed SQLite quick_check",
                exit_code=EXIT_UNHEALTHY,
            )

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        if os.name == "posix":
            temporary_path.chmod(0o600)

        backup_connection = sqlite3.connect(temporary_path, timeout=10.0)
        try:
            source_connection.backup(backup_connection)
        finally:
            backup_connection.close()

        verification = sqlite3.connect(
            f"{temporary_path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=10.0,
        )
        try:
            backup_check = _sqlite_quick_check(verification)
        finally:
            verification.close()
        if backup_check != "ok":
            raise CLIError(
                "backup_quick_check_failed",
                "new backup failed SQLite quick_check",
                exit_code=EXIT_OPERATION,
            )

        _fsync_file(temporary_path)
        size_bytes = temporary_path.stat().st_size
        sha256 = _sha256_file(temporary_path)
        try:
            # A same-directory hard-link is an atomic create-if-absent publish.
            # It cannot replace a destination created by another process.
            os.link(temporary_path, destination)
        except FileExistsError as exc:
            raise CLIError(
                "backup_destination_exists",
                "backup destination already exists; overwrite is not allowed",
                exit_code=EXIT_OPERATION,
            ) from exc
        _fsync_directory(destination.parent)
        return {
            "created": True,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "source_quick_check": source_check,
            "backup_quick_check": backup_check,
            "overwrite": False,
        }, EXIT_OK
    finally:
        if source_connection is not None:
            source_connection.close()
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def cmd_route_set(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    config = _config_for(args)
    core = _core()
    source_profile = _profile_for(args, config)
    try:
        route = core.Route(
            conversation_id=_profile_scoped_ref(
                core, "conversation", args.conversation_id, source_profile
            ),
            chat_id=args.chat_id,
            thread_id=args.thread_id,
            enabled=args.enabled,
        )
    except ValueError as exc:
        raise CLIError("invalid_route", "route failed domain validation") from exc
    with core.MirrorLedger(config.paths.ledger_path) as ledger:
        with _runtime_lock(ledger, config):
            changed = ledger.set_route(
                route,
                watermark_event_id=args.watermark_event_id,
                allow_update=args.allow_update,
            )
    return {"changed": changed, "route": _route_summary(route)}, EXIT_OK


def cmd_route_list(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    config = _config_for(args)
    core = _core()
    with core.MirrorLedger(config.paths.ledger_path) as ledger:
        routes = ledger.list_routes(enabled_only=args.enabled_only)
    return {
        "count": len(routes),
        "routes": [
            _route_summary(route, show_identifiers=args.show_identifiers) for route in routes
        ],
    }, EXIT_OK


def cmd_route_blocked_list(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    config = _config_for(args)
    core = _core()
    with core.MirrorLedger(config.paths.ledger_path) as ledger:
        blocks = ledger.list_route_blocks(state=args.state, limit=args.limit)
    return {
        "count": len(blocks),
        "state": args.state,
        "blocks": [_route_block_summary(core, block) for block in blocks],
    }, EXIT_OK


def cmd_route_reconcile(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    config = _config_for(args)
    core = _core()
    conversation_id = _profile_scoped_ref(
        core,
        "conversation",
        args.conversation_id,
        _profile_for(args, config),
    )
    with core.MirrorLedger(config.paths.ledger_path) as ledger:
        with _runtime_lock(ledger, config):
            requeued = ledger.reconcile_route_blocks(conversation_id, limit=args.limit)
            health = ledger.health()
    return {
        "conversation_ref": _opaque_summary(conversation_id),
        "requeued": requeued,
        "ledger": _health_summary(health),
    }, EXIT_OK


def cmd_route_verify_destination(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    config = _config_for(args)
    chat = _telegram_get_chat(config, args.chat_id)
    raw_type = chat.get("type")
    chat_type = str(raw_type) if raw_type in {"private", "group", "supergroup", "channel"} else "unknown"
    raw_forum = chat.get("is_forum")
    is_forum = raw_forum is True
    returned_id = chat.get("id")
    id_matches = (
        isinstance(returned_id, (str, int))
        and not isinstance(returned_id, bool)
        and str(returned_id) == args.chat_id
    )
    verified = id_matches and chat_type == "supergroup" and is_forum
    result: dict[str, object] = {
        "destination_ref": _opaque_summary(args.chat_id),
        "verified": verified,
        "id_matches": id_matches,
        "chat_type": chat_type,
        "is_forum": is_forum,
        "verification_method": "getChat",
        "read_only": True,
        "message_sent": False,
    }
    if args.thread_id is not None:
        result["topic"] = {
            "provided": True,
            "syntax_valid": True,
            "existence_verified": False,
            "reason": "getChat_does_not_expose_topic_membership",
        }
    return result, EXIT_OK if verified else EXIT_UNHEALTHY


def cmd_route_import_legacy_runtime(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    config = _config_for(args)
    core = _core()
    default_chat_id = args.default_chat_id or config.legacy.default_chat_id
    with core.MirrorLedger(config.paths.ledger_path, read_only=args.dry_run) as ledger:
        if args.dry_run:
            result = core.import_legacy_runtime_config(
                ledger,
                Path(args.source).expanduser().resolve(),
                default_chat_id=default_chat_id,
                allow_route_update=args.allow_update,
                dry_run=args.dry_run,
                source_profile_id=_profile_for(args, config),
                identity_map=(
                    Path(args.identity_map).expanduser().resolve()
                    if args.identity_map
                    else None
                ),
            )
        else:
            with _runtime_lock(ledger, config):
                result = core.import_legacy_runtime_config(
                    ledger,
                    Path(args.source).expanduser().resolve(),
                    default_chat_id=default_chat_id,
                    allow_route_update=args.allow_update,
                    dry_run=False,
                    source_profile_id=_profile_for(args, config),
                    identity_map=(
                        Path(args.identity_map).expanduser().resolve()
                        if args.identity_map
                        else None
                    ),
                )
    return _legacy_summary(result), EXIT_OK


def cmd_ingest(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    config = _config_for(args)
    core = _core()
    event = _event_from_json(
        _read_json_object(args.source),
        core,
        default_source_profile=config.worker.profile_id,
    )
    with core.MirrorLedger(config.paths.ledger_path) as ledger:
        canonical_conversation = ledger.resolve_conversation_alias(event.conversation_id)
        event = replace(event, conversation_id=canonical_conversation)
        event = replace(
            event,
            privacy_scope=ledger.get_conversation_scope(event.conversation_id),
        )
        forum_chat_id = (
            config.routing.telegram_forum_chat_id or config.legacy.default_chat_id
        )
        if (
            event.conversation_kind == "direct"
            and ledger.get_route(event.conversation_id) is None
            and config.routing.auto_create_direct_contact_topics
            and forum_chat_id
        ):
            token = resolve_telegram_token(config)
            transport = core.TelegramBotTransport(
                token,
                api_base=config.telegram.api_base,
                timeout=config.telegram.timeout_seconds,
            )
            topic_name = (
                event.actor_display_label.strip()
                or f"Contato WhatsApp {event.actor_ref[-8:]}"
            )[:128]
            thread_id = transport.create_forum_topic(
                forum_chat_id, topic_name
            )
            ledger.set_route(
                core.Route(
                    event.conversation_id,
                    forum_chat_id,
                    thread_id,
                )
            )
        try:
            ledger.authorize_event(event)
        except Exception as exc:
            code = str(getattr(exc, "code", "event_admission_rejected"))
            raise CLIError(
                code,
                "event rejected before content persistence by conversation policy",
                exit_code=EXIT_OPERATION,
            ) from exc
        try:
            existing = ledger.load_event(event.event_id)
        except Exception as exc:
            if getattr(exc, "code", None) != "event_missing":
                raise
            existing = None
        if existing is not None:
            if not _replay_compatible(event, existing):
                raise CLIError(
                    "event_payload_conflict",
                    "event identity already exists with different immutable facts",
                    exit_code=EXIT_OPERATION,
                )
            state = ledger.delivery_state(event.event_id)
            block = ledger.connection.execute(
                """SELECT reason FROM mirror_route_blocks
                   WHERE event_id = ? AND state = 'blocked_no_route'""",
                (event.event_id,),
            ).fetchone()
            if block:
                raise CLIError(
                    str(block["reason"]),
                    "event already captured and held until explicit route reconciliation",
                    exit_code=EXIT_OPERATION,
                )
            return {
                "event_ref": _opaque_summary(event.event_id),
                "conversation_ref": _opaque_summary(event.conversation_id),
                "inserted": False,
                "enqueued": state is not None,
                "delivery_state": state,
                "media_count": len(existing.media),
                "has_text": bool(existing.text),
                "privacy_scope": existing.privacy_scope,
                "schema_version": existing.schema_version,
            }, EXIT_OK
        created_media: tuple[Path, ...] = ()
        ledger.connection.execute("BEGIN IMMEDIATE")
        try:
            # The ledger write lock serializes the filesystem quota check and
            # staging step across all producers sharing this spool.
            event, created_media = core.stage_event_media(
                event,
                spool_root=config.worker.allowed_temp_root,
                source_roots=config.worker.source_media_roots,
                minimum_free_bytes=config.paths.minimum_free_bytes,
                maximum_spool_bytes=config.worker.maximum_spool_bytes,
            )
            inserted, delivery_id, blocked_reason = ledger.capture_event(event)
            ledger.connection.execute("COMMIT")
        except BaseException:
            if ledger.connection.in_transaction:
                ledger.connection.execute("ROLLBACK")
            for path in created_media:
                path.unlink(missing_ok=True)
            raise
        state = ledger.delivery_state(event.event_id)
    if blocked_reason:
        raise CLIError(
            blocked_reason,
            "event captured but held until an explicit topic route is reconciled",
            exit_code=EXIT_OPERATION,
        )
    return {
        "event_ref": _opaque_summary(event.event_id),
        "conversation_ref": _opaque_summary(event.conversation_id),
        "inserted": inserted,
        "enqueued": delivery_id is not None,
        "delivery_state": state,
        "media_count": len(event.media),
        "has_text": bool(event.text),
        "privacy_scope": event.privacy_scope,
        "schema_version": event.schema_version,
    }, EXIT_OK


def cmd_observer_once(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    config = _config_for(args)
    from .adapters.hermes_bridge import HermesBridgeObserver

    core = _core()
    with core.MirrorLedger(config.paths.ledger_path) as ledger:
        with _runtime_lock(ledger, config):
            observer = HermesBridgeObserver(
                ledger,
                bridge_url=args.bridge_url,
                source_profile_id=_profile_for(args, config),
                spool_root=config.worker.allowed_temp_root,
                source_media_roots=tuple(config.worker.source_media_roots),
                minimum_free_bytes=config.paths.minimum_free_bytes,
                maximum_spool_bytes=config.worker.maximum_spool_bytes,
                privacy_scope=args.privacy_scope,
                batch_limit=args.batch_limit,
                timeout_seconds=args.timeout_seconds,
                maximum_response_bytes=args.maximum_response_bytes,
            )
            result = observer.observe_once()
            reaction_summary = {
                "candidates": 0,
                "applied": 0,
                "already_applied": 0,
                "invalid": 0,
                "failed": 0,
            }
            if os.environ.get("ESPELHO_ZAP_RECEIPT_REACTIONS", "").strip().lower() == "enabled":
                outbound_ledger_raw = os.environ.get(
                    "ESPELHO_ZAP_HUMAN_OUTBOUND_LEDGER", ""
                ).strip()
                if not outbound_ledger_raw:
                    raise CLIError(
                        "receipt_reaction_ledger_required",
                        "receipt reaction outbound ledger is not configured",
                        exit_code=EXIT_CONFIG,
                    )
                from .receipt_reactions import TelegramReceiptReactionProjector

                token = resolve_telegram_token(config)
                projector = TelegramReceiptReactionProjector(
                    ledger,
                    Path(outbound_ledger_raw).expanduser().resolve(),
                    core.TelegramBotTransport(
                        token,
                        api_base=config.telegram.api_base,
                        timeout=config.telegram.timeout_seconds,
                    ),
                )
                reaction = projector.apply_once()
                reaction_summary = {
                    "candidates": reaction.candidates,
                    "applied": reaction.applied,
                    "already_applied": reaction.already_applied,
                    "invalid": reaction.invalid,
                    "failed": reaction.failed,
                }
    summary = result.public_summary()
    summary["receipt_reactions"] = reaction_summary
    unhealthy = bool(
        result.malformed
        or result.media_failed
        or result.ack_failed
        or result.source_media_cleanup_failed
        or reaction_summary["invalid"]
        or reaction_summary["failed"]
    )
    return summary, EXIT_OPERATION if unhealthy else EXIT_OK


def cmd_route_provision_topic(
    args: argparse.Namespace,
) -> tuple[dict[str, object], int]:
    """Create one Telegram topic and commit its exact route with compensation."""

    if not args.confirm_create:
        raise CLIError(
            "topic_creation_confirmation_required",
            "explicit --confirm-create is required for Telegram topic creation",
        )
    config = _config_for(args)
    core = _core()
    conversation_id = _profile_scoped_ref(
        core,
        "conversation",
        args.conversation_id,
        _profile_for(args, config),
    )
    token = resolve_telegram_token(config)
    transport = core.TelegramBotTransport(
        token,
        api_base=config.telegram.api_base,
        timeout=config.telegram.timeout_seconds,
    )
    with core.MirrorLedger(config.paths.ledger_path) as ledger:
        with _runtime_lock(ledger, config):
            existing = ledger.get_route(conversation_id)
            if existing is not None and not args.allow_update:
                raise CLIError(
                    "route_exists",
                    "conversation already has an explicit route",
                    exit_code=EXIT_OPERATION,
                )
            thread_id = transport.create_forum_topic(args.chat_id, args.topic_name)
            try:
                ledger.set_route(
                    core.Route(
                        conversation_id=conversation_id,
                        chat_id=args.chat_id,
                        thread_id=thread_id,
                        enabled=True,
                    ),
                    allow_update=args.allow_update,
                )
            except BaseException as route_error:
                try:
                    transport.delete_forum_topic(args.chat_id, thread_id)
                except BaseException as rollback_error:
                    raise CLIError(
                        "topic_provision_uncertain",
                        "Telegram topic exists but local route commit and compensation failed",
                        exit_code=EXIT_OPERATION,
                    ) from rollback_error
                raise CLIError(
                    "topic_provision_rolled_back",
                    "local route commit failed and the created topic was removed",
                    exit_code=EXIT_OPERATION,
                ) from route_error
    return {
        "conversation_id": conversation_id,
        "chat_id": args.chat_id,
        "thread_id": thread_id,
        "topic_created": True,
        "route_committed": True,
        "dm_fallback": False,
    }, EXIT_OK


def cmd_scope_set(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    config = _config_for(args)
    core = _core()
    conversation_id = _profile_scoped_ref(
        core,
        "conversation",
        args.conversation_id,
        _profile_for(args, config),
    )
    with core.MirrorLedger(config.paths.ledger_path) as ledger:
        with _runtime_lock(ledger, config):
            changed = ledger.set_conversation_scope(conversation_id, args.privacy_scope)
    return {
        "conversation_ref": _opaque_summary(conversation_id),
        "privacy_scope": args.privacy_scope,
        "changed": changed,
        "default_for_unmapped": "owner_private",
    }, EXIT_OK


def cmd_scope_list(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    config = _config_for(args)
    with _core().MirrorLedger(config.paths.ledger_path) as ledger:
        rows = ledger.list_conversation_scopes()
    values = []
    for conversation_id, privacy_scope in rows:
        item: dict[str, object] = {
            "conversation_ref": _opaque_summary(conversation_id),
            "privacy_scope": privacy_scope,
        }
        if args.show_identifiers:
            item["conversation_id"] = conversation_id
        values.append(item)
    return {
        "count": len(values),
        "default_for_unmapped": "owner_private",
        "policies": values,
    }, EXIT_OK


def cmd_alias_set(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    config = _config_for(args)
    core = _core()
    alias_id = _profile_scoped_ref(
        core,
        "conversation",
        args.observed_conversation,
        args.observed_source_profile or config.worker.profile_id,
    )
    canonical_id = _profile_scoped_ref(
        core,
        "conversation",
        args.canonical_conversation,
        args.canonical_source_profile or config.worker.profile_id,
    )
    with core.MirrorLedger(config.paths.ledger_path) as ledger:
        with _runtime_lock(ledger, config):
            changed = ledger.set_conversation_alias(
                alias_id, canonical_id, allow_update=args.allow_update
            )
    return {
        "observed_ref": _opaque_summary(alias_id),
        "canonical_ref": _opaque_summary(canonical_id),
        "changed": changed,
    }, EXIT_OK


def cmd_alias_list(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    config = _config_for(args)
    with _core().MirrorLedger(config.paths.ledger_path) as ledger:
        rows = ledger.list_conversation_aliases()
    values = []
    for alias_id, canonical_id in rows:
        item: dict[str, object] = {
            "observed_ref": _opaque_summary(alias_id),
            "canonical_ref": _opaque_summary(canonical_id),
        }
        if args.show_identifiers:
            item.update({"observed_id": alias_id, "canonical_id": canonical_id})
        values.append(item)
    return {"count": len(values), "aliases": values}, EXIT_OK


def cmd_delivery_list(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    config = _config_for(args)
    with _core().MirrorLedger(config.paths.ledger_path) as ledger:
        rows = ledger.list_deliveries(state=args.state, limit=args.limit)
    return {
        "count": len(rows),
        "state": args.state or "all",
        "deliveries": [_delivery_summary(row) for row in rows],
    }, EXIT_OK


def cmd_delivery_reconcile_uncertain(
    args: argparse.Namespace,
) -> tuple[dict[str, object], int]:
    config = _config_for(args)
    core = _core()
    event_id = _opaque_boundary_ref(core, "event", args.event_id)
    with core.MirrorLedger(config.paths.ledger_path) as ledger:
        with _runtime_lock(ledger, config):
            changed = ledger.reconcile_uncertain(
                event_id,
                resolution=args.resolution,
                evidence_ref=args.evidence_ref,
            )
    return {
        "event_id": event_id,
        "resolution": args.resolution,
        "changed": changed,
        "audited": True,
    }, EXIT_OK


def cmd_delivery_rebind_route(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    config = _config_for(args)
    core = _core()
    conversation_id = _profile_scoped_ref(
        core,
        "conversation",
        args.conversation_id,
        _profile_for(args, config),
    )
    with core.MirrorLedger(config.paths.ledger_path) as ledger:
        with _runtime_lock(ledger, config):
            requeued = ledger.rebind_route_changed(
                conversation_id,
                evidence_ref=args.evidence_ref,
                limit=args.limit,
            )
    return {
        "conversation_id": conversation_id,
        "requeued": requeued,
        "audited": True,
    }, EXIT_OK


def cmd_worker_once(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    config = _config_for(args)
    core = _core()
    token = resolve_telegram_token(config)
    transport = core.TelegramBotTransport(
        token,
        api_base=config.telegram.api_base,
        timeout=config.telegram.timeout_seconds,
    )
    worker_id = args.worker_id or config.worker.worker_id
    profile_id = args.profile or config.worker.profile_id
    with core.MirrorLedger(config.paths.ledger_path) as ledger:
        worker = core.MirrorWorker(
            ledger,
            transport,
            worker_id=worker_id,
            profile_id=profile_id,
            runtime_lock_seconds=config.worker.runtime_lock_seconds,
            lease_seconds=config.worker.lease_seconds,
            max_attempts=config.worker.max_attempts,
            base_backoff_seconds=config.worker.base_backoff_seconds,
            allowed_temp_root=config.worker.allowed_temp_root,
            media_retention_seconds=config.worker.media_retention_hours * 3600,
        )
        try:
            result = worker.run_once()
            health = ledger.health()
        finally:
            worker.close()
    status = getattr(result, "status", None)
    processed = status not in {None, "idle", "busy", "standby"}
    exit_code = EXIT_OPERATION if status in {"retry", "dead", "uncertain"} else EXIT_OK
    return {
        "profile_id": profile_id,
        "processed": processed,
        "outcome": _worker_summary(result),
        "ledger": _health_summary(health),
    }, exit_code


def cmd_worker_drain(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    config = _config_for(args)
    core = _core()
    token = resolve_telegram_token(config)
    transport = core.TelegramBotTransport(
        token,
        api_base=config.telegram.api_base,
        timeout=config.telegram.timeout_seconds,
    )
    worker_id = args.worker_id or config.worker.worker_id
    profile_id = args.profile or config.worker.profile_id
    with core.MirrorLedger(config.paths.ledger_path) as ledger:
        worker = core.MirrorWorker(
            ledger,
            transport,
            worker_id=worker_id,
            profile_id=profile_id,
            runtime_lock_seconds=config.worker.runtime_lock_seconds,
            lease_seconds=config.worker.lease_seconds,
            max_attempts=config.worker.max_attempts,
            base_backoff_seconds=config.worker.base_backoff_seconds,
            allowed_temp_root=config.worker.allowed_temp_root,
            media_retention_seconds=config.worker.media_retention_hours * 3600,
        )
        try:
            results = worker.run_bounded(
                max_items=args.max_items,
                max_seconds=args.max_seconds,
            )
            health = ledger.health()
        finally:
            worker.close()
    outcomes: dict[str, int] = {}
    media_removed = 0
    failed = False
    for result in results:
        outcomes[result.status] = outcomes.get(result.status, 0) + 1
        media_removed += int(result.media_removed)
        failed = failed or result.status in {"retry", "dead", "uncertain"}
    return {
        "profile_id": profile_id,
        "processed": len(results),
        "outcomes": outcomes,
        "media_removed": media_removed,
        "bounded": {"max_items": args.max_items, "max_seconds": args.max_seconds},
        "ledger": _health_summary(health),
    }, EXIT_OPERATION if failed else EXIT_OK


def cmd_media_report(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    config = _config_for(args)
    core = _core()
    with core.MirrorLedger(config.paths.ledger_path) as ledger:
        report = ledger.managed_media_report()
    return report, EXIT_OK


def cmd_group_approve(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    if not args.confirm_approve:
        raise CLIError(
            "group_approval_confirmation_required",
            "explicit --confirm-approve is required for a WhatsApp group",
        )
    config = _config_for(args)
    core = _core()
    profile = _profile_for(args, config)
    source_profile_id = _source_profile_ref(core, profile)
    conversation_id = _profile_scoped_ref(
        core, "conversation", args.conversation_id, source_profile_id
    )
    grill_json = None
    if args.grill:
        grill = _read_json_object(args.grill)
        try:
            core.GroupGrill.from_mapping(grill)
        except Exception as exc:
            raise CLIError("group_grill_invalid", "group grill is incomplete") from exc
        grill_json = json.dumps(grill, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with core.MirrorLedger(config.paths.ledger_path) as ledger:
        with _runtime_lock(ledger, config):
            route = ledger.get_route(conversation_id)
            if route is None or not route.enabled:
                raise CLIError(
                    "group_route_required",
                    "set the exact Telegram topic route before approving the group",
                    exit_code=EXIT_OPERATION,
                )
            ledger.set_conversation_scope(conversation_id, args.privacy_scope)
            ledger.approve_group(
                conversation_id,
                source_profile_id,
                agent_mode=args.agent_mode,
                grill_json=grill_json,
            )
    return {
        "conversation_ref": _opaque_summary(conversation_id),
        "source_profile_ref": _opaque_summary(source_profile_id),
        "approved": True,
        "agent_mode": args.agent_mode,
        "grill_complete": grill_json is not None,
        "route_exact": True,
        "privacy_scope": args.privacy_scope,
    }, EXIT_OK


def cmd_identity_set(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    config = _config_for(args)
    core = _core()
    profile = _profile_for(args, config)
    source_profile_id = _source_profile_ref(core, profile)
    conversation_id = _profile_scoped_ref(
        core, "conversation", args.conversation_id, source_profile_id
    )
    actor_ref = _profile_scoped_ref(core, "actor", args.actor_id, source_profile_id)
    with core.MirrorLedger(config.paths.ledger_path) as ledger:
        with _runtime_lock(ledger, config):
            changed = ledger.set_participant_identity(
                source_profile_id,
                conversation_id,
                actor_ref,
                args.display_label,
                label_source="manual",
            )
    return {
        "conversation_ref": _opaque_summary(conversation_id),
        "actor_ref": _opaque_summary(actor_ref),
        "changed": changed,
        "source": "manual",
    }, EXIT_OK


def cmd_acceptance_record(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    config = _config_for(args)
    core = _core()
    canary = core.HumanCanary(
        direction=args.direction,
        media_kind=args.media_kind,
        exact_route=args.exact_route,
        single_delivery=args.single_delivery,
        no_dm_fallback=args.no_dm_fallback,
        integrity_ok=args.integrity_ok,
        no_enrichment=args.no_enrichment,
        human_confirmed=args.human_confirmed,
    )
    if not canary.passed:
        raise CLIError(
            "human_canary_failed",
            "all acceptance assertions and human confirmation are required",
            exit_code=EXIT_OPERATION,
        )
    evidence_ref = str(core.opaque_ref("evidence", args.evidence_ref))
    canary_ref = str(
        core.opaque_ref("canary", f"{args.direction}\x1f{args.media_kind}\x1f{evidence_ref}")
    )
    with core.MirrorLedger(config.paths.ledger_path) as ledger:
        ledger.connection.execute(
            """INSERT OR IGNORE INTO mirror_acceptance_canaries(
                   canary_ref, direction, media_kind, passed, evidence_ref, recorded_at
               ) VALUES (?, ?, ?, 1, ?, strftime('%s','now'))""",
            (canary_ref, args.direction, args.media_kind, evidence_ref),
        )
    return {
        "canary_ref": _opaque_summary(canary_ref),
        "direction": args.direction,
        "media_kind": args.media_kind,
        "human_confirmed": True,
        "passed": True,
    }, EXIT_OK


def cmd_acceptance_status(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    config = _config_for(args)
    core = _core()
    with core.MirrorLedger(config.paths.ledger_path) as ledger:
        rows = ledger.connection.execute(
            """SELECT direction, media_kind, passed FROM mirror_acceptance_canaries
               ORDER BY direction, media_kind, recorded_at"""
        ).fetchall()
    canaries = tuple(
        core.HumanCanary(
            direction=str(row["direction"]), media_kind=str(row["media_kind"]),
            exact_route=True, single_delivery=True, no_dm_fallback=True,
            integrity_ok=True, no_enrichment=True,
            human_confirmed=bool(row["passed"]),
        )
        for row in rows
    )
    state = core.installation_state(canaries)
    return {"installation_state": state, "passed_canaries": len(canaries)}, (
        EXIT_OK if state == "installed_success" else EXIT_UNHEALTHY
    )


def cmd_media_authorize_purge(
    args: argparse.Namespace,
) -> tuple[dict[str, object], int]:
    config = _config_for(args)
    core = _core()
    with core.MirrorLedger(config.paths.ledger_path) as ledger:
        with _runtime_lock(ledger, config):
            queued = ledger.authorize_media_purge(
                args.event_id,
                evidence_ref=args.evidence_ref,
            )
    return {
        "event_ref": _opaque_summary(args.event_id),
        "queued": queued,
        "audited": queued > 0,
    }, EXIT_OK


def cmd_media_cleanup(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    config = _config_for(args)
    core = _core()
    removed = 0
    failed = 0
    with core.MirrorLedger(config.paths.ledger_path) as ledger:
        with _runtime_lock(ledger, config):
            for path in ledger.list_media_cleanup_due(limit=args.limit):
                candidate = Path(path)
                if not candidate.exists():
                    removed += int(ledger.mark_media_removed(path))
                    continue
                attachment = core.MediaAttachment(
                    media_id="cleanup",
                    kind="document",
                    path=path,
                    managed_temp=True,
                )
                if core.remove_managed_media(
                    attachment, config.worker.allowed_temp_root
                ):
                    removed += int(ledger.mark_media_removed(path))
                else:
                    failed += int(ledger.mark_media_cleanup_failed(path))
            remaining = ledger.managed_media_report()
    return {
        "removed": removed,
        "failed": failed,
        "remaining": remaining,
    }, EXIT_OPERATION if failed else EXIT_OK


def _read_bounded_text(source: str, *, max_bytes: int = 262_144) -> str:
    if source == "-":
        raw = sys.stdin.buffer.read(max_bytes + 1)
    else:
        path = Path(source).expanduser().resolve()
        if not path.is_file() or path.is_symlink():
            raise CLIError("input_missing", "input text file is missing or unsafe")
        if path.stat().st_size > max_bytes:
            raise CLIError("input_too_large", "input text exceeds the bounded limit")
        raw = path.read_bytes()
    if len(raw) > max_bytes:
        raise CLIError("input_too_large", "input text exceeds the bounded limit")
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CLIError("input_not_utf8", "input text must be UTF-8") from exc
    if not value.strip():
        raise CLIError("input_empty", "input text must not be empty")
    return value


def cmd_consumer_daily_notes(
    args: argparse.Namespace,
) -> tuple[dict[str, object], int]:
    config = _config_for(args)
    core = _core()
    with core.MirrorLedger(config.paths.ledger_path) as ledger:
        consumers = core.MirrorConsumers(ledger)
        result = consumers.project_daily_notes(
            Path(args.output_dir).expanduser().resolve(),
            consumer_id=args.consumer_id,
            allowed_scopes=args.scope,
            batch_limit=args.batch_limit,
        )
    return {
        "consumer_id": result.consumer_id,
        "processed_events": result.processed_events,
        "files_written": len(result.files),
        "cursors": dict(result.cursors),
    }, EXIT_OK


def cmd_consumer_claim_add(
    args: argparse.Namespace,
) -> tuple[dict[str, object], int]:
    config = _config_for(args)
    core = _core()
    text_value = _read_bounded_text(args.text_source)
    with core.MirrorLedger(config.paths.ledger_path) as ledger:
        consumers = core.MirrorConsumers(ledger)
        claim, created = consumers.add_claim(
            text_value,
            args.evidence,
            privacy_scope=args.scope,
            supersedes=args.supersedes or (),
        )
    return {
        "claim_id": claim.claim_id,
        "created": created,
        "privacy_scope": claim.privacy_scope,
        "evidence_count": len(claim.evidence_event_ids),
        "supersedes_count": len(claim.supersedes),
        "active": claim.active,
    }, EXIT_OK


def cmd_consumer_search_export(
    args: argparse.Namespace,
) -> tuple[dict[str, object], int]:
    config = _config_for(args)
    core = _core()
    with core.MirrorLedger(config.paths.ledger_path) as ledger:
        consumers = core.MirrorConsumers(ledger)
        result = consumers.export_search_projection(
            Path(args.output).expanduser().resolve(),
            allowed_scopes=args.scope,
            include_events=not args.no_events,
            include_claims=not args.no_claims,
            active_claims_only=not args.include_superseded_claims,
        )
    return {
        "event_documents": result.event_documents,
        "claim_documents": result.claim_documents,
        "sha256": result.sha256,
        "size_bytes": result.size_bytes,
    }, EXIT_OK


def cmd_consumer_report(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    config = _config_for(args)
    core = _core()
    with core.MirrorLedger(config.paths.ledger_path) as ledger:
        consumers = core.MirrorConsumers(ledger)
        if args.pending:
            result = consumers.pending_report(
                allowed_scopes=args.scope,
                include_content=False,
                limit=args.limit,
            )
        else:
            result = consumers.aggregate_report(allowed_scopes=args.scope)
    return result, EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = JSONArgumentParser(
        prog="espelho-zap",
        description="Operate the portable WhatsApp-to-Telegram mirror.",
        epilog=EXIT_CODE_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", help="secret-free TOML path (default: ESPELHO_ZAP_CONFIG or XDG path)")
    parser.add_argument("--pretty", action="store_true", help="pretty-print JSON output")
    parser.add_argument("--version", action="version", version=f"%(prog)s {_package_version()}")
    subcommands = parser.add_subparsers(dest="command", required=True)

    init_parser = subcommands.add_parser("init", help="create private runtime state and an empty ledger")
    init_parser.add_argument("--data-dir", help="override the generated data directory")
    init_parser.add_argument(
        "--minimum-free-bytes",
        type=int,
        default=268_435_456,
        help="free-space floor written to a new config",
    )
    init_parser.add_argument("--force", action="store_true", help="replace only the config file")
    init_parser.set_defaults(handler=cmd_init)

    doctor = subcommands.add_parser("doctor", help="check readiness without exposing secrets or content")
    doctor.add_argument(
        "--allow-missing-token",
        action="store_true",
        help="treat an unprovisioned Telegram token as acceptable during installation",
    )
    doctor.set_defaults(handler=cmd_doctor)

    health = subcommands.add_parser("health", help="show aggregate ledger health")
    health.set_defaults(handler=cmd_health)

    backup = subcommands.add_parser(
        "backup",
        help="create an atomic validated SQLite backup without overwriting",
    )
    backup.add_argument("destination", help="new backup file; it must not already exist")
    backup.set_defaults(handler=cmd_backup)

    route = subcommands.add_parser("route", help="manage explicit conversation-to-topic routes")
    route_commands = route.add_subparsers(dest="route_command", required=True)

    route_set = route_commands.add_parser("set", help="create or explicitly update one route")
    route_set.add_argument("conversation_id")
    route_set.add_argument("chat_id")
    route_set.add_argument("thread_id")
    route_set.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=True)
    route_set.add_argument("--allow-update", action="store_true")
    route_set.add_argument("--watermark-event-id")
    route_set.add_argument(
        "--source-profile",
        help="stable source profile; defaults to worker.profile_id",
    )
    route_set.set_defaults(handler=cmd_route_set)

    route_list = route_commands.add_parser("list", help="list route metadata without message content")
    route_list.add_argument("--enabled-only", action="store_true")
    route_list.add_argument(
        "--show-identifiers",
        action="store_true",
        help="explicitly include conversation, chat, and thread identifiers",
    )
    route_list.set_defaults(handler=cmd_route_list)

    route_blocked_list = route_commands.add_parser(
        "blocked-list",
        help="list content-free route blocks using opaque references",
    )
    route_blocked_list.add_argument(
        "--state",
        choices=("blocked_no_route", "requeued"),
        default="blocked_no_route",
    )
    route_blocked_list.add_argument("--limit", type=_positive_int, default=500)
    route_blocked_list.set_defaults(handler=cmd_route_blocked_list)

    route_reconcile = route_commands.add_parser(
        "reconcile",
        help="explicitly requeue held events after provisioning a route",
    )
    route_reconcile.add_argument("conversation_id")
    route_reconcile.add_argument("--limit", type=_positive_int, default=500)
    route_reconcile.add_argument("--source-profile")
    route_reconcile.set_defaults(handler=cmd_route_reconcile)

    route_verify = route_commands.add_parser(
        "verify-destination",
        help="verify a Telegram forum supergroup through read-only getChat",
    )
    route_verify.add_argument("chat_id", type=_negative_chat_id)
    route_verify.add_argument(
        "--thread-id",
        type=_positive_int,
        help="validate topic-id syntax; getChat cannot prove that the topic exists",
    )
    route_verify.set_defaults(handler=cmd_route_verify_destination)

    route_provision = route_commands.add_parser(
        "provision-topic",
        help="create one Telegram forum topic and commit its exact route",
    )
    route_provision.add_argument("conversation_id")
    route_provision.add_argument("chat_id", type=_negative_chat_id)
    route_provision.add_argument("topic_name")
    route_provision.add_argument("--source-profile")
    route_provision.add_argument("--allow-update", action="store_true")
    route_provision.add_argument(
        "--confirm-create",
        action="store_true",
        help="confirm the external Telegram topic mutation",
    )
    route_provision.set_defaults(handler=cmd_route_provision_topic)

    route_import = route_commands.add_parser("import-legacy_runtime", help="import legacy routes and delivered IDs")
    route_import.add_argument("source", help="legacy legacy_runtime JSON file")
    route_import.add_argument("--default-chat-id")
    route_import.add_argument("--allow-update", action="store_true")
    route_import.add_argument(
        "--source-profile",
        help="must match the runtime adapter profile; defaults to worker.profile_id",
    )
    route_import.add_argument(
        "--identity-map",
        help="optional schema-v1 JSON mapping verified runtime identities to legacy routes",
    )
    route_import.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and report changes, then roll back all database writes",
    )
    route_import.set_defaults(handler=cmd_route_import_legacy_runtime)

    scope = subcommands.add_parser(
        "scope", help="manage explicit per-conversation privacy policy"
    )
    scope_commands = scope.add_subparsers(dest="scope_command", required=True)
    scope_set = scope_commands.add_parser("set", help="set one audited privacy scope")
    scope_set.add_argument("conversation_id")
    scope_set.add_argument(
        "privacy_scope",
        choices=("area_shared", "partnership_restricted", "owner_private"),
    )
    scope_set.add_argument("--source-profile")
    scope_set.set_defaults(handler=cmd_scope_set)
    scope_list = scope_commands.add_parser("list", help="list privacy policies")
    scope_list.add_argument("--show-identifiers", action="store_true")
    scope_list.set_defaults(handler=cmd_scope_list)

    alias = subcommands.add_parser(
        "alias", help="reconcile a verified runtime conversation identity to a legacy route"
    )
    alias_commands = alias.add_subparsers(dest="alias_command", required=True)
    alias_set = alias_commands.add_parser("set", help="set one audited identity alias")
    alias_set.add_argument("observed_conversation")
    alias_set.add_argument("canonical_conversation")
    alias_set.add_argument("--allow-update", action="store_true")
    alias_set.add_argument("--observed-source-profile")
    alias_set.add_argument("--canonical-source-profile")
    alias_set.set_defaults(handler=cmd_alias_set)
    alias_list = alias_commands.add_parser("list", help="list identity aliases")
    alias_list.add_argument("--show-identifiers", action="store_true")
    alias_list.set_defaults(handler=cmd_alias_list)

    group = subcommands.add_parser(
        "group", help="approve exact WhatsApp groups without enabling an agent"
    )
    group_commands = group.add_subparsers(dest="group_command", required=True)
    group_approve = group_commands.add_parser(
        "approve", help="approve one exact group after its exact topic route exists"
    )
    group_approve.add_argument("conversation_id")
    group_approve.add_argument("--source-profile")
    group_approve.add_argument(
        "--privacy-scope", required=True,
        choices=("area_shared", "partnership_restricted", "owner_private"),
    )
    group_approve.add_argument(
        "--agent-mode", choices=("none", "mention_only"), default="none"
    )
    group_approve.add_argument(
        "--grill", help="completed ten-field JSON; mandatory for mention_only"
    )
    group_approve.add_argument("--confirm-approve", action="store_true")
    group_approve.set_defaults(handler=cmd_group_approve)

    identity = subcommands.add_parser(
        "identity", help="manage private human-readable participant identities"
    )
    identity_commands = identity.add_subparsers(dest="identity_command", required=True)
    identity_set = identity_commands.add_parser("set", help="set one approved manual label")
    identity_set.add_argument("conversation_id")
    identity_set.add_argument("actor_id")
    identity_set.add_argument("display_label")
    identity_set.add_argument("--source-profile")
    identity_set.set_defaults(handler=cmd_identity_set)

    delivery = subcommands.add_parser(
        "delivery", help="inspect and explicitly reconcile delivery state"
    )
    delivery_commands = delivery.add_subparsers(
        dest="delivery_command", required=True
    )
    delivery_list = delivery_commands.add_parser(
        "list", help="list content-free delivery state with opaque identifiers"
    )
    delivery_list.add_argument(
        "--state",
        choices=(
            "pending", "inflight", "retry", "sent", "dead", "blocked", "uncertain"
        ),
    )
    delivery_list.add_argument("--limit", type=_positive_int, default=500)
    delivery_list.set_defaults(handler=cmd_delivery_list)

    delivery_uncertain = delivery_commands.add_parser(
        "reconcile-uncertain",
        help="resolve an uncertain send only with an operator evidence reference",
    )
    delivery_uncertain.add_argument("event_id")
    delivery_uncertain.add_argument(
        "resolution", choices=("sent", "retry")
    )
    delivery_uncertain.add_argument("--evidence-ref", required=True)
    delivery_uncertain.set_defaults(handler=cmd_delivery_reconcile_uncertain)

    delivery_rebind = delivery_commands.add_parser(
        "rebind-route",
        help="rebind route_changed deliveries to the current explicit topic",
    )
    delivery_rebind.add_argument("conversation_id")
    delivery_rebind.add_argument("--evidence-ref", required=True)
    delivery_rebind.add_argument("--source-profile")
    delivery_rebind.add_argument("--limit", type=_positive_int, default=500)
    delivery_rebind.set_defaults(handler=cmd_delivery_rebind_route)

    ingest = subcommands.add_parser("ingest", help="record one inbound event from JSON file or stdin")
    ingest.add_argument("source", nargs="?", default="-", help="JSON path, or - for stdin")
    ingest.set_defaults(handler=cmd_ingest)

    observer = subcommands.add_parser(
        "observer-once",
        help="persist one bounded batch from a loopback Hermes bridge spool",
    )
    observer.add_argument(
        "--bridge-url",
        default="http://127.0.0.1:3011",
        help="loopback-only paired bridge base URL",
    )
    observer.add_argument("--source-profile")
    observer.add_argument(
        "--privacy-scope",
        choices=("area_shared", "partnership_restricted", "owner_private"),
        default="owner_private",
    )
    observer.add_argument("--batch-limit", type=_positive_int, default=100)
    observer.add_argument("--timeout-seconds", type=float, default=10.0)
    observer.add_argument(
        "--maximum-response-bytes", type=_positive_int, default=8 * 1024 * 1024
    )
    observer.set_defaults(handler=cmd_observer_once)

    worker = subcommands.add_parser("worker-once", help="process at most one due delivery")
    worker.add_argument("--worker-id")
    worker.add_argument("--profile")
    worker.set_defaults(handler=cmd_worker_once)

    drain = subcommands.add_parser(
        "worker-drain",
        help="process a bounded sequential backlog while preserving WIP=1",
    )
    drain.add_argument("--worker-id")
    drain.add_argument("--profile")
    drain.add_argument("--max-items", type=_positive_int, default=100)
    drain.add_argument("--max-seconds", type=_positive_int, default=50)
    drain.set_defaults(handler=cmd_worker_drain)

    media = subcommands.add_parser(
        "media", help="inspect and clean only product-managed spool files"
    )
    media_commands = media.add_subparsers(dest="media_command", required=True)
    media_report = media_commands.add_parser(
        "report", help="show content-free managed-media counts and bytes"
    )
    media_report.set_defaults(handler=cmd_media_report)
    media_authorize = media_commands.add_parser(
        "authorize-purge",
        help="audit and queue media from one terminal or blocked event",
    )
    media_authorize.add_argument("event_id")
    media_authorize.add_argument("--evidence-ref", required=True)
    media_authorize.set_defaults(handler=cmd_media_authorize_purge)
    media_cleanup = media_commands.add_parser(
        "cleanup", help="delete only audited cleanup-pending spool files"
    )
    media_cleanup.add_argument("--limit", type=_positive_int, default=100)
    media_cleanup.set_defaults(handler=cmd_media_cleanup)

    acceptance = subcommands.add_parser(
        "acceptance", help="record mandatory real human installation canaries"
    )
    acceptance_commands = acceptance.add_subparsers(
        dest="acceptance_command", required=True
    )
    acceptance_record = acceptance_commands.add_parser("record")
    acceptance_record.add_argument("direction", choices=("inbound", "outbound"))
    acceptance_record.add_argument("media_kind", choices=("text", "image", "audio", "voice"))
    acceptance_record.add_argument("--evidence-ref", required=True)
    for flag in (
        "exact-route", "single-delivery", "no-dm-fallback",
        "integrity-ok", "no-enrichment", "human-confirmed",
    ):
        acceptance_record.add_argument(f"--{flag}", action="store_true", required=True)
    acceptance_record.set_defaults(handler=cmd_acceptance_record)
    acceptance_status = acceptance_commands.add_parser("status")
    acceptance_status.set_defaults(handler=cmd_acceptance_status)

    consumer = subcommands.add_parser(
        "consumer",
        help="run optional Daily Notes, claim, search, and report projections",
    )
    consumer_commands = consumer.add_subparsers(
        dest="consumer_command", required=True
    )

    daily_notes = consumer_commands.add_parser(
        "daily-notes", help="project bounded multichannel Daily Notes"
    )
    daily_notes.add_argument("output_dir")
    daily_notes.add_argument(
        "--scope",
        action="append",
        required=True,
        choices=("area_shared", "partnership_restricted", "owner_private"),
    )
    daily_notes.add_argument("--consumer-id", default="daily-notes")
    daily_notes.add_argument("--batch-limit", type=_positive_int, default=500)
    daily_notes.set_defaults(handler=cmd_consumer_daily_notes)

    claim_add = consumer_commands.add_parser(
        "claim-add", help="append one immutable evidence-backed claim"
    )
    claim_add.add_argument("text_source", help="UTF-8 file, or - for stdin")
    claim_add.add_argument(
        "--scope",
        required=True,
        choices=("area_shared", "partnership_restricted", "owner_private"),
    )
    claim_add.add_argument("--evidence", action="append", required=True)
    claim_add.add_argument("--supersedes", action="append")
    claim_add.set_defaults(handler=cmd_consumer_claim_add)

    search_export = consumer_commands.add_parser(
        "search-export", help="write a deterministic provider-neutral JSONL projection"
    )
    search_export.add_argument("output")
    search_export.add_argument(
        "--scope",
        action="append",
        required=True,
        choices=("area_shared", "partnership_restricted", "owner_private"),
    )
    search_export.add_argument("--no-events", action="store_true")
    search_export.add_argument("--no-claims", action="store_true")
    search_export.add_argument("--include-superseded-claims", action="store_true")
    search_export.set_defaults(handler=cmd_consumer_search_export)

    report = consumer_commands.add_parser(
        "report", help="emit aggregate or content-free pending metadata"
    )
    report.add_argument(
        "--scope",
        action="append",
        required=True,
        choices=("area_shared", "partnership_restricted", "owner_private"),
    )
    report.add_argument("--pending", action="store_true")
    report.add_argument("--limit", type=_positive_int, default=100)
    report.set_defaults(handler=cmd_consumer_report)
    return parser


def _command_name(args: argparse.Namespace | None) -> str:
    if args is None:
        return "parse"
    values = [
        getattr(args, "command", None),
        getattr(args, "route_command", None),
        getattr(args, "delivery_command", None),
        getattr(args, "media_command", None),
        getattr(args, "consumer_command", None),
    ]
    return " ".join(str(value) for value in values if value) or "unknown"


def main(argv: Sequence[str] | None = None) -> int:
    if os.name == "posix":
        os.umask(0o077)
    args: argparse.Namespace | None = None
    pretty = False
    try:
        args = build_parser().parse_args(argv)
        pretty = bool(args.pretty)
        result, exit_code = args.handler(args)
        _emit(
            {
                "ok": exit_code == EXIT_OK,
                "command": _command_name(args),
                "result": result,
            },
            pretty=pretty,
        )
        return exit_code
    except CLIError as exc:
        exit_code = exc.exit_code
        code = exc.code
        message = exc.safe_message
    except ConfigError as exc:
        exit_code = EXIT_CONFIG
        code = "configuration_error"
        message = str(exc)
    except (ValueError, FileNotFoundError) as exc:
        exit_code = EXIT_OPERATION
        code = getattr(exc, "code", "operation_rejected")
        message = str(code).replace("_", " ")
    except KeyboardInterrupt:
        exit_code = EXIT_INTERRUPTED
        code = "interrupted"
        message = "operation interrupted"
    except Exception as exc:  # fail closed without echoing provider URLs, tokens, or content
        exit_code = EXIT_OPERATION
        code = getattr(exc, "code", "operation_failed")
        message = f"operation failed safely ({type(exc).__name__})"
    _emit(
        {
            "ok": False,
            "command": _command_name(args),
            "error": {"code": str(code), "message": message},
        },
        pretty=pretty,
    )
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
