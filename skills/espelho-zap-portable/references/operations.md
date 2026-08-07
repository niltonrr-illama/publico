# Operational reference

All operational commands emit one aggregate JSON object. They never echo message
text, captions, local media paths, or token values.

## CLI

```bash
espelho-zap --config ~/.config/espelho-zap/config.toml init
espelho-zap --config ~/.config/espelho-zap/config.toml doctor
espelho-zap --config ~/.config/espelho-zap/config.toml health
espelho-zap --config ~/.config/espelho-zap/config.toml backup NEW_BACKUP.sqlite3
espelho-zap --config ~/.config/espelho-zap/config.toml route list
espelho-zap --config ~/.config/espelho-zap/config.toml route blocked-list
espelho-zap --config ~/.config/espelho-zap/config.toml route verify-destination CHAT_ID --thread-id THREAD_ID
espelho-zap --config ~/.config/espelho-zap/config.toml route provision-topic CONVERSATION CHAT_ID TOPIC_NAME --confirm-create
espelho-zap --config ~/.config/espelho-zap/config.toml route set CONVERSATION CHAT_ID THREAD_ID
espelho-zap --config ~/.config/espelho-zap/config.toml route reconcile CONVERSATION
espelho-zap --config ~/.config/espelho-zap/config.toml route import-legacy runtime LEGACY.json
espelho-zap --config ~/.config/espelho-zap/config.toml ingest EVENT.json
espelho-zap --config ~/.config/espelho-zap/config.toml worker-once
```

Updating an existing destination requires `route set ... --allow-update`.
`route list` returns counts and opaque references by default. Add
`--show-identifiers` only for an explicitly authorized routing inspection.
`route blocked-list` returns exact namespaced opaque event and conversation
references without message content. After provisioning the route, pass its
conversation reference to `route reconcile`; route creation alone never
releases held events. Both block commands accept a positive `--limit`, and the
list can select `--state blocked_no_route` or `--state requeued`.

`route verify-destination` makes one read-only Telegram `getChat` call. Success
requires the returned ID to match, `type=supergroup`, and `is_forum=true`; the
command never sends a message. `--thread-id` validates only positive-integer
syntax because `getChat` cannot prove topic existence.

A person creates the Telegram forum supergroup once, enables Topics, and grants
the bot `can_manage_topics`. `route provision-topic ... --confirm-create` then
creates one exact topic inside that existing forum and commits its exact route;
without confirmation it performs no external mutation. It never creates or
falls back to a DM.

`backup` uses SQLite's online backup API, runs `quick_check` on source and copy,
fsyncs the copy, reports its SHA-256 and size, and atomically publishes a new
file. An existing destination is never replaced. No restore CLI exists in this
release; restoration remains a stopped-writer, separately authorized runbook
operation.

Ingest accepts raw or already opaque `conversation_id` and `actor_ref`; raw
values are SHA-256 namespaced at the CLI boundary before persistence. Supported
privacy scopes are `owner_private`, `partnership_restricted`, and `area_shared`.
Omission uses the conservative `owner_private` default. Event schema defaults to
version `1`.

Exit codes:

- `0`: success
- `2`: invalid command or input
- `3`: missing or unsafe configuration/secret
- `4`: doctor or health failure
- `5`: operation rejected or failed safely
- `130`: interrupted

## Secret provisioning

Keep TOML secret-free:

```toml
[telegram]
token_env = "ESPELHO_ZAP_TELEGRAM_BOT_TOKEN"
token_file = "~/.config/espelho-zap/telegram.token"
```

Environment takes precedence. The token file must exclude group and other access
on POSIX systems; use mode `0600`.

## Linux lifecycle

From the project root:

