"""Bounded importer for the legacy_runtime telegram-mirror-config JSON state."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .ledger import MirrorLedger, RouteConflictError
from .models import Route, canonical_whatsapp_event_ref, opaque_ref


_CONTAINERS = (
    "contactTopics",
    "topics",
    "routes",
    "mappings",
    "contacts",
    "conversations",
)
_THREAD_KEYS = (
    "threadId",
    "thread_id",
    "topicId",
    "topic_id",
    "telegramTopicId",
    "telegram_topic_id",
    "messageThreadId",
    "message_thread_id",
)
_CHAT_KEYS = (
    "forum_chat_id",
    "telegramChatId",
    "telegram_chat_id",
    "targetChatId",
    "target_chat_id",
    "telegramGroupId",
    "telegram_group_id",
    "groupId",
    "group_id",
    "groupChatId",
    "group_chat_id",
    "chatId",
    "chat_id",
)
_CONVERSATION_KEYS = (
    "conversationId",
    "conversation_id",
    "whatsappId",
    "whatsapp_id",
    "contactId",
    "contact_id",
    "jid",
    "key",
    "phone",
)
_RECENT_KEYS = ("recentRoutedInboundMessageIds", "recent_routed_inbound_message_ids")
_WATERMARK_KEYS = ("lastRoutedInboundMessageId", "last_routed_inbound_message_id")
_TERMINAL_EVENT_KEYS = (
    "lastBlockedVideoMessageId",
    "last_blocked_video_message_id",
    "lastSuggestedInboundMessageId",
    "last_suggested_inbound_message_id",
)


class LegacyImportError(RuntimeError):
    def __init__(self, code: str = "legacy_import_error"):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class LegacyImportResult:
    imported: bool
    routes_seen: int
    routes_created_or_updated: int
    recent_ids_seen: int
    tombstones_created: int
    skipped_records: int
    aliases_seen: int = 0
    aliases_created_or_updated: int = 0
    dry_run: bool = False


def legacy_conversation_id(
    raw_identifier: str, source_profile_id: str = "default"
) -> str:
    """Normalize a legacy contact/conversation identifier without retaining it."""
    value = str(raw_identifier).strip()
    if not value:
        raise LegacyImportError("legacy_conversation_missing")
    profile = opaque_ref("profile", source_profile_id)
    return opaque_ref("conversation", f"{profile}\x1f{value}")


def _first(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None and str(value).strip():
            return value
    return None


def _records(data: Mapping[str, Any]) -> list[tuple[str | None, Mapping[str, Any]]]:
    for key in _CONTAINERS:
        container = data.get(key)
        if isinstance(container, Mapping):
            return [
                (str(raw_key), value)
                for raw_key, value in container.items()
                if isinstance(value, Mapping)
            ]
        if isinstance(container, list):
            return [(None, value) for value in container if isinstance(value, Mapping)]
    return [
        (str(raw_key), value)
        for raw_key, value in data.items()
        if isinstance(value, Mapping) and _first(value, _THREAD_KEYS) is not None
    ]


def _load_source(source: str | Path | Mapping[str, Any]) -> tuple[Mapping[str, Any], str]:
    if isinstance(source, Mapping):
        try:
            raw = json.dumps(source, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        except (TypeError, ValueError):
            raise LegacyImportError("legacy_json_invalid") from None
        return source, hashlib.sha256(raw).hexdigest()
    try:
        raw = Path(source).read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise LegacyImportError("legacy_json_unreadable") from None
    if not isinstance(value, Mapping):
        raise LegacyImportError("legacy_json_invalid")
    return value, hashlib.sha256(raw).hexdigest()


def _identity_plans(
    source: str | Path | Mapping[str, Any] | None,
    *,
    default_source_profile_id: str,
) -> tuple[list[tuple[str, str, str, str]], str]:
    """Load an explicit runtime-to-legacy identity map without guessing."""

    if source is None:
        return [], ""
    value, source_hash = _load_source(source)
    if value.get("schema_version") != 1 or not isinstance(value.get("mappings"), list):
        raise LegacyImportError("legacy_identity_map_invalid")
    plans: list[tuple[str, str, str, str]] = []
    for item in value["mappings"]:
        if not isinstance(item, Mapping):
            raise LegacyImportError("legacy_identity_map_invalid")
        legacy_raw = item.get("legacy_conversation_id")
        runtime_raw = item.get("runtime_conversation_id")
        runtime_profile = item.get("runtime_source_profile_id", default_source_profile_id)
        if not all(isinstance(part, str) and part.strip() for part in (
            legacy_raw,
            runtime_raw,
            runtime_profile,
        )):
            raise LegacyImportError("legacy_identity_map_invalid")
        plans.append(
            (
                legacy_conversation_id(str(runtime_raw), str(runtime_profile)),
                legacy_conversation_id(str(legacy_raw), default_source_profile_id),
                str(runtime_raw),
                str(runtime_profile),
            )
        )
    if len({alias for alias, _, _, _ in plans}) != len(plans):
        raise LegacyImportError("legacy_identity_map_duplicate")
    return plans, source_hash


def import_legacy_runtime_config(
    ledger: MirrorLedger,
    source: str | Path | Mapping[str, Any],
    *,
    default_chat_id: str | None = None,
    allow_route_update: bool = False,
    dry_run: bool = False,
    source_profile_id: str = "default",
    identity_map: str | Path | Mapping[str, Any] | None = None,
) -> LegacyImportResult:
    """Import routing/dedupe state only; ignore names, content, and credentials."""
    data, config_hash = _load_source(source)
    alias_plans, identity_hash = _identity_plans(
        identity_map,
        default_source_profile_id=source_profile_id,
    )
    source_hash = hashlib.sha256(
        f"{config_hash}\x1f{identity_hash}\x1f{source_profile_id}".encode("utf-8")
    ).hexdigest()
    global_chat = default_chat_id or _first(data, _CHAT_KEYS)
    records = _records(data)
    routes_seen = 0
    routes_changed = 0
    recent_seen = 0
    tombstones = 0
    skipped = 0
    aliases_changed = 0
    plans: list[tuple[Route, str | None, list[str]]] = []
    recent_raw_by_conversation: dict[str, list[str]] = {}
    for parent_key, record in records:
        raw_conversation = _first(record, _CONVERSATION_KEYS) or parent_key
        raw_thread = _first(record, _THREAD_KEYS)
        raw_chat = _first(record, _CHAT_KEYS) or global_chat
        if raw_conversation is None or raw_thread is None or raw_chat is None:
            skipped += 1
            continue
        raw_conversation_text = str(raw_conversation)
        conversation_id = legacy_conversation_id(
            raw_conversation_text, source_profile_id
        )
        profile_ref = opaque_ref("profile", source_profile_id)
        try:
            route = Route(
                conversation_id=conversation_id,
                chat_id=str(raw_chat),
                thread_id=str(raw_thread),
                enabled=bool(record.get("enabled", True)),
            )
        except ValueError:
            skipped += 1
            continue
        watermark_value = _first(record, _WATERMARK_KEYS)
        watermark = str(watermark_value) if watermark_value is not None else None
        recent: list[str] = []
        for key in _RECENT_KEYS:
            values = record.get(key)
            if isinstance(values, list):
                recent.extend(str(item) for item in values if str(item).strip())
        for key in _TERMINAL_EVENT_KEYS:
            value = record.get(key)
            if value is not None and str(value).strip():
                recent.append(str(value))
        if watermark and watermark.strip():
            recent.append(watermark)
        recent = list(dict.fromkeys(recent))
        normalized_event_ids: list[str] = []
        for raw_message_id in recent:
            normalized_event_ids.extend(
                (
                    opaque_ref(
                        "event",
                        f"whatsapp\x1f{profile_ref}\x1f{raw_conversation_text}\x1f{raw_message_id}",
                    ),
                    canonical_whatsapp_event_ref(
                        profile_ref, conversation_id, raw_message_id
                    ),
                )
            )
        normalized_event_ids = list(dict.fromkeys(normalized_event_ids))
        plans.append((route, watermark, normalized_event_ids))
        recent_raw_by_conversation[conversation_id] = recent
        raw_aliases = record.get("aliases", [])
        if raw_aliases is None:
            raw_aliases = []
        if not isinstance(raw_aliases, list) or any(
            not isinstance(alias, str) or not alias.strip() for alias in raw_aliases
        ):
            raise LegacyImportError("legacy_route_aliases_invalid")
        for raw_alias in raw_aliases:
            alias_text = raw_alias.strip()
            if alias_text == raw_conversation_text:
                continue
            alias_plans.append(
                (
                    legacy_conversation_id(alias_text, source_profile_id),
                    conversation_id,
                    alias_text,
                    source_profile_id,
                )
            )
        routes_seen += 1
        recent_seen += len(recent)

    aliases_by_id: dict[str, tuple[str, str, str]] = {}
    for alias_id, canonical_id, runtime_raw, runtime_profile in alias_plans:
        previous = aliases_by_id.get(alias_id)
        if previous is not None and previous[0] != canonical_id:
            raise LegacyImportError("legacy_route_alias_conflict")
        aliases_by_id[alias_id] = (canonical_id, runtime_raw, runtime_profile)
    alias_plans = [
        (alias_id, canonical_id, runtime_raw, runtime_profile)
        for alias_id, (canonical_id, runtime_raw, runtime_profile) in aliases_by_id.items()
    ]

    # If the runtime exposes a different stable conversation key, preserve the
    # legacy dedupe set under that live event identity as well.  The tombstone
    # remains attached to the canonical legacy route; no message is replayed.
    for _, canonical_id, runtime_raw, runtime_profile in alias_plans:
        recent_raw = recent_raw_by_conversation.get(canonical_id, [])
        runtime_profile_ref = opaque_ref("profile", runtime_profile)
        live_event_ids = [
            opaque_ref(
                "event",
                f"whatsapp\x1f{runtime_profile_ref}\x1f{runtime_raw}\x1f{message_id}",
            )
            for message_id in recent_raw
        ]
        for index, (route, watermark, event_ids) in enumerate(plans):
            if route.conversation_id == canonical_id:
                plans[index] = (
                    route,
                    watermark,
                    list(dict.fromkeys([*event_ids, *live_event_ids])),
                )
                break

    if dry_run:
        for route, watermark, normalized_event_ids in plans:
            row = ledger.connection.execute(
                "SELECT chat_id, thread_id, enabled, watermark_hash FROM mirror_routes WHERE conversation_id = ?",
                (route.conversation_id,),
            ).fetchone()
            watermark_hash = (
                hashlib.sha256(watermark.encode("utf-8")).hexdigest() if watermark else None
            )
            if row is None:
                routes_changed += 1
            else:
                same = (
                    str(row["chat_id"]) == route.chat_id
                    and str(row["thread_id"]) == route.thread_id
                    and bool(row["enabled"]) == route.enabled
                )
                if not same and not allow_route_update:
                    raise RouteConflictError()
                next_watermark = watermark_hash or row["watermark_hash"]
                if not same or next_watermark != row["watermark_hash"]:
                    routes_changed += 1
            tombstones += sum(
                not ledger.is_legacy_delivered(route.conversation_id, event_id)
                for event_id in normalized_event_ids
            )
        for alias_id, canonical_id, _, _ in alias_plans:
            row = ledger.connection.execute(
                "SELECT canonical_id FROM mirror_conversation_aliases WHERE alias_id = ?",
                (alias_id,),
            ).fetchone()
            if row is None:
                aliases_changed += 1
            elif str(row["canonical_id"]) != canonical_id:
                if not allow_route_update:
                    raise RouteConflictError("conversation_alias_conflict")
                aliases_changed += 1
        imported = not bool(
            ledger.connection.execute(
                "SELECT 1 FROM mirror_legacy_imports WHERE source_hash = ?", (source_hash,)
            ).fetchone()
        )
        return LegacyImportResult(
            imported=imported,
            routes_seen=routes_seen,
            routes_created_or_updated=routes_changed,
            recent_ids_seen=recent_seen,
            tombstones_created=tombstones,
            skipped_records=skipped,
            aliases_seen=len(alias_plans),
            aliases_created_or_updated=aliases_changed,
            dry_run=True,
        )

    ledger.connection.execute("BEGIN IMMEDIATE")
    try:
        for route, watermark, normalized_event_ids in plans:
            routes_changed += int(
                ledger.set_route(
                    route,
                    watermark_event_id=watermark,
                    allow_update=allow_route_update,
                )
            )
            tombstones += ledger.add_legacy_delivered_ids(
                route.conversation_id, normalized_event_ids
            )
        for alias_id, canonical_id, _, _ in alias_plans:
            aliases_changed += int(
                ledger.set_conversation_alias(
                    alias_id,
                    canonical_id,
                    allow_update=allow_route_update,
                )
            )
        imported = ledger.record_legacy_import(source_hash, routes_seen, recent_seen)
        ledger.connection.execute("COMMIT")
    except BaseException:
        ledger.connection.execute("ROLLBACK")
        raise
    return LegacyImportResult(
        imported=imported,
        routes_seen=routes_seen,
        routes_created_or_updated=routes_changed,
        recent_ids_seen=recent_seen,
        tombstones_created=tombstones,
        skipped_records=skipped,
        aliases_seen=len(alias_plans),
        aliases_created_or_updated=aliases_changed,
        dry_run=dry_run,
    )
