from __future__ import annotations

from pathlib import Path
import os
import shlex
import shutil
import subprocess
import tempfile
import tomllib
import unittest


PROJECT = Path(__file__).resolve().parents[1]


def posix_contract_bash() -> str | None:
    explicit = os.environ.get("ESPELHO_ZAP_POSIX_TEST_BASH")
    if explicit:
        return explicit
    if os.name == "posix":
        return shutil.which("bash")
    return None


def shell_path(path: Path) -> str:
    return path.as_posix() if os.name == "nt" else str(path)


class PackagingArtifactTests(unittest.TestCase):
    def test_console_script_and_python_floor(self) -> None:
        with (PROJECT / "pyproject.toml").open("rb") as handle:
            data = tomllib.load(handle)
        self.assertEqual(data["project"]["requires-python"], ">=3.11")
        self.assertEqual(data["project"]["scripts"]["espelho-zap"], "espelho_zap.cli:main")
        self.assertEqual(data["project"]["dependencies"], [])
        self.assertEqual(data["project"]["license"], "Apache-2.0")

    def test_installer_contract_is_present(self) -> None:
        raw = (PROJECT / "installer" / "install.sh").read_text(encoding="utf-8")
        for marker in (
            "--dry-run",
            "--runtime",
            "--runtime-home",
            "--media-root",
            "--clear-media-roots",
            "--enable-runtime",
            "preflight",
            "rollback venv backup retained",
            "relocate_candidate_venv",
            "virtualenv launchers: rebound to final installation path",
            "SKILL_ROOT",
            ".espelho-zap-managed",
            "contained_by",
            "reject_symlink",
            "timer status: disabled by default",
            "flock -n",
            "runtime-staging",
            "backup_transaction_data",
            "restore_transaction_data",
            "worker.profile_id",
            "runtime activation backup: retained",
            "restore_runtime_activation_state",
            "runtime-activation.state",
            "deactivate_selected_runtime",
            "restore_original_units_for_uninstall",
            "activation is fail-closed",
            "channels.whatsapp.pluginHooks.messageReceived",
            "plugins inspect espelho-zap-portable --runtime --json",
            "ESPELHO_ZAP_SOURCE_PROFILE_ID",
            "ESPELHO_ZAP_HOOK_HEALTH_FILE",
            "capture-health.json",
            "source_media_roots",
            "preserved:",
            "0700",
            "0600",
        ):
            self.assertIn(marker, raw)

        move_offset = raw.index('mv -- "${venv_candidate}" "${VENV}"')
        relocate_offset = raw.index(
            'relocate_candidate_venv "${venv_candidate}" "${VENV}"'
        )
        initialize_offset = raw.index("initialize_runtime ||")
        self.assertLess(move_offset, relocate_offset)
        self.assertLess(relocate_offset, initialize_offset)

    def test_sdist_manifest_keeps_runtime_assets(self) -> None:
        raw = (PROJECT / "MANIFEST.in").read_text(encoding="utf-8")
        for marker in (
            "recursive-include src *.py",
            "recursive-include installer *.sh",
            "recursive-include integrations *",
            "recursive-include packaging *",
            "recursive-include skills *",
        ):
            self.assertIn(marker, raw)
        self.assertNotIn("recursive-exclude * __pycache__ *", raw)
        for legacy in (
            "capture", "capture_v2", "curation", "migration", "mirror",
            "second_brain", "tests",
        ):
            self.assertIn(f"prune {legacy}", raw)
        self.assertNotIn("recursive-include capture_v2", raw)
        self.assertNotIn("recursive-include mirror", raw)

    def test_installer_bash_syntax(self) -> None:
        if os.name != "posix":
            self.skipTest("bash syntax validation requires a POSIX runtime")
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is not installed")
        subprocess.run(
            [
                bash,
                "-n",
                str(PROJECT / "installer" / "install.sh"),
                str(PROJECT / "installer" / "smoke-runtime-targets.sh"),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_hermes_observer_installer_is_atomic_disabled_and_reversible(self) -> None:
        raw = (PROJECT / "installer" / "install.sh").read_text(encoding="utf-8")
        for marker in (
            "--prepare-hermes-observer",
            "--hermes-observer-profile",
            "--hermes-bridge-config",
            "--hermes-bridge-js",
            "--hermes-human-outbound-token-file",
            "--hermes-human-outbound-media-root",
            "--hermes-service-user",
            "--hermes-service-group",
            "prepare_hermes_observer_candidate",
            "render_hermes_observer_unit",
            "systemd-analyze verify",
            "check_hermes_observer_competitors",
            "rollback_hermes_observer",
            "exact prior Hermes observer root and unit restored",
            "preflight_hermes_observer",
            "independent system preparation; no per-user artifacts",
        ):
            self.assertIn(marker, raw)

        activate_start = raw.index("activate_hermes_observer()")
        activate_end = raw.index("rollback_hermes_observer()", activate_start)
        activate = raw[activate_start:activate_end]
        self.assertLess(
            activate.index("systemd-analyze verify"),
            activate.index('mv -- "${HERMES_OBSERVER_UNIT_CANDIDATE}"'),
        )
        self.assertIn('HERMES_OBSERVER_UNIT_LOAD_STATE}" == "loaded', activate)
        self.assertIn('HERMES_OBSERVER_UNIT_ACTIVE_STATE}" == "inactive', activate)
        self.assertIn('HERMES_OBSERVER_UNIT_ENABLED_STATE}" == "disabled', activate)
        self.assertNotRegex(
            activate,
            r"systemctl(?:\s+--\S+)*\s+(?:enable|start|restart)\b",
        )
        self.assertLess(
            activate.index('HERMES_OBSERVER_ROOT_HAD="${HERMES_OBSERVER_INSTALLED}"'),
            activate.index("HERMES_OBSERVER_ACTIVATED=1"),
        )
        self.assertLess(
            activate.index("HERMES_OBSERVER_ACTIVATED=1"),
            activate.index('mv -- "${HERMES_OBSERVER_ROOT}"'),
        )
        self.assertIn('-m 0700 -- "${HERMES_OBSERVER_BACKUP_DIR}"', activate)

        render_start = raw.index("render_hermes_observer_unit()")
        render_end = raw.index("cleanup_hermes_observer_candidate()", render_start)
        render = raw[render_start:render_end]
        for placeholder in (
            "SERVICE_USER",
            "SERVICE_GROUP",
            "DIRECT_BRIDGE_GUARD",
            "DIRECT_BRIDGE_LAUNCHER",
            "DIRECT_BRIDGE_CONFIG",
            "HUMAN_OUTBOUND_TOKEN_FILE",
            "HUMAN_OUTBOUND_MEDIA_ROOT",
        ):
            self.assertIn(f'"{placeholder}"', render)
        self.assertIn("template_placeholders != set(replacements)", render)
        self.assertIn('replace("%", "%%")', render)

        prepare_start = raw.index("prepare_hermes_observer_candidate()")
        prepare_end = raw.index("render_hermes_observer_unit()", prepare_start)
        prepare = raw[prepare_start:prepare_end]
        self.assertIn('-m 0550', prepare)
        self.assertIn('chmod 0600 -- "${config}"', prepare)
        self.assertIn(
            'chown "${HERMES_SERVICE_USER}:${HERMES_SERVICE_GROUP}"', prepare
        )
        self.assertIn("os.chmod(destination, 0o600)", prepare)
        self.assertLess(
            prepare.index("validate_hermes_observer_managed_directories"),
            prepare.index("runuser --user"),
        )
        self.assertIn("direct bridge config source must have mode 0600", raw)
        self.assertIn("paths must not contain systemd/template metacharacters", raw)
        self.assertIn('or "%" in value', raw)
        self.assertIn('or "@" in value', raw)
        self.assertIn("token must be owned by the service user/group with mode 0600", raw)
        self.assertIn("install -d -o root -g root -m 0711", raw)

        directory_check_start = raw.index(
            "validate_hermes_observer_managed_directories()"
        )
        directory_check_end = raw.index(
            "configure_hermes_observer_target()", directory_check_start
        )
        directory_check = raw[directory_check_start:directory_check_end]
        for marker in (
            'cache_root / "images"',
            'cache_root / "documents"',
            'cache_root / "audio"',
            "spool_file.parent",
            "lock_file.parent",
            "must already exist",
            "stat.S_IMODE(details.st_mode) != 0o700",
        ):
            self.assertIn(marker, directory_check)
        self.assertNotIn(".mkdir(", directory_check)

        main_start = raw.index("main()")
        observer_branch_start = raw.index(
            "if (( PREPARE_HERMES_OBSERVER )); then", main_start
        )
        observer_branch_end = raw.index("return 0", observer_branch_start)
        observer_branch = raw[observer_branch_start:observer_branch_end]
        self.assertIn("preflight_hermes_observer", observer_branch)
        self.assertIn("prepare_hermes_observer_transaction", observer_branch)
        self.assertIn('LOCK_DIR="${HERMES_OBSERVER_LOCK_DIR}"', observer_branch)
        self.assertIn('HERMES_OBSERVER_LOCK_DIR="/tmp/', raw)
        self.assertNotIn("install_or_upgrade", observer_branch)
        self.assertNotIn("private_directories", observer_branch)
        self.assertNotIn("systemctl --user", observer_branch)

    def test_hermes_activation_validates_native_config_before_restart(self) -> None:
        raw = (PROJECT / "installer" / "install.sh").read_text(encoding="utf-8")
        function_start = raw.index("enable_selected_runtime()")
        start = raw.index('if [[ "${RUNTIME}" == "hermes" ]]; then', function_start)
        end = raw.index("  else", start)
        branch = raw[start:end]
        self.assertIn("runtime_command config validate", branch)
        self.assertLess(branch.index("runtime_command config validate"), branch.index("runtime_command gateway restart"))

    def test_hermes_observer_signal_and_failure_cleanup_contract(self) -> None:
        raw = (PROJECT / "installer" / "install.sh").read_text(encoding="utf-8")
        transaction_start = raw.index("prepare_hermes_observer_transaction()")
        transaction_end = raw.index("escape_sed_replacement()", transaction_start)
        transaction = raw[transaction_start:transaction_end]
        self.assertIn("trap 'hermes_observer_signal_abort INT' INT", transaction)
        self.assertIn("trap 'hermes_observer_signal_abort TERM' TERM", transaction)
        self.assertIn("if ! hermes_observer_transaction_body; then", transaction)
        self.assertLess(
            transaction.index("rollback_hermes_observer || rollback_status=$?"),
            transaction.index('die "Hermes observer preparation failed'),
        )
        self.assertIn("rollback was incomplete; retained backups require review", transaction)
        self.assertLess(
            transaction.index("HERMES_OBSERVER_BASE_HAD=1"),
            transaction.index("trap 'hermes_observer_signal_abort INT' INT"),
        )

        signal_start = raw.index("hermes_observer_signal_abort()")
        signal_end = raw.index("preflight_hermes_observer()", signal_start)
        signal = raw[signal_start:signal_end]
        self.assertIn("trap '' INT TERM", signal)
        self.assertIn("rollback_hermes_observer", signal)
        self.assertIn('exit "${status}"', signal)

        cleanup_start = raw.index("cleanup_hermes_observer_candidate()")
        cleanup_end = raw.index("activate_hermes_observer()", cleanup_start)
        cleanup = raw[cleanup_start:cleanup_end]
        self.assertIn('rm -rf -- "${HERMES_OBSERVER_CANDIDATE}"', cleanup)
        self.assertIn('rm -rf -- "${HERMES_OBSERVER_UNIT_CANDIDATE_DIR}"', cleanup)
        self.assertIn('rmdir -- "${HERMES_OBSERVER_BASE}"', cleanup)
        self.assertNotIn('rmdir -- "${HERMES_OBSERVER_BASE}" 2>/dev/null || true', cleanup)

        gate_start = raw.index("check_hermes_observer_competitors()")
        gate_end = raw.index("resolve_source_profile_id()", gate_start)
        self.assertNotIn("die ", raw[gate_start:gate_end])

    def test_hermes_observer_posix_signal_traps_remove_candidates(self) -> None:
        bash = posix_contract_bash()
        if bash is None:
            self.skipTest("POSIX bash is unavailable")
        raw = (PROJECT / "installer" / "install.sh").read_text(encoding="utf-8")
        library = raw.rsplit('main "$@"', 1)[0]
        for signal_name, expected_status in (("INT", 130), ("TERM", 143)):
            with self.subTest(signal=signal_name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                base = root / "observer"
                candidate = base / ".default.new-test"
                unit_candidate = root / ".unit-candidate"
                candidate.mkdir(parents=True)
                unit_candidate.mkdir()
                (candidate / "partial").write_text("candidate", encoding="utf-8")
                harness = root / "signal-contract.sh"
                harness.write_text(
                    library
                    + "\n"
                    + f"HERMES_OBSERVER_BASE={shlex.quote(shell_path(base))}\n"
                    + f"HERMES_OBSERVER_CANDIDATE={shlex.quote(shell_path(candidate))}\n"
                    + "HERMES_OBSERVER_ROOT=''\n"
                    + "HERMES_OBSERVER_UNIT=''\n"
                    + "HERMES_OBSERVER_UNIT_CANDIDATE_DIR="
                    + f"{shlex.quote(shell_path(unit_candidate))}\n"
                    + "HERMES_OBSERVER_PROFILE=default\n"
                    + "HERMES_OBSERVER_BASE_HAD=1\n"
                    + "HERMES_OBSERVER_ACTIVATED=0\n"
                    + "HERMES_OBSERVER_TRANSACTION_ACTIVE=1\n"
                    + f"trap 'hermes_observer_signal_abort {signal_name}' {signal_name}\n"
                    + f"kill -{signal_name} $$\n"
                    + "exit 99\n",
                    encoding="utf-8",
                    newline="\n",
                )
                completed = subprocess.run(
                    [bash, shell_path(harness)],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(
                    completed.returncode, expected_status, completed.stderr
                )
                self.assertFalse(candidate.exists())
                self.assertFalse(unit_candidate.exists())

    def test_hermes_observer_posix_term_restores_published_prior_bytes(self) -> None:
        bash = posix_contract_bash()
        if bash is None:
            self.skipTest("POSIX bash is unavailable")
        raw = (PROJECT / "installer" / "install.sh").read_text(encoding="utf-8")
        library = raw.rsplit('main "$@"', 1)[0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "observer"
            observer = base / "default"
            backup_dir = base / ".default.backup-test"
            backup = backup_dir / "default"
            candidate = base / ".default.new-test"
            unit = root / "espelho-zap-hermes-observer@default.service"
            unit_backup = root / ".unit.backup-test"
            unit_candidate = root / ".unit-candidate"
            observer.mkdir(parents=True)
            backup.mkdir(parents=True)
            candidate.mkdir()
            unit_candidate.mkdir()
            marker = "managed_by=espelho-zap-portable-installer-v1\nprofile=default\n"
            (observer / ".espelho-zap-hermes-observer-managed").write_text(
                marker, encoding="utf-8"
            )
            (observer / "payload").write_text("new", encoding="utf-8")
            (backup / ".espelho-zap-hermes-observer-managed").write_text(
                marker, encoding="utf-8"
            )
            (backup / "payload").write_text("old", encoding="utf-8")
            unit.write_text(
                "# managed_by=espelho-zap-portable-installer-v1\n"
                "# profile=default\nnew unit\n",
                encoding="utf-8",
            )
            unit_backup.write_text(
                "# managed_by=espelho-zap-portable-installer-v1\n"
                "# profile=default\nold unit\n",
                encoding="utf-8",
            )
            (candidate / "partial").write_text("candidate", encoding="utf-8")
            harness = root / "published-signal-contract.sh"
            assignments = {
                "HERMES_OBSERVER_BASE": base,
                "HERMES_OBSERVER_ROOT": observer,
                "HERMES_OBSERVER_BACKUP_DIR": backup_dir,
                "HERMES_OBSERVER_BACKUP": backup,
                "HERMES_OBSERVER_CANDIDATE": candidate,
                "HERMES_OBSERVER_UNIT": unit,
                "HERMES_OBSERVER_UNIT_BACKUP": unit_backup,
                "HERMES_OBSERVER_UNIT_CANDIDATE_DIR": unit_candidate,
            }
            setup = "".join(
                f"{name}={shlex.quote(shell_path(path))}\n"
                for name, path in assignments.items()
            )
            harness.write_text(
                library
                + "\n"
                + setup
                + "HERMES_OBSERVER_PROFILE=default\n"
                + "HERMES_OBSERVER_BASE_HAD=1\n"
                + "HERMES_OBSERVER_ROOT_HAD=1\n"
                + "HERMES_OBSERVER_UNIT_HAD=1\n"
                + "HERMES_OBSERVER_ACTIVATED=1\n"
                + "HERMES_OBSERVER_TRANSACTION_ACTIVE=1\n"
                + "systemctl() { return 0; }\n"
                + "trap 'hermes_observer_signal_abort TERM' TERM\n"
                + "kill -TERM $$\n"
                + "exit 99\n",
                encoding="utf-8",
                newline="\n",
            )
            completed = subprocess.run(
                [bash, shell_path(harness)], capture_output=True, text=True, timeout=15
            )
            self.assertEqual(completed.returncode, 143, completed.stderr)
            self.assertEqual((observer / "payload").read_text(encoding="utf-8"), "old")
            self.assertIn("old unit", unit.read_text(encoding="utf-8"))
            self.assertFalse(backup_dir.exists())
            self.assertFalse(unit_backup.exists())
            self.assertFalse(candidate.exists())
            self.assertFalse(unit_candidate.exists())

    def test_systemd_units_are_parameterized(self) -> None:
        service = (PROJECT / "packaging" / "systemd" / "espelho-zap@.service").read_text(
            encoding="utf-8"
        )
        timer = (PROJECT / "packaging" / "systemd" / "espelho-zap@.timer").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("--profile %i", service)
        self.assertIn("worker-drain --max-items 100 --max-seconds 50", service)
        self.assertIn("Unit=espelho-zap@%i.service", timer)
        self.assertNotIn("WantedBy=default.target", service)

    def test_skill_has_portable_agent_metadata(self) -> None:
        skill = PROJECT / "skills" / "espelho-zap-portable"
        body = (skill / "SKILL.md").read_text(encoding="utf-8")
        metadata = (skill / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertTrue(body.startswith("---\nname: espelho-zap-portable\n"))
        self.assertIn("$espelho-zap-portable", metadata)
        self.assertTrue((skill / "scripts" / "preflight.sh").is_file())
        self.assertTrue((skill / "references" / "operations.md").is_file())


if __name__ == "__main__":
    unittest.main()