```bash
bash installer/install.sh preflight
bash installer/install.sh install --dry-run
bash installer/install.sh install
bash installer/install.sh install --runtime hermes --source-profile PROFILE \
  --media-root /absolute/approved/mirror-media \
  --media-root /absolute/runtime/cache/images
bash installer/install.sh install --runtime openclaw --runtime-home "$HOME/.openclaw" \
  --media-root /absolute/approved/mirror-media \
  --media-root /absolute/runtime/cache/images
bash installer/install.sh upgrade --runtime openclaw --runtime-home "$HOME/.openclaw" --enable-runtime \
  --media-root /absolute/approved/mirror-media \
  --media-root /absolute/runtime/cache/images
bash installer/install.sh upgrade --runtime openclaw --runtime-home "$HOME/.openclaw" --enable-runtime --clear-media-roots
bash installer/install.sh upgrade --source dist/espelho_zap_portable-VERSION-py3-none-any.whl --dry-run
bash installer/install.sh upgrade --source dist/espelho_zap_portable-VERSION-py3-none-any.whl
bash installer/install.sh uninstall --dry-run
bash installer/install.sh uninstall
bash installer/install.sh uninstall --runtime hermes
bash installer/install.sh uninstall --runtime openclaw --runtime-home "$HOME/.openclaw"
```

Upgrade builds a candidate venv, creates a validated ledger backup through the
candidate CLI, then captures and quiesces timer/worker. For an active runtime it
also requires the exact runtime/home plus `--enable-runtime`, quiesces the
gateway, and snapshots plugin/config/index before swapping or initializing.
Failure restores exact config/ledger/health/records, old venv/skill/plugin,
systemd bytes, and prior running/enabled states. Snapshots remain retained.

Real install/upgrade/uninstall transactions share a non-blocking per-user
`flock`; a concurrent mutation fails before writing. Preflight enforces
`ESPELHO_ZAP_MIN_FREE_BYTES` (256 MiB by default).

`--media-root` is repeatable and accepts an existing absolute readable
directory. The installer writes the approved list to both
`worker.source_media_roots` and the plugin's inert
`ESPELHO_ZAP_MEDIA_ROOTS` template. Omitting the option preserves the existing
list; only a fresh config becomes empty/fail-closed. `--clear-media-roots`
explicitly revokes all roots and cannot be combined with `--media-root`.
`--source-profile` overrides and persists `worker.profile_id`; omission
preserves it. Hook health is aggregate-only at
`${XDG_STATE_HOME:-$HOME/.local/state}/espelho-zap/capture-health.json`.

The default is prepared-only: plugin bytes are copied to
`${XDG_DATA_HOME:-$HOME/.local/share}/espelho-zap/runtime-staging/<runtime>/espelho-zap-portable`,
outside runtime discovery. No runtime home/config is created, no hook is
consented, and no gateway is restarted. Hermes `--enable-runtime` is
deliberately refused until a human canary can prove the hook was loaded in the
gateway and produced no reply/outbound.

OpenClaw `--enable-runtime` uses the official local-path install, persists the
secret-free environment, enables `messageReceived` and
`allowConversationAccess=true`, and requires runtime inspection of
`message_received`, `before_agent_reply`, `message_sending`, and
`reply_payload_sending` plus deep RPC health. It does not pair a second channel.

An active integration makes default uninstall fail before CLI removal. Pass the
exact recorded runtime/home; the installer then stops writers, deactivates by
the official CLI, restores only plugin-owned config fields, proves absence and
process state, restores pre-install units, and removes the CLI last. Data,
health, and rollback backups are preserved. For prepared-only staging, select
the target explicitly to remove its managed copy.

The timer is installed disabled. Enable only after provisioning the token and a
successful doctor:

```bash
systemctl --user enable --now espelho-zap@default.timer
```

The `default` systemd instance is only an operational label. Delivery uses the
persisted `worker.profile_id`; the unit never overrides it with `%i`.

## Installing the skill

The installer copies the skill idempotently to the AgentSkills-compatible shared
location below, with private permissions:

```bash
install -d -m 0700 ~/.agents/skills
cp -a skills/espelho-zap-portable ~/.agents/skills/
```

OpenClaw can install the same directory directly:

```bash
openclaw skills install ./skills/espelho-zap-portable --as espelho-zap-portable --global
```

For Hermes, add `~/.agents/skills` to `skills.external_dirs`.
The installer reports this requirement but never edits Hermes configuration.
