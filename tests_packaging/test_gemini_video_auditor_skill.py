from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parents[1] / "skills" / "gemini-video-auditor"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


analyzer = load_module("public_gemini_video_analyzer", ROOT / "scripts" / "analyze_video.py")
state_store = load_module("public_gemini_video_state", ROOT / "scripts" / "state_store.py")
configure = load_module("public_gemini_video_config", ROOT / "scripts" / "configure.py")


class FakeState:
    name = "ACTIVE"


class FakeUploaded:
    name = "files/test-upload"
    state = FakeState()


class FakeFiles:
    def __init__(self, fail_delete: bool = False):
        self.fail_delete = fail_delete
        self.uploaded = False
        self.deleted = False
        self.upload_stream = None
        self.upload_bytes = None
        self.upload_sha256 = None

    def upload(self, *, file, config=None):
        assert isinstance(file, io.BufferedReader)
        assert isinstance(config, dict)
        assert config["mime_type"].startswith("video/")
        self.uploaded = True
        self.upload_stream = file
        file.seek(0)
        self.upload_bytes = file.read()
        file.seek(0)
        self.upload_sha256 = hashlib.sha256(self.upload_bytes).hexdigest()
        return FakeUploaded()

    def get(self, *, name: str):
        assert name == "files/test-upload"
        return FakeUploaded()

    def delete(self, *, name: str):
        assert name == "files/test-upload"
        if self.fail_delete:
            raise RuntimeError("synthetic cleanup failure")
        self.deleted = True


class SealedMutationFiles(FakeFiles):
    def __init__(self):
        super().__init__()
        self.mutation_blocked = False
        self.observed_mode = None
        self.observed_seals = None

    def upload(self, *, file, config=None):
        self.observed_mode = stat.S_IMODE(os.fstat(file.fileno()).st_mode)
        self.observed_seals = fcntl.fcntl(file.fileno(), analyzer.F_GET_SEALS)
        try:
            os.pwrite(file.fileno(), b"transient-mutation", 0)
        except OSError:
            self.mutation_blocked = True
        else:
            raise AssertionError("sealed upload stream accepted a write")
        return super().upload(file=file, config=config)


class FakeUsage:
    prompt_token_count = 100
    candidates_token_count = 40
    thoughts_token_count = 10
    total_token_count = 150


class FakeResponse:
    text = "# Relatório\n\n## Evidências\n- 00:01 — synthetic\n"
    usage_metadata = FakeUsage()


class FakeModels:
    def __init__(self, before_return=None):
        self.calls = []
        self.before_return = before_return

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self.before_return:
            self.before_return()
        return FakeResponse()


class FakeClient:
    def __init__(self, fail_delete: bool = False, before_return=None):
        self.files = FakeFiles(fail_delete=fail_delete)
        self.models = FakeModels(before_return=before_return)


class FakeTypes:
    class ThinkingConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class GenerateContentConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs


def make_args(tmp_path: Path, video: Path) -> argparse.Namespace:
    return argparse.Namespace(
        video=video,
        profile="SOP_TREINAMENTO",
        output=tmp_path / "report.md",
        receipt=tmp_path / "receipt.json",
        language="pt-BR",
        model="gemini-2.5-flash",
        max_output_tokens=16384,
        thinking_level="high",
        retries=0,
        backoff_seconds=1,
        upload_timeout_seconds=10,
        poll_seconds=0.001,
    )


def test_dry_run_validates_without_key_or_network(tmp_path: Path):
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"synthetic-video")
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "analyze_video.py"),
            "--video",
            str(video),
            "--profile",
            "SOP_TREINAMENTO",
            "--output",
            str(tmp_path / "unused.md"),
            "--dry-run",
        ],
        check=True,
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if "GEMINI" not in k},
    )
    payload = json.loads(completed.stdout)
    assert payload["validated"] is True
    assert payload["upload_performed"] is False
    assert payload["model"] == "gemini-2.5-flash"
    assert not (tmp_path / "unused.md").exists()


