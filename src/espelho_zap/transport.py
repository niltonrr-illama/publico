"""Transport contract plus deterministic test transports and safe media I/O."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import InboundEvent, MediaAttachment, Route


class TransportError(RuntimeError):
    """A sanitized transport failure; message contains only a stable code."""

    def __init__(
        self,
        code: str = "transport_error",
        *,
        retryable: bool = True,
        outcome_unknown: bool = False,
    ):
        self.code = code if code.replace("_", "").isalnum() else "transport_error"
        self.retryable = bool(retryable)
        self.outcome_unknown = bool(outcome_unknown)
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class SendResult:
    remote_ids: tuple[str, ...] = ()


@runtime_checkable
class Transport(Protocol):
    def send(
        self,
        event: InboundEvent,
        route: Route,
        *,
        idempotency_key: str,
    ) -> SendResult: ...


@dataclass(frozen=True, slots=True)
class RecordedSend:
    event: InboundEvent
    route: Route
    idempotency_key: str


class RecordingTransport:
    """In-memory transport for tests; repeated keys are accepted once."""

    def __init__(
        self,
        *,
        failures_before_success: int = 0,
        failure_code: str = "synthetic_transport_error",
        retryable: bool = True,
    ):
        self.failures_before_success = max(0, int(failures_before_success))
        self.failure_code = failure_code
        self.retryable = retryable
        self.calls = 0
        self.records: list[RecordedSend] = []
        self._accepted: dict[str, SendResult] = {}

    def send(
        self,
        event: InboundEvent,
        route: Route,
        *,
        idempotency_key: str,
    ) -> SendResult:
        if not idempotency_key:
            raise TransportError("idempotency_key_missing", retryable=False)
        self.calls += 1
        if idempotency_key in self._accepted:
            return self._accepted[idempotency_key]
        if self.calls <= self.failures_before_success:
            raise TransportError(self.failure_code, retryable=self.retryable)
        record = RecordedSend(event=event, route=route, idempotency_key=idempotency_key)
        self.records.append(record)
        result = SendResult((f"recorded-{len(self.records)}",))
        self._accepted[idempotency_key] = result
        return result


class DryRunTransport(RecordingTransport):
    """Explicit no-network transport for installation and routing canaries."""


def validate_media_file(
    media: MediaAttachment, *, max_bytes: int | None = None
) -> Path:
    candidate = Path(media.path)
    try:
        if candidate.is_symlink():
            raise TransportError("media_symlink_rejected", retryable=False)
        resolved = candidate.resolve(strict=True)
        if not resolved.is_file():
            raise TransportError("media_unavailable")
        stat = resolved.stat()
        if max_bytes is not None and stat.st_size > int(max_bytes):
            raise TransportError("media_too_large", retryable=False)
        if media.size_bytes and stat.st_size != media.size_bytes:
            raise TransportError("media_size_mismatch", retryable=False)
        if media.sha256:
            digest = hashlib.sha256()
            with resolved.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != media.sha256:
                raise TransportError("media_hash_mismatch", retryable=False)
        return resolved
    except TransportError:
        raise
    except OSError:
        raise TransportError("media_unavailable") from None


def remove_managed_media(media: MediaAttachment, allowed_root: str | Path | None) -> bool:
    """Delete only an opted-in regular file contained by the configured root."""
    if not media.managed_temp or allowed_root is None:
        return False
    candidate = Path(media.path)
    try:
        if candidate.is_symlink():
            return False
        root = Path(allowed_root).resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            return False
        candidate.unlink()
        return True
    except OSError:
        return False


def delivery_idempotency_key(event: InboundEvent, route: Route) -> str:
    value = "\x1f".join(
        ("espelho-zap-v1", event.event_id, event.payload_hash(), route.chat_id, route.thread_id)
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
