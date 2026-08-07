"""Portable governance contracts for direct chats, groups, media and receipts.

This module contains no network code.  Runtime adapters must pass through
these validators before they are allowed to create a Telegram route or enable
an agent for a WhatsApp group.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import re
from typing import Mapping


CONVERSATION_KINDS = frozenset({"direct", "group"})
AGENT_MODES = frozenset({"none", "mention_only"})
IDENTITY_SOURCES = {
    "manual": 40,
    "session_contact": 30,
    "event": 20,
    "whatsapp_public": 10,
}
GRILL_FIELDS = (
    "agent_name",
    "mission",
    "audience",
    "authoritative_sources",
    "activation_triggers",
    "allowed_actions",
    "forbidden_actions",
    "approval_and_escalation",
    "tone_and_sla",
    "acceptance_examples",
)
_RAW_ID = re.compile(r"(?:\+?\d{7,}|@(?:g\.us|s\.whatsapp\.net|lid)\b)", re.I)


class GovernanceError(ValueError):
    """A fail-closed governance decision with a stable public code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class GroupGrill:
    agent_name: str
    mission: str
    audience: str
    authoritative_sources: str
    activation_triggers: str
    allowed_actions: str
    forbidden_actions: str
    approval_and_escalation: str
    tone_and_sla: str
    acceptance_examples: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "GroupGrill":
        unknown = set(value) - set(GRILL_FIELDS)
        if unknown:
            raise GovernanceError("group_grill_unknown_fields")
        data: dict[str, str] = {}
        for field in GRILL_FIELDS:
            item = value.get(field)
            if not isinstance(item, str) or not item.strip():
                raise GovernanceError(f"group_grill_{field}_required")
            data[field] = item.strip()
        return cls(**data)

    def as_dict(self) -> dict[str, str]:
        return {field: getattr(self, field) for field in GRILL_FIELDS}


@dataclass(frozen=True, slots=True)
class GroupAdmission:
    """Exact allowlist entry for one WhatsApp group and one Telegram topic."""

    conversation_id: str
    source_profile_id: str
    telegram_chat_id: str
    telegram_thread_id: str
    privacy_scope: str
    route_enabled: bool
    agent_mode: str = "none"
    grill: GroupGrill | None = None

    def __post_init__(self) -> None:
        if self.agent_mode not in AGENT_MODES:
            raise GovernanceError("group_agent_mode_invalid")
        if self.agent_mode != "none" and self.grill is None:
            raise GovernanceError("group_grill_required_for_agent")
        if not self.route_enabled:
            raise GovernanceError("group_route_disabled")
        if not self.telegram_chat_id.startswith("-"):
            raise GovernanceError("group_telegram_forum_required")
        if not self.telegram_thread_id.isdigit() or int(self.telegram_thread_id) <= 0:
            raise GovernanceError("group_telegram_topic_required")


@dataclass(frozen=True, slots=True)
class IdentityCandidate:
    label: str
    source: str

    def __post_init__(self) -> None:
        if self.source not in IDENTITY_SOURCES:
            raise GovernanceError("identity_source_invalid")
        validate_participant_label(self.label)


def validate_participant_label(label: str) -> str:
    """Return a human-readable label or reject raw WhatsApp identifiers."""

    if not isinstance(label, str) or not label.strip():
        raise GovernanceError("participant_identity_unresolved")
    normalized = " ".join(label.split())
    if len(normalized) > 120 or _RAW_ID.search(normalized):
        raise GovernanceError("participant_identity_unsafe")
    return normalized


def resolve_participant_identity(
    candidates: tuple[IdentityCandidate, ...],
) -> IdentityCandidate:
    if not candidates:
        raise GovernanceError("participant_identity_unresolved")
    return max(candidates, key=lambda item: IDENTITY_SOURCES[item.source])


def require_group_admission(
    *,
    conversation_kind: str,
    conversation_id: str,
    source_profile_id: str,
    admission: GroupAdmission | None,
) -> None:
    """Allow direct chats; require an exact, enabled entry for every group."""

    if conversation_kind not in CONVERSATION_KINDS:
        raise GovernanceError("conversation_kind_invalid")
    if conversation_kind == "direct":
        return
    if admission is None:
        raise GovernanceError("whatsapp_group_not_approved")
    if (
        admission.conversation_id != conversation_id
        or admission.source_profile_id != source_profile_id
    ):
        raise GovernanceError("whatsapp_group_admission_mismatch")


def render_group_prefix(label: str) -> str:
    return f"👤 {validate_participant_label(label)}\n\n"


class ReceiptState(IntEnum):
    SENT = 2
    DELIVERED = 3
    READ = 4
    PLAYED = 5


def receipt_state(value: object) -> ReceiptState:
    aliases = {
        "sent": ReceiptState.SENT,
        "device": ReceiptState.SENT,
        "delivered": ReceiptState.DELIVERED,
        "read": ReceiptState.READ,
        "played": ReceiptState.PLAYED,
    }
    if isinstance(value, ReceiptState):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            return ReceiptState(value)
        except ValueError:
            pass
    if isinstance(value, str) and value.strip().lower() in aliases:
        return aliases[value.strip().lower()]
    raise GovernanceError("receipt_state_invalid")


def advance_receipt(current: object | None, incoming: object) -> ReceiptState:
    """Advance monotonically; never infer or downgrade a WhatsApp receipt."""

    next_state = receipt_state(incoming)
    if current is None:
        return next_state
    return max(receipt_state(current), next_state)


@dataclass(frozen=True, slots=True)
class HumanCanary:
    direction: str
    media_kind: str
    exact_route: bool
    single_delivery: bool
    no_dm_fallback: bool
    integrity_ok: bool
    no_enrichment: bool
    human_confirmed: bool

    @property
    def passed(self) -> bool:
        return (
            self.direction in {"inbound", "outbound"}
            and self.media_kind in {"text", "image", "audio", "voice"}
            and all(
                (
                    self.exact_route,
                    self.single_delivery,
                    self.no_dm_fallback,
                    self.integrity_ok,
                    self.no_enrichment,
                    self.human_confirmed,
                )
            )
        )


def installation_state(canaries: tuple[HumanCanary, ...]) -> str:
    """Automatic tests can only prepare; real bidirectional canaries install."""

    required = {(direction, kind) for direction in ("inbound", "outbound") for kind in ("text", "image", "audio")}
    passed = {(item.direction, "audio" if item.media_kind == "voice" else item.media_kind) for item in canaries if item.passed}
    return "installed_success" if required <= passed else "prepared"