def test_mocked_generation_writes_report_receipt_and_deletes_remote(tmp_path: Path):
    video = tmp_path / "sample.webm"
    video.write_bytes(b"synthetic-video")
    client = FakeClient()
    result, cleanup_ok = analyzer.run_analysis(make_args(tmp_path, video), client, FakeTypes)
    assert cleanup_ok is True
    assert client.files.uploaded is True
    assert client.files.deleted is True
    assert isinstance(client.files.upload_stream, io.BufferedReader)
    assert client.files.upload_stream.closed is True
    assert result["remote_cleanup"] == "deleted"
    assert result["local_snapshot_cleanup"] == "closed"
    assert result["operation_ok"] is True
    assert result["ok"] is True
    assert (tmp_path / "report.md").read_text(encoding="utf-8") == FakeResponse.text
    receipt = json.loads((tmp_path / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["report_sha256"] == result["report_sha256"]
    assert receipt["video_sha256"] == analyzer.sha256_file(video)
    assert receipt["video_sha256"] == client.files.upload_sha256
    assert receipt["usage"] == {
        "prompt_token_count": 100,
        "candidates_token_count": 40,
        "thoughts_token_count": 10,
        "total_token_count": 150,
    }
    assert "key" not in json.dumps(receipt).lower()


def test_cleanup_failure_is_reported_and_non_successful(tmp_path: Path):
    video = tmp_path / "sample.mkv"
    video.write_bytes(b"synthetic-video")
    result, cleanup_ok = analyzer.run_analysis(
        make_args(tmp_path, video), FakeClient(fail_delete=True), FakeTypes
    )
    assert cleanup_ok is False
    assert result["remote_cleanup"] == "failed"
    assert result["cleanup_error_type"] == "RuntimeError"
    assert result["analysis_ok"] is True
    assert result["operation_ok"] is False
    assert result["ok"] is False


def test_cli_cleanup_failure_exits_four_and_writes_ok_false(
    tmp_path: Path, monkeypatch, capsys
):
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"synthetic-video")
    google_module = types.ModuleType("google")
    genai_module = types.ModuleType("google.genai")
    setattr(genai_module, "Client", lambda api_key: FakeClient(fail_delete=True))
    setattr(genai_module, "types", FakeTypes)
    setattr(google_module, "genai", genai_module)
    monkeypatch.setitem(sys.modules, "google", google_module)
    monkeypatch.setitem(sys.modules, "google.genai", genai_module)
    monkeypatch.setenv("GEMINI_VIDEO_SKILL_KEY", "synthetic-dedicated-key")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "analyze_video.py",
            "--env-file",
            str(tmp_path / "missing-env"),
            "--video",
            str(video),
            "--profile",
            "SOP_TREINAMENTO",
            "--output",
            str(tmp_path / "report.md"),
            "--receipt",
            str(tmp_path / "receipt.json"),
        ],
    )
    assert analyzer.main() == 4
    captured = capsys.readouterr()
    stdout = json.loads(captured.out)
    receipt = json.loads((tmp_path / "receipt.json").read_text(encoding="utf-8"))
    assert stdout["ok"] is False
    assert stdout["operation_ok"] is False
    assert receipt["ok"] is False
    assert "remote_file_cleanup_failed" in captured.err


def test_video_output_and_receipt_paths_must_be_distinct(tmp_path: Path):
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"synthetic-video")

    with pytest.raises(ValueError, match="distinct"):
        analyzer.validate_distinct_paths(video, video, tmp_path / "receipt.json")

    symlink = tmp_path / "symlink-report.md"
    symlink.symlink_to(video)
    with pytest.raises(ValueError, match="distinct"):
        analyzer.validate_distinct_paths(video, symlink, tmp_path / "receipt.json")

    hardlink = tmp_path / "hardlink-report.md"
    os.link(video, hardlink)
    with pytest.raises(ValueError, match="distinct"):
        analyzer.validate_distinct_paths(video, hardlink, tmp_path / "receipt.json")

    output = tmp_path / "report.md"
    with pytest.raises(ValueError, match="distinct"):
        analyzer.validate_distinct_paths(video, output, output)


