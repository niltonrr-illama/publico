from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from espelho_zap import (  # noqa: E402
    InboundEvent,
    MediaAttachment,
    MirrorLedger,
    MirrorWorker,
    RecordingTransport,
    Route,
    RuntimeLockError,
    TelegramBotTransport,
    TransportError,
    VideoPolicy,
    canonical_whatsapp_event_ref,
    import_legacy_runtime_config,
    legacy_conversation_id,
    opaque_ref,
    sanitize_image_caption,
)
from espelho_zap.transport import SendResult, remove_managed_media  # noqa: E402
from espelho_zap.adapters import RawInboundMessage, normalize_inbound  # noqa: E402
from espelho_zap.legacy import LegacyImportError  # noqa: E402


CONVERSATION = opaque_ref("conversation", "synthetic-conversation-a")
ACTOR = opaque_ref("actor", "synthetic-actor-a")


def inbound(
    event_id: str = "evt-1",
    *,
    text: str = "texto original",
    media: tuple[MediaAttachment, ...] = (),
    conversation_id: str = CONVERSATION,
    source_profile_id: str | None = None,
) -> InboundEvent:
    values = dict(
        event_id=event_id,
        source="whatsapp",
        conversation_id=conversation_id,
        occurred_at="2026-08-04T12:00:00Z",
        actor_ref=ACTOR,
        privacy_scope="area_shared",
        text=text,
        media=media,
    )
    if source_profile_id is not None:
        values["source_profile_id"] = source_profile_id
    return InboundEvent(**values)


class _Response:
    def __init__(self, value: dict[str, object]):
        self.raw = json.dumps(value).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, limit: int) -> bytes:
        return self.raw[:limit]


