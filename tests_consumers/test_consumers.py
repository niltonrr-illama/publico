from __future__ import annotations

import json
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

import espelho_zap.consumers as consumer_module  # noqa: E402
import espelho_zap.cli as cli  # noqa: E402
from espelho_zap.consumers import (  # noqa: E402
    MirrorConsumers,
    PrivacyScopeError,
)
from espelho_zap.ledger import MirrorLedger, RouteMissingError  # noqa: E402
from espelho_zap.models import (  # noqa: E402
    PRIVACY_SCOPES,
    InboundEvent,
    MediaAttachment,
    Route,
    opaque_ref,
)


CONVERSATION_A = opaque_ref("conversation", "consumer-a")
CONVERSATION_B = opaque_ref("conversation", "consumer-b")
ACTOR_A = opaque_ref("actor", "actor-a")
ACTOR_B = opaque_ref("actor", "actor-b")


def inbound(
    event_id: str,
    *,
    text: str,
    scope: str = "area_shared",
    conversation: str = CONVERSATION_A,
    actor: str = ACTOR_A,
    source: str = "hermes-whatsapp",
    occurred_at: str = "2026-08-04T12:00:00Z",
    media: tuple[MediaAttachment, ...] = (),
) -> InboundEvent:
    return InboundEvent(
        event_id=event_id,
        source=source,
        conversation_id=conversation,
        occurred_at=occurred_at,
        actor_ref=actor,
        privacy_scope=scope,
        text=text,
        media=media,
    )


class ConsumerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ledger = MirrorLedger(self.root / "mirror.sqlite3")
        self.consumers = MirrorConsumers(self.ledger)

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    def record(self, *events: InboundEvent) -> None:
        for event in events:
            self.assertTrue(self.ledger.record_event(event))


