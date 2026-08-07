from __future__ import annotations

import sqlite3
import os
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from espelho_zap.ledger import (  # noqa: E402
    EventConflictError,
    LedgerError,
    MirrorLedger,
    RouteMissingError,
)
from espelho_zap.models import InboundEvent, MediaAttachment, Route, opaque_ref  # noqa: E402


CONVERSATION = opaque_ref("conversation", "synthetic-conversation-a")
ACTOR = opaque_ref("actor", "synthetic-actor-a")


def event(event_id: str = "evt-1", *, text: str = "texto original") -> InboundEvent:
    return InboundEvent(
        event_id=event_id,
        source="whatsapp",
        conversation_id=CONVERSATION,
        occurred_at="2026-08-04T12:00:00Z",
        actor_ref=ACTOR,
        privacy_scope="area_shared",
        text=text,
    )


class ModelTest(unittest.TestCase):
    def test_domain_models_are_immutable(self) -> None:
        inbound = event()
        with self.assertRaises(FrozenInstanceError):
            inbound.text = "mutated"  # type: ignore[misc]

    def test_telegram_identifiers_are_distinct_strings_and_topic_is_required(self) -> None:
        route = Route(CONVERSATION, "-100123456", "42")
        self.assertEqual("-100123456", route.chat_id)
        self.assertEqual("42", route.thread_id)
        with self.assertRaisesRegex(ValueError, "invalid_chat_id"):
            Route(CONVERSATION, -100123456, "42")  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "group_chat_required"):
            Route(CONVERSATION, "123456", "42")
        with self.assertRaisesRegex(ValueError, "topic_required"):
            Route(CONVERSATION, "-100123456", "0")

    def test_media_hash_and_original_caption_are_part_of_payload_identity(self) -> None:
        media = MediaAttachment(
            media_id="media-a",
            kind="image",
            path="C:/private/temp/photo.jpg",
            sha256="a" * 64,
            size_bytes=12,
            caption="legenda original",
            managed_temp=True,
        )
        first = InboundEvent(
            "evt-media", "whatsapp", CONVERSATION,
            "2026-08-04T12:00:00Z", ACTOR, media=(media,)
        )
        moved = MediaAttachment(
            media_id="media-a",
            kind="image",
            path="D:/another/private/path.jpg",
            sha256="a" * 64,
            size_bytes=12,
            caption="legenda original",
            managed_temp=False,
        )
        second = InboundEvent(
            "evt-media", "whatsapp", CONVERSATION,
            "2026-08-04T12:00:00Z", ACTOR, media=(moved,)
        )
        self.assertEqual(first.payload_hash(), second.payload_hash())

    def test_privacy_scope_and_event_schema_are_versioned(self) -> None:
        inbound = event()
        self.assertEqual("area_shared", inbound.privacy_scope)
        self.assertEqual(3, inbound.schema_version)
        self.assertIn('"schema_version":3', inbound.storage_json())
        self.assertIn('"source_profile_id":"profile:', inbound.storage_json())
        with self.assertRaisesRegex(ValueError, "invalid_privacy_scope"):
            InboundEvent(
                "evt", "whatsapp", CONVERSATION, "2026-08-04T12:00:00Z",
                ACTOR, privacy_scope="public_guess", text="x"
            )

    def test_conversation_and_actor_references_must_be_opaque(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid_conversation_id"):
            InboundEvent(
                "evt", "whatsapp", "+15550000000", "2026-08-04T12:00:00Z",
                ACTOR, text="x"
            )
        with self.assertRaisesRegex(ValueError, "invalid_actor_ref"):
            InboundEvent(
                "evt", "whatsapp", CONVERSATION, "2026-08-04T12:00:00Z",
                "Person Name", text="x"
            )
        with self.assertRaisesRegex(ValueError, "unsupported_event_schema"):
            InboundEvent(
                "evt", "whatsapp", CONVERSATION, "2026-08-04T12:00:00Z",
                ACTOR, text="x", schema_version=4
            )


class LedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = MirrorLedger(Path(self.temp.name) / "mirror.sqlite3")

    def tearDown(self) -> None:
        self.db.close()
        self.temp.cleanup()

    def add_route(self) -> Route:
        route = Route(CONVERSATION, "-100123456", "42")
        self.db.set_route(route)
        return route

    def test_event_duplicate_is_idempotent_but_payload_conflict_fails(self) -> None:
        self.assertTrue(self.db.record_event(event()))
        self.assertFalse(self.db.record_event(event()))
        with self.assertRaises(EventConflictError):
            self.db.record_event(event(text="payload diferente"))

    def test_event_rows_are_immutable_at_database_level(self) -> None:
        self.db.record_event(event())
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.connection.execute(
                "UPDATE mirror_events SET source = 'changed' WHERE event_id = 'evt-1'"
            )

    def test_missing_route_fails_closed_and_never_creates_dm_delivery(self) -> None:
        self.db.record_event(event())
        with self.assertRaises(RouteMissingError):
            self.db.enqueue("evt-1")
        count = self.db.connection.execute("SELECT COUNT(*) FROM mirror_deliveries").fetchone()[0]
        self.assertEqual(0, count)
        self.assertEqual(1, self.db.health()["blocked_no_route"])

    def test_route_later_requires_explicit_reconcile(self) -> None:
        self.db.record_event(event())
        with self.assertRaises(RouteMissingError):
            self.db.enqueue("evt-1", now=100)
        self.add_route()
        with self.assertRaisesRegex(RouteMissingError, "route_blocked_requires_reconcile"):
            self.db.enqueue("evt-1", now=101)
        self.assertEqual(0, self.db.connection.execute(
            "SELECT COUNT(*) FROM mirror_deliveries"
        ).fetchone()[0])
        self.assertEqual(1, self.db.reconcile_route_blocks(CONVERSATION, now=102))
        self.assertEqual(1, self.db.connection.execute(
            "SELECT COUNT(*) FROM mirror_deliveries"
        ).fetchone()[0])
        self.assertEqual(0, self.db.health()["blocked_no_route"])

    def test_delivery_claim_preserves_exact_forum_topic(self) -> None:
        route = self.add_route()
        self.db.record_event(event())
        delivery_id = self.db.enqueue("evt-1", now=100)
        claim = self.db.claim_next("worker-a", now=100, lease_seconds=30)
        self.assertIsNotNone(claim)
        assert claim is not None
        self.assertEqual(delivery_id, claim.delivery_id)
        self.assertEqual(route.chat_id, claim.route.chat_id)
        self.assertEqual(route.thread_id, claim.route.thread_id)

    def test_duplicate_enqueue_has_one_logical_delivery(self) -> None:
        self.add_route()
        self.db.record_event(event())
        first = self.db.enqueue("evt-1")
        second = self.db.enqueue("evt-1")
        self.assertEqual(first, second)
        self.assertEqual(
            1, self.db.connection.execute("SELECT COUNT(*) FROM mirror_deliveries").fetchone()[0]
        )

    def test_global_wip_one_and_expired_lease_becomes_uncertain(self) -> None:
        self.add_route()
        for event_id in ("evt-1", "evt-2"):
            self.db.record_event(event(event_id))
            self.db.enqueue(event_id, now=100)
        first = self.db.claim_next("worker-a", now=100, lease_seconds=10)
        self.assertIsNotNone(first)
        self.assertIsNone(self.db.claim_next("worker-b", now=105, lease_seconds=10))
        next_claim = self.db.claim_next("worker-b", now=111, lease_seconds=10)
        self.assertIsNotNone(next_claim)
        assert first is not None and next_claim is not None
        self.assertNotEqual(first.delivery_id, next_claim.delivery_id)
        self.assertEqual("uncertain", self.db.delivery_state("evt-1"))

    def test_legacy_recent_id_is_a_no_replay_tombstone(self) -> None:
        self.add_route()
        self.assertEqual(
            1,
            self.db.add_legacy_delivered_ids(CONVERSATION, ["evt-old"]),
        )
        self.db.record_event(event("evt-old"))
        self.assertIsNone(self.db.enqueue("evt-old"))
        self.assertIsNone(self.db.delivery_state("evt-old"))

    def test_legacy_tombstone_is_scoped_to_conversation(self) -> None:
        other_conversation = opaque_ref("conversation", "synthetic-conversation-b")
        self.db.add_legacy_delivered_ids(CONVERSATION, ["shared-raw-id"])
        self.assertTrue(self.db.is_legacy_delivered(CONVERSATION, "shared-raw-id"))
        self.assertFalse(self.db.is_legacy_delivered(other_conversation, "shared-raw-id"))
        self.db.set_route(Route(other_conversation, "-100123456", "84"))
        other = InboundEvent(
            event_id="shared-raw-id",
            source="whatsapp",
            conversation_id=other_conversation,
            occurred_at="2026-08-04T12:00:00Z",
            actor_ref=ACTOR,
            privacy_scope="area_shared",
            text="texto da outra conversa",
        )
        self.db.record_event(other)
        self.assertIsNotNone(self.db.enqueue(other.event_id))

    def test_route_change_blocks_old_pending_target(self) -> None:
        self.add_route()
        self.db.record_event(event())
        self.db.enqueue("evt-1", now=100)
        self.db.set_route(
            Route(CONVERSATION, "-100123456", "84"),
            allow_update=True,
            now=101,
        )
        self.assertIsNone(self.db.claim_next("worker-a", now=102))
        self.assertEqual("blocked", self.db.delivery_state("evt-1"))
        self.assertEqual(
            1,
            self.db.rebind_route_changed(
                CONVERSATION,
                evidence_ref="operator-confirmed-new-topic",
                now=103,
            ),
        )
        self.assertEqual("pending", self.db.delivery_state("evt-1"))
        row = self.db.connection.execute(
            """SELECT old_state, new_state, resolution
               FROM mirror_delivery_reconciliation_audit"""
        ).fetchone()
        self.assertEqual(("blocked", "pending", "route_rebind"), tuple(row))

    def test_uncertain_retry_requires_evidence_and_is_audited(self) -> None:
        self.add_route()
        self.db.record_event(event())
        self.db.enqueue("evt-1", now=100)
        claim = self.db.claim_next("worker-a", now=100)
        assert claim is not None
        self.db.mark_uncertain(claim, now=101)
        self.assertTrue(
            self.db.reconcile_uncertain(
                "evt-1", resolution="retry", evidence_ref="telegram-not-delivered", now=102
            )
        )
        self.assertEqual("retry", self.db.delivery_state("evt-1"))
        row = self.db.connection.execute(
            """SELECT old_state, new_state, resolution
               FROM mirror_delivery_reconciliation_audit"""
        ).fetchone()
        self.assertEqual(("uncertain", "retry", "uncertain_retry"), tuple(row))

    def test_uncertain_mark_sent_is_audited(self) -> None:
        self.add_route()
        self.db.record_event(event())
        self.db.enqueue("evt-1", now=100)
        claim = self.db.claim_next("worker-a", now=100)
        assert claim is not None
        self.db.mark_uncertain(claim, now=101)
        self.assertTrue(
            self.db.reconcile_uncertain(
                "evt-1", resolution="sent", evidence_ref="telegram-visible-once", now=102
            )
        )
        self.assertEqual("sent", self.db.delivery_state("evt-1"))
        row = self.db.connection.execute(
            "SELECT resolution FROM mirror_delivery_reconciliation_audit"
        ).fetchone()
        self.assertEqual("uncertain_mark_sent", row["resolution"])

    def test_uncertain_reconciliation_is_single_winner_across_connections(self) -> None:
        self.add_route()
        self.db.record_event(event())
        self.db.enqueue("evt-1", now=100)
        claim = self.db.claim_next("worker-a", now=100)
        assert claim is not None
        self.db.mark_uncertain(claim, now=101)
        second = MirrorLedger(self.db.db_path)
        try:
            self.assertTrue(
                self.db.reconcile_uncertain(
                    "evt-1", resolution="retry", evidence_ref="operator-a", now=102
                )
            )
            with self.assertRaisesRegex(LedgerError, "uncertain_delivery_missing"):
                second.reconcile_uncertain(
                    "evt-1", resolution="sent", evidence_ref="operator-b", now=103
                )
            audit_count = second.connection.execute(
                "SELECT COUNT(*) FROM mirror_delivery_reconciliation_audit"
            ).fetchone()[0]
            self.assertEqual(1, audit_count)
        finally:
            second.close()

    def test_route_rebind_second_operator_is_noop_without_false_audit(self) -> None:
        self.add_route()
        self.db.record_event(event())
        self.db.enqueue("evt-1", now=100)
        self.db.set_route(
            Route(CONVERSATION, "-100123456", "84"),
            allow_update=True,
            now=101,
        )
        self.assertIsNone(self.db.claim_next("worker-a", now=102))
        second = MirrorLedger(self.db.db_path)
        try:
            self.assertEqual(
                1,
                self.db.rebind_route_changed(
                    CONVERSATION, evidence_ref="operator-a", now=103
                ),
            )
            self.assertEqual(
                0,
                second.rebind_route_changed(
                    CONVERSATION, evidence_ref="operator-b", now=104
                ),
            )
            audit_count = second.connection.execute(
                "SELECT COUNT(*) FROM mirror_delivery_reconciliation_audit"
            ).fetchone()[0]
            self.assertEqual(1, audit_count)
        finally:
            second.close()

    def test_schema_is_versioned_and_quick_check_is_ok(self) -> None:
        health = self.db.health()
        self.assertEqual(10, health["schema_version"])
        self.assertEqual("ok", health["quick_check"])

    def test_source_quarantine_is_idempotent_private_evidence_and_immutable(self) -> None:
        payload = {"messageId": "bad-1", "body": "preserved raw context"}
        self.assertTrue(
            self.db.quarantine_source_item(
                adapter_id="adapter/v1",
                source_item_id="bad-1",
                payload=payload,
                error_code="invalid_source_record",
                privacy_scope="owner_private",
                now=100,
            )
        )
        self.assertFalse(
            self.db.quarantine_source_item(
                adapter_id="adapter/v1",
                source_item_id="bad-1",
                payload=payload,
                error_code="invalid_source_record",
                privacy_scope="owner_private",
                now=101,
            )
        )
        row = self.db.connection.execute(
            "SELECT payload_json FROM mirror_source_quarantine"
        ).fetchone()
        self.assertIn("preserved raw context", str(row["payload_json"]))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable_source_quarantine"):
            self.db.connection.execute(
                "UPDATE mirror_source_quarantine SET error_code = 'changed'"
            )
        self.assertEqual(1, self.db.health()["source_quarantine"])

    def test_runtime_lock_enforces_single_writer_per_profile(self) -> None:
        self.assertTrue(
            self.db.acquire_runtime_lock("profile-a", "instance-a", now=100, lease_seconds=10)
        )
        self.assertFalse(
            self.db.acquire_runtime_lock("profile-a", "instance-b", now=105, lease_seconds=10)
        )
        self.assertTrue(
            self.db.acquire_runtime_lock("profile-a", "instance-b", now=111, lease_seconds=10)
        )
        self.assertFalse(self.db.release_runtime_lock("profile-a", "instance-a"))
        self.assertTrue(self.db.release_runtime_lock("profile-a", "instance-b"))

    @unittest.skipIf(os.name == "nt", "POSIX mode bits are not authoritative on Windows")
    def test_database_and_wal_sidecars_are_private(self) -> None:
        self.db.record_event(event())
        for path in (
            self.db.db_path,
            Path(str(self.db.db_path) + "-wal"),
            Path(str(self.db.db_path) + "-shm"),
        ):
            if path.exists():
                self.assertEqual(0o600, path.stat().st_mode & 0o777)


class BrownfieldCoexistenceTest(unittest.TestCase):
    def test_existing_v2_schema_and_user_version_are_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "shared.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE events(event_id TEXT PRIMARY KEY, body TEXT NOT NULL)"
            )
            connection.execute("INSERT INTO events VALUES ('v2-event', 'v2-body')")
            connection.execute("PRAGMA user_version = 5")
            connection.commit()
            connection.close()
            with MirrorLedger(path) as mirror:
                mirror.set_route(Route(CONVERSATION, "-100123", "42"))
                mirror.record_event(event())
                self.assertEqual(10, mirror.health()["schema_version"])
                self.assertEqual(5, mirror.connection.execute("PRAGMA user_version").fetchone()[0])
                row = mirror.connection.execute(
                    "SELECT event_id, body FROM events"
                ).fetchone()
                self.assertEqual(("v2-event", "v2-body"), tuple(row))


if __name__ == "__main__":
    unittest.main()
