from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from espelho_zap import (  # noqa: E402
    InboundEvent,
    MediaAttachment,
    MirrorLedger,
    MirrorWorker,
    RecordingTransport,
    Route,
    opaque_ref,
    stage_event_media,
)
from espelho_zap import cli  # noqa: E402
from espelho_zap.media import MediaSpoolError, remove_orphaned_spool_files  # noqa: E402


CONVERSATION = opaque_ref("conversation", "media-conversation")
ACTOR = opaque_ref("actor", "media-actor")


def event(path: Path, *, event_id: str = "media-event", digest: str = "") -> InboundEvent:
    return InboundEvent(
        event_id=event_id,
        source="whatsapp",
        conversation_id=CONVERSATION,
        occurred_at="2026-08-04T12:00:00Z",
        actor_ref=ACTOR,
        text="",
        media=(
            MediaAttachment(
                "media-1",
                "image",
                str(path),
                mime_type="image/jpeg",
                sha256=digest,
                size_bytes=path.stat().st_size,
                caption="legenda original",
            ),
        ),
    )


class MediaSpoolTest(unittest.TestCase):
    def test_hard_spool_cap_rejects_before_copy_without_residual_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "source"
            spool = root / "spool"
            source_root.mkdir()
            source = source_root / "photo.jpg"
            source.write_bytes(b"five!")
            with self.assertRaises(MediaSpoolError) as raised:
                stage_event_media(
                    event(source),
                    spool_root=spool,
                    source_roots=(source_root,),
                    minimum_free_bytes=1,
                    maximum_spool_bytes=4,
                )
            self.assertEqual("media_spool_hard_cap", raised.exception.code)
            self.assertEqual([], list(spool.iterdir()))

    def test_source_can_disappear_and_retry_uses_spool_then_purges_after_ack(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "source"
            spool = root / "spool"
            source_root.mkdir()
            source = source_root / "photo.jpg"
            source.write_bytes(b"original-photo")
            staged, _ = stage_event_media(
                event(source),
                spool_root=spool,
                source_roots=(source_root,),
                minimum_free_bytes=1,
            )
            staged_path = Path(staged.media[0].path)
            self.assertTrue(staged_path.is_file())
            self.assertTrue(staged.media[0].managed_temp)
            source.unlink()
            with MirrorLedger(root / "mirror.sqlite3") as ledger:
                ledger.set_route(Route(CONVERSATION, "-100123", "42"))
                worker = MirrorWorker(
                    ledger,
                    RecordingTransport(failures_before_success=1),
                    allowed_temp_root=spool,
                    base_backoff_seconds=1,
                    media_retention_seconds=0,
                )
                worker.ingest(staged, now=100)
                self.assertEqual("retry", worker.run_once(now=100).status)
                sent = worker.run_once(now=101)
                self.assertEqual("sent", sent.status)
                self.assertEqual(1, sent.media_removed)
                self.assertFalse(staged_path.exists())
                self.assertEqual([], ledger.managed_media_paths())

    def test_existing_spool_file_is_compared_by_hash_not_only_size(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "source"
            spool = root / "spool"
            source_root.mkdir()
            source = source_root / "photo.jpg"
            source.write_bytes(b"AAAA")
            staged, _ = stage_event_media(
                event(source),
                spool_root=spool,
                source_roots=(source_root,),
                minimum_free_bytes=1,
            )
            source.write_bytes(b"BBBB")
            with self.assertRaises(MediaSpoolError) as raised:
                stage_event_media(
                    event(source),
                    spool_root=spool,
                    source_roots=(source_root,),
                    minimum_free_bytes=1,
                )
            self.assertEqual("media_spool_conflict", raised.exception.code)
            self.assertEqual(
                hashlib.sha256(b"AAAA").hexdigest(), staged.media[0].sha256
            )

    @unittest.skipIf(not hasattr(os, "symlink"), "symlink unavailable")
    def test_symlink_source_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "source"
            source_root.mkdir()
            real = source_root / "real.jpg"
            link = source_root / "link.jpg"
            real.write_bytes(b"photo")
            try:
                link.symlink_to(real)
            except OSError:
                self.skipTest("symlink creation not permitted")
            with self.assertRaises(MediaSpoolError) as raised:
                stage_event_media(
                    event(link),
                    spool_root=root / "spool",
                    source_roots=(source_root,),
                    minimum_free_bytes=1,
                )
            self.assertEqual("media_symlink_rejected", raised.exception.code)

    def test_cleanup_is_idempotent_after_unlink_before_ledger_ack(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "source"
            spool = root / "spool"
            source_root.mkdir()
            source = source_root / "photo.jpg"
            source.write_bytes(b"photo")
            staged, _ = stage_event_media(
                event(source),
                spool_root=spool,
                source_roots=(source_root,),
                minimum_free_bytes=1,
            )
            with MirrorLedger(root / "mirror.sqlite3") as ledger:
                ledger.set_route(Route(CONVERSATION, "-100123", "42"))
                worker = MirrorWorker(ledger, RecordingTransport(), allowed_temp_root=spool)
                worker.ingest(staged, now=100)
                claim = ledger.claim_next("crash-sim", now=100)
                assert claim is not None
                ledger.mark_sent(
                    claim, ("remote-1",), now=100, media_retention_seconds=0
                )
                Path(staged.media[0].path).unlink()
                # Simulates a crash after unlink and before mark_media_removed.
                self.assertEqual("idle", worker.run_once(now=101).status)
                self.assertEqual([], ledger.managed_media_paths())

    def test_bounded_orphan_janitor_keeps_referenced_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.mkdir(exist_ok=True)
            referenced = root / ("a" * 64 + ".bin")
            orphan = root / ("b" * 64 + ".bin")
            referenced.write_bytes(b"keep")
            orphan.write_bytes(b"remove")
            removed = remove_orphaned_spool_files(
                root, (str(referenced),), grace_seconds=0, now=10**12
            )
            self.assertEqual(1, removed)
            self.assertTrue(referenced.exists())
            self.assertFalse(orphan.exists())

    def test_dead_media_requires_audited_authorization_then_is_purged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "source"
            spool = root / "spool"
            source_root.mkdir()
            source = source_root / "video.mp4"
            source.write_bytes(b"blocked-video")
            raw = event(source, event_id="blocked-media")
            raw = InboundEvent(
                event_id=raw.event_id,
                source=raw.source,
                conversation_id=raw.conversation_id,
                occurred_at=raw.occurred_at,
                actor_ref=raw.actor_ref,
                text=raw.text,
                media=(
                    MediaAttachment(
                        "video-1",
                        "video",
                        str(source),
                        mime_type="video/mp4",
                        size_bytes=source.stat().st_size,
                    ),
                ),
            )
            staged, _ = stage_event_media(
                raw,
                spool_root=spool,
                source_roots=(source_root,),
                minimum_free_bytes=1,
            )
            staged_path = Path(staged.media[0].path)
            with MirrorLedger(root / "mirror.sqlite3") as ledger:
                ledger.set_route(Route(CONVERSATION, "-100123", "42"))
                worker = MirrorWorker(
                    ledger,
                    RecordingTransport(),
                    allowed_temp_root=spool,
                )
                worker.ingest(staged, now=100)
                self.assertEqual("dead", worker.run_once(now=100).status)
                self.assertTrue(staged_path.exists())
                report = ledger.managed_media_report()
                self.assertEqual(1, report["media_count"])
                self.assertEqual(len(b"blocked-video"), report["total_bytes"])
                queued = ledger.authorize_media_purge(
                    staged.event_id,
                    evidence_ref="operator-confirmed-terminal-video",
                    now=101,
                )
                self.assertEqual(1, queued)
                audit = ledger.connection.execute(
                    """SELECT delivery_state, media_count, evidence_hash
                       FROM mirror_media_purge_audit"""
                ).fetchone()
                self.assertEqual("dead", audit["delivery_state"])
                self.assertEqual(1, audit["media_count"])
                self.assertNotEqual(
                    "operator-confirmed-terminal-video", audit["evidence_hash"]
                )
                cleaned = worker.run_once(now=101)
                self.assertEqual("idle", cleaned.status)
                self.assertEqual(1, cleaned.media_removed)
                self.assertFalse(staged_path.exists())

    def test_pending_media_cannot_be_purged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "source"
            spool = root / "spool"
            source_root.mkdir()
            source = source_root / "photo.jpg"
            source.write_bytes(b"photo")
            staged, _ = stage_event_media(
                event(source),
                spool_root=spool,
                source_roots=(source_root,),
                minimum_free_bytes=1,
            )
            with MirrorLedger(root / "mirror.sqlite3") as ledger:
                ledger.set_route(Route(CONVERSATION, "-100123", "42"))
                ledger.capture_event(staged, now=100)
                with self.assertRaisesRegex(
                    Exception, "media_purge_not_authorized_for_state"
                ):
                    ledger.authorize_media_purge(
                        staged.event_id, evidence_ref="not-terminal", now=101
                    )


class PrivacyAndReplayCLITest(unittest.TestCase):
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

    def _cli(self, *args: str) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(list(args))
        return code, json.loads(output.getvalue())

    def test_replay_after_sent_and_purge_does_not_recreate_media(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            self._cli("--config", str(config), "init")
            self._cli(
                "--config", str(config), "route", "set", "conversation-raw", "-100123", "42"
            )
            source = root / "source" / "photo.jpg"
            source.write_bytes(b"photo")
            payload = root / "event.json"
            payload.write_text(
                json.dumps(
                    {
                        "event_id": "replay-event",
                        "source": "whatsapp",
                        "conversation_id": "conversation-raw",
                        "occurred_at": "2026-08-04T12:00:00Z",
                        "actor_ref": "actor-raw",
                        "text": "",
                        "media": [
                            {
                                "media_id": "media-replay",
                                "kind": "image",
                                "path": str(source),
                                "size_bytes": 5,
                                "caption": "original",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            code, _ = self._cli("--config", str(config), "ingest", str(payload))
            self.assertEqual(0, code)
            ledger_path = root / "data" / "mirror.sqlite3"
            with MirrorLedger(ledger_path) as ledger:
                worker = MirrorWorker(
                    ledger,
                    RecordingTransport(),
                    profile_id="test",
                    allowed_temp_root=root / "data" / "media",
                )
                self.assertEqual("sent", worker.run_once().status)
            source.unlink()
            code, result = self._cli("--config", str(config), "ingest", str(payload))
            self.assertEqual(0, code)
            self.assertFalse(result["result"]["inserted"])
            self.assertEqual("sent", result["result"]["delivery_state"])
            self.assertEqual(1, len(list((root / "data" / "media").iterdir())))

    def test_media_cli_reports_authorizes_and_cleans_without_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            self._cli("--config", str(config), "init")
            source = root / "source" / "photo.jpg"
            source.write_bytes(b"photo")
            staged, _ = stage_event_media(
                replace(
                    event(source, event_id="terminal-event"),
                    source_profile_id=opaque_ref("profile", "test"),
                ),
                spool_root=root / "data" / "media",
                source_roots=(root / "source",),
                minimum_free_bytes=1,
            )
            ledger_path = root / "data" / "mirror.sqlite3"
            with MirrorLedger(ledger_path) as ledger:
                ledger.set_route(Route(CONVERSATION, "-100123", "42"))
                ledger.capture_event(staged, now=100)
                claim = ledger.claim_next(
                    "worker-test",
                    source_profile_id=opaque_ref("profile", "test"),
                    now=100,
                )
                assert claim is not None
                ledger.mark_failed(
                    claim,
                    error_code="operator_terminal",
                    retry_at=None,
                    permanent=True,
                    now=100,
                )
            code, report = self._cli("--config", str(config), "media", "report")
            self.assertEqual(0, code)
            self.assertEqual(1, report["result"]["media_count"])
            self.assertNotIn(str(source), json.dumps(report))
            code, authorized = self._cli(
                "--config",
                str(config),
                "media",
                "authorize-purge",
                staged.event_id,
                "--evidence-ref",
                "operator-reviewed-terminal",
            )
            self.assertEqual(0, code)
            self.assertEqual(1, authorized["result"]["queued"])
            code, cleaned = self._cli(
                "--config", str(config), "media", "cleanup", "--limit", "10"
            )
            self.assertEqual(0, code)
            self.assertEqual(1, cleaned["result"]["removed"])
            self.assertEqual(0, cleaned["result"]["remaining"]["media_count"])

    def test_provision_topic_requires_confirmation_and_commits_exact_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            self._cli("--config", str(config), "init")
            with mock.patch.dict(os.environ, {"TEST_TOKEN": "synthetic-token"}):
                with mock.patch.object(
                    cli._core().TelegramBotTransport,
                    "create_forum_topic",
                    return_value="84",
                ) as create:
                    code, result = self._cli(
                        "--config", str(config), "route", "provision-topic",
                        "conversation-raw", "-100123", "Contato",
                    )
                    self.assertNotEqual(0, code)
                    create.assert_not_called()
                    code, result = self._cli(
                        "--config", str(config), "route", "provision-topic",
                        "conversation-raw", "-100123", "Contato", "--confirm-create",
                    )
                    self.assertEqual(0, code)
                    self.assertTrue(result["result"]["route_committed"])
                    create.assert_called_once_with("-100123", "Contato")
            with MirrorLedger(root / "data" / "mirror.sqlite3") as ledger:
                routes = ledger.list_routes()
                self.assertEqual(1, len(routes))
                self.assertEqual(("-100123", "84"), (routes[0].chat_id, routes[0].thread_id))

    def test_scope_is_explicit_audited_and_unmapped_defaults_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            self._cli("--config", str(config), "init")
            code, _ = self._cli(
                "--config", str(config), "scope", "set", "conversation-raw", "area_shared"
            )
            self.assertEqual(0, code)
            with MirrorLedger(root / "data" / "mirror.sqlite3") as ledger:
                profile = opaque_ref("profile", "test")
                mapped = opaque_ref(
                    "conversation", f"{profile}\x1fconversation-raw"
                )
                self.assertEqual("area_shared", ledger.get_conversation_scope(mapped))
                self.assertEqual(
                    "owner_private",
                    ledger.get_conversation_scope(opaque_ref("conversation", "unmapped")),
                )
                audit = ledger.connection.execute(
                    "SELECT old_scope, new_scope FROM mirror_conversation_policy_audit"
                ).fetchall()
                self.assertEqual([(None, "area_shared")], [tuple(row) for row in audit])

    def test_cli_lists_and_reconciles_uncertain_with_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            self._cli("--config", str(config), "init")
            self._cli(
                "--config",
                str(config),
                "route",
                "set",
                "conversation-raw",
                "-100123",
                "42",
            )
            payload = root / "event.json"
            payload.write_text(
                json.dumps(
                    {
                        "event_id": "message-uncertain",
                        "source": "whatsapp",
                        "conversation_id": "conversation-raw",
                        "actor_ref": "actor-raw",
                        "occurred_at": "2026-08-04T12:00:00Z",
                        "text": "text",
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                0,
                self._cli("--config", str(config), "ingest", str(payload))[0],
            )
            with MirrorLedger(root / "data" / "mirror.sqlite3") as ledger:
                claim = ledger.claim_next(
                    "worker-a", source_profile_id=opaque_ref("profile", "test")
                )
                assert claim is not None
                ledger.mark_uncertain(claim)
            code, listed = self._cli(
                "--config", str(config), "delivery", "list", "--state", "uncertain"
            )
            self.assertEqual(0, code)
            event_id = listed["result"]["deliveries"][0]["event_id"]
            code, reconciled = self._cli(
                "--config",
                str(config),
                "delivery",
                "reconcile-uncertain",
                event_id,
                "sent",
                "--evidence-ref",
                "telegram-visible-once",
            )
            self.assertEqual(0, code)
            self.assertTrue(reconciled["result"]["audited"])
            with MirrorLedger(root / "data" / "mirror.sqlite3") as ledger:
                self.assertEqual("sent", ledger.delivery_state(event_id))


if __name__ == "__main__":
    unittest.main()
