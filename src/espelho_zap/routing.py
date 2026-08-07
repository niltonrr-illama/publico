"""Fail-closed routing and media policy helpers."""

from __future__ import annotations

import re
from dataclasses import replace
from enum import Enum

from .models import InboundEvent, MediaAttachment, Route
from .policy import GovernanceError, render_group_prefix


_IMAGE_MARKER_RE = re.compile(r"(?im)^\s*(?:[-—]|â€”)?\s*\[Image\]\s*$")
_USER_TEXT_RE = re.compile(r"(?im)^\s*User text:\s*(?:\r?\n)?")
_DESCRIPTION_RE = re.compile(r"(?im)^\s*Description:\s*")
_WHATSAPP_ENVELOPE_RE = re.compile(
    r"^\s*\[WhatsApp[^\]\r\n]*\]\s+[^:\r\n]+:\s*", re.IGNORECASE
)
_MEDIA_PLACEHOLDER_RE = re.compile(
    r"^\s*<media:(?:image|audio|voice|video|document|file)>\s*$", re.IGNORECASE
)


class VideoPolicy(str, Enum):
    BLOCK = "block"
    ALLOW = "allow"


class PolicyError(RuntimeError):
    code = "policy_error"

    def __init__(self, code: str | None = None):
        self.code = code or self.code
        super().__init__(self.code)


class VideoBlockedError(PolicyError):
    code = "video_blocked"


def require_topic_route(route: Route | None) -> Route:
    """Reject absent, disabled, or non-topic routes without any DM fallback."""
    if route is None or not route.enabled:
        raise PolicyError("route_missing")
    # Route itself validates string identifiers and a strictly positive thread.
    if not isinstance(route.thread_id, str) or int(route.thread_id) <= 0:
        raise PolicyError("topic_required")
    return route


def enforce_video_policy(event: InboundEvent, policy: VideoPolicy) -> None:
    if any(item.kind == "video" for item in event.media) and policy is VideoPolicy.BLOCK:
        raise VideoBlockedError()


def sanitize_image_caption(text: str) -> str:
    """Remove only the known automatic image-analysis wrapper.

    A sender caption beginning with ``Description:`` is preserved unless the
    surrounding OpenClaw ``[Image]``/``User text:`` wrapper is present.
    """
    raw = text or ""
    user_text_match = _USER_TEXT_RE.search(raw)
    if not (_IMAGE_MARKER_RE.search(raw) or user_text_match):
        return raw
    description_match = _DESCRIPTION_RE.search(raw)
    if description_match:
        raw = raw[: description_match.start()]
    raw = _IMAGE_MARKER_RE.sub("", raw)
    user_text_match = _USER_TEXT_RE.search(raw)
    if user_text_match:
        raw = raw[user_text_match.end() :]
    raw = _WHATSAPP_ENVELOPE_RE.sub("", raw, count=1)
    return raw.strip()


def original_media_caption(event: InboundEvent, media: MediaAttachment) -> str:
    # Automatic speech-to-text may be useful internally later, but it is not
    # an original WhatsApp caption and must never leak into the mirror.
    prefix = ""
    if event.conversation_kind == "group":
        try:
            prefix = render_group_prefix(event.actor_display_label)
        except GovernanceError as exc:
            raise PolicyError(exc.code) from exc
    if media.kind in {"audio", "voice"}:
        return prefix + media.caption
    caption = media.caption if media.caption else event.text
    caption = sanitize_image_caption(caption) if media.kind == "image" else caption
    return prefix + caption


def telegram_text(event: InboundEvent) -> str:
    """Render only mirror-visible text; internal context is never returned."""

    if event.conversation_kind != "group":
        return event.text
    try:
        return render_group_prefix(event.actor_display_label) + event.text
    except GovernanceError as exc:
        raise PolicyError(exc.code) from exc


def sanitize_captured_event(event: InboundEvent) -> InboundEvent:
    """Remove runtime-derived wrappers before immutable persistence.

    Exact media placeholders and the known OpenClaw image-analysis wrapper are
    technical metadata, not sender-authored content.  Audio/voice event text is
    conservatively dropped because the native runtime does not prove whether
    it is an original caption or an automatic transcript; an explicitly
    supplied ``media.caption`` remains available to the transport.
    """

    text = event.text
    media = list(event.media)
    if media and _MEDIA_PLACEHOLDER_RE.fullmatch(text):
        text = ""
    if any(item.kind == "image" for item in media):
        text = sanitize_image_caption(text)
        media = [
            replace(item, caption=sanitize_image_caption(item.caption))
            if item.kind == "image"
            else item
            for item in media
        ]
    context_text = event.context_text
    if media and all(item.kind in {"audio", "voice"} for item in media):
        # The adapter may provide an internal transcript explicitly.  A
        # runtime-derived text field is moved into context rather than echoed.
        context_text = context_text or text
        text = ""
    return replace(event, text=text, context_text=context_text, media=tuple(media))
