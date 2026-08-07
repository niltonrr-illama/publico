#!/usr/bin/env python3
"""CI validator for the portable AgentSkill and its OpenAI metadata."""

from __future__ import annotations

from pathlib import Path
import re
import sys

import yaml


NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ALLOWED_FRONTMATTER = {"name", "description", "license", "allowed-tools", "metadata"}


def fail(message: str) -> None:
    raise SystemExit(f"invalid skill: {message}")


def main(raw_path: str) -> None:
    root = Path(raw_path)
    skill_path = root / "SKILL.md"
    metadata_path = root / "agents" / "openai.yaml"
    if not skill_path.is_file():
        fail("SKILL.md not found")
    if not metadata_path.is_file():
        fail("agents/openai.yaml not found")

    body = skill_path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---(?:\n|$)", body, flags=re.DOTALL)
    if match is None:
        fail("frontmatter is missing or malformed")
    frontmatter = yaml.safe_load(match.group(1))
    if not isinstance(frontmatter, dict):
        fail("frontmatter must be a mapping")
    unexpected = set(frontmatter) - ALLOWED_FRONTMATTER
    if unexpected:
        fail(f"unexpected frontmatter keys: {', '.join(sorted(unexpected))}")
    name = frontmatter.get("name")
    description = frontmatter.get("description")
    if not isinstance(name, str) or not NAME.fullmatch(name) or len(name) > 64:
        fail("name must be hyphen-case and at most 64 characters")
    if (
        not isinstance(description, str)
        or not description.strip()
        or len(description) > 1024
        or "<" in description
        or ">" in description
    ):
        fail("description is missing or invalid")

    metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict) or not isinstance(metadata.get("interface"), dict):
        fail("OpenAI interface metadata is missing")
    interface = metadata["interface"]
    for key in ("display_name", "short_description", "default_prompt"):
        if not isinstance(interface.get(key), str) or not interface[key].strip():
            fail(f"OpenAI interface field {key} is missing")
    if f"${name}" not in interface["default_prompt"]:
        fail("default_prompt must explicitly reference the skill")
    print("Skill is valid!")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: quick_validate_skill.py SKILL_DIRECTORY")
    main(sys.argv[1])
