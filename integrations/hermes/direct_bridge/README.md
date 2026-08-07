# Hermes direct bridge integration

This integration preserves an **existing, already-paired** external Baileys
bridge. It captures inbound passively and permits only the authenticated human
reply lane from mapped Telegram topics. It does not contain or copy
`creds.json`, session keys, media, contacts or tokens.

## Safety contract

1. Run `bridge_guard.py apply` once against the deployment copy of `bridge.js`,
   with an explicit new backup path.  The patch is deterministic and idempotent.
2. `observer_launcher.py` refuses an unguarded or group/world-writable bridge,
   a missing paired `creds.json`, symlinked state, or non-private session files.
3. The launcher always overwrites `WHATSAPP_OBSERVE_ONLY=true`. Generic
   `/send`, `/send-media`, typing and agent outbound remain blocked. When both
   reviewed paths are configured, it exposes only authenticated
   `/mirror-human-send` and the read-only `/mirror-human-health` loopback
   probe. The health payload identifies `guardVersion=3`; the patch is refused
   unless its top-level `sock` and `connectionState` bindings are compatible.
   Inherited environment cannot select another token or media root.
4. A non-blocking `flock` owns the paired session for the lifetime of Node.
   A second observer fails before opening WhatsApp.
5. Only the observer spool, cache, lock and existing session roots are writable.
   The session is never paired or recreated by this launcher.
6. The portable observer removes a bridge-cache media file only after its
   managed copy is durably committed and the bridge confirms the spool ACK.
   The managed copy remains until Telegram delivery is confirmed.
7. When Baileys emits a primary LID plus `chatIdAlt`, the observer may use the
   alternate identity only when that exact identity already resolves through
   an imported alias to a configured route.  It never derives or guesses an
   identity; conflicting routed identities fail closed into quarantine.
8. Human outbound accepts only an allowlisted Telegram author in the configured
   forum and an exact mapped topic. It reserves the Telegram message ID before
   sending, serializes WIP=1 and never retries an ambiguous result.
9. The dedicated endpoint accepts the exact mapped WhatsApp JID for an
   individual contact or group. Runtime aliases such as `@lid` are inbound
   reconciliation metadata and never become an outbound destination.

## Prepared-only workflow

```bash
python3 bridge_guard.py apply /absolute/bridge.js \
  --backup /absolute/backups/bridge.js.before-observe-only
python3 bridge_guard.py check /absolute/bridge.js
python3 observer_launcher.py --config /etc/espelho-zap/default-direct-bridge.toml --check
```

Only after these commands pass should an operator render and install the
system-level templates in `packaging/systemd/`.  Merely copying these files
does not install, enable or start a unit.  A human text/photo/audio canary and
single-writer proof remain mandatory before declaring the channel accepted.

Path values may be overridden without editing the config through
`ESPELHO_ZAP_BRIDGE_NODE`, `ESPELHO_ZAP_BRIDGE_JS`,
`ESPELHO_ZAP_BRIDGE_SESSION_DIR`, `ESPELHO_ZAP_BRIDGE_SPOOL_FILE`,
`ESPELHO_ZAP_BRIDGE_CACHE_ROOT`, and `ESPELHO_ZAP_BRIDGE_LOCK_FILE`.  Scalar
fields use the same `ESPELHO_ZAP_BRIDGE_<FIELD>` convention.

The launcher passes the configured private paths to Node as
`ESPELHO_ZAP_HUMAN_OUTBOUND_TOKEN_FILE` and
`ESPELHO_ZAP_HUMAN_OUTBOUND_MEDIA_ROOT`. These are the only names consumed by
the injected V3 endpoints; the former generic manual-outbound variables are not
used.