class ConsumerCLITest(unittest.TestCase):
    def _config(self, root: Path) -> Path:
        source = root / "source"
        source.mkdir()
        config = root / "config.toml"
        config.write_text(
            f'''schema_version = 1
[paths]
data_dir = "{(root / 'data').as_posix()}"
state_dir = "{(root / 'state').as_posix()}"
ledger_path = "{(root / 'data' / 'mirror.sqlite3').as_posix()}"
minimum_free_bytes = 1
[telegram]
api_base = "https://api.telegram.org"
token_env = "TEST_TOKEN"
token_file = "{(root / 'token').as_posix()}"
timeout_seconds = 1
[worker]
profile_id = "test"
runtime_lock_seconds = 120
lease_seconds = 60
max_attempts = 2
base_backoff_seconds = 1
allowed_temp_root = "{(root / 'data' / 'media').as_posix()}"
source_media_roots = ["{source.as_posix()}"]
[legacy]
default_chat_id = ""
''',
            encoding="utf-8",
        )
        return config

    def _cli(self, *args: str) -> tuple[int, dict[str, object], str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(list(args))
        raw = output.getvalue()
        return code, json.loads(raw), raw

    def test_consumer_commands_are_executable_and_content_free_on_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            with MirrorLedger(root / "data" / "mirror.sqlite3") as ledger:
                ledger.record_event(inbound("shared", text="conteudo confidencial"))

            code, result, raw = self._cli(
                "--config", str(config), "consumer", "daily-notes",
                str(root / "notes"), "--scope", "area_shared",
            )
            self.assertEqual(0, code)
            self.assertEqual(1, result["result"]["processed_events"])
            self.assertNotIn("conteudo confidencial", raw)

            claim_text = root / "claim.txt"
            claim_text.write_text("decisao confidencial", encoding="utf-8")
            code, result, raw = self._cli(
                "--config", str(config), "consumer", "claim-add", str(claim_text),
                "--scope", "area_shared", "--evidence", "shared",
            )
            self.assertEqual(0, code)
            self.assertTrue(result["result"]["created"])
            self.assertNotIn("decisao confidencial", raw)

            code, result, raw = self._cli(
                "--config", str(config), "consumer", "search-export",
                str(root / "search.jsonl"), "--scope", "area_shared",
            )
            self.assertEqual(0, code)
            self.assertEqual(1, result["result"]["event_documents"])
            self.assertEqual(1, result["result"]["claim_documents"])
            self.assertNotIn("conteudo confidencial", raw)

            code, result, raw = self._cli(
                "--config", str(config), "consumer", "report",
                "--scope", "area_shared",
            )
            self.assertEqual(0, code)
            self.assertEqual(1, result["result"]["events"])
            self.assertNotIn("conteudo confidencial", raw)


class DailyNotesTest(ConsumerTestCase):
    def test_bounded_idempotent_projection_and_independent_cursors(self) -> None:
        self.record(
            inbound("a-1", text="primeiro", occurred_at="2026-08-04T08:00:00Z"),
            inbound("a-2", text="segundo", occurred_at="2026-08-04T09:00:00Z"),
            inbound("a-3", text="terceiro", occurred_at="2026-08-04T10:00:00Z"),
            inbound(
                "p-1", text="privado", scope="owner_private",
                occurred_at="2026-08-04T11:00:00Z"
            ),
        )
        first = self.consumers.project_daily_notes(
            self.root / "notes-a",
            consumer_id="notes-a",
            allowed_scopes=["area_shared"],
            batch_limit=2,
            now=100,
        )
        self.assertEqual(2, first.processed_events)
        first_cursor = first.cursors["area_shared"]
        self.assertGreater(first_cursor, 0)
        self.assertEqual(0, self.consumers.get_cursor("notes-a", "owner_private"))

        independent = self.consumers.project_daily_notes(
            self.root / "notes-b",
            consumer_id="notes-b",
            allowed_scopes=["area_shared"],
            batch_limit=10,
            now=101,
        )
        self.assertEqual(3, independent.processed_events)
        self.assertGreater(independent.cursors["area_shared"], first_cursor)

        second = self.consumers.project_daily_notes(
            self.root / "notes-a",
            consumer_id="notes-a",
            allowed_scopes=["area_shared"],
            batch_limit=2,
            now=102,
        )
        self.assertEqual(1, second.processed_events)
        note = self.root / "notes-a" / "area_shared" / "2026-08-04.md"
        before = note.read_bytes()
        self.assertEqual(3, before.count(b"## "))
        self.assertIn("primeiro", before.decode("utf-8"))
        self.assertNotIn("privado", before.decode("utf-8"))

        third = self.consumers.project_daily_notes(
            self.root / "notes-a",
            consumer_id="notes-a",
            allowed_scopes=["area_shared"],
            batch_limit=2,
            now=103,
        )
        self.assertEqual(0, third.processed_events)
        self.assertEqual(before, note.read_bytes())

    def test_atomic_replace_failure_keeps_old_file_and_cursor_then_recovers(self) -> None:
        self.record(inbound("a-1", text="preservado"))
        first = self.consumers.project_daily_notes(
            self.root / "notes",
            consumer_id="notes",
            allowed_scopes=["area_shared"],
            batch_limit=10,
        )
        note = first.files[0]
        old_bytes = note.read_bytes()
        old_cursor = first.cursors["area_shared"]
        self.record(inbound("a-2", text="novo", occurred_at="2026-08-04T13:00:00Z"))

        with mock.patch.object(consumer_module.os, "replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.consumers.project_daily_notes(
                    self.root / "notes",
                    consumer_id="notes",
                    allowed_scopes=["area_shared"],
                    batch_limit=10,
                )
        self.assertEqual(old_bytes, note.read_bytes())
        self.assertEqual(old_cursor, self.consumers.get_cursor("notes", "area_shared"))

        recovered = self.consumers.project_daily_notes(
            self.root / "notes",
            consumer_id="notes",
            allowed_scopes=["area_shared"],
            batch_limit=10,
        )
        self.assertEqual(1, recovered.processed_events)
        self.assertIn("novo", note.read_text(encoding="utf-8"))

    def test_consumer_failure_does_not_block_capture_or_another_consumer(self) -> None:
        self.record(inbound("a-1", text="antes"))
        with mock.patch.object(consumer_module, "_atomic_write", side_effect=OSError("full")):
            with self.assertRaises(OSError):
                self.consumers.project_daily_notes(
                    self.root / "broken",
                    consumer_id="broken-notes",
                    allowed_scopes=["area_shared"],
                )
        self.assertEqual(0, self.consumers.get_cursor("broken-notes", "area_shared"))

        # Capture remains available after the optional projection fails.
        self.assertTrue(self.ledger.record_event(inbound("a-2", text="depois")))
        healthy = self.consumers.project_daily_notes(
            self.root / "healthy",
            consumer_id="healthy-notes",
            allowed_scopes=["area_shared"],
        )
        self.assertEqual(2, healthy.processed_events)
        self.assertEqual(2, self.ledger.health()["events"])

    def test_multiple_conversations_and_adapter_sources_share_one_day_without_merging_identity(self) -> None:
        self.record(
            inbound("a", text="Hermes", source="hermes-whatsapp"),
            inbound(
                "b",
                text="OpenClaw",
                source="openclaw-whatsapp",
                conversation=CONVERSATION_B,
                actor=ACTOR_B,
                occurred_at="2026-08-04T12:01:00Z",
            ),
        )
        result = self.consumers.project_daily_notes(
            self.root / "notes",
            consumer_id="multichannel-notes",
            allowed_scopes=["area_shared"],
        )
        text = result.files[0].read_text(encoding="utf-8")
        self.assertIn("hermes-whatsapp", text)
        self.assertIn("openclaw-whatsapp", text)
        self.assertIn(CONVERSATION_A, text)
        self.assertIn(CONVERSATION_B, text)

    def test_late_captured_event_is_added_to_its_original_day_without_cursor_loss(self) -> None:
        self.record(
            inbound(
                "newer-day", text="dia cinco", occurred_at="2026-08-05T08:00:00Z"
            )
        )
        first = self.consumers.project_daily_notes(
            self.root / "notes",
            consumer_id="late-notes",
            allowed_scopes=["area_shared"],
        )
        self.assertEqual(1, first.processed_events)
        self.record(
            inbound(
                "late-old-day", text="chegou atrasado", occurred_at="2026-08-04T23:59:00Z"
            )
        )
        second = self.consumers.project_daily_notes(
            self.root / "notes",
            consumer_id="late-notes",
            allowed_scopes=["area_shared"],
        )
        self.assertEqual(1, second.processed_events)
        self.assertIn(
            "chegou atrasado",
            (self.root / "notes" / "area_shared" / "2026-08-04.md").read_text(
                encoding="utf-8"
            ),
        )


class ClaimTest(ConsumerTestCase):
    def test_claim_evidence_privacy_non_downgrade_idempotence_and_supersession(self) -> None:
        self.record(
            inbound("shared", text="evidência compartilhada"),
            inbound("private", text="evidência privada", scope="owner_private"),
        )
        with self.assertRaises(PrivacyScopeError):
            self.consumers.add_claim(
                "não pode vazar", ["private"], privacy_scope="area_shared", now=100
            )
        self.assertEqual(
            0, self.ledger.connection.execute("SELECT COUNT(*) FROM mirror_claims").fetchone()[0]
        )

        old, created = self.consumers.add_claim(
            "hipótese inicial", ["shared"], privacy_scope="area_shared", now=101
        )
        self.assertTrue(created)
        duplicate, created = self.consumers.add_claim(
            "hipótese inicial", ["shared"], privacy_scope="area_shared", now=999
        )
        self.assertFalse(created)
        self.assertEqual(old.claim_id, duplicate.claim_id)

        replacement, created = self.consumers.add_claim(
            "decisão final",
            ["private"],
            privacy_scope="owner_private",
            supersedes=[old.claim_id],
            now=102,
        )
        self.assertTrue(created)
        self.assertEqual((old.claim_id,), replacement.supersedes)
        self.assertFalse(self.consumers.get_claim(old.claim_id).active)
        self.assertTrue(self.consumers.get_claim(replacement.claim_id).active)

        with self.assertRaises(PrivacyScopeError):
            self.consumers.add_claim(
                "tentativa de rebaixar",
                ["shared"],
                privacy_scope="area_shared",
                supersedes=[replacement.claim_id],
            )
        with self.assertRaisesRegex(ValueError, "invalid_claim_evidence"):
            self.consumers.add_claim(
                "entrada inválida", [None], privacy_scope="owner_private"  # type: ignore[list-item]
            )

    def test_claim_and_evidence_rows_are_immutable(self) -> None:
        self.record(inbound("shared", text="fonte"))
        claim, _ = self.consumers.add_claim(
            "claim", ["shared"], privacy_scope="area_shared"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.connection.execute(
                "UPDATE mirror_claims SET claim_text = 'mutado' WHERE claim_id = ?",
                (claim.claim_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.connection.execute(
                "DELETE FROM mirror_claim_evidence WHERE claim_id = ?", (claim.claim_id,)
            )


class SearchAndReportTest(ConsumerTestCase):
    def test_search_export_is_deterministic_scope_bounded_and_provider_agnostic(self) -> None:
        secret_path = self.root / "never-export-this.jpg"
        self.record(
            inbound("shared", text="conteúdo permitido"),
            inbound(
                "private",
                text="segredo proibido",
                scope="owner_private",
                media=(
                    MediaAttachment(
                        media_id="m1",
                        kind="image",
                        path=str(secret_path),
                        sha256="a" * 64,
                        size_bytes=10,
                        caption="legenda privada",
                    ),
                ),
            ),
        )
        self.consumers.add_claim(
            "claim permitido", ["shared"], privacy_scope="area_shared", now=100
        )
        self.consumers.add_claim(
            "claim secreto", ["private"], privacy_scope="owner_private", now=101
        )

        os.environ["GBRAIN_URL"] = "unavailable://must-not-be-used"
        try:
            first = self.consumers.export_search_projection(
                self.root / "search-a.jsonl", allowed_scopes=["area_shared"]
            )
            second = self.consumers.export_search_projection(
                self.root / "search-b.jsonl", allowed_scopes=["area_shared"]
            )
        finally:
            os.environ.pop("GBRAIN_URL", None)
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual((1, 1), (first.event_documents, first.claim_documents))
        self.assertEqual(first.path.read_bytes(), second.path.read_bytes())
        raw = first.path.read_text(encoding="utf-8")
        self.assertIn("conteúdo permitido", raw)
        self.assertIn("claim permitido", raw)
        self.assertNotIn("segredo proibido", raw)
        self.assertNotIn("claim secreto", raw)
        self.assertNotIn(str(secret_path), raw)
        with self.assertRaisesRegex(ValueError, "allowed_scopes_required"):
            self.consumers.export_search_projection(
                self.root / "none.jsonl", allowed_scopes=[]
            )

    def test_reports_are_aggregated_and_content_free_by_default(self) -> None:
        self.record(
            inbound("pending", text="não expor este texto"),
            inbound(
                "blocked",
                text="nem este",
                scope="owner_private",
                conversation=CONVERSATION_B,
            ),
        )
        self.ledger.set_route(Route(CONVERSATION_A, "-100123", "42"))
        self.ledger.enqueue("pending", now=100)
        with self.assertRaises(RouteMissingError):
            self.ledger.enqueue("blocked", now=100)

        aggregate = self.consumers.aggregate_report(allowed_scopes=PRIVACY_SCOPES)
        serialized = json.dumps(aggregate, ensure_ascii=False)
        self.assertEqual(2, aggregate["events"])
        self.assertEqual(2, aggregate["conversations"])
        self.assertNotIn("não expor", serialized)
        self.assertNotIn("nem este", serialized)

        pending = self.consumers.pending_report(allowed_scopes=PRIVACY_SCOPES)
        serialized = json.dumps(pending, ensure_ascii=False)
        self.assertFalse(pending["include_content"])
        self.assertNotIn("não expor", serialized)
        self.assertNotIn("nem este", serialized)
        self.assertTrue(all("text" not in item for item in pending["items"]))

        with_content = self.consumers.pending_report(
            allowed_scopes=PRIVACY_SCOPES, include_content=True
        )
        serialized = json.dumps(with_content, ensure_ascii=False)
        self.assertIn("não expor este texto", serialized)
        self.assertIn("nem este", serialized)

    def test_consumer_schema_is_namespaced_and_does_not_touch_user_version(self) -> None:
        self.ledger.connection.execute("PRAGMA user_version = 27")
        # Re-opening the projection object is an idempotent migration.
        MirrorConsumers(self.ledger)
        self.assertEqual(
            27, self.ledger.connection.execute("PRAGMA user_version").fetchone()[0]
        )
        tables = {
            str(row[0])
            for row in self.ledger.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertIn("mirror_consumer_cursors", tables)
        self.assertIn("mirror_claims", tables)
        self.assertFalse(any(name.startswith("gbrain_") for name in tables))


if __name__ == "__main__":
    unittest.main()
