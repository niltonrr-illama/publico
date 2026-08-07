---
name: espelho-zap-portable
description: Operate and diagnose the portable WhatsApp-to-Telegram mirror through the espelho-zap CLI. Use for private initialization, health checks, verified SQLite backups, Telegram forum verification, legacy runtime route migration, explicit route-block reconciliation, content-safe event ingestion, one-shot delivery workers, Linux lifecycle operations, or rollback planning while preserving ledger data.
---

# Espelho Zap Portable

Operate through `espelho-zap`; do not edit the SQLite ledger or place tokens in TOML.

## Workflow

1. Resolve the config path from the user's explicit path or `ESPELHO_ZAP_CONFIG`.
2. Run `espelho-zap --config PATH doctor` before delivery or route mutation.
3. Stop if JSON reports `ok: false`; use its exit code and aggregate check name as evidence.
4. Create a new-path `backup` before a risky migration or upgrade and retain its reported SHA-256. There is no restore CLI.
5. Verify the human-created Telegram forum supergroup with
   `route verify-destination`. To create a needed exact topic inside it, require
   `route provision-topic ... --confirm-create`; never create or fall back to a
   DM.
6. Inspect `health`, `route list`, and `route blocked-list`. Use `route list --show-identifiers` only when the operator explicitly needs exact routing IDs.
7. Require `--allow-update` for an existing destination. Requeue held events only through explicit `route reconcile` after the route is ready.
8. Run `worker-once` only after doctor passes. Treat one invocation as at most one delivery attempt.
9. Re-run `health` and report counts/states, not message content.
10. Keep direct-contact and WhatsApp-group onboarding separate. Direct inbound
    may auto-create its topic when configured. A group requires the exact route
    plus `group approve`; never infer approval from an existing topic.
11. Keep every approved group at `agent_mode=none` unless the operator answers
    the ten grill questions one at a time and approves `mention_only`.
12. Resolve a human participant label before accepting a group canary. Use
    `identity set` for the highest-priority manual alias; never render raw JIDs.
13. Treat automatic tests as `prepared`. Record real inbound/outbound text,
    image, and audio canaries before reporting `installed_success`.

Use `scripts/preflight.sh` for the standard doctor call. Read
`references/operations.md` for command forms, exit codes, installer gates, and
AgentSkills installation paths.

## Safety boundaries

- Reference a Telegram token with `telegram.token_env` or `telegram.token_file` only.
- Let ingest hash raw conversation and actor identifiers at the CLI boundary. Omitted `privacy_scope` becomes the documented conservative default `owner_private`.
- Preserve `chat_id` and `thread_id` as separate strings; require a positive topic ID.
- Require `getChat` to return the requested negative ID, `type=supergroup`, and `is_forum=true`. Never use verification as proof of topic existence.
- Import legacy state through `route import-legacy runtime`; do not copy secrets from legacy files.
- Treat `blocked_no_route` as held state. Route creation never authorizes backlog release by itself.
- Create backups online with SQLite backup, source and destination `quick_check`, SHA-256, and no overwrite. Do not imply a restore command exists.
- Use installer `--dry-run` before install, upgrade, or uninstall.
- Distinguish prepared-only from enabled. Select one target with
  `--runtime hermes`, `--runtime openclaw --runtime-home ABSOLUTE_PATH`, or the
  default `none`. Prepared plugins live under the application's
  `runtime-staging`, outside runtime discovery, and do not touch runtime
  home/config/gateway.
- Hermes activation is fail-closed in this release because its CLI cannot prove
  a hook loaded inside the running gateway. Do not use disk presence or plugin
  discovery as proof. Keep it prepared until a separately authorized human
  canary proves hook execution, aggregate health delta, and zero reply/outbound.
- Use OpenClaw `--enable-runtime` only with explicit authority to persist
  runtime env/config, use the official plugin installer, restart the managed
  gateway, and run the load/RPC canaries. Require the global WhatsApp
  `messageReceived` hook, `allowConversationAccess=true`, the pre-agent silence
  hook, and both outbound-cancellation hooks. Never pair a second client.
- Pass each approved media directory with repeatable `--media-root`. Omission
  preserves existing roots; on a fresh config it produces an empty,
  fail-closed list. Use `--clear-media-roots` only for explicit revocation.
  Keep persisted `worker.profile_id`, `ESPELHO_ZAP_SOURCE_PROFILE_ID`, and the
  aggregate `capture-health.json` path aligned.
- Require the configured free-space check to pass before initialization or installation.
- Keep the systemd timer disabled until the operator explicitly enables an instance.
- Keep automatic OCR/vision/LLM out of the mirror path. Audio transcripts may
  feed internal context only and must never be rendered in Telegram.
- Purge managed media only after ACK and never retain it longer than 48 hours.
- Receipt state is monotonic and provider-backed; never infer read or played.
- On uninstall, preserve config, token file, ledger, hook health, state, and all
  rollback backups. Restore any pre-install systemd unit bytes/state.
- If an active integration record exists, never work around the installer's
  refusal: select the exact recorded runtime/home so it can quiesce, deactivate
  with the official CLI, restore only owned config fields, run absence/state
  canaries, and remove the CLI last.
- Let the installer remove only manifest-marked skill/plugin copies. Select a
  prepared runtime explicitly if its staging copy should also be removed.
