"""Bounded loopback observer for the already-paired Hermes WhatsApp bridge.

The bridge owns the WhatsApp session and a durable HTTP spool.  This adapter
only reads ``GET /messages`` and acknowledges ``POST /ack`` after the portable
ledger commit succeeds.  It contains no WhatsApp sender or agent/LLM path.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
from typing import Any, Callable, Mapping
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from ..ledger import EventConflictError, LedgerError, MirrorLedger
from ..media import MediaSpoolError, stage_event_media
from ..models import canonical_whatsapp_event_ref, opaque_ref
from ..routing import sanitize_captured_event
from ..receipts import normalize_receipt
from .base import (
    AdapterCapabilities,
    AdapterContractError,
    RawInboundMessage,
    RawMediaRef,
    normalize_inbound,
    normalize_media_kind,
)


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
_MAX_MEDIA = 8
_ADAPTER_ID = "hermes-direct-bridge/v1"
_SYNTHETIC_MEDIA_TEXT = frozenset(
    {
        "[image received]",
        "[audio received]",
        "[ptt received]",
        "[voice received]",
        "[document received]",
        "[video received]",
        "[sticker]",
        "[sticker received]",
        "[gif received]",
    }
)
_PERMANENT_MEDIA_REJECTIONS = frozenset(
    {
        "source_media_missing",
        "source_media_unsafe",
        "source_media_outside_roots",
        "media_symlink_rejected",
        "media_path_outside_root",
        "media_not_file",
        "media_size_mismatch",
        "media_hash_mismatch",
        "media_spool_conflict",
    }
)


class HermesBridgeError(RuntimeError):
    """Sanitized local-bridge failure."""

    def __init__(self, code: str = "hermes_bridge_error"):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ObserverResult:
    fetched: int = 0
    selected: int = 0
    inserted: int = 0
    duplicates: int = 0
    enqueued: int = 0
    blocked_no_route: int = 0
    quarantined: int = 0
    malformed: int = 0
    media_failed: int = 0
    acked: int = 0
    ack_failed: int = 0
    source_media_cleaned: int = 0
    source_media_cleanup_failed: int = 0
    receipts: int = 0

    def public_summary(self) -> dict[str, int]:
        return {
            "fetched": self.fetched,
            "selected": self.selected,
            "inserted": self.inserted,
            "duplicates": self.duplicates,
            "enqueued": self.enqueued,
            "blocked_no_route": self.blocked_no_route,
            "quarantined": self.quarantined,
            "malformed": self.malformed,
            "media_failed": self.media_failed,
            "acked": self.acked,
            "ack_failed": self.ack_failed,
            "source_media_cleaned": self.source_media_cleaned,
            "source_media_cleanup_failed": self.source_media_cleanup_failed,
            "receipts": self.receipts,
        }


def _required_string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise AdapterContractError(f"invalid_{key}")
    return item


def _timestamp(value: object) -> str:
    if isinstance(value, bool):
        raise AdapterContractError("invalid_timestamp")
    if isinstance(value, (int, float)):
        seconds = float(value)
        if abs(seconds) >= 100_000_000_000:
            seconds /= 1000
        try:
            return (
                datetime.fromtimestamp(seconds, timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except (OverflowError, OSError, ValueError):
            raise AdapterContractError("invalid_timestamp") from None
    if isinstance(value, str) and value.strip():
        rendered = value.strip()
        try:
            parsed = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
        except ValueError:
            raise AdapterContractError("invalid_timestamp") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise AdapterContractError("invalid_timestamp")
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    raise AdapterContractError("invalid_timestamp")


def _media_kind(raw_kind: object, path: str) -> str:
    candidate = str(raw_kind or "").strip().lower()
    if candidate == "sticker":
        return "image"
    if candidate == "gif":
        return "video"
    if candidate:
        try:
            return normalize_media_kind(candidate)
        except AdapterContractError:
            pass
    suffix = Path(path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return "image"
    if suffix in {".ogg", ".opus"}:
        return "voice"
    if suffix in {".mp3", ".m4a", ".wav", ".aac"}:
        return "audio"
    if suffix in {".mp4", ".mov", ".webm", ".mkv"}:
        return "video"
    return "document"


def _original_caption(body: str, kind: str) -> str:
    if kind in {"audio", "voice"}:
        return ""
    if body.strip().lower() in _SYNTHETIC_MEDIA_TEXT:
        return ""
    if "could not be downloaded" in body.lower():
        return ""
    return body


def _first_label(value: Mapping[str, Any]) -> str:
    for key in ("senderName", "senderLabel", "pushName", "displayName"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return " ".join(candidate.split())[:120]
    return ""


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
        if getattr(left, "sha256") and getattr(left, "sha256") != getattr(
            right, "sha256"
        ):
            return False
    return True


class _NoRedirect(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        del req, fp, code, msg, headers, newurl
        raise HermesBridgeError("bridge_redirect_rejected")


class HermesBridgeObserver:
    """Persist one bounded bridge batch and ACK only durable source IDs."""

    ADAPTER_ID = "hermes-bridge-http-v1"

    def __init__(
        self,
        ledger: MirrorLedger,
        *,
        bridge_url: str,
        source_profile_id: str,
        spool_root: str | Path | None,
        source_media_roots: tuple[str | Path, ...],
        minimum_free_bytes: int,
        maximum_spool_bytes: int,
        privacy_scope: str = "owner_private",
        batch_limit: int = 100,
        timeout_seconds: float = 10.0,
        maximum_response_bytes: int = 8 * 1024 * 1024,
        opener: Callable[..., Any] | None = None,
    ):
        parsed = urllib_parse.urlsplit(bridge_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in _LOOPBACK_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise HermesBridgeError("loopback_bridge_required")
        try:
            port = parsed.port
        except ValueError:
            raise HermesBridgeError("loopback_bridge_required") from None
        if port is None or not 1 <= port <= 65535:
            raise HermesBridgeError("loopback_bridge_port_required")
        if not isinstance(source_profile_id, str) or not source_profile_id.strip():
            raise HermesBridgeError("source_profile_required")
        if privacy_scope not in {
            "area_shared",
            "partnership_restricted",
            "owner_private",
        }:
            raise HermesBridgeError("privacy_scope_required")
        if (
            isinstance(batch_limit, bool)
            or not isinstance(batch_limit, int)
            or not 1 <= batch_limit <= 500
        ):
            raise HermesBridgeError("bridge_batch_limit_invalid")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
            or isinstance(maximum_response_bytes, bool)
            or not isinstance(maximum_response_bytes, int)
            or maximum_response_bytes <= 0
        ):
            raise HermesBridgeError("bridge_limits_invalid")
        self.ledger = ledger
        self.bridge_url = bridge_url.rstrip("/")
        self._host_header = parsed.netloc
        self.source_profile_id = source_profile_id.strip()
        self.spool_root = spool_root
        try:
            self.source_media_roots = tuple(
                Path(item).expanduser().resolve(strict=True)
                for item in source_media_roots
            )
        except OSError:
            raise HermesBridgeError("source_media_root_invalid") from None
        if any(not root.is_dir() or root.is_symlink() for root in self.source_media_roots):
            raise HermesBridgeError("source_media_root_invalid")
        self.minimum_free_bytes = int(minimum_free_bytes)
        self.maximum_spool_bytes = int(maximum_spool_bytes)
        self.privacy_scope = privacy_scope
        self.batch_limit = int(batch_limit)
        self.timeout_seconds = float(timeout_seconds)
        self.maximum_response_bytes = int(maximum_response_bytes)
        self._opener = opener or urllib_request.build_opener(
            urllib_request.ProxyHandler({}), _NoRedirect()
        ).open

    @property
    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            adapter_id=self.ADAPTER_ID,
            platforms=("whatsapp",),
            capture_stage="paired_bridge_durable_spool",
            supports_media_refs=True,
            supports_partial_records=True,
            outbound_whatsapp=False,
        )

    def _open_json(self, request: urllib_request.Request) -> object:
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read(self.maximum_response_bytes + 1)
        except HermesBridgeError:
            raise
        except urllib_error.HTTPError as exc:
            raise HermesBridgeError(f"bridge_http_{int(exc.code)}") from None
        except (urllib_error.URLError, TimeoutError, OSError):
            raise HermesBridgeError("bridge_unavailable") from None
        if len(raw) > self.maximum_response_bytes:
            raise HermesBridgeError("bridge_response_too_large")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise HermesBridgeError("bridge_response_invalid") from None

    def _request(self, path: str, *, payload: Mapping[str, object] | None = None) -> object:
        data = None
        method = "GET"
        headers = {"Accept": "application/json", "Host": self._host_header}
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            method = "POST"
            headers["Content-Type"] = "application/json"
        request = urllib_request.Request(
            f"{self.bridge_url}{path}", data=data, headers=headers, method=method
        )
        return self._open_json(request)

    def _raw_event(self, value: Mapping[str, Any]):
        message_id = _required_string(value, "messageId")
        conversation_id = _required_string(value, "chatId")
        alternate_conversation = value.get("chatIdAlt", "")
        if alternate_conversation is None:
            alternate_conversation = ""
        if not isinstance(alternate_conversation, str):
            raise AdapterContractError("invalid_chatIdAlt")
        alternate_conversation = alternate_conversation.strip()
        actor_id = _required_string(value, "senderId")
        is_group = value.get("isGroup", conversation_id.endswith("@g.us"))
        if not isinstance(is_group, bool):
            raise AdapterContractError("invalid_isGroup")
        body = value.get("body", "")
        if not isinstance(body, str):
            raise AdapterContractError("invalid_body")
        raw_urls = value.get("mediaUrls", [])
        if raw_urls is None:
            raw_urls = []
        if not isinstance(raw_urls, list) or len(raw_urls) > _MAX_MEDIA:
            raise AdapterContractError("invalid_media_urls")
        if any(not isinstance(item, str) or not item.strip() for item in raw_urls):
            raise AdapterContractError("invalid_media_url")
        has_media = value.get("hasMedia", bool(raw_urls))
        if not isinstance(has_media, bool):
            raise AdapterContractError("invalid_has_media")
        if has_media and not raw_urls:
            raise MediaSpoolError("source_media_missing")
        raw_kind = value.get("mediaType", "")
        raw_mime = value.get("mime", "")
        if not isinstance(raw_kind, str):
            raise AdapterContractError("invalid_media_kind")
        if not isinstance(raw_mime, str):
            raise AdapterContractError("invalid_media_mime_type")
        mime_type = raw_mime.split(";", 1)[0].strip()
        media: list[RawMediaRef] = []
        for index, path in enumerate(raw_urls):
            kind = _media_kind(raw_kind, path)
            media.append(
                RawMediaRef(
                    raw_id=f"{message_id}:{index}",
                    kind=kind,
                    path=path,
                    mime_type=mime_type,
                    caption=_original_caption(body, kind),
                    managed_temp=False,
                )
            )
        if media and body.strip().lower() in _SYNTHETIC_MEDIA_TEXT:
            body = ""
        internal_transcript = value.get("internalTranscript", "")
        if internal_transcript is None:
            internal_transcript = ""
        if not isinstance(internal_transcript, str):
            raise AdapterContractError("invalid_internalTranscript")
        if media and all(item.kind in {"audio", "voice"} for item in media):
            internal_transcript = internal_transcript or body
        raw = RawInboundMessage(
            platform="whatsapp",
            direction="inbound",
            raw_message_id=message_id,
            raw_conversation_id=conversation_id,
            raw_actor_id=actor_id,
            occurred_at=_timestamp(value.get("timestamp")),
            privacy_scope=self.privacy_scope,
            source_profile_id=self.source_profile_id,
            text=body,
            context_text=internal_transcript,
            media=tuple(media),
            conversation_kind="group" if is_group else "direct",
            actor_display_label=_first_label(value),
        )
        event = normalize_inbound(raw)
        if event is None:
            raise AdapterContractError("inbound_event_required")
        primary_canonical = self.ledger.resolve_conversation_alias(
            event.conversation_id
        )
        event = replace(event, conversation_id=primary_canonical)
        if alternate_conversation and alternate_conversation != conversation_id:
            alternate_raw = replace(
                raw, raw_conversation_id=alternate_conversation
            )
            alternate_event = normalize_inbound(alternate_raw)
            if alternate_event is None:
                raise AdapterContractError("alternate_inbound_event_required")
            alternate_canonical = self.ledger.resolve_conversation_alias(
                alternate_event.conversation_id
            )
            alternate_event = replace(
                alternate_event, conversation_id=alternate_canonical
            )
            primary_route = self.ledger.get_route(primary_canonical)
            alternate_route = self.ledger.get_route(alternate_canonical)
            if primary_route is not None and alternate_route is not None:
                primary_destination = (
                    primary_route.chat_id,
                    primary_route.thread_id,
                    primary_route.enabled,
                )
                alternate_destination = (
                    alternate_route.chat_id,
                    alternate_route.thread_id,
                    alternate_route.enabled,
                )
                if primary_destination != alternate_destination:
                    raise AdapterContractError(
                        "conversation_identity_route_conflict"
                    )
            if primary_route is None and alternate_route is not None:
                event = alternate_event
        event = replace(
            event,
            privacy_scope=self.ledger.get_conversation_scope(
                event.conversation_id
            ),
        )
        stable_event_id = canonical_whatsapp_event_ref(
            event.source_profile_id,
            event.conversation_id,
            message_id,
        )
        stable_media = tuple(
            replace(
                item,
                media_id=opaque_ref(
                    "media", f"{stable_event_id}\x1f{index}"
                ),
            )
            for index, item in enumerate(event.media)
        )
        event = sanitize_captured_event(
            replace(event, event_id=stable_event_id, media=stable_media)
        )
        self.ledger.authorize_event(event)
        return event

    def _source_media_candidates(
        self, event: object, *, allow_missing: bool = False
    ) -> tuple[Path, ...]:
        candidates: list[Path] = []
        for media in tuple(getattr(event, "media")):
            path = Path(str(getattr(media, "path")))
            try:
                details = path.lstat()
            except FileNotFoundError:
                if allow_missing:
                    continue
                raise MediaSpoolError("source_media_missing") from None
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
                raise MediaSpoolError("source_media_unsafe")
            try:
                resolved = path.resolve(strict=True)
            except OSError:
                raise MediaSpoolError("source_media_unsafe") from None
            if not any(
                resolved == root or resolved.is_relative_to(root)
                for root in self.source_media_roots
            ):
                raise MediaSpoolError("source_media_outside_roots")
            candidates.append(resolved)
        return tuple(dict.fromkeys(candidates))

    def _cleanup_source_media(self, paths: tuple[Path, ...]) -> tuple[int, int]:
        cleaned = 0
        failed = 0
        for path in paths:
            try:
                details = path.lstat()
                if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
                    raise OSError("source media changed type")
                resolved = path.resolve(strict=True)
                if not any(
                    resolved == root or resolved.is_relative_to(root)
                    for root in self.source_media_roots
                ):
                    raise OSError("source media escaped approved root")
                if resolved != path:
                    raise OSError("source media path changed")
                path.unlink()
                if os.name == "posix":
                    directory_fd = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                cleaned += 1
            except FileNotFoundError:
                continue
            except OSError:
                failed += 1
        return cleaned, failed

    def _persist(
        self, value: Mapping[str, Any]
    ) -> tuple[str, bool, bool, bool, tuple[Path, ...]]:
        message_id = _required_string(value, "messageId")
        event = self._raw_event(value)
        try:
            existing = self.ledger.load_event(event.event_id)
        except LedgerError as exc:
            if exc.code != "event_missing":
                raise
            existing = None
        source_media = self._source_media_candidates(
            event, allow_missing=existing is not None
        )
        if existing is not None:
            if not _replay_compatible(event, existing):
                raise EventConflictError()
            block = self.ledger.connection.execute(
                """SELECT 1 FROM mirror_route_blocks
                   WHERE event_id = ? AND state = 'blocked_no_route'""",
                (event.event_id,),
            ).fetchone()
            return (
                message_id,
                False,
                self.ledger.delivery_state(event.event_id) is not None,
                bool(block),
                source_media,
            )

        created_media: tuple[Path, ...] = ()
        self.ledger.connection.execute("BEGIN IMMEDIATE")
        try:
            event, created_media = stage_event_media(
                event,
                spool_root=self.spool_root,
                source_roots=self.source_media_roots,
                minimum_free_bytes=self.minimum_free_bytes,
                maximum_spool_bytes=self.maximum_spool_bytes,
            )
            inserted, delivery_id, blocked_reason = self.ledger.capture_event(event)
            self.ledger.connection.execute("COMMIT")
        except BaseException:
            if self.ledger.connection.in_transaction:
                self.ledger.connection.execute("ROLLBACK")
            for path in created_media:
                path.unlink(missing_ok=True)
            raise
        return (
            message_id,
            inserted,
            delivery_id is not None,
            blocked_reason is not None,
            source_media,
        )

    def _persist_receipt(self, value: Mapping[str, Any]) -> str:
        """Commit one provider receipt and return its bridge spool id.

        Receipt envelopes are deliberately kept out of the inbound event
        ledger: they update the outbound receipt projection only.  The bridge
        item is ACKed only after that projection commit succeeds.
        """

        receipt = normalize_receipt(value)
        if receipt is None:
            raise AdapterContractError("outbound_receipt_required")
        self.ledger.record_outbound_receipt(
            receipt.outbound_ref,
            receipt.state,
            provider_event=receipt.provider_event,
        )
        return receipt.event_id

    def _quarantine_permanent_rejection(
        self,
        value: Mapping[str, Any],
        *,
        error_code: str,
    ) -> str | None:
        message_id = value.get("messageId")
        if not isinstance(message_id, str) or not message_id.strip():
            return None
        self.ledger.quarantine_source_item(
            adapter_id=_ADAPTER_ID,
            source_item_id=message_id,
            payload=value,
            error_code=error_code,
            privacy_scope=self.privacy_scope,
        )
        return message_id

    def observe_once(self) -> ObserverResult:
        response = self._request(f"/messages?limit={self.batch_limit}")
        if not isinstance(response, list):
            raise HermesBridgeError("bridge_messages_invalid")
        selected = response[: self.batch_limit]
        counts = {
            "fetched": len(response),
            "selected": len(selected),
            "inserted": 0,
            "duplicates": 0,
            "enqueued": 0,
            "blocked_no_route": 0,
            "quarantined": 0,
            "malformed": 0,
            "media_failed": 0,
            "acked": 0,
            "ack_failed": 0,
            "source_media_cleaned": 0,
            "source_media_cleanup_failed": 0,
            "receipts": 0,
        }
        durable_ids: list[str] = []
        source_media_by_id: dict[str, tuple[Path, ...]] = {}
        for value in selected:
            if not isinstance(value, Mapping):
                counts["malformed"] += 1
                continue
            native_type = value.get("nativeType")
            native_metadata = value.get("nativeMetadata")
            is_receipt = native_type in {"outbound_receipt", "receipt"} or (
                isinstance(native_metadata, Mapping)
                and isinstance(native_metadata.get("receipt"), Mapping)
            )
            if is_receipt:
                try:
                    receipt_id = self._persist_receipt(value)
                except (AdapterContractError, LedgerError, ValueError):
                    # Do not quarantine or ACK an invalid receipt: retaining it
                    # in the bridge spool is safer than losing a state update.
                    counts["malformed"] += 1
                    continue
                durable_ids.append(receipt_id)
                source_media_by_id[receipt_id] = ()
                counts["receipts"] += 1
                continue
            try:
                message_id, inserted, enqueued, blocked, source_media = self._persist(value)
            except MediaSpoolError as exc:
                counts["media_failed"] += 1
                if exc.code in _PERMANENT_MEDIA_REJECTIONS:
                    try:
                        message_id = self._quarantine_permanent_rejection(
                            value, error_code=exc.code
                        )
                    except LedgerError:
                        message_id = None
                    if message_id is not None:
                        durable_ids.append(message_id)
                        source_media_by_id[message_id] = ()
                        counts["quarantined"] += 1
                continue
            except (AdapterContractError, EventConflictError, ValueError) as exc:
                counts["malformed"] += 1
                code = str(getattr(exc, "code", "invalid_source_record"))
                if not code or not code.replace("_", "").isalnum():
                    code = "invalid_source_record"
                try:
                    message_id = self._quarantine_permanent_rejection(
                        value, error_code=code
                    )
                except LedgerError:
                    message_id = None
                if message_id is not None:
                    durable_ids.append(message_id)
                    source_media_by_id[message_id] = ()
                    counts["quarantined"] += 1
                continue
            except LedgerError:
                counts["malformed"] += 1
                continue
            durable_ids.append(message_id)
            source_media_by_id[message_id] = source_media
            counts["inserted" if inserted else "duplicates"] += 1
            counts["enqueued"] += int(enqueued)
            counts["blocked_no_route"] += int(blocked)

        unique_ids = list(dict.fromkeys(durable_ids))
        if unique_ids:
            try:
                ack = self._request("/ack", payload={"messageIds": unique_ids})
                if (
                    not isinstance(ack, Mapping)
                    or ack.get("ok") is not True
                    or isinstance(ack.get("acked"), bool)
                    or int(ack.get("acked", -1)) != len(unique_ids)
                ):
                    raise HermesBridgeError("bridge_ack_invalid")
                counts["acked"] = len(unique_ids)
                cleanup_paths = tuple(
                    dict.fromkeys(
                        path
                        for message_id in unique_ids
                        for path in source_media_by_id.get(message_id, ())
                    )
                )
                cleaned, cleanup_failed = self._cleanup_source_media(cleanup_paths)
                counts["source_media_cleaned"] = cleaned
                counts["source_media_cleanup_failed"] = cleanup_failed
            except (HermesBridgeError, TypeError, ValueError):
                counts["ack_failed"] = len(unique_ids)
        return ObserverResult(**counts)
