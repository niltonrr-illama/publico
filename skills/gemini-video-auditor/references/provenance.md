# Provenance and reconstruction boundary

This public skill was reconstructed under the owner's explicit authorization from private, read-only OpenClaw migration material.

## Canonical recovery basis

The byte-level basis used for reconstruction was the migration bundle labeled `critical-current-state-20260803`. In that identified bundle, the recovered artifact contained:

- `SKILL.md` — SHA-256 `ca9b4132bdb2087575279b00ac02f6e5322d84cef51ce0390cb5f364f6cfa443`;
- `scripts/analyze_video.py` — SHA-256 `fc564d8a50daf03eb4ad9c415035f8e020992fcc01def94b02e6ef3cb5fe29b0`;
- `scripts/state_store.py` — SHA-256 `e4e4c19da282824f275a76b10877bab875afcba8b04e9ae6524000a50a9976af`;
- `scripts/configure.py` — SHA-256 `da2eb31fcb1d345eecd1ba83c5f492f8d90d422727b9f0ae58ff528b970382b0`;
- `scripts/run_analyze.sh` — SHA-256 `5d0865ef0d879e31094186e39ef1892582a14be9f285e8e56b7e316dea18d6ab`;
- `references/gemini-api.md` — SHA-256 `89a92172c94aad796e39f09788cc4d57e4a16aab74be002acb46cb132c427027`.

Two earlier migration bundles, labeled `wave1-63a2e53dbe5-001` and `wave1-63a2e53dbe5-002`, were also inspected. They share the same `SKILL.md` and wrapper hash but contain earlier variants of several scripts and the API reference. This document therefore identifies the exact canonical bundle above; it does not claim that every recovered copy was byte-identical.

Historical execution evidence independently showed the official `google.genai` workflow: `files.upload`, `models.generate_content`, and remote file deletion after processing.

## Identifier normalization

The recovered filesystem copies spell the historical profile identifier `SOP_TRAINAMENTO`. The requested public name and correct Portuguese spelling are `SOP_TREINAMENTO`. This public edition deliberately normalizes that identifier while preserving and strengthening the recovered SOP semantics. `AUDITORIA_REUNIAO` is preserved unchanged.

## Preserved behavior

- one-video approval scope;
- dedicated `GEMINI_VIDEO_SKILL_KEY` with no generic-key fallback;
- temporary Files API upload and deletion;
- bounded rate-limit retries;
- source fingerprint/state separation;
- raw Markdown report followed by human review and separately authorized delivery.

## Deliberate public hardening

- removed private host paths and OpenClaw-specific runtime assumptions;
- excluded historical processed-video state and every credential/config value;
- replaced shell-sourcing of the environment file with an allowlisted parser;
- added prompt-injection boundaries for content inside videos;
- added a kernel-sealed anonymous upload stream, pinned destination-directory descriptors, and atomic no-overwrite creation so receipt hashes bind to immutable uploaded bytes without source-path destruction;
- added atomic report/receipt writes and unambiguous operation/cleanup status;
- made scheduling a separate opt-in action;
- refreshed model, SDK, pricing, retention, and privacy documentation from official sources.

This is a source-derived, hardened public edition—not a claim that every byte matches the historical private installation.
