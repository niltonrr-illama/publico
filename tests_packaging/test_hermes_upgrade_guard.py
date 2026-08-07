from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch


from integrations.hermes import upgrade_guard


class HermesUpgradeGuardTests(unittest.TestCase):
    def _private_text(self, path: Path, value: str) -> None:
        path.write_text(value, encoding="utf-8")
        if os.name == "posix":
            path.chmod(0o600)

    def _fixture(self, root: Path) -> argparse.Namespace:
        profile = "operator_profile"
        plugin = root / "plugin.py"
        runtime_component = root / "runtime.fingerprint"
        runtime_component.write_text("b" * 64 + "\n", encoding="utf-8")
        plugin.write_text(
            "from pathlib import Path\n"
            "def _hermes_runtime_fingerprint():\n"
            "    return Path(__file__).with_name('runtime.fingerprint').read_text(encoding='utf-8').strip()\n"
            "def register(*args, **kwargs):\n"
            "    raise AssertionError('register_must_not_run')\n",
            encoding="utf-8",
        )
        if os.name == "posix":
            plugin.chmod(0o644)
        plugin_sha256 = hashlib.sha256(plugin.read_bytes()).hexdigest()
        marker = root / "startup.json"
        arm = root / "arm.json"
        ledger = root / "ledger.sqlite3"
        token = root / "bridge.token"
        profile_config = root / "config.yaml"
        env_file = root / ".env"
        release = "a" * 40
        runtime = "b" * 64
        registered_at = int(time.time())

        self._private_text(token, "t" * 64 + "\n")
        self._private_text(
            marker,
            json.dumps(
                {
                    "schema_version": 1,
                    "plugin_version": "0.3.1",
                    "release_commit": release,
                    "plugin_sha256": plugin_sha256,
                    "hermes_runtime_fingerprint": runtime,
                    "human_outbound_enabled": True,
                    "human_outbound_armed": False,
                    "gateway_pid": 321,
                    "registered_at": registered_at,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
        )
        os.utime(marker, (registered_at, registered_at))
        self._private_text(
            arm,
            json.dumps(
                {
                    "schema_version": 1,
                    "release_commit": release,
                    "plugin_sha256": plugin_sha256,
                    "hermes_runtime_fingerprint": runtime,
                    "armed": True,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
        )
        self._private_text(
            profile_config,
            "plugins:\n"
            "  enabled:\n"
            "  - espelho-zap-portable\n"
            "  disabled: [espelho-zap-passive]\n",
        )
        self._private_text(
            env_file,
            "ESPELHO_ZAP_HUMAN_OUTBOUND_ENABLED=enabled\n"
            f"ESPELHO_ZAP_RELEASE_COMMIT={release}\n"
            f"ESPELHO_ZAP_HUMAN_OUTBOUND_ARM_FILE={arm}\n"
            f"ESPELHO_ZAP_HUMAN_OUTBOUND_LEDGER={ledger}\n"
            f"ESPELHO_ZAP_HUMAN_OUTBOUND_STARTUP_MARKER={marker}\n"
            f"ESPELHO_ZAP_HUMAN_OUTBOUND_TOKEN_FILE={token}\n",
        )
        connection = sqlite3.connect(ledger)
        try:
            connection.execute(
                "CREATE TABLE hermes_human_outbound(request_id TEXT, status TEXT)"
            )
            connection.commit()
        finally:
            connection.close()
        if os.name == "posix":
            ledger.chmod(0o600)

        proc_root = root / "proc"
        process = proc_root / "321"
        process.mkdir(parents=True)
        (process / "environ").write_bytes(b"HERMES_PROFILE=operator_profile\0")
        (process / "cmdline").write_bytes(b"python\0-m\0hermes_cli.main\0gateway\0")
        return argparse.Namespace(
            profile=profile,
            env_file=env_file,
            profile_config=profile_config,
            plugin_file=plugin,
            marker_file=marker,
            arm_file=arm,
            ledger_file=ledger,
            token_file=token,
            runtime_component=runtime_component,
            proc_root=proc_root,
            rearm_compatible_update=False,
        )

    def _argv(self, args: argparse.Namespace, *, rearm: bool = False) -> list[str]:
        result = [
            "--profile",
            args.profile,
            "--env-file",
            str(args.env_file),
            "--profile-config",
            str(args.profile_config),
            "--plugin-file",
            str(args.plugin_file),
            "--marker-file",
            str(args.marker_file),
            "--arm-file",
            str(args.arm_file),
            "--ledger-file",
            str(args.ledger_file),
            "--token-file",
            str(args.token_file),
            "--proc-root",
            str(args.proc_root),
        ]
        if rearm:
            result.append("--rearm-compatible-update")
        return result

    def test_plugin_selection_requires_portable_once_and_passive_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            valid = root / "valid.yaml"
            self._private_text(
                valid,
                "plugins:\n  enabled: [platforms/telegram, espelho-zap-portable]\n"
                "  disabled:\n    - espelho-zap-passive\n"
                "  entries:\n"
                "    espelho-zap-portable:\n"
                "      enabled: true\n"
                "      disabled: false\n",
            )
            self.assertEqual(
                upgrade_guard._plugin_lists(valid),
                (
                    ["platforms/telegram", "espelho-zap-portable"],
                    ["espelho-zap-passive"],
                ),
            )
            invalid = root / "invalid.yaml"
            self._private_text(
                invalid,
                "plugins:\n  enabled: [espelho-zap-portable, espelho-zap-passive]\n"
                "  disabled: []\n",
            )
            enabled, disabled = upgrade_guard._plugin_lists(invalid)
            self.assertIn("espelho-zap-passive", enabled)
            self.assertEqual([], disabled)

    def test_ledger_requires_quick_check_and_reports_inflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self._fixture(root)
            self.assertEqual(
                {"prepared": 0, "sending": 0},
                upgrade_guard._validate_ledger(args.ledger_file),
            )
            connection = sqlite3.connect(args.ledger_file)
            try:
                connection.execute(
                    "INSERT INTO hermes_human_outbound VALUES ('one','prepared')"
                )
                connection.commit()
            finally:
                connection.close()
            self.assertEqual(
                {"prepared": 1, "sending": 0},
                upgrade_guard._validate_ledger(args.ledger_file),
            )

    def test_bridge_probe_is_fixed_authenticated_get_and_never_sends(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            token = root / "token"
            self._private_text(token, "x" * 64)
            observed: dict[str, object] = {}

            class Response:
                status = 200

                def read(self, _maximum):
                    return json.dumps(
                        {
                            "schemaVersion": 1,
                            "guardVersion": 4,
                            "observeOnly": True,
                            "connectionState": "connected",
                            "connected": True,
                        }
                    ).encode()

            class Connection:
                def __init__(self, host, port, timeout):
                    observed.update(host=host, port=port, timeout=timeout)

                def request(self, method, path, *, headers):
                    observed.update(method=method, path=path, headers=headers)

                def getresponse(self):
                    return Response()

                def close(self):
                    pass

            with patch.object(upgrade_guard.http.client, "HTTPConnection", Connection):
                upgrade_guard._validate_bridge(token)
            self.assertEqual("127.0.0.1", observed["host"])
            self.assertEqual(3011, observed["port"])
            self.assertEqual("GET", observed["method"])
            self.assertEqual("/mirror-human-health", observed["path"])

    def test_false_marker_runtime_fingerprint_is_rejected_and_disarmed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self._fixture(Path(temporary))
            marker = json.loads(args.marker_file.read_text(encoding="utf-8"))
            marker["hermes_runtime_fingerprint"] = "c" * 64
            self._private_text(
                args.marker_file,
                json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
            )
            os.utime(
                args.marker_file,
                (marker["registered_at"], marker["registered_at"]),
            )
            arm = json.loads(args.arm_file.read_text(encoding="utf-8"))
            arm["hermes_runtime_fingerprint"] = "c" * 64
            self._private_text(
                args.arm_file,
                json.dumps(arm, sort_keys=True, separators=(",", ":")) + "\n",
            )
            with patch.object(
                upgrade_guard,
                "_process_start_ns",
                return_value=(int(time.time()) - 5) * 1_000_000_000,
            ), patch.object(upgrade_guard, "_validate_bridge"):
                self.assertEqual(2, upgrade_guard.main(self._argv(args)))
            self.assertFalse(args.arm_file.exists())

    def test_runtime_component_change_after_startup_is_detected_and_disarmed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self._fixture(Path(temporary))
            args.runtime_component.write_text("c" * 64 + "\n", encoding="utf-8")
            with patch.object(
                upgrade_guard,
                "_process_start_ns",
                return_value=(int(time.time()) - 5) * 1_000_000_000,
            ), patch.object(upgrade_guard, "_validate_bridge"):
                self.assertEqual(2, upgrade_guard.main(self._argv(args)))
            self.assertFalse(args.arm_file.exists())

    def test_plugin_runtime_probe_blocks_network_and_never_calls_register(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self._fixture(Path(temporary))
            args.plugin_file.write_text(
                "import socket\n"
                "socket.socket()\n"
                "def _hermes_runtime_fingerprint():\n"
                "    return 'b' * 64\n"
                "def register(*args, **kwargs):\n"
                "    raise AssertionError('register_must_not_run')\n",
                encoding="utf-8",
            )
            plugin_sha256 = hashlib.sha256(args.plugin_file.read_bytes()).hexdigest()
            marker = json.loads(args.marker_file.read_text(encoding="utf-8"))
            marker["plugin_sha256"] = plugin_sha256
            self._private_text(
                args.marker_file,
                json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
            )
            os.utime(
                args.marker_file,
                (marker["registered_at"], marker["registered_at"]),
            )
            arm = json.loads(args.arm_file.read_text(encoding="utf-8"))
            arm["plugin_sha256"] = plugin_sha256
            self._private_text(
                args.arm_file,
                json.dumps(arm, sort_keys=True, separators=(",", ":")) + "\n",
            )
            with patch.object(
                upgrade_guard,
                "_process_start_ns",
                return_value=(int(time.time()) - 5) * 1_000_000_000,
            ), patch.object(upgrade_guard, "_validate_bridge"):
                self.assertEqual(2, upgrade_guard.main(self._argv(args)))
            self.assertFalse(args.arm_file.exists())

    def test_compatible_rearm_copies_current_runtime_only_after_all_gates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self._fixture(root)
            args.rearm_compatible_update = True
            stale = json.loads(args.arm_file.read_text(encoding="utf-8"))
            stale["hermes_runtime_fingerprint"] = "c" * 64
            self._private_text(
                args.arm_file,
                json.dumps(stale, sort_keys=True, separators=(",", ":")) + "\n",
            )
            with patch.object(
                upgrade_guard,
                "_process_start_ns",
                return_value=(int(time.time()) - 5) * 1_000_000_000,
            ), patch.object(upgrade_guard, "_validate_bridge"):
                self.assertEqual("rearm", upgrade_guard.run_guard(args))
            armed = json.loads(args.arm_file.read_text(encoding="utf-8"))
            self.assertEqual("b" * 64, armed["hermes_runtime_fingerprint"])
            self.assertEqual(upgrade_guard._ARM_KEYS, frozenset(armed))

    def test_valid_arm_with_sending_defers_without_replacement_or_disarm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self._fixture(root)
            args.rearm_compatible_update = True
            original = args.arm_file.read_bytes()
            connection = sqlite3.connect(args.ledger_file)
            try:
                connection.execute(
                    "INSERT INTO hermes_human_outbound VALUES ('one','sending')"
                )
                connection.commit()
            finally:
                connection.close()
            with patch.object(
                upgrade_guard,
                "_process_start_ns",
                return_value=(int(time.time()) - 5) * 1_000_000_000,
            ), patch.object(upgrade_guard, "_validate_bridge"):
                self.assertEqual("defer", upgrade_guard.run_guard(args))
            self.assertEqual(original, args.arm_file.read_bytes())

    def test_sending_with_incompatible_arm_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self._fixture(root)
            stale = json.loads(args.arm_file.read_text(encoding="utf-8"))
            stale["hermes_runtime_fingerprint"] = "c" * 64
            self._private_text(
                args.arm_file,
                json.dumps(stale, sort_keys=True, separators=(",", ":")) + "\n",
            )
            connection = sqlite3.connect(args.ledger_file)
            try:
                connection.execute(
                    "INSERT INTO hermes_human_outbound VALUES ('one','sending')"
                )
                connection.commit()
            finally:
                connection.close()
            with patch.object(
                upgrade_guard,
                "_process_start_ns",
                return_value=(int(time.time()) - 5) * 1_000_000_000,
            ), patch.object(upgrade_guard, "_validate_bridge"):
                self.assertEqual(2, upgrade_guard.main(self._argv(args, rearm=True)))
            self.assertFalse(args.arm_file.exists())

    def test_prepared_queue_allows_compatible_rearm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self._fixture(root)
            args.rearm_compatible_update = True
            stale = json.loads(args.arm_file.read_text(encoding="utf-8"))
            stale["hermes_runtime_fingerprint"] = "c" * 64
            self._private_text(
                args.arm_file,
                json.dumps(stale, sort_keys=True, separators=(",", ":")) + "\n",
            )
            connection = sqlite3.connect(args.ledger_file)
            try:
                connection.execute(
                    "INSERT INTO hermes_human_outbound VALUES ('one','prepared')"
                )
                connection.commit()
            finally:
                connection.close()
            with patch.object(
                upgrade_guard,
                "_process_start_ns",
                return_value=(int(time.time()) - 5) * 1_000_000_000,
            ), patch.object(upgrade_guard, "_validate_bridge"):
                self.assertEqual("rearm", upgrade_guard.run_guard(args))
            armed = json.loads(args.arm_file.read_text(encoding="utf-8"))
            self.assertEqual("b" * 64, armed["hermes_runtime_fingerprint"])

    def test_any_failed_gate_revokes_existing_arm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self._fixture(root)
            with patch.object(
                upgrade_guard,
                "_process_start_ns",
                return_value=(int(time.time()) - 5) * 1_000_000_000,
            ), patch.object(
                upgrade_guard,
                "_validate_bridge",
                side_effect=upgrade_guard.GuardError("bridge_health_invalid"),
            ):
                self.assertEqual(2, upgrade_guard.main(self._argv(args)))
            self.assertFalse(args.arm_file.exists())

    def test_unexpected_exception_also_revokes_arm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self._fixture(Path(temporary))
            with patch.object(
                upgrade_guard,
                "run_guard",
                side_effect=ValueError("unexpected internal bug"),
            ):
                self.assertEqual(2, upgrade_guard.main(self._argv(args)))
            self.assertFalse(args.arm_file.exists())

    def test_failed_regular_arm_unlink_is_not_reported_as_disarmed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            arm = Path(temporary) / "arm.json"
            self._private_text(arm, "{}\n")
            with patch.object(Path, "unlink", side_effect=PermissionError):
                self.assertFalse(upgrade_guard._atomic_disarm(arm))
            self.assertTrue(arm.exists())

    def test_stale_marker_pid_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self._fixture(root)
            marker = json.loads(args.marker_file.read_text(encoding="utf-8"))
            marker["gateway_pid"] = 999
            self._private_text(
                args.marker_file,
                json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
            )
            with patch.object(
                upgrade_guard,
                "_process_start_ns",
                return_value=(int(time.time()) - 5) * 1_000_000_000,
            ), patch.object(upgrade_guard, "_validate_bridge"):
                with self.assertRaisesRegex(
                    upgrade_guard.GuardError, "startup_marker_mismatch"
                ):
                    upgrade_guard.run_guard(args)

    def test_systemd_templates_are_prepared_only_and_support_explicit_rearm(self) -> None:
        project = Path(__file__).resolve().parents[1]
        service = (
            project
            / "packaging"
            / "systemd"
            / "espelho-zap-hermes-upgrade-guard@.service.in"
        ).read_text(encoding="utf-8")
        timer = (
            project
            / "packaging"
            / "systemd"
            / "espelho-zap-hermes-upgrade-guard@.timer.in"
        ).read_text(encoding="utf-8")
        for marker in (
            "User=@SERVICE_USER@",
            "@UPGRADE_GUARD@",
            "@REARM_FLAG@",
            "@LEDGER_PARENT_DIR@",
            "ReadWritePaths=@ARM_PARENT_DIR@ @LEDGER_PARENT_DIR@",
        ):
            self.assertIn(marker, service)
        self.assertNotIn("User=root", service)
        self.assertNotRegex(service, r"systemctl\s+(?:enable|start|restart)")
        self.assertIn("OnUnitActiveSec=60s", timer)
        self.assertIn("Unit=espelho-zap-hermes-upgrade-guard@%i.service", timer)

    def test_guard_has_no_external_endpoint_or_send_method(self) -> None:
        source = Path(upgrade_guard.__file__).read_text(encoding="utf-8")
        self.assertIn('_BRIDGE_HOST = "127.0.0.1"', source)
        self.assertIn('"GET"', source)
        for forbidden in ("https://", "http://", '"POST"', "sendMessage"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
