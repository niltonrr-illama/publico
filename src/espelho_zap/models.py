"""Immutable domain models for the portable WhatsApp mirror."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping


MEDIA_KINDS = frozenset({"image", "audio", "voice", "video", "document"})
PRIVACY_SCOPES = frozenset({"area_shared", "partnership_restricted", "owner_private"})
EVENT_SCHEMA_VERSION = 3
_INTEGER_ID = re.compile(r"^-?[0-9]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_REF = re.compile(r"^[a-z][a-z0-9_-]{0,31}:[0-9a-f]{64}$")
_MIME_TYPE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")


def _required_string(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid_{name}")
    return value


def opaque_ref(namespace: str, raw_value: str) -> str:
    if not isinstance(namespace, str) or not re.fullmatch(
        r"[a-z][a-z0-9_-]{0,31}", namespace
    ):
        raise ValueError("invalid_opaque_namespace")
    raw = _required_string("opaque_source", raw_value)
    return f"{namespace}:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


DEFAULT_SOURCE_PROFILE_ID = opaque_ref("profile", "default")


def _require_opaque_ref(name: str, value: object) -> str:
    if not isinstance(value, str) or not _OPAQUE_REF.fullmatch(value):
        raise ValueError(f"invalid_{name}")
    return value


def canonical_whatsapp_event_ref(
    source_profile_id: str,
    conversation_id: str,
    raw_message_id: str,
) -> str:
    """Return one event identity stable across explicit runtime aliases."""

    profile = _require_opaque_ref("source_profile_id", source_profile_id)
    conversation = _require_opaque_ref("conversation_id", conversation_id)
    message = _required_string("message_id", raw_message_id)
    return opaque_ref(
        "event",
        f"whatsapp-canonical\x1f{profile}\x1f{conversation}\x1f{message}",
    )


@dataclass(frozen=True, slots=True)
class MediaAttachment:
    """One original attachment referenced by an inbound event.

    ``path`` is deliberately local runtime state. It is never included in
    diagnostics, but is persisted in the private ledger so a retry can reopen
    the same file. ``managed_temp`` is the sole opt-in for post-delivery
    deletion.
    """

    media_id: str
    kind: str
    path: str
    mime_type: str = ""
    sha256: str = ""
    size_bytes: int = 0
    caption: str = ""
    managed_temp: bool = False

    def __post_init__(self) -> None:
        _required_string("media_id", self.media_id)
        _required_string("media_path", self.path)
        if not isinstance(self.kind, str) or self.kind not in MEDIA_KINDS:
            raise ValueError("invalid_media_kind")
        if not isinstance(self.mime_type, str) or (
            self.mime_type and not _MIME_TYPE.fullmatch(self.mime_type)
        ):
            raise ValueError("invalid_media_mime_type")
        if not isinstance(self.sha256, str) or (
            self.sha256 and not _SHA256.fullmatch(self.sha256)
        ):
            raise ValueError("invalid_media_sha256")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise ValueError("invalid_media_size")
        if not isinstance(self.caption, str):
            raise ValueError("invalid_media_caption")
        if not isinstance(self.managed_temp, bool):
            raise ValueError("invalid_managed_temp")

    def payload_dict(self) -> dict[str, object]:
        """Return stable, content-relevant fields (excluding the local path)."""
        return {
            "media_id": self.media_id,
            "kind": self.kind,
            "mime_type": self.mime_type,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "caption": self.caption,
        }

    def storage_dict(self) -> dict[str, object]:
        value = self.payload_dict()
        value.update({"path": self.path, "managed_temp": self.managed_temp})
        return value

    @classmethod
    def from_storage_dict(cls, value: Mapping[str, Any]) -> "MediaAttachment":
        return cls(
            media_id=str(value["media_id"]),
            kind=str(value["kind"]),
            path=str(value["path"]),
            mime_type=str(value.get("mime_type") or ""),
            sha256=str(value.get("sha256") or ""),
            size_bytes=int(value.get("size_bytes") or 0),
            caption=str(value.get("caption") or ""),
            managed_temp=bool(value.get("managed_temp", False)),
        )


@dataclass(frozen=True, slots=True)
class InboundEvent:
    """An immutable WhatsApp inbound before any LLM or projection."""

    event_id: str
    source: str
    conversation_id: str
    occurred_at: str
    actor_ref: str
    source_profile_id: str = DEFAULT_SOURCE_PROFILE_ID
    privacy_scope: str = "owner_private"
    text: str = ""
    context_text: str = ""
    media: tuple[MediaAttachment, ...] = ()
    conversation_kind: str = "direct"
    actor_display_label: str = ""
    schema_version: int = EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("event_id", "source", "occurred_at"):
            _required_string(name, getattr(self, name))
        _require_opaque_ref("conversation_id", self.conversation_id)
        _require_opaque_ref("actor_ref", self.actor_ref)
        _require_opaque_ref("source_profile_id", self.source_profile_id)
        if not isinstance(self.text, str):
            raise ValueError("invalid_text")
        if not isinstance(self.context_text, str):
            raise ValueError("invalid_context_text")
        if self.conversation_kind not in {"direct", "group"}:
            raise ValueError("invalid_conversation_kind")
        if not isinstance(self.actor_display_label, str):
            raise ValueError("invalid_actor_display_label")
        if self.privacy_scope not in PRIVACY_SCOPES:
            raise ValueError("invalid_privacy_scope")
        if not isinstance(self.media, tuple) or not all(
            isinstance(item, MediaAttachment) for item in self.media
        ):
            raise ValueError("invalid_media_collection")
        if not self.text and not self.context_text and not self.media:
            raise ValueError("empty_event")
        if not isinstance(self.schema_version, int) or isinstance(
            self.schema_version, bool
        ) or self.schema_version not in {1, 2, EVENT_SCHEMA_VERSION}:
            raise ValueError("unsupported_event_schema")

    def payload_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": self.schema_version,
            "source": self.source,
            "conversation_id": self.conversation_id,
            "occurred_at": self.occurred_at,
            "actor_ref": self.actor_ref,
            "privacy_scope": self.privacy_scope,
            "text": self.text,
            "media": [item.payload_dict() for item in self.media],
        }
        # Schema v1 predates source-profile isolation.  Omitting the new field
        # preserves its immutable payload hash during an in-place upgrade.
        if self.schema_version >= 2:
            value["source_profile_id"] = self.source_profile_id
        if self.schema_version >= 3:
            value["context_text"] = self.context_text
            value["conversation_kind"] = self.conversation_kind
            value["actor_display_label"] = self.actor_display_label
        return value

    def payload_hash(self) -> str:
        raw = json.dumps(
            self.payload_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def storage_json(self) -> str:
        value = self.payload_dict()
        value["event_id"] = self.event_id
        value["media"] = [item.storage_dict() for item in self.media]
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_storage_json(cls, raw: str) -> "InboundEvent":
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("invalid_event_storage")
        media = value.get("media") or []
        if not isinstance(media, list):
            raise ValueError("invalid_event_storage")
        return cls(
            event_id=str(value["event_id"]),
            source=str(value["source"]),
            conversation_id=str(value["conversation_id"]),
            occurred_at=str(value["occurred_at"]),
            actor_ref=str(value["actor_ref"]),
            source_profile_id=str(
                value.get("source_profile_id") or DEFAULT_SOURCE_PROFILE_ID
            ),
            privacy_scope=str(value.get("privacy_scope") or "owner_private"),
            text=str(value.get("text") or ""),
            context_text=str(value.get("context_text") or ""),
            media=tuple(MediaAttachment.from_storage_dict(item) for item in media),
            conversation_kind=str(value.get("conversation_kind") or "direct"),
            actor_display_label=str(value.get("actor_display_label") or ""),
            schema_version=int(value.get("schema_version") or 0),
        )


@dataclass(frozen=True, slots=True)
class Route:
    """A WhatsApp conversation mapped to a Telegram forum topic.

    Telegram identifiers remain strings to avoid integer truncation across
    runtimes. A thread is mandatory: a private-message destination is never a
    valid data-plane route.
    """

    conversation_id: str
    chat_id: str
    thread_id: str
    enabled: bool = True

    def __post_init__(self) -> None:
        _require_opaque_ref("conversation_id", self.conversation_id)
        for name in ("chat_id", "thread_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _INTEGER_ID.fullmatch(value):
                raise ValueError(f"invalid_{name}")
        # Telegram group/supergroup identifiers are negative.  Requiring a
        # negative chat id is the local, fail-closed proof that a configured
        # route is not a user DM.  A live doctor additionally verifies
        # ``type=supergroup`` and ``is_forum=true`` through getChat.
        if int(self.chat_id) >= 0:
            raise ValueError("group_chat_required")
        if int(self.thread_id) <= 0:
            raise ValueError("topic_required")
        if not isinstance(self.enabled, bool):
            raise ValueError("invalid_route_state")


@dataclass(frozen=True, slots=True)
class DeliveryClaim:
    delivery_id: int
    attempt_no: int
    event: InboundEvent
    route: Route
    worker_id: str
    lease_expires_at: int


@dataclass(frozen=True, slots=True)
class RouteBlock:
    """Sanitized operational view of an event held without a route."""

    event_ref: str
    conversation_id: str
    state: str
    reason: str
    blocked_at: int
    updated_at: int
    requeued_at: int | None = None
