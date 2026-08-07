"""Minimal Telegram Bot API transport using only the Python standard library."""

from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, Iterator

from .models import InboundEvent, Route
from .routing import original_media_caption, require_topic_route, telegram_text
from .transport import SendResult, TransportError, validate_media_file


class _MultipartStream:
    """Re-iterable multipart body that never loads media files into RAM."""

    def __init__(self, *parts: bytes | Path, chunk_size: int = 64 * 1024):
        self.parts = parts
        self.chunk_size = chunk_size
        self.content_length = sum(
            len(part) if isinstance(part, bytes) else part.stat().st_size
            for part in parts
        )

    def __iter__(self) -> Iterator[bytes]:
        for part in self.parts:
            if isinstance(part, bytes):
                if part:
                    yield part
                continue
            with part.open("rb") as handle:
                for block in iter(lambda: handle.read(self.chunk_size), b""):
                    yield block


def _split_utf16_prefix(value: str, max_units: int) -> tuple[str, str]:
    """Split greedily without cutting a non-BMP code point in half."""
    units = 0
    for index, char in enumerate(value):
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > max_units:
            if index == 0:
                raise TransportError("telegram_utf16_limit_too_small", retryable=False)
            return value[:index], value[index:]
        units += width
    return value, ""


def _utf16_chunks(value: str, max_units: int) -> tuple[str, ...]:
    chunks: list[str] = []
    remainder = value
    while remainder:
        chunk, remainder = _split_utf16_prefix(remainder, max_units)
        chunks.append(chunk)
    return tuple(chunks)


