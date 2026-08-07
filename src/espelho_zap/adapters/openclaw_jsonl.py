"""Incremental read-only adapter for classic OpenClaw session JSONL."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from ..ledger import MirrorLedger, RouteMissingError
from ..models import opaque_ref
from .base import (
    AdapterCapabilities,
    AdapterContractError,
    RawInboundMessage,
    RawMediaRef,
    normalize_inbound,
    normalize_media_kind,
)


class OpenClawJSONLAdapter:
    """Tail complete JSONL records without ever modifying the source file.

    ``confirmed_platform`` is an operator capability gate for old JSONL rows
    that predate ``sourceChannel``.  It must be exactly ``whatsapp``; an
    explicit non-WhatsApp channel in a row is still ignored.
    """

    ADAPTER_ID = "openclaw-jsonl-v1"

    def __init__(
        self,
        ledger: MirrorLedger,
        *,
        allowed_session_root: str | Path,
        allowed_media_roots: tuple[str | Path, ...] = (),
        confirmed_platform: str,
    ):
        if str(confirmed_platform).lower() != "whatsapp":
            raise AdapterContractError("whatsapp_capability_required")
        self.ledger = ledger
        self.allowed_session_root = Path(allowed_session_root).resolve()
        self.allowed_media_roots = tuple(Path(item).resolve() for item in allowed_media_roots)

    @property
    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            adapter_id=self.ADAPTER_ID,
            platforms=("whatsapp",),
            capture_stage="legacy_session_tail",
            supports_media_refs=True,
            supports_partial_records=True,
        )

    @staticmethod
    def _contained(path: Path, roots: tuple[Path, ...]) -> bool:
        return any(path == root or root in path.parents for root in roots)

    def _session_path(self, value: str | Path) -> Path:
        path = Path(value).resolve()
        if path != self.allowed_session_root and self.allowed_session_root not in path.parents:
            raise AdapterContractError("session_path_outside_root")
        if any(marker in path.name for marker in (".trajectory.", ".checkpoint.", ".reset.")):
            raise AdapterContractError("unsupported_session_artifact")
        return path

    @staticmethod
    def _generation(path: Path) -> str:
        stat = path.stat()
        first_complete = b""
        with path.open("rb") as handle:
            candidate = handle.readline(64 * 1024 + 1)
            if len(candidate) <= 64 * 1024 and candidate.endswith(b"\n"):
                first_complete = candidate
        identity = (
            f"{int(stat.st_dev)}:{int(stat.st_ino)}:"
            f"{hashlib.sha256(first_complete).hexdigest()}"
        )
        return hashlib.sha256(identity.encode("ascii")).hexdigest()

    @staticmethod
    def _extract_text(content: object) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                value = item.get("text")
                if not isinstance(value, str):
                    value = item.get("transcript")
                if isinstance(value, str):
                    parts.append(value)
        return "\n".join(parts)

    @staticmethod
    def _kind(value: object, path: str) -> str:
        candidate = str(value or "").strip().lower()
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
        if suffix in {".mp4", ".mov", ".webm"}:
            return "video"
        return "document"

    def _media_refs(self, message: Mapping[str, Any]) -> tuple[RawMediaRef, ...]:
        candidates: list[tuple[str, object, str, str, int]] = []
        paths = message.get("MediaPaths")
        if not isinstance(paths, list):
            single = message.get("MediaPath")
            paths = [single] if isinstance(single, str) and single else []
        kinds = message.get("MediaTypes")
        if not isinstance(kinds, list):
            kinds = []
        for index, raw_path in enumerate(paths):
            if isinstance(raw_path, str) and raw_path:
                raw_kind = kinds[index] if index < len(kinds) else message.get("MediaType")
                candidates.append((raw_path, raw_kind, "", "", 0))
        content = message.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, Mapping):
                    continue
                raw_path = item.get("path")
                if not isinstance(raw_path, str) or not raw_path:
                    continue
                raw_size = item.get("size_bytes") or item.get("size") or 0
                size = raw_size if isinstance(raw_size, int) and not isinstance(raw_size, bool) else 0
                candidates.append(
                    (
                        raw_path,
                        item.get("kind") or item.get("type"),
                        str(item.get("contentType") or item.get("mime_type") or ""),
                        str(item.get("caption") or ""),
                        size,
                    )
                )
        result: list[RawMediaRef] = []
        seen: set[str] = set()
        for raw_path, raw_kind, mime, caption, size in candidates:
            path = Path(raw_path).expanduser().resolve()
            if not self.allowed_media_roots or not self._contained(path, self.allowed_media_roots):
                raise AdapterContractError("media_path_outside_root")
            resolved_key = str(path)
            if resolved_key in seen:
                continue
            seen.add(resolved_key)
            result.append(
                RawMediaRef(
                    raw_id=raw_path,
                    kind=self._kind(raw_kind, raw_path),
                    path=str(path),
                    mime_type=mime,
                    size_bytes=size,
                    caption=caption,
                    managed_temp=False,
                )
            )
        return tuple(result)

    def _normalize_row(
        self,
        row: Mapping[str, Any],
        *,
        raw_conversation_id: str,
        privacy_scope: str,
        source_profile_id: str,
    ):
        message = row.get("message")
        if row.get("type") != "message" or not isinstance(message, Mapping):
            return None
        role = str(message.get("role") or "").lower()
        channel = str(message.get("sourceChannel") or "").lower()
        direction = str(message.get("direction") or "inbound").lower()
        if role != "user" or (channel and channel != "whatsapp") or direction != "inbound":
            return None
        occurred_at = str(
            row.get("timestamp") or row.get("ts") or message.get("timestamp") or ""
        )
        actor = str(message.get("senderId") or message.get("senderE164") or "")
        text = self._extract_text(message.get("content"))
        media = self._media_refs(message)
        if not text and not media:
            return None
        raw_message_id = str(row.get("id") or message.get("messageId") or "")
        if not raw_message_id:
            identity = json.dumps(
                {
                    "occurred_at": occurred_at,
                    "actor": actor,
                    "text": text,
                    "media": [item.raw_id for item in media],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            raw_message_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return normalize_inbound(
            RawInboundMessage(
                platform="whatsapp",
                direction="inbound",
                raw_message_id=raw_message_id,
                raw_conversation_id=raw_conversation_id,
                raw_actor_id=actor,
                occurred_at=occurred_at,
                privacy_scope=privacy_scope,
                source_profile_id=source_profile_id,
                text=text,
                media=media,
            )
        )

    def ingest_file(
        self,
        path: str | Path,
        *,
        source_ref: str,
        raw_conversation_id: str,
        privacy_scope: str,
        source_profile_id: str = "default",
    ) -> dict[str, int]:
        session_path = self._session_path(path)
        opaque_source_ref = opaque_ref("source", source_ref)
        generation = self._generation(session_path)
        cursor = self.ledger.get_source_cursor(self.ADAPTER_ID, opaque_source_ref)
        position = cursor[1] if cursor and cursor[0] == generation else 0
        size = session_path.stat().st_size
        if position > size:
            generation = hashlib.sha256(
                f"{generation}:truncated:{session_path.stat().st_mtime_ns}:{size}".encode("ascii")
            ).hexdigest()
            position = 0
        stats = {
            "inserted": 0,
            "duplicates": 0,
            "ignored": 0,
            "blocked_no_route": 0,
            "malformed": 0,
        }
        committed_position = position
        with session_path.open("rb") as handle:
            handle.seek(position)
            while True:
                line_start = handle.tell()
                raw = handle.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    handle.seek(line_start)
                    break
                line_end = handle.tell()
                try:
                    row = json.loads(raw.decode("utf-8"))
                    if not isinstance(row, Mapping):
                        stats["ignored"] += 1
                        committed_position = line_end
                        continue
                    event = self._normalize_row(
                        row,
                        raw_conversation_id=raw_conversation_id,
                        privacy_scope=privacy_scope,
                        source_profile_id=source_profile_id,
                    )
                    if event is None:
                        stats["ignored"] += 1
                    else:
                        inserted = self.ledger.record_event(event)
                        stats["inserted" if inserted else "duplicates"] += 1
                        try:
                            self.ledger.enqueue(event.event_id)
                        except RouteMissingError:
                            stats["blocked_no_route"] += 1
                except (UnicodeError, json.JSONDecodeError, AdapterContractError, ValueError):
                    stats["malformed"] += 1
                    break
                committed_position = line_end
        self.ledger.set_source_cursor(
            self.ADAPTER_ID,
            opaque_source_ref,
            generation,
            committed_position,
        )
        return stats
