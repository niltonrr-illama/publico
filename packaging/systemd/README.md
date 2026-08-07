# systemd templates

The existing `espelho-zap@.service` and timer are user-level portable worker
templates.  The `espelho-zap-hermes-*.in` files are separate **system-level,
prepared-only** templates for a host that preserves an external paired Hermes
bridge.

Before copying a rendered unit to `/etc/systemd/system`, replace every
`@NAME@` placeholder with an absolute path or the dedicated unprivileged
service user/group.  Do not render with `root`.  Keep the configured session,
spool, cache, lock, data, state and media roots disjoint and private.
The observer unit creates `/run/espelho-zap-%i` as mode `0700`; render each
profile's `lock_file` below that matching instance directory.
Render `@WORKER_ENV_FILE@` to a private environment file that already provides
the Telegram token variable named by `config.toml`; never copy its value into
the unit or the secret-free product configuration.
Render `@SOURCE_MEDIA_ROOTS@` as the space-separated, systemd-escaped list of
the same approved cache roots configured in `worker.source_media_roots`.  The
worker needs write access there only to retire a cache source after its managed
copy is committed and the bridge has acknowledged the spool item.

The templates do not install or enable themselves.  Required order is:

1. snapshot source, config, ledger, routes and supervision state;
2. apply/check the versioned bridge guard;
3. run the observer launcher with `--check`;
4. verify rendered units with `systemd-analyze verify`;
5. prove there is no old watchdog/worker/observer;
6. start the observer, then the worker timer;
7. run the human canary and rollback on any mismatch.

`Restart=on-failure` is bounded by the unit start limits.  The worker allows 12
starts per 120 seconds: enough for the healthy 15-second timer cadence, while a
persistent 10-second failure restart still reaches the limiter.  Each pipeline
run first persists and ACKs one bounded observer batch, then drains Telegram
deliveries with WIP=1.  The timer waits until the oneshot becomes inactive,
preventing overlap.  Writable paths are explicit; code and configuration stay
read-only.

The observer `ExecStart` is prefixed with systemd's `-` marker deliberately:
a capture-side error remains visible in its JSON result and private quarantine,
but it cannot starve delivery of earlier committed Telegram work.  Records with
a permanent static-contract failure are retained immutably in the ledger before
their bridge item is acknowledged; transient media/disk failures remain
unacknowledged for retry.

The separate `espelho-zap-hermes-upgrade-guard@.*.in` templates protect human
outbound across Hermes updates. They are also prepared-only and disabled by
default. Render `@REARM_FLAG@` empty for check-only operation or exactly
`--rearm-compatible-update` only for an explicitly authorized compatible
update. The guard must run as the profile owner, not root. Its full gates and
rendering contract are documented in
`integrations/hermes/UPGRADE_GUARD.md`.
Render `@PYTHON_BIN@` from the active profile gateway's resolved
`/proc/<pid>/exe`; the guard imports the exact hash-validated plugin without
registering it and recomputes the Hermes runtime fingerprint in that same
interpreter.