def test_source_mutation_aborts_without_report_and_cleans_remote(tmp_path: Path):
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"original-video")

    def mutate_source():
        video.write_bytes(b"changed-video")

    client = FakeClient(before_return=mutate_source)
    with pytest.raises(RuntimeError, match="source video changed"):
        analyzer.run_analysis(make_args(tmp_path, video), client, FakeTypes)
    assert client.files.deleted is True
    assert isinstance(client.files.upload_stream, io.BufferedReader)
    assert client.files.upload_stream.closed is True
    assert not (tmp_path / "report.md").exists()
    assert not (tmp_path / "receipt.json").exists()


def test_output_parent_symlink_swap_cannot_redirect_report_to_source(tmp_path: Path):
    source_dir = tmp_path / "source"
    safe_dir = tmp_path / "safe-output"
    source_dir.mkdir()
    safe_dir.mkdir()
    video = source_dir / "video.mp4"
    original = b"original-video-bytes"
    video.write_bytes(original)
    parent_link = tmp_path / "output-link"
    parent_link.symlink_to(safe_dir, target_is_directory=True)
    args = make_args(tmp_path, video)
    args.output = parent_link / video.name
    args.receipt = None

    def redirect_parent():
        parent_link.unlink()
        parent_link.symlink_to(source_dir, target_is_directory=True)

    result, cleanup_ok = analyzer.run_analysis(
        args, FakeClient(before_return=redirect_parent), FakeTypes
    )
    assert cleanup_ok is True
    assert result["ok"] is True
    assert video.read_bytes() == original
    assert (safe_dir / video.name).read_text(encoding="utf-8") == FakeResponse.text
    assert (parent_link / video.name).read_bytes() == original


def test_receipt_parent_symlink_swap_cannot_redirect_receipt_to_source(tmp_path: Path):
    source_dir = tmp_path / "source"
    safe_receipt_dir = tmp_path / "safe-receipt"
    source_dir.mkdir()
    safe_receipt_dir.mkdir()
    video = source_dir / "video.mp4"
    original = b"original-video-bytes"
    video.write_bytes(original)
    parent_link = tmp_path / "receipt-link"
    parent_link.symlink_to(safe_receipt_dir, target_is_directory=True)
    args = make_args(tmp_path, video)
    args.receipt = parent_link / video.name

    def redirect_parent():
        parent_link.unlink()
        parent_link.symlink_to(source_dir, target_is_directory=True)

    result, cleanup_ok = analyzer.run_analysis(
        args, FakeClient(before_return=redirect_parent), FakeTypes
    )
    assert cleanup_ok is True
    assert video.read_bytes() == original
    receipt = json.loads((safe_receipt_dir / video.name).read_text(encoding="utf-8"))
    assert receipt["video_sha256"] == result["video_sha256"]
    assert (parent_link / video.name).read_bytes() == original


@pytest.mark.parametrize("late_entry", ["hardlink", "symlink"])
def test_late_destination_entry_aborts_without_overwrite(tmp_path: Path, late_entry: str):
    video = tmp_path / "video.mp4"
    original = b"original-video-bytes"
    video.write_bytes(original)
    args = make_args(tmp_path, video)

    def create_late_entry():
        if late_entry == "hardlink":
            os.link(video, args.output)
        else:
            args.output.symlink_to(video)

    client = FakeClient(before_return=create_late_entry)
    with pytest.raises(FileExistsError, match="must not already exist"):
        analyzer.run_analysis(args, client, FakeTypes)
    assert client.files.deleted is True
    assert video.read_bytes() == original
    assert args.output.read_bytes() == original
    assert not args.receipt.exists()