class TelegramBotTransport:
    """Send one inbound message to its exact Telegram forum topic."""

    def __init__(
        self,
        token: str,
        api_base: str = "https://api.telegram.org",
        timeout: float = 30.0,
        *,
        opener: Callable[..., Any] | None = None,
        max_media_bytes: int = 50 * 1024 * 1024,
        max_caption_chars: int = 1024,
        max_text_chars: int = 4096,
    ):
        if not isinstance(token, str) or not token.strip():
            raise ValueError("telegram_token_missing")
        if not isinstance(api_base, str) or not api_base.startswith(("https://", "http://")):
            raise ValueError("telegram_api_base_invalid")
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._timeout = float(timeout)
        self._opener = opener or urllib.request.urlopen
        self._max_media_bytes = int(max_media_bytes)
        self._max_caption_chars = int(max_caption_chars)
        self._max_text_chars = int(max_text_chars)
        if min(self._max_media_bytes, self._max_caption_chars, self._max_text_chars) <= 0:
            raise ValueError("telegram_limits_invalid")

    def send(
        self,
        event: InboundEvent,
        route: Route,
        *,
        idempotency_key: str,
    ) -> SendResult:
        require_topic_route(route)
        if not idempotency_key:
            raise TransportError("idempotency_key_missing", retryable=False)
        if len(event.media) > 1:
            return self._send_media_group(event, route)
        if not event.media:
            chunks = _utf16_chunks(telegram_text(event), self._max_text_chars)
            return SendResult(self._send_text_chunks(chunks, route))

        media = event.media[0]
        media_path = validate_media_file(media, max_bytes=self._max_media_bytes)
        method, file_field = {
            "image": ("sendPhoto", "photo"),
            "audio": ("sendAudio", "audio"),
            "voice": ("sendVoice", "voice"),
            "video": ("sendVideo", "video"),
            "document": ("sendDocument", "document"),
        }[media.kind]
        caption = original_media_caption(event, media)
        first_caption, caption_remainder = _split_utf16_prefix(
            caption, self._max_caption_chars
        )
        continuation_chunks = _utf16_chunks(
            caption_remainder, self._max_text_chars
        )
        fields = {
            "chat_id": route.chat_id,
            "message_thread_id": route.thread_id,
            "caption": first_caption,
        }
        result = self._post_multipart(
            method, fields, ((file_field, media_path, media.mime_type),)
        )
        remote_ids = [self._message_id(result)]
        remote_ids.extend(self._send_text_chunks(continuation_chunks, route, remote_ids))
        return SendResult(tuple(remote_ids))

    def _send_media_group(self, event: InboundEvent, route: Route) -> SendResult:
        if not 2 <= len(event.media) <= 10:
            raise TransportError("telegram_media_group_size_invalid", retryable=False)
        media_types = {
            "image": "photo",
            "audio": "audio",
            "video": "video",
            "document": "document",
        }
        if any(media.kind not in media_types for media in event.media):
            raise TransportError("telegram_media_group_kind_unsupported", retryable=False)
        kinds = {media_types[media.kind] for media in event.media}
        if ("audio" in kinds and kinds != {"audio"}) or (
            "document" in kinds and kinds != {"document"}
        ):
            raise TransportError("telegram_media_group_mix_invalid", retryable=False)

        descriptors: list[dict[str, str]] = []
        files: list[tuple[str, Path, str]] = []
        continuations: list[str] = []
        for index, media in enumerate(event.media):
            path = validate_media_file(media, max_bytes=self._max_media_bytes)
            caption = (
                original_media_caption(event, media)
                if media.caption or index == 0
                else ""
            )
            first_caption, remainder = _split_utf16_prefix(
                caption, self._max_caption_chars
            )
            descriptor = {
                "type": media_types[media.kind],
                "media": f"attach://file{index}",
                "caption": first_caption,
            }
            descriptors.append(descriptor)
            files.append((f"file{index}", path, media.mime_type))
            continuations.extend(_utf16_chunks(remainder, self._max_text_chars))

        fields = {
            "chat_id": route.chat_id,
            "message_thread_id": route.thread_id,
            "media": json.dumps(
                descriptors, ensure_ascii=False, separators=(",", ":")
            ),
        }
        result = self._post_multipart("sendMediaGroup", fields, tuple(files))
        remote_ids = list(self._message_ids(result, expected=len(event.media)))
        remote_ids.extend(self._send_text_chunks(tuple(continuations), route, remote_ids))
        return SendResult(tuple(remote_ids))

    def _send_text_chunks(
        self,
        chunks: tuple[str, ...],
        route: Route,
        prior_remote_ids: list[str] | None = None,
    ) -> tuple[str, ...]:
        remote_ids: list[str] = []
        already_sent = bool(prior_remote_ids)
        for chunk in chunks:
            try:
                result = self._post_form(
                    "sendMessage",
                    {
                        "chat_id": route.chat_id,
                        "message_thread_id": route.thread_id,
                        "text": chunk,
                    },
                )
                remote_ids.append(self._message_id(result))
                already_sent = True
            except TransportError as exc:
                if already_sent and not exc.outcome_unknown:
                    raise TransportError(
                        exc.code,
                        retryable=exc.retryable,
                        outcome_unknown=True,
                    ) from None
                raise
        return tuple(remote_ids)

    def _url(self, method: str) -> str:
        return f"{self._api_base}/bot{self._token}/{method}"

    def create_forum_topic(self, chat_id: str, name: str) -> str:
        """Create one topic in an existing forum supergroup.

        Telegram does not expose bot-side group creation. The operator creates
        the forum supergroup and grants ``can_manage_topics`` first; this
        method performs only the explicit topic mutation.
        """

        if not isinstance(chat_id, str) or not chat_id.startswith("-"):
            raise ValueError("telegram_group_chat_required")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 128:
            raise ValueError("telegram_topic_name_invalid")
        result = self._post_form(
            "createForumTopic", {"chat_id": chat_id, "name": name.strip()}
        )
        if not isinstance(result, dict):
            raise TransportError("telegram_topic_response_invalid", retryable=False)
        thread_id = result.get("message_thread_id")
        if isinstance(thread_id, bool) or not isinstance(thread_id, int) or thread_id <= 0:
            raise TransportError("telegram_topic_response_invalid", retryable=False)
        return str(thread_id)

    def delete_forum_topic(self, chat_id: str, thread_id: str) -> None:
        """Compensate a failed local route commit after topic creation."""

        if not isinstance(chat_id, str) or not chat_id.startswith("-"):
            raise ValueError("telegram_group_chat_required")
        if not isinstance(thread_id, str) or not thread_id.isdigit() or int(thread_id) <= 0:
            raise ValueError("telegram_topic_required")
        result = self._post_form(
            "deleteForumTopic",
            {"chat_id": chat_id, "message_thread_id": thread_id},
        )
        if result is not True:
            raise TransportError("telegram_topic_delete_rejected", retryable=False)

    def set_message_reaction(
        self, chat_id: str, message_id: str, emoji: str
    ) -> None:
        """Set the bot's single reaction on an existing Telegram message.

        Receipt presentation deliberately uses a reaction instead of a bot
        reply or message edit: the operator's original outbound text remains
        untouched and the topic is not polluted with status messages.
        """

        if not isinstance(chat_id, str) or not chat_id.startswith("-"):
            raise ValueError("telegram_group_chat_required")
        if (
            not isinstance(message_id, str)
            or not message_id.isdigit()
            or int(message_id) <= 0
        ):
            raise ValueError("telegram_message_id_invalid")
        if not isinstance(emoji, str) or not emoji.strip():
            raise ValueError("telegram_reaction_invalid")
        result = self._post_form(
            "setMessageReaction",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reaction": json.dumps(
                    [{"type": "emoji", "emoji": emoji}],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        )
        if result is not True:
            raise TransportError("telegram_reaction_rejected", retryable=False)

    def _open(
        self, request: urllib.request.Request
    ) -> dict[str, Any] | list[Any] | bool:
        try:
            with self._opener(request, timeout=self._timeout) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise TransportError(
                    "telegram_response_too_large", outcome_unknown=True
                )
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise TransportError(
                    "telegram_response_invalid", outcome_unknown=True
                )
            api_ok = value.get("ok")
            if api_ok is False:
                raise TransportError("telegram_api_rejected")
            if api_ok is not True:
                raise TransportError(
                    "telegram_response_invalid", outcome_unknown=True
                )
            result = value.get("result")
            if not isinstance(result, (dict, list, bool)):
                raise TransportError(
                    "telegram_response_invalid", outcome_unknown=True
                )
            return result
        except TransportError:
            raise
        except urllib.error.HTTPError as exc:
            status = int(getattr(exc, "code", 0) or 0)
            code = f"telegram_http_{status}" if 100 <= status <= 599 else "telegram_http_error"
            retryable = status == 429
            raise TransportError(
                code,
                retryable=retryable,
                outcome_unknown=status >= 500,
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise TransportError("telegram_network_error", outcome_unknown=True) from None
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            raise TransportError(
                "telegram_response_invalid", outcome_unknown=True
            ) from None

    def _post_form(
        self, method: str, fields: dict[str, str]
    ) -> dict[str, Any] | list[Any] | bool:
        data = urllib.parse.urlencode(fields).encode("utf-8")
        request = urllib.request.Request(
            self._url(method),
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        return self._open(request)

    def _post_multipart(
        self,
        method: str,
        fields: dict[str, str],
        files: tuple[tuple[str, Path, str], ...],
    ) -> dict[str, Any] | list[Any]:
        boundary = f"espelhozap{secrets.token_hex(12)}"
        parts: list[bytes | Path] = []
        for name, value in fields.items():
            parts.extend(
                [
                    f"--{boundary}\r\n".encode("ascii"),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"),
                    value.encode("utf-8"),
                    b"\r\n",
                ]
            )
        for index, (file_field, path, mime_type) in enumerate(files):
            safe_filename = f"attachment{index}{path.suffix.lower()}"
            parts.extend(
                [
                    f"--{boundary}\r\n".encode("ascii"),
                    (
                        f'Content-Disposition: form-data; name="{file_field}"; '
                        f'filename="{safe_filename}"\r\n'
                    ).encode("ascii"),
                    f"Content-Type: {mime_type or 'application/octet-stream'}\r\n\r\n".encode(
                        "ascii"
                    ),
                    path,
                    b"\r\n",
                ]
            )
        parts.append(f"--{boundary}--\r\n".encode("ascii"))
        try:
            body = _MultipartStream(*parts)
        except OSError:
            raise TransportError("media_unavailable") from None
        request = urllib.request.Request(
            self._url(method),
            data=body,  # type: ignore[arg-type]  # urllib accepts iterable byte bodies
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(body.content_length),
            },
            method="POST",
        )
        return self._open(request)

    @staticmethod
    def _message_id(result: object) -> str:
        if not isinstance(result, dict):
            raise TransportError("telegram_response_invalid", outcome_unknown=True)
        message_id = result.get("message_id")
        if isinstance(message_id, bool) or not isinstance(message_id, (int, str)):
            raise TransportError("telegram_message_id_missing", outcome_unknown=True)
        return str(message_id)

    @classmethod
    def _message_ids(cls, result: object, *, expected: int) -> tuple[str, ...]:
        if not isinstance(result, list) or len(result) != expected:
            raise TransportError("telegram_response_invalid", outcome_unknown=True)
        try:
            return tuple(cls._message_id(item) for item in result)
        except TransportError as exc:
            raise TransportError(exc.code, outcome_unknown=True) from None
