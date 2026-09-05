from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from espelho_zap.adapters import (  # noqa: E402
    OpenClawJSONLAdapter,
    RawInboundMessage,
    RawMediaRef,
    normalize_inbound,
)
from espelho_zap import MirrorLedger, MirrorWorker, RecordingTransport, Route, opaque_ref  # noqa: E402


class HermesPlatform(Enum):
    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"


def raw_message(*, direction: str = "inbound", media=()):
    return RawInboundMessage(
        platform="whatsapp",
        direction=direction,
        raw_message_id="message-1",
        raw_conversation_id="conversation-1",
        raw_actor_id="actor-1",
        occurred_at="2026-08-04T12:00:00Z",
        privacy_scope="owner_private",
        text=" texto original ",
        media=tuple(media),
    )


def load_hermes_module():
    path = PROJECT / "integrations" / "hermes" / "__init__.py"
    name = "espelho_zap_test_hermes_plugin"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class BaseAdapterContractTest(unittest.TestCase):
    def test_equivalent_host_inputs_normalize_to_same_event(self) -> None:
        first = normalize_inbound(raw_message())
        second = normalize_inbound(raw_message())
        self.assertIsNotNone(first)
        self.assertEqual(first, second)
        assert first is not None
        self.assertEqual(" texto original ", first.text)
        self.assertEqual(first.payload_hash(), second.payload_hash())  # type: ignore[union-attr]

    def test_outbound_whatsapp_and_other_platform_are_ignored(self) -> None:
        self.assertIsNone(normalize_inbound(raw_message(direction="outbound")))
        value = raw_message()
        self.assertIsNone(
            normalize_inbound(
                RawInboundMessage(
                    platform="telegram",
                    direction=value.direction,
                    raw_message_id=value.raw_message_id,
                    raw_conversation_id=value.raw_conversation_id,
                    raw_actor_id=value.raw_actor_id,
                    occurred_at=value.occurred_at,
                    privacy_scope=value.privacy_scope,
                    text=value.text,
                )
            )
        )

    def test_source_profiles_isolate_identical_chat_and_message_ids(self) -> None:
        first = normalize_inbound(raw_message())
        original = raw_message()
        second = normalize_inbound(
            RawInboundMessage(
                platform=original.platform,
                direction=original.direction,
                raw_message_id=original.raw_message_id,
                raw_conversation_id=original.raw_conversation_id,
                raw_actor_id=original.raw_actor_id,
                occurred_at=original.occurred_at,
                privacy_scope=original.privacy_scope,
                source_profile_id="second-profile",
                text=original.text,
            )
        )
        assert first is not None and second is not None
        self.assertNotEqual(first.event_id, second.event_id)
        self.assertNotEqual(first.conversation_id, second.conversation_id)
        self.assertNotEqual(first.actor_ref, second.actor_ref)
        self.assertNotEqual(first.source_profile_id, second.source_profile_id)

    def test_voice_reference_is_preserved_without_becoming_outbound_whatsapp(self) -> None:
        item = RawMediaRef(
            raw_id="voice-raw",
            kind="ptt",
            path="/private/staged/voice.ogg",
            mime_type="audio/ogg",
            size_bytes=12,
        )
        event = normalize_inbound(raw_message(media=(item,)))
        assert event is not None
        self.assertEqual("voice", event.media[0].kind)
        self.assertEqual("/private/staged/voice.ogg", event.media[0].path)


