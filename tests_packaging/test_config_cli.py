from __future__ import annotations

from contextlib import closing, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
SRC = PROJECT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from espelho_zap import cli  # noqa: E402
from espelho_zap import MirrorLedger, opaque_ref  # noqa: E402
from espelho_zap.config import (  # noqa: E402
    ConfigError,
    load_config,
    resolve_telegram_token,
    write_default_config,
)


def private_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if os.name == "posix":
        path.chmod(0o600)


def config_text(root: Path) -> str:
    return f'''schema_version = 1

[paths]
data_dir = "{(root / 'data').as_posix()}"
state_dir = "{(root / 'state').as_posix()}"
ledger_path = "{(root / 'data' / 'mirror.sqlite3').as_posix()}"
minimum_free_bytes = 1

[telegram]
api_base = "https://api.telegram.org"
token_env = "TEST_ESPELHO_ZAP_TOKEN"
token_file = "{(root / 'config' / 'telegram.token').as_posix()}"
timeout_seconds = 1

[worker]
worker_id = "test-worker"
profile_id = "test-profile"
runtime_lock_seconds = 120
lease_seconds = 60
max_attempts = 2
base_backoff_seconds = 1
allowed_temp_root = ""

[legacy]
default_chat_id = ""
'''


def run_cli(*args: str) -> tuple[int, dict[str, object], str]:
    output = io.StringIO()
    with redirect_stdout(output):
        code = cli.main(list(args))
    raw = output.getvalue().strip()
    return code, json.loads(raw), raw


class ConfigTests(unittest.TestCase):
    def test_generated_config_is_secret_free_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "config" / "config.toml"
            created_path, created = write_default_config(target, data_dir=root / "data")
            self.assertTrue(created)
            raw = created_path.read_text(encoding="utf-8")
            self.assertIn("token_env", raw)
            self.assertIn("token_file", raw)
            self.assertIn("maximum_spool_bytes = 1073741824", raw)
            self.assertNotIn("\ntoken =", raw)
            if os.name == "posix":
                self.assertEqual(created_path.stat().st_mode & 0o777, 0o600)

    def test_literal_token_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            private_write(path, '[telegram]\ntoken = "do-not-store-this"\n')
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_environment_token_takes_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "config" / "config.toml"
            private_write(path, config_text(root))
            token_file = root / "config" / "telegram.token"
            private_write(token_file, "from-file\n")
            config = load_config(path)
            value = resolve_telegram_token(config, environ={"TEST_ESPELHO_ZAP_TOKEN": "from-env"})
            self.assertEqual(value, "from-env")


class CLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = self.root / "config" / "config.toml"
        private_write(self.config, config_text(self.root))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_init_doctor_health_and_route_commands(self) -> None:
        code, payload, _ = run_cli("--config", str(self.config), "init")
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])

        code, payload, _ = run_cli(
            "--config", str(self.config), "doctor", "--allow-missing-token"
        )
        self.assertEqual(code, 0)
        self.assertTrue(payload["result"]["ready"])

        code, _, _ = run_cli(
            "--config",
            str(self.config),
            "route",
            "set",
            "conversation-1",
            "-100123",
            "42",
        )
        self.assertEqual(code, 0)
        code, payload, _ = run_cli("--config", str(self.config), "route", "list")
        self.assertEqual(code, 0)
        self.assertEqual(payload["result"]["count"], 1)
        self.assertNotIn("conversation_id", payload["result"]["routes"][0])
        self.assertNotIn("chat_id", payload["result"]["routes"][0])

        code, payload, _ = run_cli(
            "--config", str(self.config), "route", "list", "--show-identifiers"
        )
        self.assertEqual(code, 0)
        self.assertIn("conversation_id", payload["result"]["routes"][0])

        code, payload, _ = run_cli("--config", str(self.config), "health")
        self.assertEqual(code, 0)
        self.assertEqual(payload["result"]["ledger"]["quick_check"], "ok")

    def test_ingest_output_does_not_echo_content(self) -> None:
        run_cli("--config", str(self.config), "init")
        run_cli(
            "--config",
            str(self.config),
            "route",
            "set",
            "conversation-1",
            "-100123",
            "42",
        )
        event = self.root / "event.json"
        event.write_text(
            json.dumps(
                {
                    "event_id": "event-1",
                    "source": "whatsapp",
                    "conversation_id": "conversation-1",
                    "occurred_at": "2026-08-04T00:00:00Z",
                    "actor_ref": "actor-1",
                    "text": "DO_NOT_ECHO_MESSAGE_CONTENT",
                }
            ),
            encoding="utf-8",
        )
        code, payload, raw = run_cli("--config", str(self.config), "ingest", str(event))
        self.assertEqual(code, 0)
        self.assertTrue(payload["result"]["has_text"])
        self.assertEqual(payload["result"]["privacy_scope"], "owner_private")
        self.assertNotIn("DO_NOT_ECHO_MESSAGE_CONTENT", raw)
        self.assertNotIn("conversation-1", raw)

    def test_observer_once_cli_emits_only_aggregate_state(self) -> None:
        from espelho_zap.adapters.hermes_bridge import ObserverResult

        run_cli("--config", str(self.config), "init")
        result = ObserverResult(fetched=1, selected=1, inserted=1, enqueued=1, acked=1)
        with mock.patch(
            "espelho_zap.adapters.hermes_bridge.HermesBridgeObserver.observe_once",
            return_value=result,
        ):
            code, payload, raw = run_cli(
                "--config",
                str(self.config),
                "observer-once",
                "--bridge-url",
                "http://127.0.0.1:3011",
                "--batch-limit",
                "1",
            )
        self.assertEqual(0, code)
        self.assertEqual(1, payload["result"]["inserted"])
        self.assertEqual(1, payload["result"]["acked"])
        self.assertNotIn("message", raw.lower())

    def test_argument_error_is_json(self) -> None:
        code, payload, _ = run_cli("route")
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "invalid_arguments")

    def test_runtime_lock_blocks_route_mutation(self) -> None:
        run_cli("--config", str(self.config), "init")
        config = load_config(self.config)
        profile_id = opaque_ref("profile", config.worker.profile_id)
        with MirrorLedger(config.paths.ledger_path) as ledger:
            self.assertTrue(
                ledger.acquire_runtime_lock(
                    profile_id,
                    "other-owner",
                    lease_seconds=120,
                )
            )
            code, payload, raw = run_cli(
                "--config",
                str(self.config),
                "route",
                "set",
                "sensitive-conversation-id",
                "-100123",
                "42",
            )
            ledger.release_runtime_lock(profile_id, "other-owner")
        self.assertEqual(code, 5)
        self.assertEqual(payload["error"]["code"], "runtime_lock_unavailable")
        self.assertNotIn("sensitive-conversation-id", raw)

    def test_worker_idle_is_aggregate_and_does_not_contact_provider(self) -> None:
        run_cli("--config", str(self.config), "init")
        with mock.patch.dict(os.environ, {"TEST_ESPELHO_ZAP_TOKEN": "NEVER_ECHO_TOKEN"}):
            code, payload, raw = run_cli(
                "--config", str(self.config), "worker-once", "--profile", "test-profile"
            )
        self.assertEqual(code, 0)
        self.assertFalse(payload["result"]["processed"])
        self.assertEqual(payload["result"]["outcome"]["status"], "idle")
        self.assertNotIn("NEVER_ECHO_TOKEN", raw)

    def test_route_blocks_are_opaque_and_require_explicit_reconcile(self) -> None:
        run_cli("--config", str(self.config), "init")
        event = self.root / "blocked-event.json"
        event.write_text(
            json.dumps(
                {
                    "event_id": "blocked-event-1",
                    "source": "whatsapp",
                    "conversation_id": "sensitive-conversation-id",
                    "occurred_at": "2026-08-04T00:00:00Z",
                    "actor_ref": "sensitive-actor-id",
                    "text": "SENSITIVE_BLOCKED_TEXT",
                }
            ),
            encoding="utf-8",
        )
        code, payload, raw = run_cli("--config", str(self.config), "ingest", str(event))
        self.assertEqual(code, 5)
        self.assertEqual(payload["error"]["code"], "route_missing")
        self.assertNotIn("SENSITIVE_BLOCKED_TEXT", raw)

        code, payload, raw = run_cli(
            "--config", str(self.config), "route", "blocked-list"
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["result"]["count"], 1)
        block = payload["result"]["blocks"][0]
        conversation_ref = block["conversation_ref"]
        self.assertRegex(conversation_ref, r"^conversation:[0-9a-f]{64}$")
        self.assertRegex(block["event_ref"], r"^event:[0-9a-f]{64}$")
        self.assertNotIn("sensitive-conversation-id", raw)

        code, _, _ = run_cli(
            "--config",
            str(self.config),
            "route",
            "set",
            conversation_ref,
            "-100123",
            "42",
        )
        self.assertEqual(code, 0)
        code, payload, _ = run_cli(
            "--config",
            str(self.config),
            "route",
            "reconcile",
            conversation_ref,
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["result"]["requeued"], 1)
        self.assertEqual(payload["result"]["ledger"]["blocked_no_route"], 0)

        code, payload, _ = run_cli(
            "--config", str(self.config), "route", "blocked-list", "--state", "requeued"
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["result"]["count"], 1)

    def test_get_chat_verifies_forum_without_sending(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self, limit: int) -> bytes:
                self.limit = limit
                return json.dumps(
                    {
                        "ok": True,
                        "result": {
                            "id": -100123,
                            "type": "supergroup",
                            "is_forum": True,
                            "title": "DO_NOT_ECHO_CHAT_TITLE",
                        },
                    }
                ).encode("utf-8")

        requests = []

        def fake_urlopen(request, *, timeout):
            requests.append((request, timeout))
            return Response()

        with mock.patch.dict(os.environ, {"TEST_ESPELHO_ZAP_TOKEN": "NEVER_ECHO_TOKEN"}), mock.patch.object(
            cli.urllib_request, "urlopen", side_effect=fake_urlopen
        ):
            code, payload, raw = run_cli(
                "--config",
                str(self.config),
                "route",
                "verify-destination",
                "-100123",
                "--thread-id",
                "42",
            )
        self.assertEqual(code, 0)
        self.assertTrue(payload["result"]["verified"])
        self.assertTrue(payload["result"]["read_only"])
        self.assertFalse(payload["result"]["message_sent"])
        self.assertFalse(payload["result"]["topic"]["existence_verified"])
        self.assertEqual(len(requests), 1)
        request, timeout = requests[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertTrue(request.full_url.endswith("/getChat"))
        self.assertEqual(request.data, b"chat_id=-100123")
        self.assertEqual(timeout, 1)
        self.assertNotIn("NEVER_ECHO_TOKEN", raw)
        self.assertNotIn("DO_NOT_ECHO_CHAT_TITLE", raw)
        self.assertNotIn("-100123", raw)

    def test_get_chat_rejects_non_forum_without_sending(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self, limit: int) -> bytes:
                return b'{"ok":true,"result":{"id":-100123,"type":"supergroup"}}'

        with mock.patch.dict(os.environ, {"TEST_ESPELHO_ZAP_TOKEN": "token"}), mock.patch.object(
            cli.urllib_request, "urlopen", return_value=Response()
        ):
            code, payload, _ = run_cli(
                "--config", str(self.config), "route", "verify-destination", "-100123"
            )
        self.assertEqual(code, 4)
        self.assertFalse(payload["result"]["verified"])
        self.assertFalse(payload["result"]["message_sent"])

    def test_backup_is_verified_atomic_and_never_overwrites(self) -> None:
        run_cli("--config", str(self.config), "init")
        destination = self.root / "backups" / "ledger.sqlite3"
        code, payload, raw = run_cli(
            "--config", str(self.config), "backup", str(destination)
        )
        self.assertEqual(code, 0)
        self.assertTrue(payload["result"]["created"])
        self.assertFalse(payload["result"]["overwrite"])
        self.assertEqual(payload["result"]["source_quick_check"], "ok")
        self.assertEqual(payload["result"]["backup_quick_check"], "ok")
        self.assertTrue(destination.is_file())
        with closing(sqlite3.connect(destination)) as connection:
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
        expected_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
        self.assertEqual(payload["result"]["sha256"], expected_hash)
        self.assertEqual(payload["result"]["size_bytes"], destination.stat().st_size)
        self.assertNotIn(str(destination), raw)

        original = destination.read_bytes()
        code, payload, _ = run_cli(
            "--config", str(self.config), "backup", str(destination)
        )
        self.assertEqual(code, 5)
        self.assertEqual(payload["error"]["code"], "backup_destination_exists")
        self.assertEqual(destination.read_bytes(), original)

    def test_doctor_enforces_configured_disk_floor(self) -> None:
        private_write(
            self.config,
            config_text(self.root).replace(
                "minimum_free_bytes = 1", "minimum_free_bytes = 9223372036854775807"
            ),
        )
        code, payload, _ = run_cli(
            "--config", str(self.config), "doctor", "--allow-missing-token"
        )
        self.assertEqual(code, 4)
        checks = {item["name"]: item for item in payload["result"]["checks"]}
        self.assertFalse(checks["disk_space"]["ok"])


if __name__ == "__main__":
    unittest.main()
