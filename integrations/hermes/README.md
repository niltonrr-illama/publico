# Hermes adapter

The standalone plugin registers the official `pre_gateway_dispatch` hook. It
observes WhatsApp inbound and the configured Telegram mirror forum. WhatsApp
events are normalized and recorded/enqueued locally. Every WhatsApp event returns
`{"action":"skip","reason":"espelho-zap-passive"}`, even when capture fails,
so it cannot enter the auth/agent/reply loop.

Required environment: absolute `ESPELHO_ZAP_CLI`, absolute
`ESPELHO_ZAP_CONFIG`, non-empty `ESPELHO_ZAP_SOURCE_PROFILE_ID`, explicit
`ESPELHO_ZAP_PRIVACY_SCOPE`, absolute `ESPELHO_ZAP_HOOK_HEALTH_FILE`, and
`ESPELHO_ZAP_MEDIA_ROOTS` when media is enabled. The health file stores only
aggregate counters, timestamps, and sanitized error codes.

Set `ESPELHO_ZAP_TELEGRAM_FORUM_CHAT_ID` to the exact mirror forum supergroup.
Telegram events from that chat are also returned as `action=skip`: the forum is
a data plane, never an agent command surface. With human outbound disabled they
are only suppressed. With it enabled, an ordinary message from an allowlisted
human in an exactly mapped topic is reserved and sent to that WhatsApp route;
it never enters the LLM. Telegram DMs and other chats keep normal Hermes
dispatch.

Human outbound is explicit opt-in through these environment references:

- `ESPELHO_ZAP_HUMAN_OUTBOUND_ENABLED=enabled`;
- `ESPELHO_ZAP_HUMAN_OUTBOUND_ALLOWED_USERS`;
- absolute `ESPELHO_ZAP_HUMAN_OUTBOUND_ROUTE_MAP`;
- absolute private `ESPELHO_ZAP_HUMAN_OUTBOUND_TOKEN_FILE`;
- absolute `ESPELHO_ZAP_HUMAN_OUTBOUND_LEDGER`;
- absolute existing `ESPELHO_ZAP_HUMAN_OUTBOUND_MIRROR_LEDGER`;
- absolute `ESPELHO_ZAP_HUMAN_OUTBOUND_MANAGED_MEDIA_ROOT`;
- absolute private `ESPELHO_ZAP_HUMAN_OUTBOUND_ARM_FILE`;
- exact lowercase 40-hex `ESPELHO_ZAP_RELEASE_COMMIT`.

### Telegram receipt reactions

Set `ESPELHO_ZAP_RECEIPT_REACTIONS=enabled` to project provider-confirmed
WhatsApp receipts onto the original Telegram outbound message. The observer
reads the private human-outbound ledger and the mirror receipt projection,
then calls Telegram `setMessageReaction` in the exact forum topic:

- `✅` means WhatsApp delivery was provider-confirmed;
- `👀` means WhatsApp read/played was provider-confirmed.

The original Telegram message is never edited and no status reply is sent.
Reactions advance monotonically and are persisted idempotently in the mirror
ledger. If Telegram rejects a reaction, the receipt remains durable and the
next bounded observer cycle may retry it; there is no textual fallback.

The setting is disabled unless explicitly enabled. It requires the existing
`ESPELHO_ZAP_HUMAN_OUTBOUND_LEDGER` path and the configured Telegram bot token.

Direct-contact routes remain authorized by the exact topic map. A WhatsApp
group route has an additional mandatory gate: the mirror ledger must contain
the exact profile-scoped conversation as `group_approved`. Merely adding a
`@g.us` destination to the topic map never authorizes group outbound.

`ENABLED=enabled` loads the route map and initializes the private ledger, but it
does **not** authorize a send. Human outbound is armed only while the ARM file
is a regular non-symlink file, mode `0600` on POSIX, whose exact JSON is:

```json
{"schema_version":1,"release_commit":"<40hex>","plugin_sha256":"<64hex>","hermes_runtime_fingerprint":"<64hex>","armed":true}
```

The commit must equal `ESPELHO_ZAP_RELEASE_COMMIT`, the plugin SHA-256 must
equal the bytes loaded by the running gateway, and the runtime fingerprint must
equal the fingerprint published by that process's startup marker. Missing,
replaced, permissive, malformed or mismatched ARM means persistent fail-closed
disarm: no Telegram event is prepared and no queued item is drained. A reviewed activation helper
may create the ARM file only after preflight; removing it revokes new sends
that have not crossed the final pre-request validation, without deleting the
ledger. It is not a linearizable cancellation mechanism for an already
 in-flight system call. Re-arming admits new events; a dedicated local ARM
 watcher wakes an older `prepared` queue automatically, without waiting for a
 new chat event or consuming a retry while disarmed. The optional private startup marker reports only