class TransportTest(unittest.TestCase):
    def test_set_message_reaction_uses_exact_message_without_text_reply(self) -> None:
        captured = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["fields"] = urllib.parse.parse_qs(
                request.data.decode("utf-8"), keep_blank_values=True
            )
            return _Response({"ok": True, "result": True})

        TelegramBotTransport("token", opener=opener).set_message_reaction(
            "-100123", "812", "✅"
        )
        self.assertTrue(captured["url"].endswith("/setMessageReaction"))
        self.assertEqual(["-100123"], captured["fields"]["chat_id"])
        self.assertEqual(["812"], captured["fields"]["message_id"])
        self.assertEqual(
            ['[{"type":"emoji","emoji":"✅"}]'],
            captured["fields"]["reaction"],
        )

    def test_forum_topic_provision_and_compensation_use_group_only(self) -> None:
        requests: list[tuple[str, dict[str, list[str]]]] = []
        responses = iter(
            [
                {"ok": True, "result": {"message_thread_id": 84, "name": "Contato"}},
                {"ok": True, "result": True},
            ]
        )

        def opener(request, timeout):
            requests.append(
                (
                    request.full_url,
                    urllib.parse.parse_qs(request.data.decode("utf-8")),
                )
            )
            return _Response(next(responses))

        transport = TelegramBotTransport("token", opener=opener)
        thread_id = transport.create_forum_topic("-100123", "Contato")
        self.assertEqual("84", thread_id)
        transport.delete_forum_topic("-100123", thread_id)
        self.assertTrue(requests[0][0].endswith("/createForumTopic"))
        self.assertEqual(["-100123"], requests[0][1]["chat_id"])
        self.assertEqual(["Contato"], requests[0][1]["name"])
        self.assertTrue(requests[1][0].endswith("/deleteForumTopic"))
        self.assertEqual(["84"], requests[1][1]["message_thread_id"])
        with self.assertRaisesRegex(ValueError, "telegram_group_chat_required"):
            transport.create_forum_topic("123", "DM proibida")

    def test_image_wrapper_drops_generated_description_only(self) -> None:
        wrapped = (
            "— [Image]\nUser text:\n"
            "[WhatsApp redacted Thu 2026-07-23 UTC] redacted: legenda original\n"
            "Description:\nautomatic analysis"
        )
        self.assertEqual("legenda original", sanitize_image_caption(wrapped))
        original = "Description: legenda escrita pelo remetente"
        self.assertEqual(original, sanitize_image_caption(original))

    def test_recording_transport_honors_idempotency_key(self) -> None:
        transport = RecordingTransport()
        route = Route(CONVERSATION, "-100123", "42")
        first = transport.send(inbound(), route, idempotency_key="stable-key")
        second = transport.send(inbound(), route, idempotency_key="stable-key")
        self.assertEqual(first, second)
        self.assertEqual(1, len(transport.records))
        self.assertEqual("42", transport.records[0].route.thread_id)

    def test_telegram_text_uses_exact_topic_and_exact_text(self) -> None:
        captured = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["data"] = request.data
            return _Response({"ok": True, "result": {"message_id": 91}})

        transport = TelegramBotTransport(
            "synthetic-token", api_base="https://telegram.invalid", opener=opener
        )
        result = transport.send(
            inbound(text="  texto exato\nsegunda linha  "),
            Route(CONVERSATION, "-100123", "42"),
            idempotency_key="stable-key",
        )
        fields = urllib.parse.parse_qs(captured["data"].decode("utf-8"), keep_blank_values=True)
        self.assertEqual(["-100123"], fields["chat_id"])
        self.assertEqual(["42"], fields["message_thread_id"])
        self.assertEqual(["  texto exato\nsegunda linha  "], fields["text"])
        self.assertEqual(("91",), result.remote_ids)

    def test_telegram_text_missing_message_id_is_unknown_after_post(self) -> None:
        transport = TelegramBotTransport(
            "synthetic-token",
            opener=lambda request, timeout: _Response({"ok": True, "result": {}}),
        )
        with self.assertRaises(TransportError) as raised:
            transport.send(
                inbound(text="texto"),
                Route(CONVERSATION, "-100123", "42"),
                idempotency_key="missing-message-id",
            )
        self.assertEqual("telegram_message_id_missing", raised.exception.code)
        self.assertTrue(raised.exception.outcome_unknown)

    def test_telegram_text_truncated_response_is_unknown_after_post(self) -> None:
        class TruncatedResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self, limit: int) -> bytes:
                return b'{"ok":true,"result":'

        transport = TelegramBotTransport(
            "synthetic-token", opener=lambda request, timeout: TruncatedResponse()
        )
        with self.assertRaises(TransportError) as raised:
            transport.send(
                inbound(text="texto"),
                Route(CONVERSATION, "-100123", "42"),
                idempotency_key="truncated-response",
            )
        self.assertEqual("telegram_response_invalid", raised.exception.code)
        self.assertTrue(raised.exception.outcome_unknown)

    def test_telegram_text_missing_ok_is_unknown_after_post(self) -> None:
        transport = TelegramBotTransport(
            "synthetic-token",
            opener=lambda request, timeout: _Response({"result": {"message_id": 91}}),
        )
        with self.assertRaises(TransportError) as raised:
            transport.send(
                inbound(text="texto"),
                Route(CONVERSATION, "-100123", "42"),
                idempotency_key="missing-ok",
            )
        self.assertEqual("telegram_response_invalid", raised.exception.code)
        self.assertTrue(raised.exception.outcome_unknown)

    def test_telegram_image_keeps_file_and_caption_without_description(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            photo = Path(temp) / "photo.jpg"
            photo.write_bytes(b"original-image-bytes")
            captured = {}

            def opener(request, timeout):
                captured["stream_type"] = type(request.data).__name__
                captured["data"] = b"".join(request.data)
                return _Response({"ok": True, "result": {"message_id": "92"}})

            attachment = MediaAttachment(
                "media-1",
                "image",
                str(photo),
                mime_type="image/jpeg",
                sha256=hashlib.sha256(photo.read_bytes()).hexdigest(),
                size_bytes=photo.stat().st_size,
                caption=(
                    "[Image]\nUser text:\nlegenda original\n"
                    "Description:\nautomatic analysis"
                ),
            )
            transport = TelegramBotTransport("token", opener=opener)
            transport.send(
                inbound(text="", media=(attachment,)),
                Route(CONVERSATION, "-100123", "42"),
                idempotency_key="stable-key",
            )
            payload = captured["data"]
            self.assertIn(b"original-image-bytes", payload)
            self.assertIn("legenda original".encode(), payload)
            self.assertNotIn(b"automatic analysis", payload)
            self.assertNotIn(b"Description:", payload)
            self.assertEqual("_MultipartStream", captured["stream_type"])

    def test_telegram_single_media_missing_message_id_is_unknown_after_post(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            photo = Path(temp) / "photo.jpg"
            photo.write_bytes(b"image")
            transport = TelegramBotTransport(
                "token",
                opener=lambda request, timeout: _Response({"ok": True, "result": {}}),
            )
            with self.assertRaises(TransportError) as raised:
                transport.send(
                    inbound(
                        text="",
                        media=(MediaAttachment("media-1", "image", str(photo)),),
                    ),
                    Route(CONVERSATION, "-100123", "42"),
                    idempotency_key="single-media-missing-message-id",
                )
            self.assertEqual("telegram_message_id_missing", raised.exception.code)
            self.assertTrue(raised.exception.outcome_unknown)

    def test_telegram_voice_uses_send_voice_and_ogg_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            voice = Path(temp) / "voice.ogg"
            voice.write_bytes(b"ogg-voice-bytes")
            captured = {}

            def opener(request, timeout):
                captured["url"] = request.full_url
                captured["data"] = b"".join(request.data)
                return _Response({"ok": True, "result": {"message_id": 93}})

            transport = TelegramBotTransport("token", opener=opener)
            transport.send(
                inbound(
                    text="",
                    media=(
                        MediaAttachment(
                            "voice-1", "voice", str(voice), mime_type="audio/ogg"
                        ),
                    ),
                ),
                Route(CONVERSATION, "-100123", "42"),
                idempotency_key="stable-key",
            )
            self.assertTrue(captured["url"].endswith("/sendVoice"))
            self.assertIn(b'name="voice"', captured["data"])
            self.assertIn(b"ogg-voice-bytes", captured["data"])

    def test_telegram_media_group_streams_files_and_preserves_each_caption(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            first = Path(temp) / "first.jpg"
            second = Path(temp) / "second.mp4"
            first.write_bytes(b"first-image-bytes")
            second.write_bytes(b"second-video-bytes")
            captured = {}

            def opener(request, timeout):
                captured["url"] = request.full_url
                captured["stream_type"] = type(request.data).__name__
                captured["data"] = b"".join(request.data)
                return _Response(
                    {
                        "ok": True,
                        "result": [{"message_id": 101}, {"message_id": "102"}],
                    }
                )

            media = (
                MediaAttachment(
                    "album-1",
                    "image",
                    str(first),
                    mime_type="image/jpeg",
                    caption="[Image]\nUser text:\ncaption one\nDescription:\nvision output",
                ),
                MediaAttachment(
                    "album-2",
                    "video",
                    str(second),
                    mime_type="video/mp4",
                    caption="caption two",
                ),
            )
            result = TelegramBotTransport("token", opener=opener).send(
                inbound(text="", media=media),
                Route(CONVERSATION, "-100123", "42"),
                idempotency_key="stable-album",
            )

            payload = captured["data"]
            media_json = payload.split(b'name="media"\r\n\r\n', 1)[1].split(b"\r\n--", 1)[0]
            descriptors = json.loads(media_json.decode("utf-8"))
            self.assertTrue(captured["url"].endswith("/sendMediaGroup"))
            self.assertEqual("_MultipartStream", captured["stream_type"])
            self.assertIn(b'name="chat_id"\r\n\r\n-100123', payload)
            self.assertIn(b'name="message_thread_id"\r\n\r\n42', payload)
            self.assertEqual(
                [
                    {"type": "photo", "media": "attach://file0", "caption": "caption one"},
                    {"type": "video", "media": "attach://file1", "caption": "caption two"},
                ],
                descriptors,
            )
            self.assertIn(b"first-image-bytes", payload)
            self.assertIn(b"second-video-bytes", payload)
            self.assertNotIn(b"vision output", payload)
            self.assertNotIn(b"Description:", payload)
            self.assertEqual(("101", "102"), result.remote_ids)

    def test_telegram_media_group_network_failure_has_unknown_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = [Path(temp) / f"{index}.jpg" for index in range(2)]
            for path in paths:
                path.write_bytes(b"image")

            def opener(request, timeout):
                raise OSError("connection lost after upload")

            transport = TelegramBotTransport("token", opener=opener)
            with self.assertRaises(TransportError) as raised:
                transport.send(
                    inbound(
                        text="",
                        media=tuple(
                            MediaAttachment(f"album-{index}", "image", str(path))
                            for index, path in enumerate(paths)
                        ),
                    ),
                    Route(CONVERSATION, "-100123", "42"),
                    idempotency_key="stable-album",
                )
            self.assertEqual("telegram_network_error", raised.exception.code)
            self.assertTrue(raised.exception.outcome_unknown)

    def test_telegram_long_text_chunks_by_utf16_without_truncation(self) -> None:
        requests = []
        emoji = chr(0x1F600)

        def opener(request, timeout):
            requests.append(request)
            return _Response(
                {"ok": True, "result": {"message_id": 200 + len(requests)}}
            )

        result = TelegramBotTransport(
            "token", opener=opener, max_text_chars=4
        ).send(
            inbound(text=f"ab{emoji}cd{emoji}e"),
            Route(CONVERSATION, "-100123", "42"),
            idempotency_key="long-text",
        )
        fields = [
            urllib.parse.parse_qs(
                request.data.decode("utf-8"), keep_blank_values=True
            )
            for request in requests
        ]
        self.assertEqual(
            [f"ab{emoji}", f"cd{emoji}", "e"],
            [item["text"][0] for item in fields],
        )
        self.assertTrue(all(item["message_thread_id"] == ["42"] for item in fields))
        self.assertEqual(("201", "202", "203"), result.remote_ids)

    def test_telegram_partial_long_text_failure_is_uncertain(self) -> None:
        calls = 0

        def opener(request, timeout):
            nonlocal calls
            calls += 1
            if calls == 1:
                return _Response({"ok": True, "result": {"message_id": 301}})
            raise urllib.error.HTTPError(request.full_url, 400, "bad", {}, None)

        transport = TelegramBotTransport("token", opener=opener, max_text_chars=4)
        with self.assertRaises(TransportError) as raised:
            transport.send(
                inbound(text="abcdEFGH"),
                Route(CONVERSATION, "-100123", "42"),
                idempotency_key="partial-text",
            )
        self.assertEqual("telegram_http_400", raised.exception.code)
        self.assertTrue(raised.exception.outcome_unknown)

    def test_telegram_long_caption_keeps_media_prefix_and_sends_exact_remainder(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            photo = Path(temp) / "photo.jpg"
            photo.write_bytes(b"image")
            captured = []
            emoji = chr(0x1F600)

            def opener(request, timeout):
                data = b"".join(request.data) if not isinstance(request.data, bytes) else request.data
                captured.append((request.full_url, data))
                return _Response(
                    {"ok": True, "result": {"message_id": 400 + len(captured)}}
                )

            result = TelegramBotTransport(
                "token", opener=opener, max_caption_chars=4, max_text_chars=4
            ).send(
                inbound(
                    text="",
                    media=(
                        MediaAttachment(
                            "long-caption",
                            "image",
                            str(photo),
                            caption=f"ab{emoji}cd{emoji}e",
                        ),
                    ),
                ),
                Route(CONVERSATION, "-100123", "42"),
                idempotency_key="long-caption",
            )
            self.assertIn(f"ab{emoji}".encode("utf-8"), captured[0][1])
            tail_fields = [
                urllib.parse.parse_qs(data.decode("utf-8"), keep_blank_values=True)
                for _, data in captured[1:]
            ]
            self.assertEqual(
                [f"cd{emoji}", "e"], [item["text"][0] for item in tail_fields]
            )
            self.assertTrue(all(item["message_thread_id"] == ["42"] for item in tail_fields))
            self.assertEqual(("401", "402", "403"), result.remote_ids)

    def test_telegram_partial_long_caption_failure_is_uncertain(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            photo = Path(temp) / "photo.jpg"
            photo.write_bytes(b"image")
            calls = 0

            def opener(request, timeout):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return _Response({"ok": True, "result": {"message_id": 501}})
                raise urllib.error.HTTPError(request.full_url, 400, "bad", {}, None)

            transport = TelegramBotTransport(
                "token", opener=opener, max_caption_chars=4, max_text_chars=4
            )
            with self.assertRaises(TransportError) as raised:
                transport.send(
                    inbound(
                        text="",
                        media=(
                            MediaAttachment(
                                "partial-caption",
                                "image",
                                str(photo),
                                caption="abcdEFGH",
                            ),
                        ),
                    ),
                    Route(CONVERSATION, "-100123", "42"),
                    idempotency_key="partial-caption",
                )
            self.assertEqual("telegram_http_400", raised.exception.code)
            self.assertTrue(raised.exception.outcome_unknown)

    def test_telegram_media_limit_fails_closed_without_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            media_path = Path(temp) / "large.jpg"
            media_path.write_bytes(b"12345")
            attachment = MediaAttachment("media-limit", "image", str(media_path))
            transport = TelegramBotTransport(
                "token", opener=lambda *args, **kwargs: None,
                max_media_bytes=4, max_caption_chars=4
            )
            with self.assertRaises(TransportError) as raised:
                transport.send(
                    inbound(text="", media=(attachment,)),
                    Route(CONVERSATION, "-100123", "42"),
                    idempotency_key="stable-key",
                )
            self.assertEqual("media_too_large", raised.exception.code)

    def test_multipart_media_body_streams_in_bounded_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            media_path = Path(temp) / "document.bin"
            media_path.write_bytes(b"x" * (200 * 1024))
            captured = {}

            def opener(request, timeout):
                chunks = list(request.data)
                captured["sizes"] = [len(chunk) for chunk in chunks]
                captured["total"] = sum(captured["sizes"])
                captured["content_length"] = int(request.headers["Content-length"])
                return _Response({"ok": True, "result": {"message_id": 94}})

            transport = TelegramBotTransport(
                "token", opener=opener, max_media_bytes=300 * 1024
            )
            transport.send(
                inbound(
                    text="",
                    media=(MediaAttachment("doc-1", "document", str(media_path)),),
                ),
                Route(CONVERSATION, "-100123", "42"),
                idempotency_key="stable-key",
            )
            self.assertEqual(captured["content_length"], captured["total"])
            # File chunks are 64 KiB; only tiny multipart prefix/suffix surround them.
            self.assertLessEqual(max(captured["sizes"]), 64 * 1024)

    def test_transport_error_does_not_echo_sensitive_exception_material(self) -> None:
        def opener(request, timeout):
            raise OSError("token secret contact /private/path")

        transport = TelegramBotTransport("super-secret-token", opener=opener)
        with self.assertRaises(TransportError) as raised:
            transport.send(
                inbound(),
                Route(CONVERSATION, "-100123", "42"),
                idempotency_key="stable-key",
            )
        self.assertEqual("telegram_network_error", str(raised.exception))
        self.assertTrue(raised.exception.outcome_unknown)

    def test_managed_media_deletion_requires_opt_in_and_containment(self) -> None:
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as outside:
            root = Path(temp)
            kept = root / "kept.bin"
            kept.write_bytes(b"x")
            unmanaged = MediaAttachment("m1", "document", str(kept))
            self.assertFalse(remove_managed_media(unmanaged, root))
            self.assertTrue(kept.exists())

            external = Path(outside) / "external.bin"
            external.write_bytes(b"x")
            managed_external = MediaAttachment(
                "m2", "document", str(external), managed_temp=True
            )
            self.assertFalse(remove_managed_media(managed_external, root))
            self.assertTrue(external.exists())

            removable = root / "remove.bin"
            removable.write_bytes(b"x")
            managed = MediaAttachment("m3", "document", str(removable), managed_temp=True)
            self.assertTrue(remove_managed_media(managed, root))
            self.assertFalse(removable.exists())


class WorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = MirrorLedger(Path(self.temp.name) / "mirror.sqlite3")
        self.db.set_route(Route(CONVERSATION, "-100123", "42"))

    def tearDown(self) -> None:
        self.db.close()
        self.temp.cleanup()

    def test_success_is_one_logical_delivery_and_never_replayed(self) -> None:
        transport = RecordingTransport()
        worker = MirrorWorker(self.db, transport)
        worker.ingest(inbound(), now=100)
        worker.ingest(inbound(), now=100)
        self.assertEqual("sent", worker.run_once(now=100).status)
        self.assertEqual("idle", worker.run_once(now=100).status)
        self.assertEqual(1, len(transport.records))
        self.assertEqual("sent", self.db.delivery_state("evt-1"))

    def test_worker_can_claim_only_its_source_profile(self) -> None:
        profile_a = opaque_ref("profile", "account-a")
        profile_b = opaque_ref("profile", "account-b")
        event_b = inbound("event-b", source_profile_id=profile_b)
        self.db.capture_event(event_b, now=100)
        transport_a = RecordingTransport()
        transport_b = RecordingTransport()
        worker_a = MirrorWorker(self.db, transport_a, profile_id="account-a")
        worker_b = MirrorWorker(self.db, transport_b, profile_id="account-b")
        self.assertEqual("idle", worker_a.run_once(now=100).status)
        self.assertEqual([], transport_a.records)
        self.assertEqual("sent", worker_b.run_once(now=101).status)
        self.assertEqual(1, len(transport_b.records))
        with self.assertRaisesRegex(ValueError, "source_profile_mismatch"):
            worker_a.ingest(inbound("wrong", source_profile_id=profile_b))

    def test_bounded_drain_processes_backlog_serially_without_losing_wip_one(self) -> None:
        transport = RecordingTransport()
        worker = MirrorWorker(self.db, transport)
        for item in ("evt-1", "evt-2", "evt-3"):
            worker.ingest(inbound(item))
        first = worker.run_bounded(max_items=2, max_seconds=10)
        self.assertEqual(("sent", "sent"), tuple(item.status for item in first))
        self.assertEqual(2, len(transport.records))
        second = worker.run_bounded(max_items=100, max_seconds=10)
        self.assertEqual(("sent",), tuple(item.status for item in second))
        self.assertEqual(3, len(transport.records))
        self.assertEqual((), worker.run_bounded(max_items=100, max_seconds=10))

    def test_proven_pre_accept_failure_retries_with_backoff(self) -> None:
        transport = RecordingTransport(failures_before_success=1, retryable=True)
        worker = MirrorWorker(self.db, transport, base_backoff_seconds=5)
        worker.ingest(inbound(), now=100)
        first = worker.run_once(now=100)
        self.assertEqual("retry", first.status)
        self.assertEqual("idle", worker.run_once(now=104).status)
        self.assertEqual("sent", worker.run_once(now=105).status)
        self.assertEqual(2, self.db.attempt_count("evt-1"))
        self.assertEqual(1, len(transport.records))

    def test_unknown_outcome_is_quarantined_and_never_retried(self) -> None:
        class UnknownTransport:
            def send(self, event, route, *, idempotency_key):
                raise TransportError("network_lost", outcome_unknown=True)

        worker = MirrorWorker(self.db, UnknownTransport())
        worker.ingest(inbound(), now=100)
        result = worker.run_once(now=100)
        self.assertEqual("uncertain", result.status)
        self.assertEqual("uncertain", self.db.delivery_state("evt-1"))
        self.assertEqual("idle", worker.run_once(now=1000).status)

    def test_worker_crash_expires_to_uncertain_not_automatic_retry(self) -> None:
        class Crash(BaseException):
            pass

        class CrashTransport:
            def send(self, event, route, *, idempotency_key):
                raise Crash()

        worker = MirrorWorker(
            self.db, CrashTransport(), lease_seconds=10, runtime_lock_seconds=10
        )
        worker.ingest(inbound(), now=100)
        with self.assertRaises(Crash):
            worker.run_once(now=100)
        observer = MirrorWorker(self.db, RecordingTransport(), worker_id="observer")
        self.assertEqual("idle", observer.run_once(now=111).status)
        self.assertEqual("uncertain", self.db.delivery_state("evt-1"))

    def test_second_worker_for_same_profile_is_standby(self) -> None:
        first = MirrorWorker(
            self.db, RecordingTransport(), worker_id="instance-a", profile_id="profile-a"
        )
        second = MirrorWorker(
            self.db, RecordingTransport(), worker_id="instance-b", profile_id="profile-a"
        )
        profile = opaque_ref("profile", "profile-a")
        first.ingest(inbound(source_profile_id=profile), now=100)
        self.assertEqual("sent", first.run_once(now=100).status)
        self.assertEqual("standby", second.run_once(now=101).status)
        # Capture is independent from the single delivery writer.  A second
        # producer may append while the first worker owns the runtime lease.
        self.assertIsNotNone(
            second.ingest(inbound("evt-2", source_profile_id=profile), now=101)
        )

    def test_slow_transport_renews_global_and_delivery_leases(self) -> None:
        observations = []
        db_path = Path(self.temp.name) / "mirror.sqlite3"

        class SlowTransport:
            def send(inner_self, event, route, *, idempotency_key):
                del inner_self, event, route, idempotency_key
                time.sleep(1.2)
                with MirrorLedger(db_path) as observer:
                    observations.append(
                        observer.claim_next(
                            "competing-worker",
                            source_profile_id=opaque_ref("profile", "profile-a"),
                            lease_seconds=1,
                        )
                    )
                return SendResult(("remote-1",))

        worker = MirrorWorker(
            self.db,
            SlowTransport(),
            worker_id="slow-worker",
            profile_id="profile-a",
            lease_seconds=1,
            runtime_lock_seconds=2,
        )
        worker.ingest(
            inbound(source_profile_id=opaque_ref("profile", "profile-a")),
            now=int(time.time()),
        )
        self.assertEqual("sent", worker.run_once().status)
        self.assertEqual([None], observations)

    def test_video_policy_is_explicit_and_blocks_without_transport(self) -> None:
        video = Path(self.temp.name) / "video.mp4"
        video.write_bytes(b"video")
        attachment = MediaAttachment("v1", "video", str(video))
        transport = RecordingTransport()
        worker = MirrorWorker(self.db, transport, video_policy=VideoPolicy.BLOCK)
        worker.ingest(inbound(text="", media=(attachment,)), now=100)
        result = worker.run_once(now=100)
        self.assertEqual("dead", result.status)
        self.assertEqual("video_blocked", result.error_code)
        self.assertEqual([], transport.records)


class LegacyImportTest(unittest.TestCase):
    def test_hermes_v2_route_map_imports_explicit_aliases_without_using_names(self) -> None:
        route_map = {
            "schema": "whatsapp-telegram-route-map/v2",
            "forum_chat_id": "-100123",
            "routes": {
                "+15550000000": {
                    "thread_id": 42,
                    "topic_name": "NAME_MUST_NOT_ROUTE",
                    "enabled": True,
                    "kind": "contact",
                    "aliases": [
                        "15550000000@s.whatsapp.net",
                        "123456789@lid",
                    ],
                }
            },
        }
        profile = "hermes-main"
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "mirror.sqlite3"
            with MirrorLedger(db_path) as ledger:
                first = import_legacy_runtime_config(
                    ledger, route_map, source_profile_id=profile
                )
                self.assertEqual(1, first.routes_seen)
                self.assertEqual(2, first.aliases_seen)
                self.assertEqual(2, first.aliases_created_or_updated)
                canonical = legacy_conversation_id("+15550000000", profile)
                for raw_alias in (
                    "15550000000@s.whatsapp.net",
                    "123456789@lid",
                ):
                    observed = legacy_conversation_id(raw_alias, profile)
                    self.assertEqual(
                        canonical, ledger.resolve_conversation_alias(observed)
                    )
                route = ledger.get_route(canonical)
                assert route is not None
                self.assertEqual(("-100123", "42"), (route.chat_id, route.thread_id))
                again = import_legacy_runtime_config(
                    ledger, route_map, source_profile_id=profile
                )
                self.assertFalse(again.imported)
                self.assertEqual(0, again.routes_created_or_updated)
                self.assertEqual(0, again.aliases_created_or_updated)
            self.assertNotIn(b"NAME_MUST_NOT_ROUTE", db_path.read_bytes())

    def test_hermes_v2_alias_collision_fails_closed(self) -> None:
        route_map = {
            "schema": "whatsapp-telegram-route-map/v2",
            "forum_chat_id": "-100123",
            "routes": {
                "canonical-a": {
                    "thread_id": 42,
                    "enabled": True,
                    "aliases": ["same@lid"],
                },
                "canonical-b": {
                    "thread_id": 43,
                    "enabled": True,
                    "aliases": ["same@lid"],
                },
            },
        }
        with tempfile.TemporaryDirectory() as temp:
            with MirrorLedger(Path(temp) / "mirror.sqlite3") as ledger:
                with self.assertRaisesRegex(
                    LegacyImportError, "legacy_route_alias_conflict"
                ):
                    import_legacy_runtime_config(
                        ledger, route_map, source_profile_id="hermes-main"
                    )
                self.assertEqual([], ledger.list_routes())
                self.assertEqual([], ledger.list_conversation_aliases())

    def test_imported_legacy_runtime_route_and_dedupe_are_reused_by_live_profile(self) -> None:
        profile = "openclaw-primary"
        config = {
            "groupChatId": "-100123",
            "contactTopics": {
                "conversation-a": {
                    "topicId": "42",
                    "recentRoutedInboundMessageIds": ["already-sent"],
                }
            },
        }
        with tempfile.TemporaryDirectory() as temp:
            with MirrorLedger(Path(temp) / "mirror.sqlite3") as ledger:
                import_legacy_runtime_config(
                    ledger,
                    config,
                    source_profile_id=profile,
                )
                old = normalize_inbound(
                    RawInboundMessage(
                        platform="whatsapp",
                        direction="inbound",
                        raw_message_id="already-sent",
                        raw_conversation_id="conversation-a",
                        raw_actor_id="actor-a",
                        occurred_at="2026-08-04T12:00:00Z",
                        privacy_scope="owner_private",
                        source_profile_id=profile,
                        text="old",
                    )
                )
                assert old is not None
                self.assertEqual(
                    legacy_conversation_id("conversation-a", profile),
                    old.conversation_id,
                )
                ledger.record_event(old)
                self.assertIsNone(ledger.enqueue(old.event_id))

                fresh = normalize_inbound(
                    RawInboundMessage(
                        platform="whatsapp",
                        direction="inbound",
                        raw_message_id="new-message",
                        raw_conversation_id="conversation-a",
                        raw_actor_id="actor-a",
                        occurred_at="2026-08-04T12:01:00Z",
                        privacy_scope="owner_private",
                        source_profile_id=profile,
                        text="new",
                    )
                )
                assert fresh is not None
                inserted, delivery_id, blocked = ledger.capture_event(fresh)
                self.assertTrue(inserted)
                self.assertIsNotNone(delivery_id)
                self.assertIsNone(blocked)

    def test_verified_identity_map_aliases_runtime_key_to_existing_topic(self) -> None:
        config = {
            "groupChatId": "-100123",
            "contactTopics": {
                "legacy-key": {
                    "topicId": "42",
                    "recentRoutedInboundMessageIds": ["already-sent"],
                }
            },
        }
        identity_map = {
            "schema_version": 1,
            "mappings": [
                {
                    "legacy_conversation_id": "legacy-key",
                    "runtime_conversation_id": "runtime-key",
                    "runtime_source_profile_id": "openclaw-primary",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            with MirrorLedger(Path(temp) / "mirror.sqlite3") as ledger:
                result = import_legacy_runtime_config(
                    ledger,
                    config,
                    source_profile_id="openclaw-primary",
                    identity_map=identity_map,
                )
                self.assertEqual(1, result.aliases_seen)
                self.assertEqual(1, result.aliases_created_or_updated)
                observed = legacy_conversation_id(
                    "runtime-key", "openclaw-primary"
                )
                canonical = legacy_conversation_id(
                    "legacy-key", "openclaw-primary"
                )
                self.assertEqual(
                    canonical, ledger.resolve_conversation_alias(observed)
                )
                route = ledger.get_route(canonical)
                assert route is not None
                self.assertEqual(("-100123", "42"), (route.chat_id, route.thread_id))
                live = normalize_inbound(
                    RawInboundMessage(
                        platform="whatsapp",
                        direction="inbound",
                        raw_message_id="already-sent",
                        raw_conversation_id="runtime-key",
                        raw_actor_id="actor-a",
                        occurred_at="2026-08-04T12:00:00Z",
                        privacy_scope="owner_private",
                        source_profile_id="openclaw-primary",
                        text="old",
                    )
                )
                assert live is not None
                self.assertTrue(
                    ledger.is_legacy_delivered(canonical, live.event_id)
                )

    def test_dry_run_uses_immutable_read_only_connection_and_changes_no_files(self) -> None:
        config = {
            "groupChatId": "-100123",
            "contactTopics": {"contact-a": {"topicId": "42"}},
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db_path = root / "mirror.sqlite3"
            with MirrorLedger(db_path):
                pass
            before = {
                item.name: (
                    hashlib.sha256(item.read_bytes()).hexdigest(),
                    item.stat().st_size,
                    item.stat().st_mtime_ns,
                )
                for item in root.iterdir()
                if item.is_file()
            }
            with MirrorLedger(db_path, read_only=True) as ledger:
                result = import_legacy_runtime_config(ledger, config, dry_run=True)
                self.assertTrue(result.dry_run)
                self.assertEqual(1, result.routes_created_or_updated)
                self.assertEqual([], ledger.list_routes())
            after = {
                item.name: (
                    hashlib.sha256(item.read_bytes()).hexdigest(),
                    item.stat().st_size,
                    item.stat().st_mtime_ns,
                )
                for item in root.iterdir()
                if item.is_file()
            }
            self.assertEqual(before, after)

    def test_legacy_import_preserves_topic_watermark_and_recent_ids_only(self) -> None:
        secret = "secret-token-that-must-not-be-persisted"
        config = {
            "groupChatId": "-100123",
            "botToken": secret,
            "contactTopics": {
                "+5511987654321": {
                    "topicId": "42",
                    "lastRoutedInboundMessageId": "last-id",
                    "recentRoutedInboundMessageIds": ["old-id", "last-id"],
                    "lastBlockedVideoMessageId": "blocked-video-id",
                    "lastSuggestedInboundMessageId": "suggested-id",
                    "topicName": "Sensitive Contact Name",
                }
            },
        }
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "mirror.sqlite3"
            with MirrorLedger(db_path) as ledger:
                result = import_legacy_runtime_config(ledger, config)
                self.assertTrue(result.imported)
                self.assertEqual(1, result.routes_seen)
                self.assertEqual(4, result.recent_ids_seen)
                route = ledger.get_route(legacy_conversation_id("+5511987654321"))
                self.assertIsNotNone(route)
                assert route is not None
                self.assertEqual("-100123", route.chat_id)
                self.assertEqual("42", route.thread_id)
                profile = opaque_ref("profile", "default")
                normalized_old = opaque_ref(
                    "event",
                    f"whatsapp\x1f{profile}\x1f+5511987654321\x1fold-id",
                )
                self.assertTrue(
                    ledger.is_legacy_delivered(
                        legacy_conversation_id("+5511987654321"), normalized_old
                    )
                )
                canonical_old = canonical_whatsapp_event_ref(
                    profile,
                    legacy_conversation_id("+5511987654321"),
                    "old-id",
                )
                self.assertTrue(
                    ledger.is_legacy_delivered(
                        legacy_conversation_id("+5511987654321"),
                        canonical_old,
                    )
                )
                for raw_id in ("blocked-video-id", "suggested-id"):
                    normalized_terminal = opaque_ref(
                        "event",
                        f"whatsapp\x1f{profile}\x1f+5511987654321\x1f{raw_id}",
                    )
                    self.assertTrue(
                        ledger.is_legacy_delivered(
                            legacy_conversation_id("+5511987654321"),
                            normalized_terminal,
                        )
                    )
                old_event = inbound(
                    normalized_old,
                    conversation_id=legacy_conversation_id("+5511987654321"),
                )
                ledger.record_event(old_event)
                self.assertIsNone(ledger.enqueue(old_event.event_id))
                same_id_other_conversation = opaque_ref(
                    "event",
                    f"whatsapp\x1f{profile}\x1fother-contact\x1fold-id",
                )
                other_event = inbound(
                    same_id_other_conversation,
                    conversation_id=legacy_conversation_id("other-contact"),
                )
                ledger.set_route(
                    Route(
                        legacy_conversation_id("other-contact"), "-100123", "43"
                    )
                )
                ledger.record_event(other_event)
                self.assertIsNotNone(ledger.enqueue(other_event.event_id))
                again = import_legacy_runtime_config(ledger, config)
                self.assertFalse(again.imported)
                self.assertEqual(0, again.tombstones_created)
            database_bytes = db_path.read_bytes()
            self.assertNotIn(secret.encode(), database_bytes)
            self.assertNotIn(b"Sensitive Contact Name", database_bytes)
            self.assertNotIn(b"+5511987654321", database_bytes)


if __name__ == "__main__":
    unittest.main()