def test_upload_uses_read_only_kernel_sealed_stream(tmp_path: Path):
    video = tmp_path / "video.mp4"
    original = b"immutable-upload-bytes"
    video.write_bytes(original)
    client = FakeClient()
    client.files = SealedMutationFiles()
    result, cleanup_ok = analyzer.run_analysis(make_args(tmp_path, video), client, FakeTypes)
    assert cleanup_ok is True
    assert client.files.mutation_blocked is True
    assert client.files.observed_mode == 0o400
    assert client.files.observed_seals & analyzer.REQUIRED_SEALS == analyzer.REQUIRED_SEALS
    assert client.files.upload_bytes == original
    assert result["video_sha256"] == hashlib.sha256(original).hexdigest()
    receipt = json.loads((tmp_path / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["video_sha256"] == hashlib.sha256(client.files.upload_bytes).hexdigest()


def test_environment_file_is_allowlisted_and_private(tmp_path: Path, monkeypatch):
    env_file = tmp_path / "env"
    env_file.write_text(
        "GEMINI_VIDEO_SKILL_KEY=synthetic-test-value\n"
        "GEMINI_VIDEO_MODEL=gemini-2.5-flash\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    monkeypatch.delenv("GEMINI_VIDEO_SKILL_KEY", raising=False)
    analyzer.load_env_file(env_file)
    assert os.environ["GEMINI_VIDEO_SKILL_KEY"] == "synthetic-test-value"

    bad = tmp_path / "bad-env"
    bad.write_text("UNEXPECTED_COMMAND=value\n", encoding="utf-8")
    bad.chmod(0o600)
    with pytest.raises(ValueError, match="not allowed"):
        analyzer.load_env_file(bad)


def test_generic_sdk_key_is_ignored_and_dedicated_key_is_required(tmp_path: Path):
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"synthetic-video")
    env = {k: v for k, v in os.environ.items() if "GEMINI" not in k}
    env["GEMINI_API_KEY"] = "synthetic-general-runtime-key"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "analyze_video.py"),
            "--env-file",
            str(tmp_path / "missing-env"),
            "--video",
            str(video),
            "--profile",
            "SOP_TREINAMENTO",
            "--output",
            str(tmp_path / "report.md"),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.returncode == 1
    assert json.loads(completed.stderr)["error_type"] == "ValueError"
    assert not (tmp_path / "report.md").exists()
    assert "GEMINI_API_KEY" not in analyzer.ALLOWED_ENV_KEYS


def test_configuration_rejects_unknown_fields_without_printing_them(tmp_path: Path):
    root = tmp_path / "config-root"
    root.mkdir()
    (root / "config.json").write_text(
        json.dumps({"unknown_secret": "synthetic-secret-value"}), encoding="utf-8"
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "configure.py"),
            "--root",
            str(root),
            "--show",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert completed.stdout == ""
    assert "synthetic-secret-value" not in completed.stderr
    with pytest.raises(ValueError, match="unknown"):
        configure.validate_config({"unknown_secret": "synthetic-secret-value"})


def test_files_api_limit_uses_decimal_two_gigabytes():
    assert analyzer.MAX_FILE_BYTES == 2_000_000_000


def test_state_fingerprint_is_deterministic_and_opaque(tmp_path: Path):
    first = state_store.fingerprint("source-1", "etag-2", 123, "2026-01-01T00:00:00Z")
    second = state_store.fingerprint("source-1", "etag-2", 123, "2026-01-01T00:00:00Z")
    assert first == second
    assert re.fullmatch(r"[0-9a-f]{64}", first)
    path = tmp_path / "state.json"
    state_store.save(path, {"schema_version": 1, "videos": {first: {"status": "processed"}}})
    assert path.stat().st_mode & 0o077 == 0
    assert state_store.load(path)["videos"][first]["status"] == "processed"


def test_public_skill_contains_no_obvious_secret_or_private_host_material():
    patterns = [
        re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
        re.compile(r"sk-[0-9A-Za-z]{20,}"),
        re.compile(re.escape("/" + "root" + "/." + "openclaw")),
        re.compile(r"187\.124\.250\.201"),
        re.compile(r"100\.105\.201\.102"),
    ]
    text_suffixes = {".md", ".py", ".sh", ".txt", ".yaml", ".yml", ".json"}
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in text_suffixes:
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            assert not pattern.search(text), f"forbidden material in {path}: {pattern.pattern}"


def test_direct_sdk_dependency_is_pinned():
    assert (ROOT / "scripts" / "requirements.txt").read_text(encoding="utf-8") == (
        "google-genai==2.18.1\n"
    )
