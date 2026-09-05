# Changelog

## 0.3.5 - 2026-08-12

- prevents auxiliary Hermes CLI/maintenance processes from overwriting the
  live gateway startup marker;
- binds marker ownership to `HERMES_PROFILE` plus the exact `gateway run`
  process, using `/proc/self/cmdline` on Linux and a portable argv fallback;
- adds a regression test and installation guidance for the resulting
  `startup_marker_mismatch` false disarm;
- preserves fail-closed outbound behavior when marker identity is invalid.

## 0.3.4 - 2026-08-07

- documents the bidirectional media-root contract for Hermes and OpenClaw;
- requires the runtime attachment cache to be passed as an explicit second
  `--media-root` when human outbound media is enabled;
- keeps OCR and vision outside the transport path and only on explicit request;
- adds a regression note for `media_path_rejected` caused by an omitted runtime
  cache root, without broadening access to the whole cache or `/tmp`.

## 0.3.3 - 2026-08-06

- projects provider-confirmed WhatsApp delivery/read receipts as one
  idempotent Telegram reaction on the original outbound message;
- uses the Bot API-compatible `👀` reaction for read/played state;
- never edits the operator's text and never emits a textual receipt fallback;
- adds a schema migration and regression coverage for monotonic reaction state.

## 0.3.2 - 2026-08-06

- captures provider receipt events for human outbound IDs and persists
  monotonic states without inference;
- upgrades the Hermes direct-bridge guard from V3 to V4 in place, with no
  parallel bridge or writer;
- validates the native Hermes profile configuration before a gateway restart,
  aborting the transaction on malformed YAML instead of accepting fallback
  configuration;
- preserves the bridge owner/group when a root-run guard replaces the source,
  preventing a safe patch from making the service unreadable on restart;
- adds regression tests for the canary failures observed during deployment.

## 0.3.1 - 2026-08-05

- requires the exact `group_approved` admission in the mirror ledger before
  Hermes can send from a Telegram topic to a WhatsApp group;
- keeps direct-contact outbound unchanged and fails closed if group admission
  evidence is missing, unreadable or belongs to another profile.
- permits SQLite WAL shared-memory coordination inside the guard sandbox while
  retaining `mode=ro`, `query_only` and zero data-changing SQL.
- neutralizes the legacy group-pilot `WHATSAPP_GROUP_ONLY_CAPTURE` environment
  flag so direct contacts remain captured while groups stay allowlist-gated.
- documents the required Telegram `free_response_topics` exception for the
  exact mirror forum topic; mention requirements remain enabled elsewhere.

## 0.3.0 - 2026-08-05

- separa autocriacao de topicos diretos da admissao seletiva de grupos;
- adiciona allowlist exata, modo de agente fail-closed e grill de dez campos;
- adiciona identidade humana com prioridade e bloqueio de IDs crus;
- separa transcript interno de audio do conteudo visivel no Telegram;
- limita retencao de midia gerenciada a 48 horas depois do ACK;
- adiciona receipts monotonicos e matriz de canarios humanos;
- eleva evento para schema 3 e ledger namespaced para schema 9.

All notable changes to Espelho Zap Portable are recorded here. The project
uses semantic versions for the portable product; a runtime installation is
accepted only after the separate human canary in `docs/ACCEPTANCE_TESTS.md`.

## 0.2.1 - 2026-08-04

- separates plugin registration from outbound authority with a private,
  release-bound ARM file created only after deployment preflight;
- revalidates ARM before reservation, claim, drain and the loopback request;
- keeps a missing, malformed, replaced or permissive ARM persistently
  fail-closed without losing the durable ledger;
- documents Hermes profile `.env` loading and the post-update compatibility
  gate without relying on the process's initial exec environment;
- binds the startup proof to the gateway PID so a marker from the previous
  process cannot make an updated runtime appear healthy;
- binds ARM authority to a deterministic fingerprint of the Hermes executable,
  venv identity, required package metadata and bounded full Python manifests,
  so updates disarm until the new runtime proves compatibility;
- adds a periodic post-update guard that independently recomputes that
  fingerprint, validates single-writer, ledger and authenticated bridge V3
  health, and atomically rearms only a compatible running gateway;
- automatically wakes durable `prepared` work after a compatible rearm while
  preserving WIP=1 and zero retry consumption while disarmed;
- hardens route and media reads against symlink replacement and concurrent
  mutation immediately before durable reservation and send;
- explicitly disables the inert legacy Hermes mirror plugin during activation.

## 0.2.0 - 2026-08-04

- restores the legacy runtime natural reply UX: an allowlisted human writes normally
  in the mapped Telegram topic and the exact WhatsApp conversation receives it;
- supports text, image, voice/audio, video and document without an LLM or `/wa`;
- reserves by Telegram message ID before sending, serializes WIP=1 and blocks
  replay or automatic retry of an ambiguous result;
- uses the already-paired runtime transport: OpenClaw's native WhatsApp channel
  adapter or Hermes's authenticated loopback endpoint, while generic
  agent/automatic outbound remains blocked;
- stages outbound media privately, verifies hash/size and removes the managed
  copy only after the runtime transport confirms delivery;
- keeps PR #187 and legacy legacy runtime workers out of discovery and execution.

## 0.1.0 - 2026-08-04

Initial portable product candidate:

- immutable inbound event ledger and explicit consumer cursors;
- exact WhatsApp-conversation to Telegram-forum-topic routing;
- persistent no-route blocks with no private-DM fallback;
- delivery outbox, leases, retries and ambiguous-outcome quarantine;
- original text, image-caption, audio/voice and temporary-media lifecycle;
- additive legacy runtime route/deduplication import;
- thin Hermes and OpenClaw adapters;
- deterministic Daily Notes, claims and search projections;
- secret-free configuration, CLI, per-user installer, systemd timer and skill;
- offline contract, packaging, migration and rollback tests.

Known acceptance boundary: no live channel is declared accepted by this
release until a human verifies one new text, image and audio in their exact
forum topics and a replay cycle proves zero duplicate.
