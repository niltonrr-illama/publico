#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def default_state() -> Path:
    root = Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return root / "gemini-video-auditor" / "processed_videos.json"


def fingerprint(source_id: str, version: str, size: int, modified: str) -> str:
    raw = "\x1f".join((source_id, version, str(size), modified))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load(path: Path) -> dict:
    if not path.exists():
        return {"schema_version": 1, "videos": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("videos"), dict):
        raise ValueError("unsupported or malformed state")
    return payload


def save(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def main() -> int:
    parser = argparse.ArgumentParser(description="Track opaque source-video fingerprints")
    parser.add_argument("--state", type=Path, default=default_state())
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--version", default="")
    parser.add_argument("--size", type=int, required=True)
    parser.add_argument("--modified", required=True)
    parser.add_argument("--mark", choices=("processed", "ignored", "failed"))
    args = parser.parse_args()

    token = fingerprint(args.source_id, args.version, args.size, args.modified)
    data = load(args.state)
    current = data["videos"].get(token)
    if args.mark:
        data["videos"][token] = {
            "status": args.mark,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        save(args.state, data)
        current = data["videos"][token]
    print(json.dumps({"fingerprint": token, "status": (current or {}).get("status")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