class OpenClawJSONLAdapterTest(unittest.TestCase):
    @staticmethod
    def row(message_id: str, text: str = "conteudo") -> dict[str, object]:
        return {
            "type": "message",
            "id": message_id,
            "timestamp": "2026-08-04T12:00:00Z",
            "message": {
                "role": "user",
                "sourceChannel": "whatsapp",
                "senderId": "actor-1",
                "content": text,
            },
        }

    def test_cursor_partial_line_rotation_and_explicit_route_release(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session = root / "session.jsonl"
            first = json.dumps(self.row("m1"), separators=(",", ":")) + "\n"
            second = json.dumps(self.row("m2"), separators=(",", ":"))
            split = len(second) // 2
            session.write_bytes((first + second[:split]).encode("utf-8"))
            with MirrorLedger(root / "ledger.sqlite3") as ledger:
                adapter = OpenClawJSONLAdapter(
                    ledger,
                    allowed_session_root=root,
                    confirmed_platform="whatsapp",
                )
                result = adapter.ingest_file(
                    session,
                    source_ref="session-a",
                    raw_conversation_id="conversation-1",
                    privacy_scope="owner_private",
                )
                self.assertEqual(1, result["inserted"])
                self.assertEqual(1, result["blocked_no_route"])
                cursor_ref = opaque_ref("source", "session-a")
                cursor = ledger.get_source_cursor(adapter.ADAPTER_ID, cursor_ref)
                self.assertEqual(len(first.encode("utf-8")), cursor[1])  # type: ignore[index]
                self.assertEqual(1, ledger.health()["blocked_no_route"])

                with session.open("ab") as handle:
                    handle.write((second[split:] + "\n").encode("utf-8"))
                result = adapter.ingest_file(
                    session,
                    source_ref="session-a",
                    raw_conversation_id="conversation-1",
                    privacy_scope="owner_private",
                )
                self.assertEqual(1, result["inserted"])
                profile = opaque_ref("profile", "default")
                conversation = opaque_ref(
                    "conversation", f"{profile}\x1fconversation-1"
                )
                ledger.set_route(Route(conversation, "-100123", "42"))
                self.assertEqual(2, ledger.reconcile_route_blocks(conversation))
                self.assertEqual(2, ledger.connection.execute(
                    "SELECT COUNT(*) FROM mirror_deliveries"
                ).fetchone()[0])

                old_generation = ledger.get_source_cursor(adapter.ADAPTER_ID, cursor_ref)[0]  # type: ignore[index]
                session.write_bytes(b"")
                adapter.ingest_file(
                    session,
                    source_ref="session-a",
                    raw_conversation_id="conversation-1",
                    privacy_scope="owner_private",
                )
                new_cursor = ledger.get_source_cursor(adapter.ADAPTER_ID, cursor_ref)
                self.assertNotEqual(old_generation, new_cursor[0])  # type: ignore[index]
                self.assertEqual(0, new_cursor[1])  # type: ignore[index]

    def test_inbound_voice_media_only_and_assistant_outbound_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            media_root = root / "media"
            media_root.mkdir()
            voice = media_root / "voice.ogg"
            voice.write_bytes(b"voice-bytes")
            row = self.row("voice-1", "")
            row["message"]["MediaPath"] = str(voice)  # type: ignore[index]
            row["message"]["MediaType"] = "ptt"  # type: ignore[index]
            outbound = self.row("assistant-1")
            outbound["message"]["role"] = "assistant"  # type: ignore[index]
            session = root / "session.jsonl"
            session.write_text(
                "\n".join(json.dumps(item) for item in (row, outbound)) + "\n",
                encoding="utf-8",
            )
            with MirrorLedger(root / "ledger.sqlite3") as ledger:
                conversation = opaque_ref("conversation", "conversation-1")
                ledger.set_route(Route(conversation, "-100123", "42"))
                adapter = OpenClawJSONLAdapter(
                    ledger,
                    allowed_session_root=root,
                    allowed_media_roots=(media_root,),
                    confirmed_platform="whatsapp",
                )
                result = adapter.ingest_file(
                    session,
                    source_ref="session-a",
                    raw_conversation_id="conversation-1",
                    privacy_scope="owner_private",
                )
                self.assertEqual(1, result["inserted"])
                self.assertEqual(1, result["ignored"])
                stored = ledger.connection.execute(
                    "SELECT event_id FROM mirror_events"
                ).fetchone()
                event = ledger.load_event(str(stored["event_id"]))
                self.assertEqual("voice", event.media[0].kind)
                self.assertEqual(voice.resolve(), Path(event.media[0].path))


class HermesPluginContractTest(unittest.TestCase):
    @staticmethod
    def _write_human_outbound_arm(
        module,
        outbound,
        *,
        release_commit=None,
        plugin_sha256=None,
        hermes_runtime_fingerprint=None,
    ):
        value = {
            "schema_version": 1,
            "release_commit": release_commit or outbound.release_commit,
            "plugin_sha256": plugin_sha256 or outbound.plugin_sha256,
            "hermes_runtime_fingerprint": (
                hermes_runtime_fingerprint
                or outbound.hermes_runtime_fingerprint
            ),
            "armed": True,
        }
        outbound.arm_file.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.chmod(outbound.arm_file, 0o600)

    @staticmethod
    def _human_outbound_settings(module, root: Path, media_roots=()):
        config = root / "config.toml"
        config.write_text("schema_version = 1\n", encoding="utf-8")
        route_map = root / "routes.json"
        route_map.write_text(
            json.dumps(
                {
                    "forum_chat_id": "-100777",
                    "routes": {
                        "15550000000@s.whatsapp.net": {
                            "thread_id": "42",
                            "enabled": True,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        token_file = root / "bridge-token"
        token_file.write_text("test-token-local-only", encoding="utf-8")
        managed = root / "managed"
        managed.mkdir()
        mirror_ledger = root / "mirror.sqlite3"
        connection = sqlite3.connect(mirror_ledger)
        try:
            connection.execute(
                """CREATE TABLE mirror_conversation_admission (
                       conversation_id TEXT PRIMARY KEY,
                       source_profile_id TEXT NOT NULL,
                       conversation_kind TEXT NOT NULL,
                       approval_state TEXT NOT NULL
                   )"""
            )
            connection.commit()
        finally:
            connection.close()
        release_commit = "a" * 40
        plugin_sha256 = hashlib.sha256(
            Path(module.__file__).resolve(strict=True).read_bytes()
        ).hexdigest()
        hermes_runtime_fingerprint = "f" * 64
        human = module._HumanOutboundSettings(
            allowed_users=frozenset({"owner-1"}),
            route_map=route_map,
            token_file=token_file,
            ledger_file=root / "human-outbound.sqlite3",
            mirror_ledger_file=mirror_ledger,
            managed_media_root=managed,
            timeout_seconds=2.0,
            arm_file=root / "human-outbound.arm.json",
            release_commit=release_commit,
            plugin_sha256=plugin_sha256,
            hermes_runtime_fingerprint=hermes_runtime_fingerprint,
        )
        HermesPluginContractTest._write_human_outbound_arm(module, human)
        return module._Settings(
            Path(sys.executable),
            config,
            "hermes-main",
            root / "capture-health.json",
            "owner_private",
            tuple(media_roots),
            1024 * 1024,
            15.0,
            "-100777",
            True,
            human,
            hermes_runtime_fingerprint,
        )

    @staticmethod
    def _session_store():
        class Store:
            def __init__(self):
                self.rows = []

            def get_or_create_session(self, source):
                del source
                return types.SimpleNamespace(session_id="topic-session")

            def has_platform_message_id(self, session_id, platform_message_id):
                return any(
                    row[0] == session_id
                    and row[1]["platform_message_id"] == platform_message_id
                    for row in self.rows
                )

            def append_to_transcript(self, session_id, value):
                self.rows.append((session_id, value))

        return Store()

    def _register(self, module, root: Path):
        config = root / "config.toml"
        config.write_text("schema_version = 1\n", encoding="utf-8")

        class Context:
            def register_hook(self, name, callback):
                self.name = name
                self.callback = callback

        ctx = Context()
        with patch.dict(
            os.environ,
            {
                "ESPELHO_ZAP_CLI": sys.executable,
                "ESPELHO_ZAP_CONFIG": str(config),
                "ESPELHO_ZAP_SOURCE_PROFILE_ID": "hermes-main",
                "ESPELHO_ZAP_HOOK_HEALTH_FILE": str(root / "capture-health.json"),
                "ESPELHO_ZAP_PRIVACY_SCOPE": "owner_private",
                "ESPELHO_ZAP_MEDIA_ROOTS": "",
            },
            clear=False,
        ):
            module.register(ctx)
        return ctx

    def test_hermes_runtime_fingerprint_tracks_interpreter_venv_and_hook_code(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prefix = root / "venv"
            executable = prefix / "bin" / "python"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"python-runtime-v1")
            (prefix / "pyvenv.cfg").write_text(
                "home = /runtime/base\n", encoding="utf-8"
            )
            for relative, content in (
                ("gateway/run.py", "gateway-v1\n"),
                ("gateway/platforms/base.py", "dispatch-v1\n"),
                ("hermes_cli/plugins.py", "plugins-v1\n"),
            ):
                candidate = root / relative
                candidate.parent.mkdir(parents=True, exist_ok=True)
                candidate.write_text(content, encoding="utf-8")

            runtime = types.SimpleNamespace(
                executable=str(executable),
                prefix=str(prefix),
                base_prefix=str(root / "base-python"),
                path=[str(root)],
                modules={},
            )
            with patch.object(module, "sys", runtime):
                first = module._hermes_runtime_fingerprint()
                self.assertEqual(first, module._hermes_runtime_fingerprint())

                extra = root / "gateway" / "upgrade_surface.py"
                extra.write_text("surface-v1\n", encoding="utf-8")
                expanded = module._hermes_runtime_fingerprint()
                self.assertNotEqual(first, expanded)
                extra.write_text("surface-v2\n", encoding="utf-8")
                self.assertNotEqual(expanded, module._hermes_runtime_fingerprint())
                extra.unlink()

                for relative, original in (
                    ("gateway/run.py", "gateway-v1\n"),
                    ("gateway/platforms/base.py", "dispatch-v1\n"),
                    ("hermes_cli/plugins.py", "plugins-v1\n"),
                ):
                    candidate = root / relative
                    candidate.write_text(f"changed:{relative}\n", encoding="utf-8")
                    self.assertNotEqual(
                        first, module._hermes_runtime_fingerprint(), relative
                    )
                    candidate.write_text(original, encoding="utf-8")

                (prefix / "pyvenv.cfg").write_text(
                    "home = /runtime/updated\n", encoding="utf-8"
                )
                venv_changed = module._hermes_runtime_fingerprint()
                self.assertNotEqual(first, venv_changed)

                (prefix / "pyvenv.cfg").write_text(
                    "home = /runtime/base\n", encoding="utf-8"
                )
                executable.write_bytes(b"python-runtime-v2")
                executable_changed = module._hermes_runtime_fingerprint()
                self.assertNotEqual(first, executable_changed)

                required = root / "hermes_cli" / "plugins.py"
                required.unlink()
                with self.assertRaisesRegex(
                    RuntimeError, "hermes_runtime_fingerprint_invalid"
                ):
                    module._hermes_runtime_fingerprint()

    def test_human_outbound_ledger_rejects_nonregular_and_symlink_paths(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            managed = root / "managed"
            managed.mkdir()
            route_map = root / "routes.json"
            route_map.write_text("{}", encoding="utf-8")
            token = root / "token"
            token.write_text("local", encoding="utf-8")

            ledger_directory = root / "ledger-directory"
            ledger_directory.mkdir()
            outbound = module._HumanOutboundSettings(
                frozenset({"owner"}),
                route_map,
                token,
                ledger_directory,
                managed,
                2.0,
            )
            with self.assertRaisesRegex(RuntimeError, "human_outbound_ledger_invalid"):
                module._outbound_connection(outbound)

            unsafe_parent = root / "unsafe-parent"
            unsafe_parent.write_text("not-a-directory", encoding="utf-8")
            outbound = module._HumanOutboundSettings(
                frozenset({"owner"}),
                route_map,
                token,
                unsafe_parent / "ledger.sqlite3",
                managed,
                2.0,
            )
            with self.assertRaises((FileExistsError, NotADirectoryError, RuntimeError)):
                module._outbound_connection(outbound)

            sidecar_ledger = root / "sidecar-ledger.sqlite3"
            sidecar_ledger.write_bytes(b"")
            Path(f"{sidecar_ledger}-wal").mkdir()
            outbound = module._HumanOutboundSettings(
                frozenset({"owner"}),
                route_map,
                token,
                sidecar_ledger,
                managed,
                2.0,
            )
            with self.assertRaisesRegex(RuntimeError, "human_outbound_ledger_invalid"):
                module._outbound_connection(outbound)

            target = root / "ledger-target.sqlite3"
            target.write_bytes(b"")
            link = root / "ledger-link.sqlite3"
            try:
                link.symlink_to(target)
            except OSError:
                link = None
            if link is not None:
                outbound = module._HumanOutboundSettings(
                    frozenset({"owner"}),
                    route_map,
                    token,
                    link,
                    managed,
                    2.0,
                )
                with self.assertRaisesRegex(
                    RuntimeError, "human_outbound_ledger_invalid"
                ):
                    module._outbound_connection(outbound)

            real_parent = root / "real-parent"
            real_parent.mkdir()
            linked_parent = root / "linked-parent"
            try:
                linked_parent.symlink_to(real_parent, target_is_directory=True)
            except OSError:
                linked_parent = None
            if linked_parent is not None:
                outbound = module._HumanOutboundSettings(
                    frozenset({"owner"}),
                    route_map,
                    token,
                    linked_parent / "ledger.sqlite3",
                    managed,
                    2.0,
                )
                with self.assertRaisesRegex(
                    RuntimeError, "human_outbound_ledger_parent_invalid"
                ):
                    module._outbound_connection(outbound)

    @unittest.skipUnless(os.name == "posix", "POSIX permission semantics required")
    def test_human_outbound_sqlite_is_private_even_with_permissive_umask(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            managed = root / "managed"
            managed.mkdir()
            route_map = root / "routes.json"
            route_map.write_text("{}", encoding="utf-8")
            token = root / "token"
            token.write_text("local", encoding="utf-8")
            private_parent = root / "private-ledger"
            ledger = private_parent / "human-outbound.sqlite3"
            outbound = module._HumanOutboundSettings(
                frozenset({"owner"}), route_map, token, ledger, managed, 2.0
            )
            previous_umask = os.umask(0)
            try:
                connection = module._outbound_connection(outbound)
                try:
                    self.assertEqual("wal", connection.execute("PRAGMA journal_mode").fetchone()[0])
                    self.assertTrue(Path(f"{ledger}-wal").is_file())
                    self.assertTrue(Path(f"{ledger}-shm").is_file())
                    for candidate in (ledger, Path(f"{ledger}-wal"), Path(f"{ledger}-shm")):
                        self.assertEqual(0o600, candidate.stat().st_mode & 0o777)
                    self.assertEqual(0o700, private_parent.stat().st_mode & 0o777)
                finally:
                    connection.close()
            finally:
                os.umask(previous_umask)

    def test_human_outbound_arm_rejects_missing_nonregular_symlink_and_mismatch(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self._human_outbound_settings(module, root)
            outbound = settings.human_outbound
            self.assertIsNotNone(module._human_outbound_arm_identity(outbound))

            outbound.arm_file.unlink()
            self.assertIsNone(module._human_outbound_arm_identity(outbound))

            outbound.arm_file.mkdir()
            with self.assertRaisesRegex(RuntimeError, "human_outbound_arm_invalid"):
                module._human_outbound_arm_identity(outbound)
            outbound.arm_file.rmdir()

            target = root / "arm-target.json"
            self._write_human_outbound_arm(
                module,
                module._HumanOutboundSettings(
                    outbound.allowed_users,
                    outbound.route_map,
                    outbound.token_file,
                    outbound.ledger_file,
                    outbound.managed_media_root,
                    outbound.timeout_seconds,
                    target,
                    outbound.release_commit,
                    outbound.plugin_sha256,
                ),
            )
            try:
                outbound.arm_file.symlink_to(target)
            except OSError:
                pass
            else:
                with self.assertRaisesRegex(
                    RuntimeError, "human_outbound_arm_invalid"
                ):
                    module._human_outbound_arm_identity(outbound)
                outbound.arm_file.unlink()

            self._write_human_outbound_arm(
                module, outbound, release_commit="b" * 40
            )
            with self.assertRaisesRegex(RuntimeError, "human_outbound_arm_mismatch"):
                module._human_outbound_arm_identity(outbound)
            self._write_human_outbound_arm(
                module, outbound, plugin_sha256="b" * 64
            )
            with self.assertRaisesRegex(RuntimeError, "human_outbound_arm_mismatch"):
                module._human_outbound_arm_identity(outbound)

            self._write_human_outbound_arm(
                module, outbound, hermes_runtime_fingerprint="b" * 64
            )
            with self.assertRaisesRegex(RuntimeError, "human_outbound_arm_mismatch"):
                module._human_outbound_arm_identity(outbound)

            self._write_human_outbound_arm(module, outbound)
            incomplete = json.loads(outbound.arm_file.read_text(encoding="utf-8"))
            incomplete.pop("hermes_runtime_fingerprint")
            outbound.arm_file.write_text(
                json.dumps(incomplete, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            os.chmod(outbound.arm_file, 0o600)
            with self.assertRaisesRegex(RuntimeError, "human_outbound_arm_invalid"):
                module._human_outbound_arm_identity(outbound)

            self._write_human_outbound_arm(module, outbound)
            self.assertIsNotNone(module._human_outbound_arm_identity(outbound))

    @unittest.skipUnless(os.name == "posix", "POSIX permission semantics required")
    def test_human_outbound_arm_rejects_permissive_mode(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outbound = self._human_outbound_settings(module, root).human_outbound
            os.chmod(outbound.arm_file, 0o644)
            with self.assertRaisesRegex(
                RuntimeError, "human_outbound_arm_permissions_invalid"
            ):
                module._human_outbound_arm_identity(outbound)

    def test_register_without_arm_initializes_ledger_and_marks_disarmed(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.toml"
            config.write_text("schema_version = 1\n", encoding="utf-8")
            routes = root / "routes.json"
            routes.write_text(
                json.dumps(
                    {
                        "forum_chat_id": "-100777",
                        "routes": {
                            "15550000000@s.whatsapp.net": {
                                "thread_id": "42",
                                "enabled": True,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            token = root / "bridge-token"
            token.write_text("local-only", encoding="utf-8")
            ledger = root / "private" / "human-outbound.sqlite3"
            mirror_ledger = root / "private" / "mirror.sqlite3"
            mirror_ledger.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(mirror_ledger)
            try:
                connection.execute(
                    """CREATE TABLE mirror_conversation_admission (
                           conversation_id TEXT PRIMARY KEY,
                           source_profile_id TEXT NOT NULL,
                           conversation_kind TEXT NOT NULL,
                           approval_state TEXT NOT NULL
                       )"""
                )
                connection.commit()
            finally:
                connection.close()
            marker = root / "private" / "startup.json"
            arm = root / "private" / "human-outbound.arm.json"
            managed = root / "managed"

            class Context:
                def register_hook(self, name, callback):
                    self.name = name
                    self.callback = callback

            context = Context()
            with patch.dict(
                os.environ,
                {
                    "ESPELHO_ZAP_CLI": sys.executable,
                    "ESPELHO_ZAP_CONFIG": str(config),
                    "ESPELHO_ZAP_SOURCE_PROFILE_ID": "hermes-main",
                    "HERMES_PROFILE": "hermes-main",
                    "ESPELHO_ZAP_HOOK_HEALTH_FILE": str(root / "health.json"),
                    "ESPELHO_ZAP_PRIVACY_SCOPE": "owner_private",
                    "ESPELHO_ZAP_MEDIA_ROOTS": "",
                    "ESPELHO_ZAP_TELEGRAM_FORUM_CHAT_ID": "-100777",
                    "ESPELHO_ZAP_HUMAN_OUTBOUND_ENABLED": "enabled",
                    "ESPELHO_ZAP_HUMAN_OUTBOUND_ALLOWED_USERS": "owner-1",
                    "ESPELHO_ZAP_HUMAN_OUTBOUND_ROUTE_MAP": str(routes),
                    "ESPELHO_ZAP_HUMAN_OUTBOUND_TOKEN_FILE": str(token),
                    "ESPELHO_ZAP_HUMAN_OUTBOUND_LEDGER": str(ledger),
                    "ESPELHO_ZAP_HUMAN_OUTBOUND_MIRROR_LEDGER": str(mirror_ledger),
                    "ESPELHO_ZAP_HUMAN_OUTBOUND_MANAGED_MEDIA_ROOT": str(managed),
                    "ESPELHO_ZAP_HUMAN_OUTBOUND_ARM_FILE": str(arm),
                    "ESPELHO_ZAP_HUMAN_OUTBOUND_STARTUP_MARKER": str(marker),
                    "ESPELHO_ZAP_RELEASE_COMMIT": "a" * 40,
                },
                clear=False,
            ), patch.object(
                module, "_hermes_runtime_fingerprint", return_value="f" * 64
            ), patch.object(
                module, "_start_human_outbound_rearm_watcher"
            ), patch.object(
                module,
                "_process_arguments",
                return_value=("hermes", "-p", "hermes-main", "gateway", "run"),
            ), patch.object(module, "_submit_human_outbound") as submit:
                module.register(context)
            submit.assert_not_called()
            self.assertFalse(arm.exists())
            self.assertTrue(ledger.is_file())
            connection = sqlite3.connect(ledger)
            try:
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM hermes_human_outbound"
                    ).fetchone()[0],
                )
            finally:
                connection.close()
            startup = json.loads(marker.read_text(encoding="utf-8"))
            self.assertTrue(startup["human_outbound_enabled"])
            self.assertFalse(startup["human_outbound_armed"])
            self.assertEqual(os.getpid(), startup["gateway_pid"])
            self.assertRegex(startup["hermes_runtime_fingerprint"], r"^[0-9a-f]{64}$")
            self.assertNotIn("arm_file", startup)

    def test_startup_marker_reports_valid_arm_without_secret_material(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self._human_outbound_settings(module, root)
            marker = root / "startup.json"
            with patch.dict(
                os.environ,
                {
                    "ESPELHO_ZAP_HUMAN_OUTBOUND_STARTUP_MARKER": str(marker),
                    "ESPELHO_ZAP_RELEASE_COMMIT": (
                        settings.human_outbound.release_commit
                    ),
                    "HERMES_PROFILE": "hermes-main",
                },
                clear=False,
            ), patch.object(
                module,
                "_process_arguments",
                return_value=("hermes", "-p", "hermes-main", "gateway", "run"),
            ):
                module._write_startup_marker(settings)
            value = json.loads(marker.read_text(encoding="utf-8"))
            self.assertTrue(value["human_outbound_enabled"])
            self.assertTrue(value["human_outbound_armed"])
            self.assertEqual(os.getpid(), value["gateway_pid"])
            self.assertEqual(
                settings.human_outbound.plugin_sha256, value["plugin_sha256"]
            )
            self.assertEqual(
                settings.human_outbound.hermes_runtime_fingerprint,
                value["hermes_runtime_fingerprint"],
            )
            self.assertNotIn("arm_file", value)

    def test_auxiliary_hermes_process_cannot_replace_gateway_marker(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self._human_outbound_settings(module, root)
            marker = root / "startup.json"
            original = b'{"gateway_pid":12345,"sentinel":"live-gateway"}\n'
            marker.write_bytes(original)
            with patch.dict(
                os.environ,
                {
                    "ESPELHO_ZAP_HUMAN_OUTBOUND_STARTUP_MARKER": str(marker),
                    "ESPELHO_ZAP_RELEASE_COMMIT": (
                        settings.human_outbound.release_commit
                    ),
                    "HERMES_PROFILE": "hermes-main",
                },
                clear=False,
            ), patch.object(
                module,
                "_process_arguments",
                return_value=("hermes", "-p", "hermes-main", "skills", "list"),
            ):
                module._write_startup_marker(settings)
            self.assertEqual(original, marker.read_bytes())

    def test_register_with_valid_arm_writes_marker_then_drains_prepared_job(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self._human_outbound_settings(module, root)
            event = types.SimpleNamespace(
                text="drenar no restart",
                source=types.SimpleNamespace(
                    platform="telegram",
                    chat_id="-100777",
                    thread_id="42",
                    user_id="owner-1",
                ),
                message_id="restart-register",
                media_urls=[],
                media_types=[],
                raw_message=types.SimpleNamespace(text="drenar no restart"),
            )
            with patch.object(module, "_submit_human_outbound"):
                module._build_hook(settings)(event, object(), self._session_store())

            class Context:
                def register_hook(self, name, callback):
                    self.name = name
                    self.callback = callback

            marker = root / "startup-restart.json"
            sent = []
            with patch.object(
                module._Settings, "from_environment", return_value=settings
            ), patch.dict(
                os.environ,
                {
                    "ESPELHO_ZAP_HUMAN_OUTBOUND_STARTUP_MARKER": str(marker),
                    "ESPELHO_ZAP_RELEASE_COMMIT": (
                        settings.human_outbound.release_commit
                    ),
                    "HERMES_PROFILE": "hermes-main",
                },
                clear=False,
            ), patch.object(
                module,
                "_post_human_outbound",
                side_effect=lambda _outbound, job: sent.append(job) or ("wa-1",),
            ), patch.object(
                module,
                "_submit_human_outbound",
                side_effect=module._drain_human_outbound,
            ), patch.object(
                module, "_start_human_outbound_rearm_watcher"
            ), patch.object(
                module,
                "_process_arguments",
                return_value=("hermes", "-p", "hermes-main", "gateway", "run"),
            ):
                context = Context()
                module.register(context)
            self.assertEqual("pre_gateway_dispatch", context.name)
            self.assertEqual(1, len(sent))
            self.assertTrue(
                json.loads(marker.read_text(encoding="utf-8"))[
                    "human_outbound_armed"
                ]
            )
            connection = sqlite3.connect(settings.human_outbound.ledger_file)
            try:
                self.assertEqual(
                    "sent",
                    connection.execute(
                        "SELECT status FROM hermes_human_outbound"
                    ).fetchone()[0],
                )
            finally:
                connection.close()

    def test_pre_dispatch_hook_normalizes_official_datetime_and_invokes_cli_observer(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ctx = self._register(module, root)
            self.assertEqual("pre_gateway_dispatch", ctx.name)
            event = types.SimpleNamespace(
                text="texto",
                source=types.SimpleNamespace(
                    platform=HermesPlatform.WHATSAPP,
                    chat_id="chat-a",
                    user_id="actor-a",
                ),
                message_id="message-a",
                platform_update_id=None,
                media_urls=[],
                media_types=[],
                timestamp=datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc),
            )
            captured = []
            with patch.object(module, "_run_ingest", side_effect=lambda payload, settings: captured.append(payload)):
                self.assertEqual(
                    {"action": "skip", "reason": "espelho-zap-passive"},
                    ctx.callback(event, object(), object()),
                )
            self.assertEqual(1, len(captured))
            self.assertEqual("2026-08-04T12:00:00Z", captured[0]["occurred_at"])
            self.assertEqual("owner_private", captured[0]["privacy_scope"])
            self.assertEqual("hermes-main", captured[0]["source_profile_id"])
            self.assertEqual(3, captured[0]["schema_version"])
            self.assertEqual("direct", captured[0]["conversation_kind"])
            health = json.loads((root / "capture-health.json").read_text(encoding="utf-8"))
            self.assertEqual(1, health["successes"])

    def test_missing_route_is_visible_and_missing_timestamp_is_signaled_safely(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ctx = self._register(module, root)
            good = types.SimpleNamespace(
                text="texto",
                source=types.SimpleNamespace(
                    platform="whatsapp", chat_id="chat-without-route", user_id="actor-a"
                ),
                message_id="message-a",
                platform_update_id=None,
                media_urls=[],
                media_types=[],
                timestamp="2026-08-04T12:00:00Z",
            )
            with self.assertLogs("espelho_zap.hermes", logging.WARNING) as rejected:
                with patch.object(module, "_run_ingest", side_effect=RuntimeError("route_missing")):
                    self.assertEqual(
                        {"action": "skip", "reason": "espelho-zap-passive"},
                        ctx.callback(good, object(), object()),
                    )
            self.assertIn("RuntimeError", " ".join(rejected.output))

            missing_time = types.SimpleNamespace(**good.__dict__)
            del missing_time.timestamp
            with self.assertLogs("espelho_zap.hermes", logging.WARNING) as captured:
                self.assertEqual(
                    {"action": "skip", "reason": "espelho-zap-passive"},
                    ctx.callback(missing_time, object(), object()),
                )
            joined = " ".join(captured.output)
            self.assertIn("ValueError", joined)
            self.assertNotIn("chat-without-route", joined)
            self.assertNotIn("message-a", joined)
            self.assertGreaterEqual(module.hook_health().get("ValueError", 0), 1)

    def test_voice_media_reference_is_enqueued_and_non_whatsapp_is_ignored(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            media_root = root / "media"
            media_root.mkdir()
            voice = media_root / "voice.ogg"
            voice.write_bytes(b"voice")
            config = root / "config.toml"
            config.write_text("schema_version = 1\n", encoding="utf-8")
            ctx = types.SimpleNamespace(register_hook=lambda name, callback: setattr(ctx, "callback", callback))
            with patch.dict(
                os.environ,
                {
                    "ESPELHO_ZAP_CLI": sys.executable,
                    "ESPELHO_ZAP_CONFIG": str(config),
                    "ESPELHO_ZAP_SOURCE_PROFILE_ID": "hermes-main",
                    "ESPELHO_ZAP_HOOK_HEALTH_FILE": str(root / "capture-health.json"),
                    "ESPELHO_ZAP_PRIVACY_SCOPE": "owner_private",
                    "ESPELHO_ZAP_MEDIA_ROOTS": str(media_root),
                },
                clear=False,
            ):
                module.register(ctx)
            event = types.SimpleNamespace(
                text="",
                source=types.SimpleNamespace(platform="whatsapp", chat_id="chat-a", user_id="actor-a"),
                message_id="voice-a",
                platform_update_id=None,
                media_urls=[str(voice)],
                media_types=["voice"],
                occurred_at="2026-08-04T12:00:00Z",
            )
            captured = []
            with patch.object(module, "_run_ingest", side_effect=lambda payload, settings: captured.append(payload)):
                self.assertEqual(
                    {"action": "skip", "reason": "espelho-zap-passive"},
                    ctx.callback(event, object(), object()),
                )
                event.source.platform = "telegram"
                event.message_id = "ignored"
                self.assertIsNone(ctx.callback(event, object(), object()))
            self.assertEqual(1, len(captured))
            self.assertEqual("voice", captured[0]["media"][0]["kind"])

    def test_mirror_forum_is_data_plane_but_dm_and_other_chats_still_dispatch(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.toml"
            config.write_text("schema_version = 1\n", encoding="utf-8")
            ctx = types.SimpleNamespace(
                register_hook=lambda name, callback: setattr(ctx, "callback", callback)
            )
            with patch.dict(
                os.environ,
                {
                    "ESPELHO_ZAP_CLI": sys.executable,
                    "ESPELHO_ZAP_CONFIG": str(config),
                    "ESPELHO_ZAP_SOURCE_PROFILE_ID": "hermes-main",
                    "ESPELHO_ZAP_HOOK_HEALTH_FILE": str(root / "capture-health.json"),
                    "ESPELHO_ZAP_PRIVACY_SCOPE": "owner_private",
                    "ESPELHO_ZAP_MEDIA_ROOTS": "",
                    "ESPELHO_ZAP_TELEGRAM_FORUM_CHAT_ID": "-100777",
                },
                clear=False,
            ):
                module.register(ctx)
            forum = types.SimpleNamespace(
                source=types.SimpleNamespace(
                    platform=HermesPlatform.TELEGRAM, chat_id="-100777"
                )
            )
            other_group = types.SimpleNamespace(
                source=types.SimpleNamespace(platform="telegram", chat_id="-100778")
            )
            direct = types.SimpleNamespace(
                source=types.SimpleNamespace(platform="telegram", chat_id="123")
            )
            with patch.object(module, "_run_ingest") as ingest:
                self.assertEqual(
                    {"action": "skip", "reason": "espelho-zap-forum-data-plane"},
                    ctx.callback(forum, object(), object()),
                )
                self.assertIsNone(ctx.callback(other_group, object(), object()))
                self.assertIsNone(ctx.callback(direct, object(), object()))
            ingest.assert_not_called()

    def test_external_bridge_mode_blocks_native_whatsapp_without_double_ingest(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.toml"
            config.write_text("schema_version = 1\n", encoding="utf-8")
            ctx = types.SimpleNamespace(
                register_hook=lambda name, callback: setattr(ctx, "callback", callback)
            )
            with patch.dict(
                os.environ,
                {
                    "ESPELHO_ZAP_CLI": sys.executable,
                    "ESPELHO_ZAP_CONFIG": str(config),
                    "ESPELHO_ZAP_SOURCE_PROFILE_ID": "hermes-main",
                    "ESPELHO_ZAP_HOOK_HEALTH_FILE": str(root / "capture-health.json"),
                    "ESPELHO_ZAP_PRIVACY_SCOPE": "owner_private",
                    "ESPELHO_ZAP_MEDIA_ROOTS": "",
                    "ESPELHO_ZAP_HERMES_NATIVE_WHATSAPP_CAPTURE": "disabled",
                },
                clear=False,
            ):
                module.register(ctx)
            event = types.SimpleNamespace(
                text="must-not-reach-agent",
                source=types.SimpleNamespace(
                    platform=HermesPlatform.WHATSAPP,
                    chat_id="chat-a",
                    user_id="actor-a",
                ),
                message_id="message-a",
                media_urls=[],
                media_types=[],
                timestamp="2026-08-04T12:00:00Z",
            )
            with patch.object(module, "_run_ingest") as ingest:
                self.assertEqual(
                    {
                        "action": "skip",
                        "reason": "espelho-zap-native-whatsapp-blocked",
                    },
                    ctx.callback(event, object(), object()),
                )
            ingest.assert_not_called()

    def test_cli_delegation_is_shell_free_bounded_and_content_not_logged(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._register(module, root)
            settings = module._Settings(
                Path(sys.executable),
                root / "config.toml",
                "hermes-main",
                root / "capture-health.json",
                "owner_private",
                (),
                1024,
                15.0,
            )
            completed = types.SimpleNamespace(returncode=0)
            with patch.object(module.subprocess, "run", return_value=completed) as invoked:
                module._run_ingest({"text": "sensitive"}, settings)
            kwargs = invoked.call_args.kwargs
            self.assertFalse(kwargs["shell"])
            self.assertEqual(module.subprocess.DEVNULL, kwargs["stdout"])
            self.assertEqual(module.subprocess.DEVNULL, kwargs["stderr"])
            self.assertLessEqual(kwargs["timeout"], 15)

    def test_allowed_topic_text_is_prepared_appended_and_woken_once(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self._human_outbound_settings(module, root)
            hook = module._build_hook(settings)
            store = self._session_store()
            event = types.SimpleNamespace(
                text="resposta humana literal",
                source=types.SimpleNamespace(
                    platform="telegram",
                    chat_id="-100777",
                    thread_id="42",
                    user_id="owner-1",
                    is_bot=False,
                ),
                message_id="telegram-message-9",
                media_urls=[],
                media_types=[],
                raw_message=types.SimpleNamespace(text="resposta humana literal"),
            )
            submitted = []
            with patch.object(
                module,
                "_submit_human_outbound",
                side_effect=lambda *args: submitted.append(args),
            ):
                self.assertEqual(
                    {"action": "skip", "reason": "espelho-zap-human-outbound"},
                    hook(event, object(), store),
                )
                self.assertEqual(
                    {
                        "action": "skip",
                        "reason": "espelho-zap-human-outbound-replay-blocked",
                    },
                    hook(event, object(), store),
                )
            self.assertEqual(1, len(submitted))
            self.assertIs(settings.human_outbound, submitted[0][0])
            self.assertEqual(1, len(store.rows))
            self.assertEqual(
                "telegram:-100777:42:telegram-message-9",
                store.rows[0][1]["platform_message_id"],
            )
            connection = sqlite3.connect(settings.human_outbound.ledger_file)
            try:
                row = connection.execute(
                    """SELECT request_id,status,destination,text
                       FROM hermes_human_outbound"""
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(
                (
                    "telegram:-100777:42:telegram-message-9",
                    "prepared",
                    "15550000000@s.whatsapp.net",
                    "resposta humana literal",
                ),
                row,
            )

    def test_disarmed_topic_event_never_prepares_projects_or_submits(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self._human_outbound_settings(module, root)
            connection = module._outbound_connection(settings.human_outbound)
            connection.close()
            settings.human_outbound.arm_file.unlink()
            hook = module._build_hook(settings)
            event = types.SimpleNamespace(
                text="nao aceitar desarmado",
                source=types.SimpleNamespace(
                    platform="telegram",
                    chat_id="-100777",
                    thread_id="42",
                    user_id="owner-1",
                    is_bot=False,
                ),
                message_id="disarmed-event",
                media_urls=[],
                media_types=[],
                raw_message=types.SimpleNamespace(text="nao aceitar desarmado"),
            )
            with patch.object(module, "_prepare_outbound") as prepare, patch.object(
                module, "_append_topic_context"
            ) as project, patch.object(module, "_submit_human_outbound") as submit:
                self.assertEqual(
                    {
                        "action": "skip",
                        "reason": "espelho-zap-human-outbound-disarmed",
                    },
                    hook(event, object(), self._session_store()),
                )
            prepare.assert_not_called()
            project.assert_not_called()
            submit.assert_not_called()
            connection = sqlite3.connect(settings.human_outbound.ledger_file)
            try:
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM hermes_human_outbound"
                    ).fetchone()[0],
                )
            finally:
                connection.close()

    def test_arm_revoked_during_staging_cannot_commit_prepared_row(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outbound = self._human_outbound_settings(module, root).human_outbound

            def revoke_during_stage(*_args, **_kwargs):
                outbound.arm_file.unlink()
                return ()

            with patch.object(
                module, "_stage_outbound_media", side_effect=revoke_during_stage
            ), self.assertRaises(module._OutboundDisarmed):
                module._prepare_outbound(
                    outbound,
                    request_id="telegram:-100777:42:stage-revoke",
                    message_id="stage-revoke",
                    forum_chat_id="-100777",
                    thread_id="42",
                    destination="15550000000@s.whatsapp.net",
                    text="nao reservar",
                    payload_sha256="c" * 64,
                    media=(),
                )
            connection = sqlite3.connect(outbound.ledger_file)
            try:
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM hermes_human_outbound"
                    ).fetchone()[0],
                )
            finally:
                connection.close()

    def test_disarm_preserves_prepared_job_and_rearm_drains_once(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self._human_outbound_settings(module, root)
            event = types.SimpleNamespace(
                text="aguardar rearm",
                source=types.SimpleNamespace(
                    platform="telegram",
                    chat_id="-100777",
                    thread_id="42",
                    user_id="owner-1",
                    is_bot=False,
                ),
                message_id="prepared-before-disarm",
                media_urls=[],
                media_types=[],
                raw_message=types.SimpleNamespace(text="aguardar rearm"),
            )
            with patch.object(module, "_submit_human_outbound"):
                module._build_hook(settings)(event, object(), self._session_store())
            settings.human_outbound.arm_file.unlink()
            with patch.object(module, "_post_human_outbound") as send:
                module._drain_human_outbound(settings.human_outbound)
            send.assert_not_called()
            connection = sqlite3.connect(settings.human_outbound.ledger_file)
            try:
                self.assertEqual(
                    ("prepared", 0),
                    connection.execute(
                        "SELECT status,attempt_count FROM hermes_human_outbound"
                    ).fetchone(),
                )
            finally:
                connection.close()

    def test_rearm_watcher_wakes_prepared_once_without_new_event(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self._human_outbound_settings(module, root)
            event = types.SimpleNamespace(
                text="acordar depois do rearme",
                source=types.SimpleNamespace(
                    platform="telegram",
                    chat_id="-100777",
                    thread_id="42",
                    user_id="owner-1",
                    is_bot=False,
                ),
                message_id="watcher-rearm",
                media_urls=[],
                media_types=[],
                raw_message=types.SimpleNamespace(text="acordar depois do rearme"),
            )
            with patch.object(module, "_submit_human_outbound"):
                module._build_hook(settings)(event, object(), self._session_store())
            settings.human_outbound.arm_file.unlink()
            sent: list[object] = []
            delivered = threading.Event()

            def send(_outbound, job):
                sent.append(job)
                delivered.set()
                return ("wa-watcher",)

            with patch.object(module, "_post_human_outbound", side_effect=send):
                thread, stop = module._start_human_outbound_rearm_watcher(
                    settings.human_outbound, poll_seconds=0.01
                )
                second_thread, _ = module._start_human_outbound_rearm_watcher(
                    settings.human_outbound, poll_seconds=0.01
                )
                self.assertIs(thread, second_thread)
                self._write_human_outbound_arm(module, settings.human_outbound)
                self.assertTrue(delivered.wait(2.0))
                wake_key = str(settings.human_outbound.ledger_file)
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    with module._OUTBOUND_WAKE_LOCK:
                        pending = wake_key in module._OUTBOUND_WAKE_PENDING
                    if not pending:
                        break
                    time.sleep(0.01)
                self.assertFalse(pending)
                stop.set()
                thread.join(2.0)
            self.assertEqual(1, len(sent))

    def test_marker_failure_leaves_registered_hook_not_ready(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            settings = self._human_outbound_settings(module, Path(temp))

            class Context:
                def register_hook(self, _name, callback):
                    self.callback = callback

            context = Context()
            with patch.object(
                module._Settings, "from_environment", return_value=settings
            ), patch.object(
                module, "_write_startup_marker", side_effect=RuntimeError("marker_failed")
            ), self.assertRaisesRegex(RuntimeError, "marker_failed"):
                module.register(context)
            telegram = types.SimpleNamespace(
                source=types.SimpleNamespace(platform="telegram", chat_id="-100777")
            )
            whatsapp = types.SimpleNamespace(
                source=types.SimpleNamespace(platform="whatsapp", chat_id="wa")
            )
            with patch.object(module, "_prepare_outbound") as prepare, patch.object(
                module, "_run_ingest"
            ) as ingest:
                for event in (telegram, whatsapp):
                    self.assertEqual(
                        "espelho-zap-plugin-not-ready",
                        context.callback(event, object(), self._session_store())["reason"],
                    )
            prepare.assert_not_called()
            ingest.assert_not_called()

            connection = sqlite3.connect(settings.human_outbound.ledger_file)
            try:
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM hermes_human_outbound"
                    ).fetchone()[0],
                )
            finally:
                connection.close()

    def test_disarm_after_claim_returns_job_without_consuming_attempt(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self._human_outbound_settings(module, root)
            event = types.SimpleNamespace(
                text="revogado antes da rede",
                source=types.SimpleNamespace(
                    platform="telegram",
                    chat_id="-100777",
                    thread_id="42",
                    user_id="owner-1",
                ),
                message_id="disarm-after-claim",
                media_urls=[],
                media_types=[],
                raw_message=types.SimpleNamespace(text="revogado antes da rede"),
            )
            with patch.object(module, "_submit_human_outbound"):
                module._build_hook(settings)(event, object(), self._session_store())
            job = module._claim_prepared_outbound(settings.human_outbound)
            self.assertIsNotNone(job)
            settings.human_outbound.arm_file.unlink()
            with patch.object(module.http.client, "HTTPConnection") as transport:
                module._dispatch_human_outbound(settings.human_outbound, job)
            transport.assert_not_called()
            connection = sqlite3.connect(settings.human_outbound.ledger_file)
            try:
                self.assertEqual(
                    ("prepared", 0, 0),
                    connection.execute(
                        """SELECT status,attempt_count,next_attempt_at
                           FROM hermes_human_outbound"""
                    ).fetchone(),
                )
            finally:
                connection.close()

    def test_prepared_job_and_staged_media_survive_restart_and_drain_once(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            media_root = root / "telegram-media"
            media_root.mkdir()
            photo = media_root / "photo.jpg"
            photo.write_bytes(b"photo-durable")
            settings = self._human_outbound_settings(
                module, root, media_roots=(media_root.resolve(),)
            )
            hook = module._build_hook(settings)
            event = types.SimpleNamespace(
                text="legenda humana",
                source=types.SimpleNamespace(
                    platform="telegram",
                    chat_id="-100777",
                    thread_id="42",
                    user_id="owner-1",
                ),
                message_id="restart-prepared",
                media_urls=[str(photo)],
                media_types=["image/jpeg"],
                raw_message=types.SimpleNamespace(
                    photo=[object()], caption="legenda humana"
                ),
            )
            with patch.object(module, "_submit_human_outbound"):
                self.assertEqual(
                    "espelho-zap-human-outbound",
                    hook(event, object(), self._session_store())["reason"],
                )
            connection = sqlite3.connect(settings.human_outbound.ledger_file)
            try:
                row = connection.execute(
                    """SELECT status,destination,text,media_json
                       FROM hermes_human_outbound"""
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual("prepared", row[0])
            self.assertEqual("15550000000@s.whatsapp.net", row[1])
            self.assertEqual("legenda humana", row[2])
            staged = json.loads(row[3])
            self.assertEqual(1, len(staged))
            self.assertTrue(Path(staged[0]["filePath"]).is_file())

            sent_jobs = []
            with patch.object(
                module,
                "_post_human_outbound",
                side_effect=lambda _outbound, job: sent_jobs.append(job) or ("wa-restart",),
            ):
                self.assertEqual(0, module._recover_outbound(settings.human_outbound))
                module._drain_human_outbound(settings.human_outbound)
                module._drain_human_outbound(settings.human_outbound)
            self.assertEqual(1, len(sent_jobs))
            connection = sqlite3.connect(settings.human_outbound.ledger_file)
            try:
                status = connection.execute(
                    "SELECT status FROM hermes_human_outbound"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual("sent", status)
            self.assertEqual([], list(settings.human_outbound.managed_media_root.iterdir()))

    def test_restart_quarantines_sending_without_replay(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self._human_outbound_settings(module, root)
            hook = module._build_hook(settings)
            event = types.SimpleNamespace(
                text="nao duplicar",
                source=types.SimpleNamespace(
                    platform="telegram",
                    chat_id="-100777",
                    thread_id="42",
                    user_id="owner-1",
                ),
                message_id="restart-sending",
                media_urls=[],
                media_types=[],
                raw_message=types.SimpleNamespace(text="nao duplicar"),
            )
            with patch.object(module, "_submit_human_outbound"):
                hook(event, object(), self._session_store())
            claimed = module._claim_prepared_outbound(settings.human_outbound)
            self.assertIsNotNone(claimed)
            self.assertEqual(1, module._recover_outbound(settings.human_outbound))
            with patch.object(module, "_post_human_outbound") as send:
                module._drain_human_outbound(settings.human_outbound)
            send.assert_not_called()
            connection = sqlite3.connect(settings.human_outbound.ledger_file)
            try:
                status = connection.execute(
                    "SELECT status FROM hermes_human_outbound"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual("uncertain", status)

    def test_context_or_wakeup_failure_does_not_destroy_prepared_job(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self._human_outbound_settings(module, root)
            hook = module._build_hook(settings)
            event = types.SimpleNamespace(
                text="persistir apesar dos consumidores",
                source=types.SimpleNamespace(
                    platform="telegram",
                    chat_id="-100777",
                    thread_id="42",
                    user_id="owner-1",
                ),
                message_id="consumer-failure",
                media_urls=[],
                media_types=[],
                raw_message=types.SimpleNamespace(
                    text="persistir apesar dos consumidores"
                ),
            )
            with patch.object(
                module, "_append_topic_context", side_effect=RuntimeError("context unavailable")
            ), patch.object(
                module, "_submit_human_outbound", side_effect=RuntimeError("executor unavailable")
            ):
                self.assertEqual(
                    "espelho-zap-human-outbound",
                    hook(event, object(), object())["reason"],
                )
            connection = sqlite3.connect(settings.human_outbound.ledger_file)
            try:
                status = connection.execute(
                    "SELECT status FROM hermes_human_outbound"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual("prepared", status)

    def test_same_telegram_id_with_changed_payload_is_conflict(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self._human_outbound_settings(module, root)
            hook = module._build_hook(settings)
            event = types.SimpleNamespace(
                text="primeiro",
                source=types.SimpleNamespace(
                    platform="telegram",
                    chat_id="-100777",
                    thread_id="42",
                    user_id="owner-1",
                ),
                message_id="same-id",
                media_urls=[],
                media_types=[],
                raw_message=types.SimpleNamespace(text="primeiro"),
            )
            with patch.object(module, "_submit_human_outbound"):
                self.assertEqual(
                    "espelho-zap-human-outbound",
                    hook(event, object(), self._session_store())["reason"],
                )
                event.text = "editado"
                event.raw_message.text = "editado"
                self.assertEqual(
                    "espelho-zap-human-outbound-blocked",
                    hook(event, object(), self._session_store())["reason"],
                )
            connection = sqlite3.connect(settings.human_outbound.ledger_file)
            try:
                row = connection.execute(
                    "SELECT status,text FROM hermes_human_outbound"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(("prepared", "primeiro"), row)

    def test_reverse_route_ignores_lid_alias_and_requires_approved_group(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self._human_outbound_settings(module, root)
            route_map = settings.human_outbound.route_map
            value = json.loads(route_map.read_text(encoding="utf-8"))
            value["routes"]["15550000000@s.whatsapp.net"]["aliases"] = [
                "15550000000@s.whatsapp.net",
                "123456789@lid",
            ]
            value["routes"]["120363000000000000@g.us"] = {
                "thread_id": "43",
                "enabled": True,
                "kind": "group",
                "aliases": ["987654321@lid"],
            }
            route_map.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(
                "15550000000@s.whatsapp.net",
                module._reverse_route(settings, settings.human_outbound, "42"),
            )
            with self.assertRaisesRegex(RuntimeError, "group_outbound_not_approved"):
                module._reverse_route(settings, settings.human_outbound, "43")
            profile_ref = "profile:" + hashlib.sha256(b"hermes-main").hexdigest()
            conversation_ref = "conversation:" + hashlib.sha256(
                f"{profile_ref}\x1f120363000000000000@g.us".encode("utf-8")
            ).hexdigest()
            connection = sqlite3.connect(settings.human_outbound.mirror_ledger_file)
            try:
                connection.execute(
                    """INSERT INTO mirror_conversation_admission(
                           conversation_id, source_profile_id,
                           conversation_kind, approval_state
                       ) VALUES (?, ?, 'group', 'group_approved')""",
                    (conversation_ref, profile_ref),
                )
                connection.commit()
            finally:
                connection.close()
            self.assertEqual(
                "120363000000000000@g.us",
                module._reverse_route(settings, settings.human_outbound, "43"),
            )

    def test_bridge_partial_or_server_error_is_uncertain_not_failed(self) -> None:
        module = load_hermes_module()

        class Response:
            status = 502

            @staticmethod
            def read(_maximum):
                return json.dumps(
                    {
                        "error": "delivery_outcome_uncertain",
                        "uncertain": True,
                        "messageIds": ["wa-partial-1"],
                    }
                ).encode("utf-8")

        class Connection:
            def __init__(self, *_args, **_kwargs):
                pass

            def request(self, *_args, **_kwargs):
                return None

            def getresponse(self):
                return Response()

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self._human_outbound_settings(module, root)
            job = module._OutboundJob(
                "telegram:1:2:partial",
                "15550000000@s.whatsapp.net",
                "texto",
                (),
            )
            with patch.object(module.http.client, "HTTPConnection", Connection):
                with self.assertRaises(module._BridgeUncertain) as captured:
                    module._post_human_outbound(settings.human_outbound, job)
            self.assertEqual(("wa-partial-1",), captured.exception.message_ids)

    def test_bridge_503_proven_pre_send_is_retryable_not_uncertain(self) -> None:
        module = load_hermes_module()

        class Response:
            status = 503

            @staticmethod
            def read(_maximum):
                return json.dumps(
                    {"error": "whatsapp_unavailable", "attempted": False}
                ).encode("utf-8")

        class Connection:
            def __init__(self, *_args, **_kwargs):
                pass

            def request(self, *_args, **_kwargs):
                return None

            def getresponse(self):
                return Response()

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self._human_outbound_settings(module, root)
            job = module._OutboundJob(
                "telegram:1:2:pre-send",
                "15550000000@s.whatsapp.net",
                "texto",
                (),
            )
            with patch.object(module.http.client, "HTTPConnection", Connection):
                with self.assertRaises(module._BridgeRetryable):
                    module._post_human_outbound(settings.human_outbound, job)

    def test_bridge_connection_refused_is_retryable_not_uncertain(self) -> None:
        module = load_hermes_module()

        class Connection:
            def __init__(self, *_args, **_kwargs):
                pass

            def request(self, *_args, **_kwargs):
                raise ConnectionRefusedError("loopback bridge unavailable")

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self._human_outbound_settings(module, root)
            job = module._OutboundJob(
                "telegram:1:2:connection-refused",
                "15550000000@s.whatsapp.net",
                "texto",
                (),
            )
            with patch.object(module.http.client, "HTTPConnection", Connection):
                with self.assertRaises(module._BridgeRetryable):
                    module._post_human_outbound(settings.human_outbound, job)

    def test_pre_send_unavailable_retries_three_times_then_fails(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self._human_outbound_settings(module, root)
            hook = module._build_hook(settings)
            event = types.SimpleNamespace(
                text="retry bounded",
                source=types.SimpleNamespace(
                    platform="telegram",
                    chat_id="-100777",
                    thread_id="42",
                    user_id="owner-1",
                ),
                message_id="retry-three",
                media_urls=[],
                media_types=[],
                raw_message=types.SimpleNamespace(text="retry bounded"),
            )
            with patch.object(module, "_submit_human_outbound"):
                hook(event, object(), self._session_store())
            with patch.object(
                module,
                "_post_human_outbound",
                side_effect=module._BridgeRetryable("pre-send"),
            ):
                for expected_attempt in (1, 2, 3):
                    job = module._claim_prepared_outbound(settings.human_outbound)
                    self.assertIsNotNone(job)
                    self.assertEqual(expected_attempt, job.attempt_count)
                    module._dispatch_human_outbound(settings.human_outbound, job)
                    connection = sqlite3.connect(settings.human_outbound.ledger_file)
                    try:
                        row = connection.execute(
                            """SELECT status,attempt_count,next_attempt_at
                               FROM hermes_human_outbound"""
                        ).fetchone()
                        if expected_attempt < 3:
                            self.assertEqual("prepared", row[0])
                            self.assertEqual(expected_attempt, row[1])
                            self.assertGreater(row[2], 0)
                            connection.execute(
                                "UPDATE hermes_human_outbound SET next_attempt_at=0"
                            )
                            connection.commit()
                        else:
                            self.assertEqual(("failed", 3, 0), row)
                    finally:
                        connection.close()
            self.assertIsNone(
                module._claim_prepared_outbound(settings.human_outbound)
            )

    def test_other_user_bot_dm_and_unmapped_topic_never_submit(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self._human_outbound_settings(module, root)
            hook = module._build_hook(settings)
            store = self._session_store()

            def event(chat_id, thread_id, user_id, message_id, **extra):
                return types.SimpleNamespace(
                    text="nao enviar",
                    source=types.SimpleNamespace(
                        platform="telegram",
                        chat_id=chat_id,
                        thread_id=thread_id,
                        user_id=user_id,
                        **extra,
                    ),
                    message_id=message_id,
                    media_urls=[],
                    media_types=[],
                )

            with patch.object(module, "_submit_human_outbound") as submit:
                self.assertEqual(
                    "espelho-zap-human-outbound-not-authorized",
                    hook(
                        event("-100777", "42", "other-user", "m1"),
                        object(),
                        store,
                    )["reason"],
                )
                self.assertEqual(
                    "espelho-zap-human-outbound-not-authorized",
                    hook(
                        event("-100777", "42", "owner-1", "m2", is_bot=True),
                        object(),
                        store,
                    )["reason"],
                )
                self.assertEqual(
                    "espelho-zap-human-outbound-not-authorized",
                    hook(
                        event("-100777", "42", "owner-1", "m2-service", is_service=True),
                        object(),
                        store,
                    )["reason"],
                )
                self.assertEqual(
                    "espelho-zap-human-outbound-not-authorized",
                    hook(
                        event(
                            "-100777",
                            "42",
                            "owner-1",
                            "m2-automation",
                            message_type="automation",
                        ),
                        object(),
                        store,
                    )["reason"],
                )
                self.assertIsNone(
                    hook(event("123", "", "owner-1", "m3"), object(), store)
                )
                self.assertEqual(
                    "espelho-zap-human-outbound-blocked",
                    hook(
                        event("-100777", "99", "owner-1", "m4"),
                        object(),
                        store,
                    )["reason"],
                )
            submit.assert_not_called()

    def test_human_outbound_media_enforces_item_and_aggregate_limits(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            media_root = root / "telegram-media"
            media_root.mkdir()
            too_large = media_root / "too-large.bin"
            first = media_root / "first.bin"
            second = media_root / "second.bin"
            too_large.write_bytes(b"x" * 9)
            first.write_bytes(b"a" * 7)
            second.write_bytes(b"b" * 7)
            settings = self._human_outbound_settings(
                module, root, media_roots=(media_root.resolve(),)
            )
            hook = module._build_hook(settings)
            store = self._session_store()

            def event(message_id, paths):
                return types.SimpleNamespace(
                    text="",
                    source=types.SimpleNamespace(
                        platform="telegram",
                        chat_id="-100777",
                        thread_id="42",
                        user_id="owner-1",
                    ),
                    message_id=message_id,
                    media_urls=[str(path) for path in paths],
                    media_types=["document"] * len(paths),
                    raw_message=types.SimpleNamespace(
                        document=types.SimpleNamespace(file_name="arquivo.bin"),
                        caption="",
                    ),
                )

            with (
                patch.object(module, "_MAX_MEDIA_BYTES", 8),
                patch.object(module, "_MAX_TOTAL_MEDIA_BYTES", 12),
                patch.object(module, "_submit_human_outbound") as submit,
            ):
                self.assertEqual(
                    "espelho-zap-human-outbound-blocked",
                    hook(event("oversized-item", [too_large]), object(), store)["reason"],
                )
                self.assertEqual(
                    "espelho-zap-human-outbound-blocked",
                    hook(event("oversized-total", [first, second]), object(), store)["reason"],
                )
            submit.assert_not_called()

    def test_media_types_use_managed_copy_and_delete_only_after_confirmation(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            media_root = root / "telegram-media"
            media_root.mkdir()
            fixtures = [
                ("photo.jpg", "image/jpeg", b"photo"),
                ("file_0.oga", "audio/ogg", b"voice"),
                ("audio.mp3", "audio/mpeg", b"audio"),
                ("video.mp4", "video/mp4", b"video"),
                ("report.pdf", "application/pdf", b"document"),
            ]
            paths = []
            for name, _media_type, content in fixtures:
                path = media_root / name
                path.write_bytes(content)
                paths.append(str(path))
            settings = self._human_outbound_settings(
                module, root, media_roots=(media_root.resolve(),)
            )
            hook = module._build_hook(settings)
            store = self._session_store()
            event = types.SimpleNamespace(
                text="legenda original",
                source=types.SimpleNamespace(
                    platform="telegram",
                    chat_id="-100777",
                    thread_id="42",
                    user_id="owner-1",
                    is_bot=False,
                ),
                message_id="media-message",
                media_urls=paths,
                media_types=[item[1] for item in fixtures],
                raw_message=types.SimpleNamespace(
                    document=types.SimpleNamespace(file_name="report.pdf"),
                    caption="legenda original",
                ),
            )
            sent_jobs = []

            def confirmed(outbound, job):
                sent_jobs.append(job)
                self.assertTrue(all(Path(item["filePath"]).is_file() for item in job.media))
                return ("wa-1", "wa-2")

            with patch.object(module, "_post_human_outbound", side_effect=confirmed):
                with patch.object(
                    module,
                    "_submit_human_outbound",
                    side_effect=module._drain_human_outbound,
                ):
                    self.assertEqual(
                        "espelho-zap-human-outbound",
                        hook(event, object(), store)["reason"],
                    )
            self.assertEqual(1, len(sent_jobs))
            self.assertEqual(
                ["image", "voice", "audio", "video", "document"],
                [item["mediaType"] for item in sent_jobs[0].media],
            )
            self.assertEqual([], list(settings.human_outbound.managed_media_root.iterdir()))
            connection = sqlite3.connect(settings.human_outbound.ledger_file)
            try:
                status = connection.execute(
                    "SELECT status,remote_message_ids_json FROM hermes_human_outbound"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual("sent", status[0])
            self.assertEqual(["wa-1", "wa-2"], json.loads(status[1]))

    def test_official_telegram_document_preserves_original_bounded_file_name(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            media_root = root / "telegram-media"
            media_root.mkdir()
            cached = media_root / "doc_a1b2c3_relatorio.pdf"
            cached.write_bytes(b"document")
            settings = self._human_outbound_settings(
                module, root, media_roots=(media_root.resolve(),)
            )
            hook = module._build_hook(settings)
            store = self._session_store()
            event = types.SimpleNamespace(
                text="legenda",
                source=types.SimpleNamespace(
                    platform="telegram",
                    chat_id="-100777",
                    thread_id="42",
                    user_id="owner-1",
                    is_bot=False,
                ),
                message_id="document-name",
                media_urls=[str(cached)],
                media_types=["application/pdf"],
                raw_message=types.SimpleNamespace(
                    document=types.SimpleNamespace(
                        file_name=r"..\financeiro/relatorio final.pdf"
                    ),
                    caption="legenda",
                ),
            )
            captured = []

            def confirmed(outbound, job):
                captured.append(job)
                return ("wa-document",)

            with patch.object(module, "_post_human_outbound", side_effect=confirmed):
                with patch.object(
                    module,
                    "_submit_human_outbound",
                    side_effect=module._drain_human_outbound,
                ):
                    self.assertEqual(
                        "espelho-zap-human-outbound",
                        hook(event, object(), store)["reason"],
                    )
            self.assertEqual(1, len(captured))
            self.assertEqual("relatorio final.pdf", captured[0].media[0]["fileName"])

    def test_only_current_raw_telegram_content_can_become_human_outbound(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            media_root = root / "telegram-media"
            media_root.mkdir()
            document = media_root / "doc_deadbeef_decisao.txt"
            document.write_text("conteudo extraido que nao deve virar legenda", encoding="utf-8")
            replied_photo = media_root / "replied-photo.jpg"
            replied_photo.write_bytes(b"replied")
            settings = self._human_outbound_settings(
                module, root, media_roots=(media_root.resolve(),)
            )
            hook = module._build_hook(settings)
            store = self._session_store()
            source = types.SimpleNamespace(
                platform="telegram",
                chat_id="-100777",
                thread_id="42",
                user_id="owner-1",
                is_bot=False,
            )

            document_event = types.SimpleNamespace(
                text="[Content of decisao.txt]:\nconteudo extraido\n\nlegenda humana",
                source=source,
                message_id="raw-document",
                media_urls=[str(document)],
                media_types=["text/plain"],
                raw_message=types.SimpleNamespace(
                    document=types.SimpleNamespace(file_name="decisao.txt"),
                    caption="legenda humana",
                ),
            )
            sticker_event = types.SimpleNamespace(
                text="descricao vision gerada automaticamente",
                source=source,
                message_id="raw-sticker",
                media_urls=[],
                media_types=[],
                raw_message=types.SimpleNamespace(sticker=object()),
            )
            replied_media_event = types.SimpleNamespace(
                text=(
                    "resposta humana ao anexo anterior\n\n"
                    "[Replied-to image 'replied-photo.jpg' saved at: cache]"
                ),
                source=source,
                message_id="raw-reply",
                media_urls=[str(replied_photo)],
                media_types=["image/jpeg"],
                raw_message=types.SimpleNamespace(
                    text="resposta humana ao anexo anterior",
                    reply_to_message=types.SimpleNamespace(photo=[object()]),
                ),
            )
            unstaged_photo_event = types.SimpleNamespace(
                text="nota tecnica de falha do cache",
                source=source,
                message_id="raw-unstaged-photo",
                media_urls=[],
                media_types=[],
                raw_message=types.SimpleNamespace(
                    photo=[object()], caption="legenda humana"
                ),
            )
            with patch.object(module, "_submit_human_outbound") as submit:
                self.assertEqual(
                    "espelho-zap-human-outbound",
                    hook(document_event, object(), store)["reason"],
                )
                self.assertEqual(
                    "espelho-zap-human-outbound",
                    hook(replied_media_event, object(), store)["reason"],
                )
                for event in (sticker_event, unstaged_photo_event):
                    self.assertEqual(
                        "espelho-zap-human-outbound-blocked",
                        hook(event, object(), store)["reason"],
                    )
            self.assertEqual(2, submit.call_count)
            connection = sqlite3.connect(settings.human_outbound.ledger_file)
            try:
                row = connection.execute(
                    """SELECT text,media_json FROM hermes_human_outbound
                       WHERE request_id='telegram:-100777:42:raw-document'"""
                ).fetchone()
                reply_row = connection.execute(
                    """SELECT text,media_json FROM hermes_human_outbound
                       WHERE request_id='telegram:-100777:42:raw-reply'"""
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual("legenda humana", row[0])
            self.assertNotIn("conteudo extraido", row[1])
            self.assertEqual(
                ("resposta humana ao anexo anterior", "[]"), reply_row
            )

    def test_raw_text_batch_preserves_all_human_chunks(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self._human_outbound_settings(module, root)
            hook = module._build_hook(settings)
            event = types.SimpleNamespace(
                text="primeira parte\nsegunda parte",
                source=types.SimpleNamespace(
                    platform="telegram",
                    chat_id="-100777",
                    thread_id="42",
                    user_id="owner-1",
                    is_bot=False,
                ),
                message_id="batched-text",
                media_urls=[],
                media_types=[],
                raw_message=types.SimpleNamespace(text="primeira parte"),
            )
            with patch.object(module, "_submit_human_outbound"):
                self.assertEqual(
                    "espelho-zap-human-outbound",
                    hook(event, object(), self._session_store())["reason"],
                )
            connection = sqlite3.connect(settings.human_outbound.ledger_file)
            try:
                body = connection.execute(
                    "SELECT text FROM hermes_human_outbound"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual("primeira parte\nsegunda parte", body)

    def test_timeout_is_uncertain_preserves_media_and_replay_stays_blocked(self) -> None:
        module = load_hermes_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            media_root = root / "telegram-media"
            media_root.mkdir()
            photo = media_root / "photo.jpg"
            photo.write_bytes(b"photo")
            settings = self._human_outbound_settings(
                module, root, media_roots=(media_root.resolve(),)
            )
            hook = module._build_hook(settings)
            store = self._session_store()
            event = types.SimpleNamespace(
                text="",
                source=types.SimpleNamespace(
                    platform="telegram",
                    chat_id="-100777",
                    thread_id="42",
                    user_id="owner-1",
                ),
                message_id="timeout-message",
                media_urls=[str(photo)],
                media_types=["image/jpeg"],
                raw_message=types.SimpleNamespace(photo=[object()], caption=""),
            )
            with patch.object(
                module, "_post_human_outbound", side_effect=TimeoutError
            ):
                with patch.object(
                    module,
                    "_submit_human_outbound",
                    side_effect=module._drain_human_outbound,
                ):
                    self.assertEqual(
                        "espelho-zap-human-outbound",
                        hook(event, object(), store)["reason"],
                    )
                    self.assertEqual(
                        "espelho-zap-human-outbound-replay-blocked",
                        hook(event, object(), store)["reason"],
                    )
            connection = sqlite3.connect(settings.human_outbound.ledger_file)
            try:
                status = connection.execute(
                    "SELECT status FROM hermes_human_outbound"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual("uncertain", status)
            self.assertEqual(1, len(list(settings.human_outbound.managed_media_root.iterdir())))


class NativeIntegrationStaticContractTest(unittest.TestCase):
    def test_openclaw_plugin_uses_official_hook_and_shell_free_stdin(self) -> None:
        root = PROJECT / "integrations" / "openclaw"
        manifest = json.loads((root / "openclaw.plugin.json").read_text(encoding="utf-8"))
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        source = (root / "dist" / "index.js").read_text(encoding="utf-8")
        self.assertEqual("espelho-zap-portable", manifest["id"])
        self.assertEqual(["./dist/index.js"], package["openclaw"]["extensions"])
        self.assertIn('api.on("inbound_claim"', source)
        self.assertIn('api.on("message_received"', source)
        self.assertIn('api.on("before_agent_reply"', source)
        self.assertIn('api.on("message_sending"', source)
        self.assertIn('api.on("reply_payload_sending"', source)
        self.assertNotIn("registerHook", source)
        self.assertIn("spawn(cli", source)
        self.assertIn("shell: false", source)
        self.assertNotIn("exec(", source)
        self.assertIn("channels?.whatsapp?.pluginHooks?.messageReceived", source)
        self.assertIn("ESPELHO_ZAP_SOURCE_PROFILE_ID", source)
        self.assertIn("source_profile_id: runtimeProfile", source)
        message_hook_index = source.index('api.on("message_received"')
        hook_body = source[message_hook_index:]
        capture_body = source[source.index("async function capture(") : message_hook_index]
        self.assertIn("await queueIngest(payload)", capture_body)
        self.assertIn("recordHealth(healthFile, false, code)", source)
        self.assertIn("throw new Error(code)", hook_body)
        self.assertNotIn("throw error", hook_body)
        self.assertIn("return { handled: true }", source)
        self.assertIn("return { handled: true, reason: PASSIVE_REASON }", source)
        self.assertIn("return { cancel: true, cancelReason: PASSIVE_REASON }", source)
        self.assertIn("return { cancel: true, reason: PASSIVE_REASON }", source)
        self.assertIn('platformOf(event, ctx) !== "whatsapp"', source)
        claim_body = source[
            source.index('api.on("inbound_claim"') : source.index(
                'api.on("message_received"'
            )
        ]
        self.assertLess(
            claim_body.index("await capture(event, ctx"),
            claim_body.index("return { handled: true }"),
        )
        self.assertIn(
            "if (mediaStagingPending(event)) return undefined;",
            claim_body,
        )
        self.assertIn("event?.metadata?.originatingTo", source)
        self.assertIn("metadata.mediaPaths", source)
        self.assertNotIn("?? asNonEmpty(ctx?.channelId)", source)
        before_reply_body = source[
            source.index('api.on("before_agent_reply"') : source.index(
                'api.on("message_sending"'
            )
        ]
        self.assertIn('platformOf(event, ctx) !== "whatsapp"', before_reply_body)
        self.assertIn("return undefined", before_reply_body)
        self.assertNotIn('from "node:http"', source)
        self.assertNotIn('/mirror-human-send', source)
        self.assertIn("ESPELHO_ZAP_HUMAN_OUTBOUND_ALLOWED_USERS", source)
        self.assertIn("ESPELHO_ZAP_HUMAN_OUTBOUND_ROUTE_MAP", source)
        self.assertIn("ESPELHO_ZAP_HUMAN_OUTBOUND_LEDGER", source)
        self.assertIn('await loadAdapter("whatsapp")', source)
        self.assertIn("adapter.sendText", source)
        self.assertIn("adapter.sendMedia", source)
        self.assertIn("const MAX_MEDIA_BYTES = 128 * 1024 * 1024", source)
        self.assertIn("const MAX_TOTAL_MEDIA_BYTES = 256 * 1024 * 1024", source)
        self.assertIn("copyHumanMediaBounded", source)
        self.assertIn("isServiceOrAutomation(event, ctx)", source)
        self.assertIn('"service_message"', source)
        self.assertIn('"is_automation"', source)
        self.assertIn(
            "const entry = registerPendingHumanOutbound(event, ctx);",
            source,
        )
        self.assertIn("await entry.promise;", source)
        self.assertIn(
            "Promise.allSettled(entries.map((entry) => entry.promise))",
            before_reply_body,
        )
        self.assertIn(
            "const handled = { handled: true, reason: FORUM_DATA_PLANE_REASON };",
            before_reply_body,
        )
        self.assertIn("return handled;", before_reply_body)
        self.assertIn('api.on("gateway_start"', source)
        self.assertIn("lstatSync", source)
        self.assertIn("details.isSymbolicLink()", source)
        self.assertIn("fsConstants.O_NOFOLLOW", source)
        self.assertIn("fsyncDirectory(path.dirname(ledgerFile))", source)
        self.assertIn("chmodSync(directory, 0o700)", source)
        self.assertIn("fchmodSync(descriptor, 0o600)", source)
        self.assertNotIn('from "node:https"', source)
        integration_readme = (root / "README.md").read_text(encoding="utf-8")
        self.assertIn(
            "plugins.entries.espelho-zap-portable.hooks.allowConversationAccess=true",
            integration_readme,
        )
        if subprocess.run(["node", "--version"], capture_output=True).returncode == 0:
            checked = subprocess.run(
                ["node", "--check", str(root / "dist" / "index.js")],
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, checked.returncode, checked.stderr)

    def test_hermes_plugin_uses_fixed_authenticated_loopback_for_human_outbound(self) -> None:
        root = PROJECT / "integrations" / "hermes"
        manifest = (root / "plugin.yaml").read_text(encoding="utf-8")
        source = (root / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("pre_gateway_dispatch", manifest)
        self.assertIn('ctx.register_hook("pre_gateway_dispatch"', source)
        self.assertIn('{"action": "skip", "reason": "espelho-zap-passive"}', source)
        self.assertIn('_HUMAN_OUTBOUND_HOST = "127.0.0.1"', source)
        self.assertIn('_HUMAN_OUTBOUND_PORT = 3011', source)
        self.assertIn('_HUMAN_OUTBOUND_PATH = "/mirror-human-send"', source)
        self.assertIn('"x-espelho-token": token', source)
        self.assertIn("max_workers=1", source)
        self.assertIn('"requestId": job.request_id', source)
        self.assertIn('"chatId": job.destination', source)
        self.assertIn('"media": list(job.media)', source)
        self.assertIn("_prepare_outbound_ledger_path", source)
        self.assertIn('connection.execute("PRAGMA journal_mode=WAL")', source)
        self.assertIn('_harden_outbound_sqlite_files', source)
        self.assertIn("ESPELHO_ZAP_HUMAN_OUTBOUND_ARM_FILE", source)
        self.assertIn("ESPELHO_ZAP_HUMAN_OUTBOUND_MIRROR_LEDGER", source)
        self.assertIn("human_outbound_armed", source)
        self.assertIn("_require_human_outbound_arm(outbound)", source)
        self.assertIn('os.fchmod(descriptor, 0o700)', source)
        self.assertIn('os.fchmod(descriptor, 0o600)', source)
        for forbidden in ("requests", "urllib", "socket", "sendMessage", "send_whatsapp"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
