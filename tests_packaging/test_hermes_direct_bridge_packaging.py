from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]

from integrations.hermes.direct_bridge import bridge_guard  # noqa: E402
from integrations.hermes.direct_bridge import observer_launcher  # noqa: E402


FIXTURE = Path(__file__).with_name("fixtures") / "hermes_bridge_compatible.js"


class BridgeGuardTests(unittest.TestCase):
    def test_compatible_fixture_is_patched_before_manual_route_and_is_idempotent(self) -> None:
        original = FIXTURE.read_text(encoding="utf-8")
        patched = bridge_guard.patch_bridge_source(original)
        self.assertTrue(bridge_guard.bridge_is_guarded(patched))
        self.assertLess(
            patched.index(bridge_guard.GUARD_MARKER),
            patched.index("app.post('/mirror-manual-send'"),
        )
        self.assertIn("app.post(_ESPELHO_HUMAN_ROUTE", patched)
        self.assertIn("app.get(_ESPELHO_HUMAN_HEALTH_ROUTE", patched)
        self.assertIn("sendManualWithTimeout", patched)
        self.assertIn("_ESPELHO_BLOCKED_OUTBOUND_ROUTES", patched)
        self.assertIn("'/send-media'", patched)
        self.assertEqual(bridge_guard.patch_bridge_source(patched), patched)

    def test_v1_is_upgraded_in_place_without_parallel_guard(self) -> None:
        original = FIXTURE.read_text(encoding="utf-8")
        legacy = original.replace(
            bridge_guard._EXPRESS_ANCHOR,
            bridge_guard._EXPRESS_ANCHOR + bridge_guard._LEGACY_GUARD,
            1,
        )
        patched = bridge_guard.patch_bridge_source(legacy)
        self.assertTrue(bridge_guard.bridge_is_guarded(patched))
        self.assertEqual(patched.count(bridge_guard.GUARD_MARKER), 1)
        self.assertNotIn(bridge_guard.LEGACY_GUARD_MARKER, patched)

    def test_v2_is_upgraded_in_place_to_authenticated_health_route(self) -> None:
        original = FIXTURE.read_text(encoding="utf-8")
        prior = original.replace(
            bridge_guard._EXPRESS_ANCHOR,
            bridge_guard._EXPRESS_ANCHOR + bridge_guard._GUARD_V2,
            1,
        )
        patched = bridge_guard.patch_bridge_source(prior)
        self.assertTrue(bridge_guard.bridge_is_guarded(patched))
        self.assertEqual(patched.count(bridge_guard.GUARD_MARKER), 1)
        self.assertNotIn(bridge_guard.PRIOR_GUARD_MARKER, patched)
        self.assertIn("app.get(_ESPELHO_HUMAN_HEALTH_ROUTE", patched)

    def test_v3_is_upgraded_in_place_to_receipt_capable_v4(self) -> None:
        original = FIXTURE.read_text(encoding="utf-8")
        prior = original.replace(
            bridge_guard._EXPRESS_ANCHOR,
            bridge_guard._EXPRESS_ANCHOR + bridge_guard._GUARD_V3,
            1,
        )
        patched = bridge_guard.patch_bridge_source(prior)
        self.assertTrue(bridge_guard.bridge_is_guarded(patched))
        self.assertEqual(patched.count(bridge_guard.GUARD_MARKER), 1)
        self.assertNotIn(bridge_guard.PRIOR_GUARD_MARKER, patched)
        self.assertIn("_espelhoQueueOutboundReceipt", patched)
        self.assertIn("message-receipt.update", patched)

    def test_old_v4_receipt_hook_is_upgraded_for_async_socket_creation(self) -> None:
        original = FIXTURE.read_text(encoding="utf-8")
        prior = original.replace(
            bridge_guard._EXPRESS_ANCHOR,
            bridge_guard._EXPRESS_ANCHOR + bridge_guard._GUARD_V4_OLD,
            1,
        )
        patched = bridge_guard.patch_bridge_source(prior)
        self.assertIn("_espelhoInstallReceiptHooks", patched)
        self.assertIn("_espelhoReceiptHookTimer", patched)
        self.assertTrue(bridge_guard.bridge_is_guarded(patched))

    def test_incompatible_source_fails_closed(self) -> None:
        with self.assertRaises(bridge_guard.BridgeGuardError):
            bridge_guard.patch_bridge_source("app.use(express.json());")

    def test_source_without_top_level_bridge_state_bindings_fails_closed(self) -> None:
        original = FIXTURE.read_text(encoding="utf-8")
        for old, replacement in (
            ("const sock = {};", "sock = {};"),
            (
                "const connectionState = process.env.ESPELHO_FIXTURE_CONNECTION_STATE || 'connected';",
                "connectionState = 'connected';",
            ),
        ):
            with self.subTest(binding=old.split()[1]):
                incompatible = original.replace(old, replacement, 1)
                with self.assertRaisesRegex(
                    bridge_guard.BridgeGuardError,
                    "top-level socket or connection-state bindings",
                ):
                    bridge_guard.patch_bridge_source(incompatible)

    def test_tampered_v2_guard_fails_closed(self) -> None:
        original = FIXTURE.read_text(encoding="utf-8")
        patched = bridge_guard.patch_bridge_source(original)
        tampered = patched.replace(
            "const _ESPELHO_HUMAN_ROUTE = '/mirror-human-send';",
            "const _ESPELHO_HUMAN_ROUTE = '/tampered-send';",
            1,
        )
        self.assertFalse(bridge_guard.bridge_is_guarded(tampered))
        with self.assertRaises(bridge_guard.BridgeGuardError):
            bridge_guard.patch_bridge_source(tampered)

    def test_apply_requires_new_backup_and_preserves_original(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bridge = root / "bridge.js"
            backup = root / "backup" / "bridge.js"
            original = FIXTURE.read_text(encoding="utf-8")
            bridge.write_text(original, encoding="utf-8")
            if os.name == "posix":
                bridge.chmod(0o644)
            source_stat = bridge.stat()
            self.assertTrue(bridge_guard.apply_bridge_guard(bridge, backup.resolve()))
            self.assertEqual(backup.read_text(encoding="utf-8"), original)
            bridge_guard.check_bridge(bridge.resolve())
            if os.name == "posix":
                guarded_stat = bridge.stat()
                self.assertEqual(source_stat.st_uid, guarded_stat.st_uid)
                self.assertEqual(source_stat.st_gid, guarded_stat.st_gid)
            self.assertFalse(
                bridge_guard.apply_bridge_guard(bridge.resolve(), (root / "unused").resolve())
            )

    def _run_fixture(
        self,
        root: Path,
        request: dict[str, object],
        *,
        token: str,
        connection_state: str = "connected",
        environment_overrides: dict[str, str] | None = None,
    ) -> dict[str, object]:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is required for the executable bridge fixture")
        bridge = root / "bridge.js"
        bridge.write_text(
            bridge_guard.patch_bridge_source(FIXTURE.read_text(encoding="utf-8")),
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment.update(
            {
                "ESPELHO_ZAP_HUMAN_OUTBOUND_TOKEN_FILE": str(root / "token"),
                "ESPELHO_ZAP_HUMAN_OUTBOUND_MEDIA_ROOT": str(root / "media"),
                "ESPELHO_FIXTURE_REQUEST": json.dumps(request, separators=(",", ":")),
                "ESPELHO_FIXTURE_CONNECTION_STATE": connection_state,
            }
        )
        environment.update(environment_overrides or {})
        completed = subprocess.run(
            [node, str(bridge)],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
            timeout=15,
        )
        return json.loads(completed.stdout)

    def _runtime_root(self, root: Path) -> str:
        token = "t" * 64
        (root / "media").mkdir(mode=0o700)
        (root / "token").write_text(token, encoding="utf-8")
        if os.name == "posix":
            (root / "media").chmod(0o700)
            (root / "token").chmod(0o600)
        return token

    def test_runtime_sends_text_once_and_keeps_legacy_routes_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            token = self._runtime_root(root)
            request = {
                "method": "POST",
                "path": "/mirror-human-send",
                "headers": {"x-espelho-token": token},
                "body": {
                    "requestId": "telegram:1:2:3",
                    "chatId": "15550000001@s.whatsapp.net",
                    "text": "mensagem humana",
                    "media": [],
                },
            }
            result = self._run_fixture(root, request, token=token)
            self.assertEqual(result["status"], 200)
            self.assertEqual(result["body"]["messageIds"], ["sent-1"])
            self.assertEqual(len(result["sends"]), 1)
            self.assertEqual(result["sends"][0]["payload"], {"text": "mensagem humana"})

            unavailable = self._run_fixture(
                root,
                request,
                token=token,
                connection_state="close",
            )
            self.assertEqual(unavailable["status"], 503)
            self.assertIs(unavailable["body"]["attempted"], False)
            self.assertEqual(unavailable["sends"], [])

            group_request = json.loads(json.dumps(request))
            group_request["body"]["requestId"] = "telegram:1:2:group"
            group_request["body"]["chatId"] = "120363000000000000@g.us"
            group = self._run_fixture(root, group_request, token=token)
            self.assertEqual(group["status"], 200)
            self.assertEqual(group["sends"][0]["chatId"], "120363000000000000@g.us")

            for legacy_path in (
                "/send",
                "/send/",
                "/send//",
                "/send%2F",
                "/send-media",
                "/mirror-manual-send",
            ):
                blocked = self._run_fixture(
                    root,
                    {"method": "POST", "path": legacy_path, "body": {}},
                    token=token,
                )
                self.assertEqual(blocked["status"], 405, legacy_path)
                self.assertEqual(blocked["sends"], [])

    def test_runtime_health_is_authenticated_read_only_and_reports_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            token = self._runtime_root(root)
            healthy = self._run_fixture(
                root,
                {
                    "method": "GET",
                    "path": "/mirror-human-health",
                    "headers": {"x-espelho-token": token},
                },
                token=token,
            )
            self.assertEqual(healthy["status"], 200)
            self.assertEqual(
                healthy["body"],
                {
                    "schemaVersion": 1,
                    "guardVersion": 4,
                    "observeOnly": True,
                    "connectionState": "connected",
                    "connected": True,
                },
            )
            self.assertEqual(healthy["sends"], [])

            disconnected = self._run_fixture(
                root,
                {
                    "method": "GET",
                    "path": "/mirror-human-health",
                    "headers": {"x-espelho-token": token},
                },
                token=token,
                connection_state="close",
            )
            self.assertEqual(disconnected["status"], 200)
            self.assertIs(disconnected["body"]["connected"], False)
            self.assertEqual(disconnected["sends"], [])

            forbidden = self._run_fixture(
                root,
                {
                    "method": "GET",
                    "path": "/mirror-human-health",
                    "headers": {"x-espelho-token": "wrong"},
                },
                token=token,
            )
            self.assertEqual(forbidden["status"], 403)
            self.assertEqual(forbidden["sends"], [])

    def test_runtime_sends_all_media_sequentially_without_duplicate_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            token = self._runtime_root(root)
            media_items = []
            for index, media_type in enumerate(
                ("image", "audio", "voice", "video", "document"), start=1
            ):
                payload = f"media-{media_type}".encode()
                media_path = root / "media" / f"item-{index}.bin"
                media_path.write_bytes(payload)
                media_items.append(
                    {
                        "filePath": str(media_path),
                        "mediaType": media_type,
                        "mimeType": "application/octet-stream",
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "sizeBytes": len(payload),
                        "caption": "legenda" if index == 1 else "nao-repetir",
                        "fileName": f"item-{index}.bin",
                    }
                )
            result = self._run_fixture(
                root,
                {
                    "method": "POST",
                    "path": "/mirror-human-send",
                    "headers": {"x-espelho-token": token},
                    "body": {
                        "requestId": "telegram:1:2:album",
                        "chatId": "15550000001@s.whatsapp.net",
                        "text": "legenda",
                        "media": media_items,
                    },
                },
                token=token,
            )
            self.assertEqual(result["status"], 200)
            self.assertEqual(len(result["body"]["messageIds"]), 5)
            self.assertEqual(len(result["sends"]), 5)
            self.assertEqual(result["sends"][0]["payload"]["caption"], "legenda")
            self.assertNotIn("caption", result["sends"][1]["payload"])
            self.assertFalse(result["sends"][1]["payload"]["ptt"])
            self.assertTrue(result["sends"][2]["payload"]["ptt"])
            self.assertFalse(any("text" in sent["payload"] for sent in result["sends"]))

    def test_runtime_rejects_bad_token_outside_root_and_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            token = self._runtime_root(root)
            outside = root / "outside.bin"
            outside.write_bytes(b"outside")
            base_request = {
                "method": "POST",
                "path": "/mirror-human-send",
                "headers": {"x-espelho-token": token},
                "body": {
                    "requestId": "telegram:1:2:bad",
                    "chatId": "15550000001@s.whatsapp.net",
                    "text": "",
                    "media": [
                        {
                            "filePath": str(outside),
                            "mediaType": "document",
                            "sha256": hashlib.sha256(b"outside").hexdigest(),
                            "sizeBytes": 7,
                        }
                    ],
                },
            }
            bad_token = json.loads(json.dumps(base_request))
            bad_token["headers"]["x-espelho-token"] = "wrong"
            result = self._run_fixture(root, bad_token, token=token)
            self.assertEqual(result["status"], 403)
            self.assertEqual(result["sends"], [])

            non_loopback = json.loads(json.dumps(base_request))
            non_loopback["remoteAddress"] = "192.0.2.10"
            result = self._run_fixture(root, non_loopback, token=token)
            self.assertEqual(result["status"], 403)
            self.assertEqual(result["body"]["error"], "loopback_required")
            self.assertEqual(result["sends"], [])

            result = self._run_fixture(root, base_request, token=token)
            self.assertEqual(result["status"], 400)
            self.assertEqual(result["body"]["error"], "media_outside_managed_root")
            self.assertEqual(result["sends"], [])

            inside = root / "media" / "inside.bin"
            inside.write_bytes(b"inside")
            bad_hash = json.loads(json.dumps(base_request))
            bad_hash["body"]["requestId"] = "telegram:1:2:hash"
            bad_hash["body"]["media"][0].update(
                {"filePath": str(inside), "sha256": "0" * 64, "sizeBytes": 6}
            )
            result = self._run_fixture(root, bad_hash, token=token)
            self.assertEqual(result["status"], 409)
            self.assertEqual(result["body"]["error"], "media_sha256_mismatch")
            self.assertEqual(result["sends"], [])

    def test_runtime_rejects_aggregate_media_before_any_send(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            token = self._runtime_root(root)
            items = []
            for index in range(2):
                payload = b"123456"
                media_path = root / "media" / f"total-{index}.bin"
                media_path.write_bytes(payload)
                items.append(
                    {
                        "filePath": str(media_path),
                        "mediaType": "document",
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "sizeBytes": len(payload),
                    }
                )
            result = self._run_fixture(
                root,
                {
                    "method": "POST",
                    "path": "/mirror-human-send",
                    "headers": {"x-espelho-token": token},
                    "body": {
                        "requestId": "telegram:1:2:total",
                        "chatId": "15550000001@s.whatsapp.net",
                        "text": "",
                        "media": items,
                    },
                },
                token=token,
                environment_overrides={
                    "ESPELHO_ZAP_HUMAN_OUTBOUND_MAX_MEDIA_BYTES": "8",
                    "ESPELHO_ZAP_HUMAN_OUTBOUND_MAX_TOTAL_MEDIA_BYTES": "10",
                },
            )
            self.assertEqual(result["status"], 413)
            self.assertEqual(result["body"]["error"], "media_total_too_large")
            self.assertIs(result["body"]["attempted"], False)
            self.assertEqual(result["sends"], [])


class ObserverLauncherTests(unittest.TestCase):
    def _settings(self, root: Path) -> observer_launcher.ObserverSettings:
        return observer_launcher.ObserverSettings(
            node=(root / "node").resolve(),
            bridge_js=(root / "bridge.js").resolve(),
            session_dir=(root / "session").resolve(),
            spool_file=(root / "spool" / "messages.jsonl").resolve(),
            cache_root=(root / "cache").resolve(),
            lock_file=(root / "run" / "observer.lock").resolve(),
            port=3011,
            mode="bot",
            dm_policy="open",
            group_policy="disabled",
            allowed_users="*",
            forward_owner_messages=False,
            human_outbound_token_file=None,
            human_outbound_media_root=None,
            debug=False,
        )

    def test_environment_clears_unconfigured_outbound_even_if_inherited(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = self._settings(Path(temporary))
            environment = observer_launcher.build_environment(
                settings,
                {
                    "WHATSAPP_OBSERVE_ONLY": "false",
                    "WHATSAPP_GROUP_ONLY_CAPTURE": "true",
                    "ESPELHO_ZAP_HUMAN_OUTBOUND_TOKEN_FILE": "/stale/token",
                    "ESPELHO_ZAP_HUMAN_OUTBOUND_MEDIA_ROOT": "/stale/media",
                    "NODE_OPTIONS": "--require=/untrusted/preload.js",
                    "NODE_PATH": "/untrusted/modules",
                },
            )
        self.assertEqual(environment["WHATSAPP_OBSERVE_ONLY"], "true")
        self.assertEqual(environment["WHATSAPP_GROUP_ONLY_CAPTURE"], "false")
        self.assertEqual(environment["ESPELHO_ZAP_HUMAN_OUTBOUND_TOKEN_FILE"], "")
        self.assertEqual(environment["ESPELHO_ZAP_HUMAN_OUTBOUND_MEDIA_ROOT"], "")
        self.assertEqual(environment["WHATSAPP_FORWARD_OWNER_MESSAGES"], "false")
        self.assertNotIn("NODE_OPTIONS", environment)
        self.assertNotIn("NODE_PATH", environment)

    def test_load_settings_accepts_path_overrides_but_not_non_bot_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = root / "direct-bridge.toml"
            config.write_text(
                "schema_version = 1\n[bridge]\n"
                f'node = "{(root / "node").as_posix()}"\n'
                f'bridge_js = "{(root / "bridge.js").as_posix()}"\n'
                f'session_dir = "{(root / "session").as_posix()}"\n'
                f'spool_file = "{(root / "spool/messages.jsonl").as_posix()}"\n'
                f'cache_root = "{(root / "cache").as_posix()}"\n'
                f'lock_file = "{(root / "run/observer.lock").as_posix()}"\n'
                'port = 3011\nmode = "bot"\ndm_policy = "open"\n'
                'group_policy = "disabled"\nallowed_users = "*"\n'
                "forward_owner_messages = false\n"
                'human_outbound_token_file = ""\n'
                'human_outbound_media_root = ""\n'
                "debug = false\n",
                encoding="utf-8",
            )
            overridden = (root / "other-session").resolve()
            settings = observer_launcher.load_settings(
                config,
                {"ESPELHO_ZAP_BRIDGE_SESSION_DIR": str(overridden)},
            )
            self.assertEqual(settings.session_dir, overridden)
            with self.assertRaises(observer_launcher.LauncherError):
                observer_launcher.load_settings(
                    config,
                    {"ESPELHO_ZAP_BRIDGE_MODE": "self-chat"},
                )

    def test_human_outbound_paths_are_explicit_and_override_inherited_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            settings = self._settings(root)
            token = root / "human.token"
            media = root / "human-media"
            configured = observer_launcher.ObserverSettings(
                **{
                    **settings.__dict__,
                    "human_outbound_token_file": token,
                    "human_outbound_media_root": media,
                }
            )
            environment = observer_launcher.build_environment(
                configured,
                {
                    "ESPELHO_ZAP_HUMAN_OUTBOUND_TOKEN_FILE": "/inherited/token",
                    "ESPELHO_ZAP_HUMAN_OUTBOUND_MEDIA_ROOT": "/inherited/media",
                },
            )
        self.assertEqual(environment["WHATSAPP_OBSERVE_ONLY"], "true")
        self.assertEqual(environment["ESPELHO_ZAP_HUMAN_OUTBOUND_TOKEN_FILE"], str(token))
        self.assertEqual(environment["ESPELHO_ZAP_HUMAN_OUTBOUND_MEDIA_ROOT"], str(media))

    def test_human_outbound_requires_fixed_plugin_port(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = root / "direct-bridge.toml"
            config.write_text(
                "schema_version = 1\n[bridge]\n"
                f'node = "{(root / "node").as_posix()}"\n'
                f'bridge_js = "{(root / "bridge.js").as_posix()}"\n'
                f'session_dir = "{(root / "session").as_posix()}"\n'
                f'spool_file = "{(root / "spool/messages.jsonl").as_posix()}"\n'
                f'cache_root = "{(root / "cache").as_posix()}"\n'
                f'lock_file = "{(root / "run/observer.lock").as_posix()}"\n'
                'port = 3012\nmode = "bot"\ndm_policy = "open"\n'
                'group_policy = "disabled"\nallowed_users = "*"\n'
                "forward_owner_messages = false\n"
                f'human_outbound_token_file = "{(root / "token").as_posix()}"\n'
                f'human_outbound_media_root = "{(root / "media").as_posix()}"\n'
                "debug = false\n",
                encoding="utf-8",
            )
            with self.assertRaises(observer_launcher.LauncherError):
                observer_launcher.load_settings(config)

    def test_system_level_units_are_prepared_only_and_hardened(self) -> None:
        systemd = PROJECT / "packaging" / "systemd"
        observer = (systemd / "espelho-zap-hermes-observer@.service.in").read_text(
            encoding="utf-8"
        )
        worker = (systemd / "espelho-zap-hermes-worker@.service.in").read_text(
            encoding="utf-8"
        )
        timer = (systemd / "espelho-zap-hermes-worker@.timer.in").read_text(
            encoding="utf-8"
        )
        for unit in (observer, worker):
            self.assertIn("User=@SERVICE_USER@", unit)
            self.assertIn("Group=@SERVICE_GROUP@", unit)
            self.assertIn("UMask=0077", unit)
            self.assertIn("Restart=on-failure", unit)
            self.assertIn("ProtectSystem=strict", unit)
            self.assertIn("ReadWritePaths=", unit)
        self.assertIn("bridge_guard", observer.lower())
        self.assertIn("@HUMAN_OUTBOUND_TOKEN_FILE@", observer)
        self.assertIn("@HUMAN_OUTBOUND_MEDIA_ROOT@", observer)
        self.assertIn("RuntimeDirectory=espelho-zap-%i", observer)
        self.assertIn("observer-once --bridge-url @BRIDGE_URL@", worker)
        self.assertIn("ExecStart=-@ESPELHO_ZAP_BIN@", worker)
        self.assertIn("EnvironmentFile=-@WORKER_ENV_FILE@", worker)
        self.assertIn("@SOURCE_MEDIA_ROOTS@", worker)
        self.assertIn("StartLimitIntervalSec=120", worker)
        self.assertIn("StartLimitBurst=12", worker)
        self.assertLess(worker.index("observer-once"), worker.index("worker-drain"))
        self.assertIn("Unit=espelho-zap-hermes-worker@%i.service", timer)
        self.assertNotIn("WantedBy=default.target", observer + worker + timer)


if __name__ == "__main__":
    unittest.main()
