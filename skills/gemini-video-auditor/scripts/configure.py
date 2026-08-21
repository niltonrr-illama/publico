#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path

ALLOWED_CONFIG_KEYS = {
    "mode",
    "suggested_schedule",
    "scheduler_installed",
    "timezone",
    "model",
    "model_env",
    "key_env",
    "approval_required",
    "automatic_outbound",
}


def default_root() -> Path:
    return Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config")) / "gemini-video-auditor"


def atomic_json(path: Path, payload: dict) -> None:
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


def validate_config(config: dict) -> dict:
    unknown = set(config) - ALLOWED_CONFIG_KEYS
    if unknown:
        raise ValueError("configuration contains unknown fields")
    if "mode" in config and config["mode"] not in {"proactive", "on-demand"}:
        raise ValueError("invalid mode")
    if "model" in config and not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", config["model"]):
        raise ValueError("invalid model identifier")
    if "timezone" in config and not re.fullmatch(r"[A-Za-z0-9_+./-]{1,64}", config["timezone"]):
        raise ValueError("invalid timezone")
    if "key_env" in config and config["key_env"] != "GEMINI_VIDEO_SKILL_KEY":
        raise ValueError("invalid key environment name")
    if "model_env" in config and config["model_env"] != "GEMINI_VIDEO_MODEL":
        raise ValueError("invalid model environment name")
    return {key: config[key] for key in sorted(config) if key in ALLOWED_CONFIG_KEYS}


def main() -> int:
    parser = argparse.ArgumentParser(description="Write secret-free Gemini video auditor configuration")
    parser.add_argument("--root", type=Path, default=default_root())
    parser.add_argument("--mode", choices=("proactive", "on-demand"))
    parser.add_argument("--timezone", default="UTC")
    parser.add_argument("--model", default="gemini-2.5-flash")
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()

    config_path = args.root / "config.json"
    config: dict = {}
    if config_path.exists():
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("configuration must be a JSON object")
        config = validate_config(loaded)

    if args.mode:
        config.update(
            {
                "mode": args.mode,
                "suggested_schedule": "0 7 * * *" if args.mode == "proactive" else None,
                "scheduler_installed": False,
                "timezone": args.timezone,
                "model": args.model,
                "model_env": "GEMINI_VIDEO_MODEL",
                "key_env": "GEMINI_VIDEO_SKILL_KEY",
                "approval_required": True,
                "automatic_outbound": False,
            }
        )
        config = validate_config(config)
        atomic_json(config_path, config)

    if args.show or not args.mode:
        print(json.dumps(validate_config(config), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
