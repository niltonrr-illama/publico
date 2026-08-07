# OpenClaw adapter

This external ESM plugin uses the documented `inbound_claim`,
`message_received`, `before_agent_reply`, `message_sending`, and
`reply_payload_sending` hooks. A fixed, shell-free child process feeds the
portable CLI over stdin. A bound WhatsApp inbound is captured synchronously
before `inbound_claim` returns a silent handled result; the other hooks provide
unbound-channel capture and fail-closed automatic outbound suppression. It
never registers a model tool. In version 0.3.1, an explicitly enabled human
lane accepts a normal message from an authorized operator in the exact mapped
Telegram topic and sends it through OpenClaw's native WhatsApp outbound
adapter and the account already paired in OpenClaw. It creates no second
WhatsApp client, HTTP bridge, token, QR code, `/wa` command, or LLM turn.

Required environment: absolute `ESPELHO_ZAP_CLI`, absolute
`ESPELHO_ZAP_CONFIG`, non-empty `ESPELHO_ZAP_SOURCE_PROFILE_ID`, explicit
`ESPELHO_ZAP_PRIVACY_SCOPE`, absolute `ESPELHO_ZAP_HOOK_HEALTH_FILE`, and
`ESPELHO_ZAP_MEDIA_ROOTS` for staged media.
OpenClaw must also have
`channels.whatsapp.pluginHooks.messageReceived=true`; registration fails closed
otherwise. Because this is a non-bundled plugin using the conversation hook
`before_agent_reply`, OpenClaw also requires
`plugins.entries.espelho-zap-portable.hooks.allowConversationAccess=true`.
Without that explicit consent the runtime must be considered not installed,
even if discovery succeeds.

Human outbound is disabled unless
`ESPELHO_ZAP_HUMAN_OUTBOUND_ENABLED=enabled`. Enabling it additionally
requires `ESPELHO_ZAP_TELEGRAM_FORUM_CHAT_ID`,
`ESPELHO_ZAP_HUMAN_OUTBOUND_ALLOWED_USERS`, absolute
`ESPELHO_ZAP_HUMAN_OUTBOUND_ROUTE_MAP`, absolute
`ESPELHO_ZAP_HUMAN_OUTBOUND_LEDGER`, and absolute private
`ESPELHO_ZAP_HUMAN_OUTBOUND_MANAGED_MEDIA_ROOT`. If more than one WhatsApp
account exists, set `ESPELHO_ZAP_HUMAN_OUTBOUND_WHATSAPP_ACCOUNT_ID`;
otherwise OpenClaw resolves its configured default account.

The exact Telegram forum remains a data plane: `before_agent_reply` silences
every message there. Only a non-bot sender in the explicit allowlist and a
single exact active route may invoke
`api.runtime.channel.outbound.loadAdapter("whatsapp")`. Text uses `sendText`;
image, voice/audio, video, and document facts use `sendMedia`, preserving voice
and document semantics explicitly. Media comes from the documented top-level
OpenClaw `media` array, is copied into private managed storage before the
durable reservation, removed after confirmed success or proven rejection, and
retained for an uncertain delivery. The append-only private ledger deduplicates by
`telegram:<chat>:<thread>:<message>` and serializes sends at WIP=1. A reserved
job resumes after restart; a job that had begun dispatch becomes `uncertain`
and is never replayed blindly.

Each media item is limited to 128 MiB and one Telegram message to 256 MiB in
total. The adapter checks declared filesystem size before hashing, enforces the
same limits while copying, then verifies size and SHA-256 after the copy.
`service`, `service_message`, `automation`, `automatic`, bot/assistant/system/
tool roles, and their boolean flags are always non-human and never dispatched.

Prepared-only copies this directory to
`${XDG_DATA_HOME:-$HOME/.local/share}/espelho-zap/runtime-staging/openclaw/espelho-zap-portable`,
outside `<RUNTIME_HOME>/extensions`; it neither creates/touches runtime config
nor enters discovery. Explicit `--enable-runtime` quiesces the managed Gateway,
snapshots config/index/plugin state, uses the official local-path install to
record provenance, persists variables under
`plugins.entries.espelho-zap-portable.env`, enables both required consents, and
restarts the Gateway. `inspect --runtime` must then show `message_received`,
`before_agent_reply`, `message_sending`, and `reply_payload_sending`, followed
by a deep RPC health check. The source also registers `inbound_claim` for bound
conversations. It does not pair or enable a second WhatsApp channel.

`message_received` alone is observation-only and is not sufficient evidence of
passive operation. `before_agent_reply` supplies the silent pre-agent gate;
`message_sending` and `reply_payload_sending` cancel automatic WhatsApp
delivery paths as defense in depth. Human outbound bypasses those agent paths
and can use only OpenClaw's loaded WhatsApp channel adapter with the configured
account. Any missing hook, consent, outbound loader, channel adapter, or exact
route keeps activation fail-closed.

Runtime proof after installation: `openclaw plugins inspect
espelho-zap-portable --runtime --json`.

Contract references: <https://docs.openclaw.ai/plugins/building-plugins> and
<https://docs.openclaw.ai/plugins/hooks#message-hooks>.