`human_outbound_enabled` and `human_outbound_armed` plus release/plugin/runtime
identity, gateway PID and timestamp; it contains no route, destination, token
or message. A post-update checker must require that PID to be the currently
running gateway, preventing a stale marker from proving a new process.

`hermes_runtime_fingerprint` is deterministic for the running Hermes code. It
 hashes the configured and resolved `sys.executable`, its bytes, `sys.prefix`,
 `sys.base_prefix`, an existing `pyvenv.cfg`, package metadata, and bounded
 deterministic manifests of all Python source under the required `gateway` and
 `hermes_cli` package roots. Discovery uses an already-loaded module's
 `__file__` first, then ordered `sys.path` lookup; it never imports a Hermes
 module. A missing required component invalidates the fingerprint and keeps the
 lane disarmed.

### Gateway launchers and Hermes upgrades

Hermes loads the selected profile's `.env` inside the gateway before plugin
discovery. A multi-profile launcher therefore needs to select the correct
profile/home; it must not duplicate the whole `.env` into the initial exec
environment. On Linux, `/proc/<pid>/environ` reflects the exec-time environment
and is not sufficient proof that Hermes failed to load profile values later.

Every restart or upgrade is an acceptance gate, not proof by absence of an
error. Before retaining or recreating the ARM file, verify in the live gateway
process that the plugin startup marker names the expected release, exact
 plugin hash, runtime fingerprint and current gateway PID, the outbound ledger
 passes `quick_check`, the direct bridge is connected in observe-only mode, and
 only one writer owns the profile. A `prepared` row may remain durable and be
 rearmed after those gates. A `sending` row defers a polling cycle only when the
 current ARM already matches; an incompatible sending row fails closed. If any
 other check fails, leave/remove ARM and report the runtime as disarmed. A future
Hermes API incompatibility can therefore interrupt outbound, but cannot
authorize an unproved transport or fail silently as a successful upgrade.

The fixed transport is `http://127.0.0.1:3011/mirror-human-send`. The route is
chosen from the topic map, never from message content. Text, image, voice,
audio, video and document are supported. See `../../docs/HUMAN_OUTBOUND.md`.
`human-outbound.env.example` is an inert key/path template; it carries no
runtime ID, route, token or conversation data.

The direct-bridge compatibility guard is version 4. It captures only provider
receipts for message IDs tracked by the human route and upgrades an existing
version 3 guard in place; it never installs a second bridge or a parallel
writer. The installer validates the native Hermes config before any restart.

The host Telegram adapter must also admit the exact mirror forum topic as a
data-plane topic. Add the forum chat to `allowed_chats` and
`group_allowed_chats`, and add `<forum_chat_id>:<thread_id>` to
`free_response_topics`. Do not disable mention requirements globally and do
not use `free_response_chats` for the whole forum: that would either filter
the topic before this plugin or make every topic conversational. The Portable
installer/deployment must preserve this topic-level exception across Hermes
restarts and upgrades.

When an external paired bridge is the single capture owner, set
`ESPELHO_ZAP_HERMES_NATIVE_WHATSAPP_CAPTURE=disabled`. The plugin still blocks
any native Hermes WhatsApp event before the LLM, but does not ingest it a
second time. The default is `enabled` for installations that use the native
Hermes hook as their only capture adapter.

The installer prepared-only mode copies this directory to
`${XDG_DATA_HOME:-$HOME/.local/share}/espelho-zap/runtime-staging/hermes/espelho-zap-portable`
and writes an inert secret-free template. This path is deliberately outside
Hermes discovery; prepared means neither discovered nor loaded, and the runtime
home/config/gateway is untouched.

The current Hermes CLI cannot prove that `pre_gateway_dispatch` was loaded in
the running gateway, so the installer fails closed on
`--runtime hermes --enable-runtime`. A separately reviewed and authorized human
activation procedure must prove hook registration in the gateway process, an
expected aggregate health-file delta from a synthetic/authorized inbound, zero
agent/automatic outbound, and one authorized human outbound canary when that
lane is enabled. Until all are evidenced, report only
`prepared`, never `enabled`.

Contract references:
<https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/hooks.md#pre_gateway_dispatch>
and
<https://github.com/NousResearch/hermes-agent/blob/main/gateway/platforms/base.py>.
