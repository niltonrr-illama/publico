#!/usr/bin/env python3
"""Fail-closed post-update guard for one Hermes mirror profile.

The guard performs no external network access and never sends a message.  Its
only socket operation is an authenticated GET to the fixed loopback health
route installed by the portable direct-bridge guard.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import tempfile
import time
from typing import Mapping
from urllib.parse import quote


_PORTABLE_PLUGIN = "espelho-zap-portable"
_PASSIVE_PLUGIN = "espelho-zap-passive"
_BRIDGE_HOST = "127.0.0.1"
_BRIDGE_PORT = 3011
_BRIDGE_HEALTH_PATH = "/mirror-human-health"
_MAX_CONTROL_BYTES = 64 * 1024
_MARKER_KEYS = frozenset(
    {
        "schema_version",
        "plugin_version",
        "release_commit",
        "plugin_sha256",
        "hermes_runtime_fingerprint",
        "human_outbound_enabled",
        "human_outbound_armed",
        "gateway_pid",
        "registered_at",
    }
)
_ARM_KEYS = frozenset(
    {
        "schema_version",
        "release_commit",
        "plugin_sha256",
        "hermes_runtime_fingerprint",
        "armed",
    }
)
_PATH_ENV = {
    "ESPELHO_ZAP_HUMAN_OUTBOUND_ARM_FILE": "arm_file",
    "ESPELHO_ZAP_HUMAN_OUTBOUND_LEDGER": "ledger_file",
    "ESPELHO_ZAP_HUMAN_OUTBOUND_STARTUP_MARKER": "marker_file",
    "ESPELHO_ZAP_HUMAN_OUTBOUND_TOKEN_FILE": "token_file",
}
_PLUGIN_NETWORK_AUDIT_EVENTS = frozenset(
    {
        "http.client.connect",
        "http.client.send",
        "urllib.Request",
    }
)
_plugin_import_network_blocked = False
_plugin_import_audit_installed = False


class GuardError(RuntimeError):
    """A sanitized, fail-closed post-update gate failure."""


def _fail(code: str) -> GuardError:
    if not re.fullmatch(r"[a-z0-9_]+", code):
        code = "guard_error"
    return GuardError(code)


def _plugin_import_audit_hook(event: str, _arguments: tuple[object, ...]) -> None:
    """Deny network creation/use while the trusted local plugin is inspected."""

    if _plugin_import_network_blocked and (
        event.startswith("socket.") or event in _PLUGIN_NETWORK_AUDIT_EVENTS
    ):
        raise RuntimeError("plugin_runtime_probe_network_denied")


def _install_plugin_import_audit_hook() -> None:
    global _plugin_import_audit_installed
    if _plugin_import_audit_installed:
        return
    try:
        sys.addaudithook(_plugin_import_audit_hook)
    except BaseException:
        raise _fail("plugin_runtime_probe_invalid") from None
    _plugin_import_audit_installed = True


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _regular_file(
    path: Path,
    *,
    code: str,
    private: bool = False,
) -> os.stat_result:
    if not path.is_absolute():
        raise _fail(code)
    try:
        details = path.lstat()
    except OSError:
        raise _fail(code) from None
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise _fail(code)
    if os.name == "posix":
        if private and details.st_uid != os.geteuid():
            raise _fail(code)
        if not private and details.st_uid not in {0, os.geteuid()}:
            raise _fail(code)
        mode = stat.S_IMODE(details.st_mode)
        if (private and mode != 0o600) or (not private and mode & 0o022):
            raise _fail(code)
    return details


def _read_bounded(
    path: Path,
    *,
    code: str,
    private: bool = False,
    maximum: int = _MAX_CONTROL_BYTES,
) -> tuple[bytes, os.stat_result]:
    named = _regular_file(path, code=code, private=private)
    if named.st_size > maximum:
        raise _fail(code)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise _fail(code) from None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise _fail(code)
        raw = os.read(descriptor, maximum + 1)
    finally:
        os.close(descriptor)
    if len(raw) > maximum:
        raise _fail(code)
    try:
        after = path.lstat()
    except OSError:
        raise _fail(code) from None
    if (
        stat.S_ISLNK(after.st_mode)
        or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
        or after.st_size != opened.st_size
        or after.st_mtime_ns != opened.st_mtime_ns
    ):
        raise _fail(code)
    return raw, after


def _read_json(path: Path, *, code: str, private: bool = True) -> tuple[dict, os.stat_result]:
    raw, details = _read_bounded(path, code=code, private=private)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _fail(code) from None
    if not isinstance(value, dict):
        raise _fail(code)
    return value, details


def _parse_dotenv(path: Path) -> dict[str, str]:
    raw, _ = _read_bounded(path, code="env_invalid", private=True)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _fail("env_invalid") from None
    result: dict[str, str] = {}
    for source_line in text.splitlines():
        line = source_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise _fail("env_invalid")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or key in result:
            raise _fail("env_invalid")
        if len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        elif len(value) >= 2 and value[0] == value[-1] == '"':
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                raise _fail("env_invalid") from None
        result[key] = value
    return result


def _strip_yaml_comment(value: str) -> str:
    quote_char = ""
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote_char == '"':
            escaped = True
            continue
        if character in {"'", '"'}:
            if not quote_char:
                quote_char = character
            elif quote_char == character:
                quote_char = ""
            continue
        if character == "#" and not quote_char:
            return value[:index]
    return value


def _yaml_scalar(value: str) -> str:
    rendered = _strip_yaml_comment(value).strip()
    if not rendered or any(token in rendered for token in ("&", "*", "{", "}")):
        raise _fail("profile_plugins_invalid")
    if len(rendered) >= 2 and rendered[0] == rendered[-1] and rendered[0] in {"'", '"'}:
        rendered = rendered[1:-1]
    segments = rendered.split("/")
    if any(
        segment in {"", ".", ".."}
        or re.fullmatch(r"[A-Za-z0-9_.-]+", segment) is None
        for segment in segments
    ):
        raise _fail("profile_plugins_invalid")
    return rendered


def _inline_yaml_list(value: str) -> list[str]:
    rendered = _strip_yaml_comment(value).strip()
    if rendered == "[]":
        return []
    if not (rendered.startswith("[") and rendered.endswith("]")):
        raise _fail("profile_plugins_invalid")
    try:
        row = next(csv.reader([rendered[1:-1]], skipinitialspace=True))
    except (csv.Error, StopIteration):
        raise _fail("profile_plugins_invalid") from None
    return [_yaml_scalar(item) for item in row if item.strip()]


def _plugin_lists(path: Path) -> tuple[list[str], list[str]]:
    raw, _ = _read_bounded(path, code="profile_config_invalid", private=True)
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        raise _fail("profile_config_invalid") from None
    section_matches: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        content = _strip_yaml_comment(line).rstrip()
        if content.strip() == "plugins:":
            section_matches.append((index, len(content) - len(content.lstrip())))
    if len(section_matches) != 1:
        raise _fail("profile_plugins_invalid")
    section_index, section_indent = section_matches[0]
    values: dict[str, list[str]] = {}
    child_indent: int | None = None
    index = section_index + 1
    while index < len(lines):
        raw_line = _strip_yaml_comment(lines[index]).rstrip()
        if not raw_line.strip():
            index += 1
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        if indent <= section_indent:
            break
        if child_indent is None:
            child_indent = indent
        if indent != child_indent:
            index += 1
            continue
        match = re.fullmatch(r"\s*(enabled|disabled):\s*(.*?)\s*", raw_line)
        if match is None:
            index += 1
            continue
        key, inline = match.groups()
        if key in values:
            raise _fail("profile_plugins_invalid")
        if inline:
            values[key] = _inline_yaml_list(inline)
            index += 1
            continue
        key_indent = indent
        items: list[str] = []
        index += 1
        while index < len(lines):
            item_line = _strip_yaml_comment(lines[index]).rstrip()
            if not item_line.strip():
                index += 1
                continue
            item_indent = len(item_line) - len(item_line.lstrip())
            if item_indent < key_indent or (
                item_indent == key_indent and not item_line.lstrip().startswith("- ")
            ):
                break
            item_match = re.fullmatch(r"\s*-\s+(.+?)\s*", item_line)
            if item_match is None:
                raise _fail("profile_plugins_invalid")
            items.append(_yaml_scalar(item_match.group(1)))
            index += 1
        values[key] = items
    if set(values) != {"enabled", "disabled"}:
        raise _fail("profile_plugins_invalid")
    return values["enabled"], values["disabled"]


def _hash_file(path: Path, *, code: str) -> str:
    _regular_file(path, code=code)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        raise _fail(code) from None
    return digest.hexdigest()


def _plugin_runtime_fingerprint(path: Path, plugin_sha256: str) -> str:
    """Recompute the runtime binding from the exact trusted plugin on disk.

    The module is loaded under a one-use private name and is inserted in
    ``sys.modules`` only for the duration of execution.  This is required by
    Python's dataclass/type machinery for modules loaded from a file.  It is
    removed before return and is never passed to Hermes' plugin registry.  A
    Python audit hook denies socket and HTTP activity for both module
    execution and the fingerprint call.
    """

    global _plugin_import_network_blocked
    if re.fullmatch(r"[0-9a-f]{64}", plugin_sha256) is None:
        raise _fail("plugin_runtime_probe_invalid")
    _install_plugin_import_audit_hook()
    if _plugin_import_network_blocked:
        raise _fail("plugin_runtime_probe_invalid")
    module_name = (
        f"_espelho_zap_upgrade_probe_{plugin_sha256[:16]}_"
        f"{os.getpid()}_{time.monotonic_ns()}"
    )
    try:
        specification = importlib.util.spec_from_file_location(module_name, path)
        if specification is None or specification.loader is None:
            raise RuntimeError("plugin_spec_unavailable")
        module = importlib.util.module_from_spec(specification)
        # Dataclasses and other standard-library type helpers resolve
        # ``cls.__module__`` through sys.modules while the plugin executes.
        # Keep this private probe name ephemeral so the guard remains isolated
        # from Hermes' registry and leaves no module behind.
        sys.modules[module_name] = module
        _plugin_import_network_blocked = True
        specification.loader.exec_module(module)
        fingerprint_function = getattr(module, "_hermes_runtime_fingerprint", None)
        if not callable(fingerprint_function):
            raise RuntimeError("plugin_runtime_probe_missing")
        fingerprint = fingerprint_function()
    except BaseException:
        raise _fail("plugin_runtime_probe_invalid") from None
    finally:
        _plugin_import_network_blocked = False
        sys.modules.pop(module_name, None)
    if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        raise _fail("plugin_runtime_probe_invalid")
    return fingerprint


def _proc_environment(path: Path) -> dict[str, str]:
    try:
        raw = path.read_bytes()
    except OSError:
        raise _fail("gateway_process_unreadable") from None
    if len(raw) > 2 * 1024 * 1024:
        raise _fail("gateway_process_unreadable")
    result: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        try:
            key, value = entry.decode("utf-8").split("=", 1)
        except (UnicodeDecodeError, ValueError):
            continue
        if key in result:
            raise _fail("gateway_process_unreadable")
        result[key] = value
    return result


def _discover_gateway(
    proc_root: Path,
    profile: str,
) -> tuple[int, dict[str, str]]:
    matches: list[tuple[int, dict[str, str]]] = []
    try:
        candidates = tuple(proc_root.iterdir())
    except OSError:
        raise _fail("proc_unavailable") from None
    for candidate in candidates:
        if not candidate.name.isdigit():
            continue
        try:
            environment = _proc_environment(candidate / "environ")
            command = (candidate / "cmdline").read_bytes().lower()
        except GuardError:
            continue
        except OSError:
            continue
        if environment.get("HERMES_PROFILE") != profile or b"gateway" not in command:
            continue
        matches.append((int(candidate.name), environment))
    if len(matches) != 1:
        raise _fail("gateway_count_invalid")
    return matches[0]


def _process_start_ns(proc_root: Path, pid: int) -> int:
    try:
        global_stat = (proc_root / "stat").read_text(encoding="utf-8")
        process_stat = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
        boot_time = int(
            next(line.split()[1] for line in global_stat.splitlines() if line.startswith("btime "))
        )
        tail = process_stat[process_stat.rfind(")") + 2 :].split()
        start_ticks = int(tail[19])
        ticks_per_second = int(os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError, IndexError, StopIteration):
        raise _fail("gateway_start_time_invalid") from None
    return boot_time * 1_000_000_000 + (start_ticks * 1_000_000_000) // ticks_per_second


def _validate_marker(
    marker: Mapping[str, object],
    details: os.stat_result,
    *,
    pid: int,
    process_start_ns: int,
    profile: str,
    release_commit: str,
    plugin_sha256: str,
) -> str:
    runtime_fingerprint = marker.get("hermes_runtime_fingerprint")
    registered_at = marker.get("registered_at")
    if (
        frozenset(marker) != _MARKER_KEYS
        or marker.get("schema_version") != 1
        or not isinstance(marker.get("plugin_version"), str)
        or not marker.get("plugin_version")
        or marker.get("release_commit") != release_commit
        or marker.get("plugin_sha256") != plugin_sha256
        or not isinstance(runtime_fingerprint, str)
        or re.fullmatch(r"[0-9a-f]{64}", runtime_fingerprint) is None
        or marker.get("human_outbound_enabled") is not True
        or type(marker.get("human_outbound_armed")) is not bool
        or type(marker.get("gateway_pid")) is not int
        or marker.get("gateway_pid") != pid
        or type(registered_at) is not int
    ):
        raise _fail("startup_marker_mismatch")
    now = int(time.time())
    if (
        registered_at < process_start_ns // 1_000_000_000
        or registered_at > now + 5
        or details.st_mtime_ns + 1_000_000_000 < process_start_ns
        or abs(details.st_mtime_ns // 1_000_000_000 - registered_at) > 5
    ):
        raise _fail("startup_marker_stale")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", profile):
        raise _fail("profile_invalid")
    return runtime_fingerprint


def _validate_arm(
    value: Mapping[str, object],
    *,
    release_commit: str,
    plugin_sha256: str,
    runtime_fingerprint: str,
) -> None:
    if (
        frozenset(value) != _ARM_KEYS
        or value.get("schema_version") != 1
        or value.get("armed") is not True
        or value.get("release_commit") != release_commit
        or value.get("plugin_sha256") != plugin_sha256
        or value.get("hermes_runtime_fingerprint") != runtime_fingerprint
    ):
        raise _fail("arm_mismatch")


def _validate_ledger(path: Path) -> dict[str, int]:
    _regular_file(path, code="ledger_invalid", private=True)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists() or sidecar.is_symlink():
            _regular_file(sidecar, code="ledger_invalid", private=True)
    uri = f"file:{quote(path.as_posix(), safe='/')}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            connection.execute("PRAGMA query_only=ON")
            quick = connection.execute("PRAGMA quick_check").fetchall()
            if quick != [("ok",)]:
                raise _fail("ledger_quick_check_failed")
            counts = dict(
                connection.execute(
                    """SELECT status,COUNT(*) FROM hermes_human_outbound
                       WHERE status IN ('prepared','sending') GROUP BY status"""
                ).fetchall()
            )
        finally:
            connection.close()
    except GuardError:
        raise
    except sqlite3.Error:
        raise _fail("ledger_invalid") from None
    return {
        "prepared": int(counts.get("prepared", 0)),
        "sending": int(counts.get("sending", 0)),
    }


def _read_token(path: Path) -> str:
    raw, _ = _read_bounded(path, code="bridge_token_invalid", private=True, maximum=4096)
    try:
        token = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise _fail("bridge_token_invalid") from None
    if len(token) < 43:
        raise _fail("bridge_token_invalid")
    return token


def _validate_bridge(token_file: Path) -> None:
    token = _read_token(token_file)
    connection = http.client.HTTPConnection(_BRIDGE_HOST, _BRIDGE_PORT, timeout=3)
    try:
        connection.request(
            "GET",
            _BRIDGE_HEALTH_PATH,
            headers={"x-espelho-token": token, "host": f"{_BRIDGE_HOST}:{_BRIDGE_PORT}"},
        )
        response = connection.getresponse()
        raw = response.read(4097)
    except OSError:
        raise _fail("bridge_health_unavailable") from None
    finally:
        connection.close()
    if response.status != 200 or len(raw) > 4096:
        raise _fail("bridge_health_unavailable")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _fail("bridge_health_invalid") from None
    if (
        not isinstance(value, dict)
        or frozenset(value) != {
            "schemaVersion",
            "guardVersion",
            "observeOnly",
            "connectionState",
            "connected",
        }
        or value.get("schemaVersion") != 1
        or value.get("guardVersion") != 4
        or value.get("observeOnly") is not True
        or value.get("connectionState") != "connected"
        or value.get("connected") is not True
    ):
        raise _fail("bridge_health_invalid")


def _atomic_disarm(path: Path) -> bool:
    if not path.is_absolute():
        return True
    try:
        details = path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
        try:
            path.unlink()
            _fsync_directory(path.parent)
        except OSError:
            # A symlink can never authorize the plugin; a regular file could.
            return stat.S_ISLNK(details.st_mode)
        return True
    # The plugin rejects directories, devices and every other non-regular ARM
    # representation, so they are already fail-closed even if not removable.
    return True


def _atomic_arm(path: Path, payload: Mapping[str, object]) -> None:
    parent = path.parent
    try:
        details = parent.lstat()
    except OSError:
        raise _fail("arm_parent_invalid") from None
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise _fail("arm_parent_invalid")
    if os.name == "posix" and (
        details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) != 0o700
    ):
        raise _fail("arm_parent_invalid")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(parent)
    finally:
        temporary.unlink(missing_ok=True)


def _path_from_environment(
    environment: Mapping[str, str],
    key: str,
    expected: Path,
) -> None:
    raw = environment.get(key, "")
    if not raw or Path(raw) != expected:
        raise _fail("env_path_mismatch")


def run_guard(args: argparse.Namespace) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.profile):
        raise _fail("profile_invalid")
    source_environment = _parse_dotenv(args.env_file)
    if source_environment.get("ESPELHO_ZAP_HUMAN_OUTBOUND_ENABLED") != "enabled":
        raise _fail("human_outbound_not_enabled")
    release_commit = source_environment.get("ESPELHO_ZAP_RELEASE_COMMIT", "")
    if re.fullmatch(r"[0-9a-f]{40}", release_commit) is None:
        raise _fail("release_commit_invalid")
    for key, attribute in _PATH_ENV.items():
        _path_from_environment(source_environment, key, getattr(args, attribute))

    enabled, disabled = _plugin_lists(args.profile_config)
    if (
        enabled.count(_PORTABLE_PLUGIN) != 1
        or _PASSIVE_PLUGIN in enabled
        or _PORTABLE_PLUGIN in disabled
        or disabled.count(_PASSIVE_PLUGIN) != 1
    ):
        raise _fail("plugin_selection_invalid")

    pid, _live_environment = _discover_gateway(args.proc_root, args.profile)
    start_ns = _process_start_ns(args.proc_root, pid)
    plugin_sha256 = _hash_file(args.plugin_file, code="plugin_invalid")
    marker, marker_details = _read_json(
        args.marker_file, code="startup_marker_invalid", private=True
    )
    runtime_fingerprint = _validate_marker(
        marker,
        marker_details,
        pid=pid,
        process_start_ns=start_ns,
        profile=args.profile,
        release_commit=release_commit,
        plugin_sha256=plugin_sha256,
    )
    observed_runtime_fingerprint = _plugin_runtime_fingerprint(
        args.plugin_file,
        plugin_sha256,
    )
    if observed_runtime_fingerprint != runtime_fingerprint:
        raise _fail("plugin_runtime_fingerprint_mismatch")
    arm_error: GuardError | None = None
    current_arm_valid = False
    try:
        current_arm, _ = _read_json(args.arm_file, code="arm_invalid", private=True)
        _validate_arm(
            current_arm,
            release_commit=release_commit,
            plugin_sha256=plugin_sha256,
            runtime_fingerprint=runtime_fingerprint,
        )
        current_arm_valid = True
    except GuardError as exc:
        arm_error = exc

    ledger_counts = _validate_ledger(args.ledger_file)
    _validate_bridge(args.token_file)

    # A healthy in-flight send has already crossed an ARM validation inside
    # the plugin. Do not replace or remove that still-compatible authority in a
    # polling cycle. Every other gate above has nevertheless passed. If the
    # current ARM is not compatible, `sending` is never accepted as healthy.
    if ledger_counts["sending"] > 0:
        if not current_arm_valid:
            raise _fail("ledger_sending_disarmed")
        return "defer"

    if args.rearm_compatible_update:
        payload = {
            "schema_version": 1,
            "release_commit": release_commit,
            "plugin_sha256": plugin_sha256,
            "hermes_runtime_fingerprint": runtime_fingerprint,
            "armed": True,
        }
        _atomic_arm(args.arm_file, payload)
        value, _ = _read_json(args.arm_file, code="arm_invalid", private=True)
        _validate_arm(
            value,
            release_commit=release_commit,
            plugin_sha256=plugin_sha256,
            runtime_fingerprint=runtime_fingerprint,
        )
        return "rearm"
    else:
        if not current_arm_valid:
            assert arm_error is not None
            raise arm_error
        return "check"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--profile-config", type=Path, required=True)
    parser.add_argument("--plugin-file", type=Path, required=True)
    parser.add_argument("--marker-file", type=Path, required=True)
    parser.add_argument("--arm-file", type=Path, required=True)
    parser.add_argument("--ledger-file", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--proc-root", type=Path, default=Path("/proc"))
    parser.add_argument(
        "--rearm-compatible-update",
        action="store_true",
        help="recreate the runtime-bound ARM only after every gate passes",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # The plugin itself rejects an ARM from another runtime fingerprint.  Keep
    # a currently valid ARM in place while a compatible re-arm is checked, and
    # atomically replace it only after every gate passes.  Every failure path
    # still revokes the current ARM before returning.
    try:
        outcome = run_guard(args)
    except Exception as exc:
        disarmed = _atomic_disarm(args.arm_file)
        code = str(exc) if isinstance(exc, GuardError) else "guard_error"
        if not disarmed:
            code = "arm_disarm_failed"
        print(
            "HERMES_UPGRADE_GUARD=FAIL "
            f"reason={code} disarmed={'true' if disarmed else 'false'}",
            file=sys.stderr,
        )
        return 2 if disarmed else 3
    print(f"HERMES_UPGRADE_GUARD=PASS mode={outcome} armed=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
