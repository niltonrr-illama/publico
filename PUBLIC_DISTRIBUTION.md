# Public distribution

This repository contains the portable Espelho Zap product only. It does not
contain real conversations, media, contacts, route maps, ledgers, session
files, tokens, provider credentials, or runtime state.

## Per-operator isolation

Each installation must use its own runtime profile and its own:

- WhatsApp pairing/session;
- Telegram bot and forum/topic map;
- `profile_id`, ledger, cursor and locks;
- media roots and temporary spool;
- outbound authorization and credentials.

Do not copy another operator's `.env`, state database, session directory or
route map. SecondaryOperator can use the same package inside a separate Hermes profile;
the package itself does not merge profiles or share their state.

## Media safety

Pass every approved media directory explicitly with repeated `--media-root`.
Include the runtime's attachment cache only when human outbound media is
intended. Never pass `/tmp`, an entire home directory or a broad cache. OCR and
vision are outside transport and require an explicit operator request.

Before production use, follow `docs/ACCEPTANCE_TESTS.md` and complete both the
inbound and outbound human canaries.
