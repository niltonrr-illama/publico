from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from urllib import parse as urllib_parse

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from espelho_zap import (  # noqa: E402
    MirrorLedger,
    Route,
    canonical_whatsapp_event_ref,
    opaque_ref,
)
from espelho_zap.adapters import HermesBridgeError, HermesBridgeObserver


class Response:
    def __init__(self, value: object):
        self.payload = json.dumps(value, separators=(",", ":")).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, limit: int) -> bytes:
        return self.payload[:limit]


class FakeBridge:
    def __init__(self, messages: list[object], before_ack=None):
        self.messages = messages
        self.before_ack = before_ack
        self.requests = []

    def __call__(self, request, *, timeout):
        self.requests.append((request, timeout))
        path = urllib_parse.urlsplit(request.full_url).path
        if path == "/messages":
            return Response(self.messages)
        if path == "/ack":
            payload = json.loads(request.data.decode("utf-8"))
            if self.before_ack:
                self.before_ack(payload)
            return Response(
                {
                    "ok": True,
                    "acked": len(payload["messageIds"]),
                    "remaining": 0,
                }
            )
        raise AssertionError(f"unexpected bridge path: {path}")


def message(message_id: str, **overrides) -> dict[str, object]:
    value: dict[str, object] = {
        "messageId": message_id,
        "chatId": "chat-a",
        "senderId": "actor-a",
        "body": f"texto-{message_id}",
        "timestamp": 1_700_000_000,
        "hasMedia": False,
        "mediaUrls": [],
    }
    value.update(overrides)
    return value


class HermesBridgeObserverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.spool = self.root / "spool"
        self.source.mkdir()
        self.profile = "hermes-main"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def route(self, ledger: MirrorLedger, raw_conversation: str = "chat-a") -> str:
        profile_ref = opaque_ref("profile", self.profile)
        conversation = opaque_ref(
            "conversation", f"{profile_ref}\x1f{raw_conversation}"
        )
        ledger.set_route(Route(conversation, "-100123", "42"))
        return conversation

    def observer(
        self,
        ledger: MirrorLedger,
        bridge: FakeBridge,
        *,
        batch_limit: int = 100,
    ) -> HermesBridgeObserver:
        return HermesBridgeObserver(
            ledger,
            bridge_url="http://127.0.0.1:3011",
            source_profile_id=self.profile,
            spool_root=self.spool,
            source_media_roots=(self.source,),
            minimum_free_bytes=1,
            maximum_spool_bytes=10_000_000,
            batch_limit=batch_limit,
            opener=bridge,
        )

    def test_loopback_host_header_batch_bound_and_no_whatsapp_outbound(self) -> None:
        bridge = FakeBridge([message("m1"), message("m2"), message("m3")])
        with MirrorLedger(self.root / "ledger.sqlite3") as ledger:
            self.route(ledger)
            observer = self.observer(ledger, bridge, batch_limit=2)
            result = observer.observe_once()
            self.assertFalse(observer.capabilities.outbound_whatsapp)
            self.assertEqual((3, 2, 2, 2), (
                result.fetched,
                result.selected,
                result.inserted,
                result.acked,
            ))
            self.assertEqual(
                2,
                ledger.connection.execute("SELECT COUNT(*) FROM mirror_events").fetchone()[0],
            )
        self.assertEqual(["GET", "POST"], [item[0].get_method() for item in bridge.requests])
        for request, _ in bridge.requests:
            self.assertEqual("127.0.0.1:3011", request.get_header("Host"))
            self.assertNotIn("send", request.full_url)
        ack = json.loads(bridge.requests[1][0].data.decode("utf-8"))
        self.assertEqual(["m1", "m2"], ack["messageIds"])

        with MirrorLedger(self.root / "other.sqlite3") as ledger:
            with self.assertRaises(HermesBridgeError):
                HermesBridgeObserver(
                    ledger,
                    bridge_url="http://example.com:3011",
                    source_profile_id=self.profile,
                    spool_root=self.spool,
                    source_media_roots=(self.source,),
                    minimum_free_bytes=1,
                    maximum_spool_bytes=1000,
                )

    def test_provider_receipts_update_outbound_projection_and_ack(self) -> None:
        bridge = FakeBridge(
            [
                {
                    "messageId": "receipt-event-1",
                    "nativeType": "outbound_receipt",
                    "nativeMetadata": {
                        "receipt": {
                            "outboundMessageId": "3EB-test",
                            "state": 3,
                            "providerEvent": "message-receipt.update",
                        }
                    },
                },
                {
                    "messageId": "receipt-event-2",
                    "nativeType": "outbound_receipt",
                    "nativeMetadata": {
                        "receipt": {
                            "outboundMessageId": "3EB-test",
                            "state": 4,
                            "providerEvent": "messages.update",
                        }
                    },
                },
            ]
        )
        with MirrorLedger(self.root / "ledger.sqlite3") as ledger:
            result = self.observer(ledger, bridge).observe_once()
            self.assertEqual((2, 2, 0), (result.receipts, result.acked, result.malformed))
            row = ledger.connection.execute(
                "SELECT state, provider_event FROM mirror_outbound_receipts WHERE outbound_ref=?",
                ("3EB-test",),
            ).fetchone()
            self.assertEqual(4, int(row["state"]))
            self.assertEqual("messages.update", row["provider_event"])
            self.assertEqual(
                0,
                ledger.connection.execute("SELECT COUNT(*) FROM mirror_events").fetchone()[0],
            )

    def test_malformed_provider_receipt_is_not_acked_or_quarantined(self) -> None:
        bridge = FakeBridge(
            [
                {
                    "messageId": "receipt-bad",
                    "nativeType": "outbound_receipt",
                    "nativeMetadata": {"receipt": {"outboundMessageId": "3EB-test"}},
                }
            ]
        )
        with MirrorLedger(self.root / "ledger.sqlite3") as ledger:
            result = self.observer(ledger, bridge).observe_once()
            self.assertEqual((1, 0, 0), (result.malformed, result.acked, result.receipts))
            self.assertEqual(["GET"], [item[0].get_method() for item in bridge.requests])

    def test_text_photo_audio_and_original_caption_are_persisted(self) -> None:
        photo = self.source / "photo.jpg"
        audio = self.source / "voice.ogg"
        photo.write_bytes(b"photo-bytes")
        audio.write_bytes(b"voice-bytes")
        bridge = FakeBridge(
            [
                message("text"),
                message(
                    "photo",
                    body=(
                        "[Image]\nUser text:\nlegenda original\n"
                        "Description:\nanalise automatica"
                    ),
                    hasMedia=True,
                    mediaType="image",
                    mime="image/jpeg",
                    mediaUrls=[str(photo)],
                ),
                message(
                    "audio",
                    body="[audio received]",
                    hasMedia=True,
                    mediaType="ptt",
                    mime="audio/ogg; codecs=opus",
                    mediaUrls=[str(audio)],
                ),
            ]
        )
        with MirrorLedger(self.root / "ledger.sqlite3") as ledger:
            self.route(ledger)
            result = self.observer(ledger, bridge).observe_once()
            self.assertEqual((3, 3, 0), (result.inserted, result.acked, result.media_failed))
            rows = ledger.connection.execute(
                "SELECT event_id FROM mirror_events ORDER BY captured_at, event_id"
            ).fetchall()
            events = [ledger.load_event(str(row["event_id"])) for row in rows]
            text_event = next(item for item in events if item.text == "texto-text")
            photo_event = next(item for item in events if item.media and item.media[0].kind == "image")
            audio_event = next(item for item in events if item.media and item.media[0].kind == "voice")
            self.assertEqual("texto-text", text_event.text)
            self.assertEqual("legenda original", photo_event.text)
            self.assertEqual("legenda original", photo_event.media[0].caption)
            self.assertNotIn("Description:", photo_event.storage_json())
            self.assertEqual("", audio_event.text)
            self.assertEqual("", audio_event.media[0].caption)
            self.assertEqual(b"photo-bytes", Path(photo_event.media[0].path).read_bytes())
            self.assertEqual(b"voice-bytes", Path(audio_event.media[0].path).read_bytes())
            self.assertTrue(photo_event.media[0].managed_temp)
            self.assertTrue(audio_event.media[0].managed_temp)
            self.assertEqual(2, result.source_media_cleaned)
            self.assertEqual(0, result.source_media_cleanup_failed)
        self.assertFalse(photo.exists())
        self.assertFalse(audio.exists())

    def test_permanent_invalid_records_are_quarantined_before_ack(self) -> None:
        bridge = FakeBridge(
            [
                {"messageId": "missing-fields"},
                message(
                    "missing-media",
                    hasMedia=True,
                    mediaType="image",
                    mediaUrls=[str(self.source / "absent.jpg")],
                ),
            ]
        )
        with MirrorLedger(self.root / "ledger.sqlite3") as ledger:
            self.route(ledger)
            result = self.observer(ledger, bridge).observe_once()
            self.assertEqual(1, result.malformed)
            self.assertEqual(1, result.media_failed)
            self.assertEqual(2, result.quarantined)
            self.assertEqual(2, result.acked)
            self.assertEqual(
                0,
                ledger.connection.execute("SELECT COUNT(*) FROM mirror_events").fetchone()[0],
            )
            rows = ledger.connection.execute(
                """SELECT payload_json, error_code
                   FROM mirror_source_quarantine ORDER BY error_code"""
            ).fetchall()
            self.assertEqual(2, len(rows))
            self.assertTrue(all(json.loads(str(row["payload_json"])) for row in rows))
        self.assertEqual(["GET", "POST"], [item[0].get_method() for item in bridge.requests])

    def test_unidentifiable_invalid_record_remains_unacknowledged(self) -> None:
        bridge = FakeBridge([{"chatId": "missing-message-id"}])
        with MirrorLedger(self.root / "ledger.sqlite3") as ledger:
            result = self.observer(ledger, bridge).observe_once()
            self.assertEqual(1, result.malformed)
            self.assertEqual(0, result.quarantined)
            self.assertEqual(0, result.acked)
            self.assertEqual(
                0,
                ledger.connection.execute(
                    "SELECT COUNT(*) FROM mirror_source_quarantine"
                ).fetchone()[0],
            )
        self.assertEqual(["GET"], [item[0].get_method() for item in bridge.requests])

    def test_media_replay_acks_from_durable_ledger_after_source_disappears(self) -> None:
        photo = self.source / "photo.jpg"
        photo.write_bytes(b"photo")
        item = message(
            "photo-replay",
            body="caption",
            hasMedia=True,
            mediaType="photo",
            mime="image/jpeg",
            mediaUrls=[str(photo)],
        )
        bridge = FakeBridge([item])
        with MirrorLedger(self.root / "ledger.sqlite3") as ledger:
            self.route(ledger)
            first = self.observer(ledger, bridge).observe_once()
            photo.unlink(missing_ok=True)
            second = self.observer(ledger, bridge).observe_once()
            self.assertEqual((1, 0), (first.inserted, first.duplicates))
            self.assertEqual((0, 1, 1), (second.inserted, second.duplicates, second.acked))
            self.assertEqual(
                1,
                ledger.connection.execute("SELECT COUNT(*) FROM mirror_events").fetchone()[0],
            )

    def test_ack_failure_keeps_source_media_for_safe_retry(self) -> None:
        photo = self.source / "photo.jpg"
        photo.write_bytes(b"photo")

        class InvalidAckBridge(FakeBridge):
            def __call__(self, request, *, timeout):
                if urllib_parse.urlsplit(request.full_url).path == "/ack":
                    self.requests.append((request, timeout))
                    return Response({"ok": False, "acked": 0})
                return super().__call__(request, timeout=timeout)

        bridge = InvalidAckBridge(
            [
                message(
                    "photo-ack-failed",
                    body="caption",
                    hasMedia=True,
                    mediaType="photo",
                    mime="image/jpeg",
                    mediaUrls=[str(photo)],
                )
            ]
        )
        with MirrorLedger(self.root / "ledger.sqlite3") as ledger:
            self.route(ledger)
            result = self.observer(ledger, bridge).observe_once()
            self.assertEqual(1, result.ack_failed)
            self.assertEqual(0, result.source_media_cleaned)
            self.assertTrue(photo.is_file())

    def test_sticker_and_gif_placeholders_are_not_mirrored_as_captions(self) -> None:
        sticker = self.source / "sticker.webp"
        gif = self.source / "clip.mp4"
        sticker.write_bytes(b"sticker")
        gif.write_bytes(b"gif")
        bridge = FakeBridge(
            [
                message(
                    "sticker",
                    body="[Sticker]",
                    hasMedia=True,
                    mediaType="sticker",
                    mediaUrls=[str(sticker)],
                ),
                message(
                    "gif",
                    body="[gif received]",
                    hasMedia=True,
                    mediaType="gif",
                    mediaUrls=[str(gif)],
                ),
            ]
        )
        with MirrorLedger(self.root / "ledger.sqlite3") as ledger:
            self.route(ledger)
            result = self.observer(ledger, bridge).observe_once()
            self.assertEqual((2, 2), (result.inserted, result.acked))
            rows = ledger.connection.execute(
                "SELECT payload_json FROM mirror_events"
            ).fetchall()
            payloads = [json.loads(str(row["payload_json"])) for row in rows]
            self.assertTrue(all(not item["text"] for item in payloads))
            self.assertTrue(all(not item["media"][0]["caption"] for item in payloads))

    def test_route_missing_is_durable_before_ack(self) -> None:
        db_path = self.root / "ledger.sqlite3"

        def assert_durable(payload):
            self.assertEqual(["held"], payload["messageIds"])
            uri = db_path.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as proof:
                self.assertEqual(
                    1,
                    proof.execute("SELECT COUNT(*) FROM mirror_events").fetchone()[0],
                )
                self.assertEqual(
                    1,
                    proof.execute(
                        "SELECT COUNT(*) FROM mirror_route_blocks WHERE state='blocked_no_route'"
                    ).fetchone()[0],
                )

        bridge = FakeBridge([message("held")], before_ack=assert_durable)
        with MirrorLedger(db_path) as ledger:
            result = self.observer(ledger, bridge).observe_once()
            self.assertEqual(1, result.inserted)
            self.assertEqual(1, result.blocked_no_route)
            self.assertEqual(1, result.acked)
            self.assertEqual(0, result.enqueued)

    def test_lid_primary_uses_only_explicit_alt_alias_route(self) -> None:
        profile_ref = opaque_ref("profile", self.profile)
        canonical = opaque_ref(
            "conversation", f"{profile_ref}\x1fdigits-canonical"
        )
        with MirrorLedger(self.root / "ledger.sqlite3") as ledger:
            ledger.set_route(Route(canonical, "-100123", "42"))
            alternate = opaque_ref(
                "conversation", f"{profile_ref}\x1fcontact@s.whatsapp.net"
            )
            ledger.set_conversation_alias(alternate, canonical)
            bridge = FakeBridge(
                [
                    message(
                        "lid-message",
                        chatId="contact@lid",
                        chatIdAlt="contact@s.whatsapp.net",
                    )
                ]
            )
            result = self.observer(ledger, bridge).observe_once()
            self.assertEqual((1, 1, 0), (
                result.inserted,
                result.enqueued,
                result.blocked_no_route,
            ))
            stored = ledger.connection.execute(
                "SELECT event_id FROM mirror_events"
            ).fetchone()
            event = ledger.load_event(str(stored["event_id"]))
            self.assertEqual(canonical, event.conversation_id)
            expected_event = canonical_whatsapp_event_ref(
                profile_ref, canonical, "lid-message"
            )
            self.assertEqual(expected_event, event.event_id)

    def test_flipped_explicit_lid_and_phone_aliases_dedupe_to_one_event(self) -> None:
        profile_ref = opaque_ref("profile", self.profile)
        canonical = opaque_ref(
            "conversation", f"{profile_ref}\x1fdigits-canonical"
        )
        lid = opaque_ref(
            "conversation", f"{profile_ref}\x1fcontact@lid"
        )
        phone = opaque_ref(
            "conversation", f"{profile_ref}\x1fcontact@s.whatsapp.net"
        )
        with MirrorLedger(self.root / "ledger.sqlite3") as ledger:
            ledger.set_route(Route(canonical, "-100123", "42"))
            ledger.set_conversation_alias(lid, canonical)
            ledger.set_conversation_alias(phone, canonical)
            first = self.observer(
                ledger,
                FakeBridge(
                    [
                        message(
                            "same-message",
                            chatId="contact@lid",
                            chatIdAlt="contact@s.whatsapp.net",
                        )
                    ]
                ),
            ).observe_once()
            second = self.observer(
                ledger,
                FakeBridge(
                    [
                        message(
                            "same-message",
                            chatId="contact@s.whatsapp.net",
                            chatIdAlt="contact@lid",
                        )
                    ]
                ),
            ).observe_once()
            self.assertEqual((1, 0), (first.inserted, first.duplicates))
            self.assertEqual((0, 1), (second.inserted, second.duplicates))
            self.assertEqual(
                1,
                ledger.connection.execute(
                    "SELECT COUNT(*) FROM mirror_events"
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                ledger.connection.execute(
                    "SELECT COUNT(*) FROM mirror_deliveries"
                ).fetchone()[0],
            )


if __name__ == "__main__":
    unittest.main()
