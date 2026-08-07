"""Portable adapter contract and canonical WhatsApp normalization.

Host integrations may have different object shapes, but they must produce this
small raw contract before touching the durable ledger.  Raw identifiers are
hashed at this boundary; message bodies and original media references are not
interpreted by an LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..models import InboundEvent, MediaAttachment, PRIVACY_SCOPES, opaque_ref


_MEDIA_KIND_ALIASES = {
    "image": "image",
    "photo": "image",
    "audio": "audio",
    "voice": "voice",
    "ptt": "voice",
    "video": "video",
    "document": "document",
    "file": "document",
}


class AdapterContractError(ValueError):
    """A sanitized, machine-readable adapter failure."""

    def __init__(self, code: str = "adapter_contract_error"):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class AdapterCapabilities:
    adapter_id: str
    platforms: tuple[str, ...]
    capture_stage: str
    supports_media_refs: bool
    supports_partial_records: bool
    requires_explicit_privacy_scope: bool = True
    outbound_whatsapp: bool = False


@dataclass(frozen=True, slots=True)
class RawMediaRef:
    raw_id: str
    kind: str
    path: str
    mime_type: str = ""
    sha256: str = ""
    size_bytes: int = 0
    caption: str = ""
    managed_temp: bool = False


@dataclass(frozen=True, slots=True)
class RawInboundMessage:
    platform: str
    direction: str
    raw_message_id: str
    raw_conversation_id: str
    raw_actor_id: str
    occurred_at: str
    privacy_scope: str
    source_profile_id: str = "default"
    text: str = ""
    context_text: str = ""
    media: tuple[RawMediaRef, ...] = ()
    conversation_kind: str = "direct"
    actor_display_label: str = ""


@runtime_checkable
class InboundAdapter(Protocol):
    @property
    def capabilities(self) -> AdapterCapabilities: ...


def _required(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AdapterContractError(f"invalid_{name}")
    return value


def normalize_media_kind(value: str) -> str:
    key = _required("media_kind", value).lower()
    try:
        return _MEDIA_KIND_ALIASES[key]
    except KeyError:
        raise AdapterContractError("unsupported_media_kind") from None


def normalize_inbound(raw: RawInboundMessage) -> InboundEvent | None:
    """Normalize one proven inbound WhatsApp message.

    Non-WhatsApp and outbound records are deliberately ignored.  Missing
    identity, scope, timestamp, or media facts fail closed with an opaque error
    code rather than inventing a contact, route, or destination.
    """
    if not isinstance(raw, RawInboundMessage):
        raise AdapterContractError("invalid_raw_message")
    platform = _required("platform", raw.platform).lower()
    direction = _required("direction", raw.direction).lower()
    if platform != "whatsapp" or direction != "inbound":
        return None
    if raw.privacy_scope not in PRIVACY_SCOPES:
        raise AdapterContractError("invalid_privacy_scope")
    raw_message_id = _required("message_id", raw.raw_message_id)
    raw_profile_id = _required("source_profile_id", raw.source_profile_id)
    profile_id = opaque_ref("profile", raw_profile_id)
    raw_conversation_id = _required("conversation_id", raw.raw_conversation_id)
    raw_actor_id = _required("actor_id", raw.raw_actor_id)
    occurred_at = _required("occurred_at", raw.occurred_at)
    if not isinstance(raw.text, str):
        raise AdapterContractError("invalid_text")
    if not isinstance(raw.context_text, str):
        raise AdapterContractError("invalid_context_text")
    if raw.conversation_kind not in {"direct", "group"}:
        raise AdapterContractError("invalid_conversation_kind")
    if not isinstance(raw.actor_display_label, str):
        raise AdapterContractError("invalid_actor_display_label")
    if not isinstance(raw.media, tuple):
        raise AdapterContractError("invalid_media_collection")

    media: list[MediaAttachment] = []
    for index, item in enumerate(raw.media):
        if not isinstance(item, RawMediaRef):
            raise AdapterContractError("invalid_media_ref")
        raw_media_id = _required("media_id", item.raw_id)
        try:
            media.append(
                MediaAttachment(
                    media_id=opaque_ref(
                        "media",
                        f"{profile_id}\x1f{raw_conversation_id}\x1f{raw_message_id}\x1f{index}\x1f{raw_media_id}",
                    ),
                    kind=normalize_media_kind(item.kind),
                    path=_required("media_path", item.path),
                    mime_type=item.mime_type,
                    sha256=item.sha256,
                    size_bytes=item.size_bytes,
                    caption=item.caption,
                    managed_temp=item.managed_temp,
                )
            )
        except ValueError as exc:
            code = getattr(exc, "code", None) or "invalid_media_ref"
            raise AdapterContractError(str(code)) from None

    if not raw.text and not raw.context_text and not media:
        raise AdapterContractError("empty_event")
    return InboundEvent(
        event_id=opaque_ref(
            "event", f"whatsapp\x1f{profile_id}\x1f{raw_conversation_id}\x1f{raw_message_id}"
        ),
        source="whatsapp",
        conversation_id=opaque_ref(
            "conversation", f"{profile_id}\x1f{raw_conversation_id}"
        ),
        occurred_at=occurred_at,
        actor_ref=opaque_ref("actor", f"{profile_id}\x1f{raw_actor_id}"),
        source_profile_id=profile_id,
        privacy_scope=raw.privacy_scope,
        text=raw.text,
        context_text=raw.context_text,
        media=tuple(media),
        conversation_kind=raw.conversation_kind,
        actor_display_label=raw.actor_display_label,
    )
