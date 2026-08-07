"""Provider receipt normalization for the portable WhatsApp mirror.

The WhatsApp bridge may expose receipts through more than one Baileys event.
This module deliberately accepts only the small, provider-backed shape needed
by the ledger.  It never infers delivery from a successful HTTP response and it
never turns a receipt into an inbound message.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .policy import ReceiptState, receipt_state


_EVENTS = frozenset({"messages.update", "message-receipt.update"})


@dataclass(frozen=True, slots=True)
class OutboundReceipt:
    """One normalized receipt for a previously sent WhatsApp message."""

    event_id: str
    outbound_ref: str
    state: ReceiptState
    provider_event: str


def _text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value).strip()
    return ""


def _state(value: object) -> ReceiptState | None:
    if value is None:
        return None
    try:
        return receipt_state(value)
    except Exception:
        return None


def _first(mapping: Mapping[str, Any], *names: str) -> object:
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return None


def normalize_receipt(value: object) -> OutboundReceipt | None:
    """Normalize a bridge receipt event, or return ``None`` for non-receipts.

    Accepted bridge shape::

        {
          "messageId": "receipt:...",
          "nativeType": "outbound_receipt",
          "nativeMetadata": {
            "receipt": {
              "outboundMessageId": "...",
              "state": 3,
              "providerEvent": "message-receipt.update"
            }
          }
        }

    The parser also accepts the equivalent top-level fields so a future
    adapter can pass the event without wrapping it.  Missing or malformed
    values are rejected rather than guessed.
    """

    if not isinstance(value, Mapping):
        return None
    native_type = _text(value.get("nativeType"))
    metadata = value.get("nativeMetadata")
    receipt = metadata.get("receipt") if isinstance(metadata, Mapping) else None
    if native_type not in {"outbound_receipt", "receipt"} and not isinstance(receipt, Mapping):
        return None
    if not isinstance(receipt, Mapping):
        receipt = value
    outbound_ref = _text(
        _first(receipt, "outboundMessageId", "messageId", "remoteMessageId", "outbound_ref")
    )
    event_id = _text(_first(value, "messageId", "eventId", "event_id"))
    provider_event = _text(
        _first(receipt, "providerEvent", "provider_event", "sourcePath", "eventType")
    )
    state = _state(_first(receipt, "state", "status", "receipt"))
    if not event_id or not outbound_ref or not provider_event or provider_event not in _EVENTS or state is None:
        raise ValueError("outbound_receipt_invalid")
    return OutboundReceipt(event_id, outbound_ref, state, provider_event)


__all__ = ["OutboundReceipt", "normalize_receipt"]
