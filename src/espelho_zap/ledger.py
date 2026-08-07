"""Versioned SQLite ledger for idempotent mirror delivery."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import replace
from pathlib import Path
from typing import Iterable

from .models import (
    DEFAULT_SOURCE_PROFILE_ID,
    DeliveryClaim,
    InboundEvent,
    PRIVACY_SCOPES,
    Route,
    RouteBlock,
    opaque_ref,
)


SCHEMA_VERSION = 10


class LedgerError(RuntimeError):
    code = "ledger_error"

    def __init__(self, code: str | None = None):
        self.code = code or self.code
        super().__init__(self.code)


class EventConflictError(LedgerError):
    code = "event_payload_conflict"


class RouteMissingError(LedgerError):
    code = "route_missing"


class RouteConflictError(LedgerError):
    code = "route_conflict"


class LeaseLostError(LedgerError):
    code = "lease_lost"


class RuntimeLockError(LedgerError):
    code = "runtime_lock_unavailable"


def _now_seconds(value: float | int | None = None) -> int:
    return int(time.time() if value is None else value)


def _opaque_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class MirrorLedger:
    """Private append-first event ledger plus mutable delivery state."""

    def __init__(self, db_path: str | Path, *, read_only: bool = False):
        self.db_path = Path(db_path)
        self.read_only = bool(read_only)
        if self.read_only:
            if not self.db_path.is_file():
                raise LedgerError("ledger_missing")
            uri = self.db_path.resolve().as_uri() + "?mode=ro&immutable=1"
            self.connection = sqlite3.connect(
                uri, uri=True, isolation_level=None, timeout=10, check_same_thread=False
            )
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(
                self.db_path, isolation_level=None, timeout=10, check_same_thread=False
            )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 10000")
        if self.read_only:
            current = int(
                self.connection.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM mirror_schema_migrations"
                ).fetchone()[0]
            )
            if current != SCHEMA_VERSION:
                raise LedgerError("unsupported_schema_version")
        else:
            self.connection.execute("PRAGMA journal_mode = WAL")
            self.connection.execute("PRAGMA synchronous = FULL")
            self._migrate()
            self._harden_files()

    def __enter__(self) -> "MirrorLedger":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        if not self.read_only:
            self._harden_files()
        self.connection.close()

    def _harden_files(self) -> None:
        if self.read_only:
            return
        for candidate in (
            self.db_path,
            Path(str(self.db_path) + "-wal"),
            Path(str(self.db_path) + "-shm"),
        ):
            try:
                if candidate.exists():
                    os.chmod(candidate, 0o600)
            except OSError:
                pass

    def _migrate(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS mirror_schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at INTEGER NOT NULL
            )
            """
        )
        current = int(
            self.connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM mirror_schema_migrations"
            ).fetchone()[0]
        )
        if current > SCHEMA_VERSION:
            raise LedgerError("unsupported_schema_version")
        if current == 0:
            try:
                self.connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS mirror_events (
                        event_id TEXT PRIMARY KEY,
                        payload_hash TEXT NOT NULL,
                        source TEXT NOT NULL,
                        conversation_id TEXT NOT NULL,
                        occurred_at TEXT NOT NULL,
                        privacy_scope TEXT NOT NULL CHECK (
                            privacy_scope IN ('area_shared', 'partnership_restricted', 'owner_private')
                        ),
                        payload_json TEXT NOT NULL,
                        captured_at INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS mirror_events_conversation_time
                    ON mirror_events(conversation_id, occurred_at, event_id);

                    CREATE TRIGGER IF NOT EXISTS mirror_events_immutable_update
                    BEFORE UPDATE ON mirror_events BEGIN
                        SELECT RAISE(ABORT, 'immutable_event');
                    END;
                    CREATE TRIGGER IF NOT EXISTS mirror_events_immutable_delete
                    BEFORE DELETE ON mirror_events BEGIN
                        SELECT RAISE(ABORT, 'immutable_event');
                    END;

                    CREATE TABLE IF NOT EXISTS mirror_routes (
                        conversation_id TEXT PRIMARY KEY,
                        chat_id TEXT NOT NULL,
                        thread_id TEXT NOT NULL CHECK (
                            thread_id <> '' AND CAST(thread_id AS INTEGER) > 0
                        ),
                        enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                        watermark_hash TEXT,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS mirror_deliveries (
                        delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT NOT NULL UNIQUE REFERENCES mirror_events(event_id),
                        target_chat_id TEXT NOT NULL,
                        target_thread_id TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (
                            state IN (
                                'pending', 'inflight', 'retry', 'sent',
                                'dead', 'blocked', 'uncertain'
                            )
                        ),
                        attempts INTEGER NOT NULL DEFAULT 0,
                        next_attempt_at INTEGER NOT NULL,
                        remote_ids_json TEXT,
                        last_error_code TEXT,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL,
                        UNIQUE(event_id, target_chat_id, target_thread_id)
                    );
                    CREATE INDEX IF NOT EXISTS mirror_deliveries_ready
                    ON mirror_deliveries(state, next_attempt_at, delivery_id);

                    CREATE TABLE IF NOT EXISTS mirror_leases (
                        delivery_id INTEGER PRIMARY KEY REFERENCES mirror_deliveries(delivery_id),
                        worker_id TEXT NOT NULL,
                        acquired_at INTEGER NOT NULL,
                        expires_at INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS mirror_leases_expiry ON mirror_leases(expires_at);

                    CREATE TABLE IF NOT EXISTS mirror_runtime_locks (
                        profile_id TEXT PRIMARY KEY,
                        owner_id TEXT NOT NULL,
                        acquired_at INTEGER NOT NULL,
                        expires_at INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS mirror_runtime_locks_expiry
                    ON mirror_runtime_locks(expires_at);

                    CREATE TABLE IF NOT EXISTS mirror_delivery_attempts (
                        attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        delivery_id INTEGER NOT NULL REFERENCES mirror_deliveries(delivery_id),
                        attempt_no INTEGER NOT NULL,
                        started_at INTEGER NOT NULL,
                        finished_at INTEGER,
                        outcome TEXT NOT NULL CHECK (
                            outcome IN ('started', 'sent', 'retry', 'dead', 'uncertain')
                        ),
                        error_code TEXT,
                        UNIQUE(delivery_id, attempt_no)
                    );

                    CREATE TABLE IF NOT EXISTS mirror_legacy_delivered (
                        event_key_hash TEXT PRIMARY KEY,
                        conversation_id TEXT NOT NULL,
                        imported_at INTEGER NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS mirror_legacy_imports (
                        source_hash TEXT PRIMARY KEY,
                        routes_seen INTEGER NOT NULL,
                        recent_ids_seen INTEGER NOT NULL,
                        imported_at INTEGER NOT NULL
                    );
                    """
                )
                now = _now_seconds()
                self.connection.execute(
                    "INSERT OR IGNORE INTO mirror_schema_migrations(version, applied_at) VALUES (?, ?)",
                    (1, now),
                )
                self.connection.execute("COMMIT")
            except BaseException:
                if self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                raise
            current = 1
        if current == 1:
            try:
                self.connection.executescript(
                    """
                    BEGIN IMMEDIATE;

                    -- A WhatsApp message id is scoped to its conversation.
                    -- Rebuild the early global tombstone index so an import
                    -- for one contact can never suppress another contact.
                    ALTER TABLE mirror_legacy_delivered RENAME TO mirror_legacy_delivered_v1;
                    CREATE TABLE mirror_legacy_delivered (
                        conversation_id TEXT NOT NULL,
                        event_key_hash TEXT NOT NULL,
                        imported_at INTEGER NOT NULL,
                        PRIMARY KEY(conversation_id, event_key_hash)
                    );
                    INSERT OR IGNORE INTO mirror_legacy_delivered(
                        conversation_id, event_key_hash, imported_at
                    )
                    SELECT conversation_id, event_key_hash, imported_at
                    FROM mirror_legacy_delivered_v1;
                    DROP TABLE mirror_legacy_delivered_v1;

                    CREATE TABLE mirror_route_blocks (
                        event_id TEXT PRIMARY KEY REFERENCES mirror_events(event_id),
                        conversation_id TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (
                            state IN ('blocked_no_route', 'requeued')
                        ),
                        reason TEXT NOT NULL CHECK (
                            reason IN ('route_missing', 'route_disabled')
                        ),
                        blocked_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL,
                        requeued_at INTEGER
                    );
                    CREATE INDEX mirror_route_blocks_active
                    ON mirror_route_blocks(state, conversation_id, blocked_at);

                    CREATE TABLE mirror_source_cursors (
                        adapter_id TEXT NOT NULL,
                        source_ref TEXT NOT NULL,
                        generation TEXT NOT NULL,
                        byte_offset INTEGER NOT NULL CHECK (byte_offset >= 0),
                        updated_at INTEGER NOT NULL,
                        PRIMARY KEY(adapter_id, source_ref)
                    );

                    CREATE TRIGGER mirror_routes_require_group_insert
                    BEFORE INSERT ON mirror_routes
                    WHEN CAST(NEW.chat_id AS INTEGER) >= 0
                    BEGIN
                        SELECT RAISE(ABORT, 'group_chat_required');
                    END;
                    CREATE TRIGGER mirror_routes_require_group_update
                    BEFORE UPDATE OF chat_id ON mirror_routes
                    WHEN CAST(NEW.chat_id AS INTEGER) >= 0
                    BEGIN
                        SELECT RAISE(ABORT, 'group_chat_required');
                    END;
                    """
                )
                if self.connection.execute(
                    "SELECT 1 FROM mirror_routes WHERE CAST(chat_id AS INTEGER) >= 0 LIMIT 1"
                ).fetchone():
                    raise LedgerError("legacy_dm_route_requires_remediation")
                self.connection.execute(
                    "INSERT INTO mirror_schema_migrations(version, applied_at) VALUES (?, ?)",
                    (2, _now_seconds()),
                )
                self.connection.execute("COMMIT")
            except BaseException:
                if self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                raise
            current = 2
        if current == 2:
            try:
                self.connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE mirror_managed_media (
                        path TEXT PRIMARY KEY,
                        event_id TEXT NOT NULL REFERENCES mirror_events(event_id),
                        state TEXT NOT NULL CHECK (
                            state IN ('active', 'cleanup_pending')
                        ),
                        cleanup_attempts INTEGER NOT NULL DEFAULT 0,
                        next_attempt_at INTEGER NOT NULL DEFAULT 0,
                        last_error_code TEXT,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL
                    );
                    CREATE INDEX mirror_managed_media_cleanup
                    ON mirror_managed_media(state, next_attempt_at, path);
                    """
                )
                self.connection.execute(
                    "INSERT INTO mirror_schema_migrations(version, applied_at) VALUES (?, ?)",
                    (3, _now_seconds()),
                )
                self.connection.execute("COMMIT")
            except BaseException:
                if self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                raise
            current = 3
        if current == 3:
            try:
                self.connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE mirror_conversation_policies (
                        conversation_id TEXT PRIMARY KEY,
                        privacy_scope TEXT NOT NULL CHECK (
                            privacy_scope IN (
                                'area_shared', 'partnership_restricted', 'owner_private'
                            )
                        ),
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL
                    );
                    CREATE TABLE mirror_conversation_policy_audit (
                        audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        conversation_id TEXT NOT NULL,
                        old_scope TEXT,
                        new_scope TEXT NOT NULL,
                        changed_at INTEGER NOT NULL
                    );
                    CREATE INDEX mirror_conversation_policy_audit_lookup
                    ON mirror_conversation_policy_audit(conversation_id, changed_at, audit_id);
                    """
                )
                self.connection.execute(
                    "INSERT INTO mirror_schema_migrations(version, applied_at) VALUES (?, ?)",
                    (4, _now_seconds()),
                )
                self.connection.execute("COMMIT")
            except BaseException:
                if self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                raise
            current = 4
        if current == 4:
            try:
                self.connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE mirror_conversation_aliases (
                        alias_id TEXT PRIMARY KEY,
                        canonical_id TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL
                    );
                    CREATE INDEX mirror_conversation_aliases_canonical
                    ON mirror_conversation_aliases(canonical_id, alias_id);
                    CREATE TABLE mirror_conversation_alias_audit (
                        audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        alias_id TEXT NOT NULL,
                        old_canonical_id TEXT,
                        new_canonical_id TEXT NOT NULL,
                        changed_at INTEGER NOT NULL
                    );
                    """
                )
                self.connection.execute(
                    "INSERT INTO mirror_schema_migrations(version, applied_at) VALUES (?, ?)",
                    (5, _now_seconds()),
                )
                self.connection.execute("COMMIT")
            except BaseException:
                if self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                raise
            current = 5
        if current == 5:
            try:
                self.connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE mirror_delivery_reconciliation_audit (
                        audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        delivery_id INTEGER NOT NULL REFERENCES mirror_deliveries(delivery_id),
                        old_state TEXT NOT NULL,
                        new_state TEXT NOT NULL,
                        resolution TEXT NOT NULL CHECK (
                            resolution IN (
                                'uncertain_mark_sent',
                                'uncertain_retry',
                                'route_rebind'
                            )
                        ),
                        evidence_hash TEXT NOT NULL,
                        changed_at INTEGER NOT NULL
                    );
                    CREATE INDEX mirror_delivery_reconciliation_lookup
                    ON mirror_delivery_reconciliation_audit(
                        delivery_id, changed_at, audit_id
                    );
                    """
                )
                self.connection.execute(
                    "INSERT INTO mirror_schema_migrations(version, applied_at) VALUES (?, ?)",
                    (6, _now_seconds()),
                )
                self.connection.execute("COMMIT")
            except BaseException:
                if self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                raise
            current = 6
        if current == 6:
            try:
                self.connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    DROP TRIGGER IF EXISTS mirror_events_immutable_update;
                    DROP TRIGGER IF EXISTS mirror_events_immutable_delete;
                    ALTER TABLE mirror_events
                    ADD COLUMN source_profile_id TEXT NOT NULL DEFAULT '';
                    ALTER TABLE mirror_managed_media
                    ADD COLUMN size_bytes INTEGER NOT NULL DEFAULT 0
                    CHECK(size_bytes >= 0);
                    CREATE TABLE mirror_media_purge_audit (
                        audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT NOT NULL REFERENCES mirror_events(event_id),
                        delivery_state TEXT NOT NULL,
                        media_count INTEGER NOT NULL CHECK(media_count > 0),
                        evidence_hash TEXT NOT NULL,
                        changed_at INTEGER NOT NULL
                    );
                    CREATE INDEX mirror_media_purge_audit_lookup
                    ON mirror_media_purge_audit(event_id, changed_at, audit_id);
                    """
                )
                rows = self.connection.execute(
                    "SELECT event_id, payload_json FROM mirror_events"
                ).fetchall()
                for row in rows:
                    event = InboundEvent.from_storage_json(str(row["payload_json"]))
                    self.connection.execute(
                        "UPDATE mirror_events SET source_profile_id = ? WHERE event_id = ?",
                        (event.source_profile_id, str(row["event_id"])),
                    )
                    sizes = {
                        media.path: int(media.size_bytes)
                        for media in event.media
                        if media.managed_temp
                    }
                    for path, size_bytes in sizes.items():
                        self.connection.execute(
                            """UPDATE mirror_managed_media SET size_bytes = ?
                               WHERE event_id = ? AND path = ?""",
                            (max(0, size_bytes), event.event_id, path),
                        )
                self.connection.execute(
                    """CREATE INDEX mirror_events_profile_ready
                       ON mirror_events(source_profile_id, captured_at, event_id)"""
                )
                self.connection.execute(
                    """CREATE TRIGGER mirror_events_immutable_update
                       BEFORE UPDATE ON mirror_events BEGIN
                           SELECT RAISE(ABORT, 'immutable_event');
                       END"""
                )
                self.connection.execute(
                    """CREATE TRIGGER mirror_events_immutable_delete
                       BEFORE DELETE ON mirror_events BEGIN
                           SELECT RAISE(ABORT, 'immutable_event');
                       END"""
                )
                if self.connection.execute(
                    "SELECT 1 FROM mirror_events WHERE source_profile_id = '' LIMIT 1"
                ).fetchone():
                    raise LedgerError("source_profile_backfill_failed")
                self.connection.execute(
                    "INSERT INTO mirror_schema_migrations(version, applied_at) VALUES (?, ?)",
                    (7, _now_seconds()),
                )
                self.connection.execute("COMMIT")
            except BaseException:
                if self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                raise
            current = 7
        if current == 7:
            try:
                self.connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE mirror_source_quarantine (
                        adapter_id TEXT NOT NULL,
                        source_item_ref TEXT NOT NULL,
                        payload_hash TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        error_code TEXT NOT NULL,
                        privacy_scope TEXT NOT NULL CHECK (
                            privacy_scope IN (
                                'area_shared', 'partnership_restricted', 'owner_private'
                            )
                        ),
                        quarantined_at INTEGER NOT NULL,
                        PRIMARY KEY(adapter_id, source_item_ref)
                    );
                    CREATE INDEX mirror_source_quarantine_time
                    ON mirror_source_quarantine(quarantined_at, adapter_id, source_item_ref);
                    CREATE TRIGGER mirror_source_quarantine_immutable_update
                    BEFORE UPDATE ON mirror_source_quarantine BEGIN
                        SELECT RAISE(ABORT, 'immutable_source_quarantine');
                    END;
                    CREATE TRIGGER mirror_source_quarantine_immutable_delete
                    BEFORE DELETE ON mirror_source_quarantine BEGIN
                        SELECT RAISE(ABORT, 'immutable_source_quarantine');
                    END;
                    """
                )
                self.connection.execute(
                    "INSERT INTO mirror_schema_migrations(version, applied_at) VALUES (?, ?)",
                    (8, _now_seconds()),
                )
                self.connection.execute("COMMIT")
            except BaseException:
                if self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                raise
            current = 8
        if current == 8:
            try:
                self.connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE mirror_conversation_admission (
                        conversation_id TEXT PRIMARY KEY,
                        source_profile_id TEXT NOT NULL,
                        conversation_kind TEXT NOT NULL CHECK (
                            conversation_kind IN ('direct', 'group')
                        ),
                        approval_state TEXT NOT NULL CHECK (
                            approval_state IN ('direct_auto', 'group_pending', 'group_approved')
                        ),
                        agent_mode TEXT NOT NULL DEFAULT 'none' CHECK (
                            agent_mode IN ('none', 'mention_only')
                        ),
                        grill_json TEXT,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL
                    );
                    CREATE TABLE mirror_participant_identity (
                        source_profile_id TEXT NOT NULL,
                        conversation_id TEXT NOT NULL,
                        actor_ref TEXT NOT NULL,
                        display_label TEXT NOT NULL,
                        label_source TEXT NOT NULL CHECK (
                            label_source IN ('manual', 'session_contact', 'event', 'whatsapp_public')
                        ),
                        source_priority INTEGER NOT NULL,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL,
                        PRIMARY KEY(source_profile_id, conversation_id, actor_ref)
                    );
                    CREATE TABLE mirror_admission_blocks (
                        source_profile_id TEXT NOT NULL,
                        conversation_id TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        blocked_count INTEGER NOT NULL,
                        first_blocked_at INTEGER NOT NULL,
                        last_blocked_at INTEGER NOT NULL,
                        PRIMARY KEY(source_profile_id, conversation_id, reason)
                    );
                    CREATE TABLE mirror_outbound_receipts (
                        outbound_ref TEXT PRIMARY KEY,
                        state INTEGER NOT NULL CHECK (state BETWEEN 2 AND 5),
                        provider_event TEXT NOT NULL,
                        updated_at INTEGER NOT NULL
                    );
                    CREATE TABLE mirror_acceptance_canaries (
                        canary_ref TEXT PRIMARY KEY,
                        direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
                        media_kind TEXT NOT NULL CHECK (media_kind IN ('text', 'image', 'audio', 'voice')),
                        passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
                        evidence_ref TEXT NOT NULL,
                        recorded_at INTEGER NOT NULL
                    );
                    """
                )
                self.connection.execute(
                    "INSERT INTO mirror_schema_migrations(version, applied_at) VALUES (?, ?)",
                    (9, _now_seconds()),
                )
                self.connection.execute("COMMIT")
            except BaseException:
                if self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                raise
            current = 9
        if current == 9:
            try:
                self.connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE mirror_outbound_receipt_reactions (
                        outbound_request_id TEXT PRIMARY KEY,
                        telegram_message_id TEXT NOT NULL,
                        forum_chat_id TEXT NOT NULL,
                        telegram_thread_id TEXT NOT NULL,
                        state INTEGER NOT NULL CHECK (state BETWEEN 3 AND 5),
                        emoji TEXT NOT NULL,
                        applied_at INTEGER NOT NULL
                    );
                    """
                )
                self.connection.execute(
                    "INSERT INTO mirror_schema_migrations(version, applied_at) VALUES (?, ?)",
                    (10, _now_seconds()),
                )
                self.connection.execute("COMMIT")
            except BaseException:
                if self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                raise
        self._harden_files()

    def approve_group(
        self,
        conversation_id: str,
        source_profile_id: str,
        *,
        agent_mode: str = "none",
        grill_json: str | None = None,
        now: float | int | None = None,
    ) -> None:
        from .policy import GroupGrill, GovernanceError

        if agent_mode not in {"none", "mention_only"}:
            raise LedgerError("group_agent_mode_invalid")
        if agent_mode == "mention_only":
            if not grill_json:
                raise LedgerError("group_grill_required_for_agent")
            try:
                value = json.loads(grill_json)
                if not isinstance(value, dict):
                    raise GovernanceError("group_grill_invalid")
                GroupGrill.from_mapping(value)
            except (json.JSONDecodeError, GovernanceError):
                raise LedgerError("group_grill_invalid") from None
        route = self.get_route(conversation_id)
        if route is None or not route.enabled:
            raise LedgerError("group_route_required")
        timestamp = _now_seconds(now)
        self.connection.execute(
            """INSERT INTO mirror_conversation_admission(
                   conversation_id, source_profile_id, conversation_kind,
                   approval_state, agent_mode, grill_json, created_at, updated_at
               ) VALUES (?, ?, 'group', 'group_approved', ?, ?, ?, ?)
               ON CONFLICT(conversation_id) DO UPDATE SET
                   source_profile_id=excluded.source_profile_id,
                   conversation_kind='group', approval_state='group_approved',
                   agent_mode=excluded.agent_mode, grill_json=excluded.grill_json,
                   updated_at=excluded.updated_at""",
            (conversation_id, source_profile_id, agent_mode, grill_json, timestamp, timestamp),
        )
        self._harden_files()

    def authorize_event(self, event: InboundEvent, *, now: float | int | None = None) -> None:
        """Reject unapproved groups before content or media enters the ledger."""

        from .policy import GovernanceError, validate_participant_label

        timestamp = _now_seconds(now)
        if event.conversation_kind == "direct":
            self.connection.execute(
                """INSERT OR IGNORE INTO mirror_conversation_admission(
                       conversation_id, source_profile_id, conversation_kind,
                       approval_state, agent_mode, created_at, updated_at
                   ) VALUES (?, ?, 'direct', 'direct_auto', 'none', ?, ?)""",
                (event.conversation_id, event.source_profile_id, timestamp, timestamp),
            )
        else:
            row = self.connection.execute(
                """SELECT source_profile_id, approval_state
                   FROM mirror_conversation_admission WHERE conversation_id=?""",
                (event.conversation_id,),
            ).fetchone()
            allowed = bool(
                row
                and str(row["source_profile_id"]) == event.source_profile_id
                and str(row["approval_state"]) == "group_approved"
                and self.get_route(event.conversation_id) is not None
            )
            if not allowed:
                self.connection.execute(
                    """INSERT INTO mirror_admission_blocks(
                           source_profile_id, conversation_id, reason, blocked_count,
                           first_blocked_at, last_blocked_at
                       ) VALUES (?, ?, 'whatsapp_group_not_approved', 1, ?, ?)
                       ON CONFLICT(source_profile_id, conversation_id, reason)
                       DO UPDATE SET blocked_count=blocked_count+1,
                                     last_blocked_at=excluded.last_blocked_at""",
                    (event.source_profile_id, event.conversation_id, timestamp, timestamp),
                )
                self._harden_files()
                raise LedgerError("whatsapp_group_not_approved")
        existing_label = self.participant_label(event)
        if event.actor_display_label:
            try:
                label = validate_participant_label(event.actor_display_label)
            except GovernanceError:
                if event.conversation_kind == "group" and not existing_label:
                    raise LedgerError("participant_identity_unresolved") from None
            else:
                self.set_participant_identity(
                    event.source_profile_id,
                    event.conversation_id,
                    event.actor_ref,
                    label,
                    label_source="event",
                    now=timestamp,
                )
        elif event.conversation_kind == "group" and not existing_label:
            raise LedgerError("participant_identity_unresolved")

    def set_participant_identity(
        self,
        source_profile_id: str,
        conversation_id: str,
        actor_ref: str,
        display_label: str,
        *,
        label_source: str = "manual",
        now: float | int | None = None,
    ) -> bool:
        from .policy import IDENTITY_SOURCES, validate_participant_label

        if label_source not in IDENTITY_SOURCES:
            raise LedgerError("identity_source_invalid")
        label = validate_participant_label(display_label)
        priority = IDENTITY_SOURCES[label_source]
        timestamp = _now_seconds(now)
        row = self.connection.execute(
            """SELECT display_label, source_priority FROM mirror_participant_identity
               WHERE source_profile_id=? AND conversation_id=? AND actor_ref=?""",
            (source_profile_id, conversation_id, actor_ref),
        ).fetchone()
        if row and int(row["source_priority"]) > priority:
            return False
        if row and str(row["display_label"]) == label and int(row["source_priority"]) == priority:
            return False
        self.connection.execute(
            """INSERT INTO mirror_participant_identity(
                   source_profile_id, conversation_id, actor_ref, display_label,
                   label_source, source_priority, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_profile_id, conversation_id, actor_ref) DO UPDATE SET
                   display_label=excluded.display_label,
                   label_source=excluded.label_source,
                   source_priority=excluded.source_priority,
                   updated_at=excluded.updated_at""",
            (source_profile_id, conversation_id, actor_ref, label, label_source, priority, timestamp, timestamp),
        )
        self._harden_files()
        return True

    def participant_label(self, event: InboundEvent) -> str:
        row = self.connection.execute(
            """SELECT display_label FROM mirror_participant_identity
               WHERE source_profile_id=? AND conversation_id=? AND actor_ref=?""",
            (event.source_profile_id, event.conversation_id, event.actor_ref),
        ).fetchone()
        return str(row["display_label"]) if row else event.actor_display_label

    def record_outbound_receipt(
        self,
        outbound_ref: str,
        state: object,
        *,
        provider_event: str,
        now: float | int | None = None,
    ) -> int:
        """Persist one provider-backed receipt without downgrade or inference."""

        from .policy import advance_receipt

        if not isinstance(outbound_ref, str) or not outbound_ref.strip():
            raise LedgerError("outbound_receipt_ref_required")
        if provider_event not in {"messages.update", "message-receipt.update"}:
            raise LedgerError("outbound_receipt_provider_event_invalid")
        row = self.connection.execute(
            "SELECT state FROM mirror_outbound_receipts WHERE outbound_ref=?",
            (outbound_ref,),
        ).fetchone()
        resolved = advance_receipt(int(row["state"]) if row else None, state)
        self.connection.execute(
            """INSERT INTO mirror_outbound_receipts(
                   outbound_ref, state, provider_event, updated_at
               ) VALUES (?, ?, ?, ?)
               ON CONFLICT(outbound_ref) DO UPDATE SET
                   state=excluded.state, provider_event=excluded.provider_event,
                   updated_at=excluded.updated_at""",
            (outbound_ref, int(resolved), provider_event, _now_seconds(now)),
        )
        self._harden_files()
        return int(resolved)

    def receipt_reaction_state(self, outbound_request_id: str) -> int | None:
        """Return the highest reaction state already applied for one request."""

        row = self.connection.execute(
            """SELECT state FROM mirror_outbound_receipt_reactions
               WHERE outbound_request_id=?""",
            (outbound_request_id,),
        ).fetchone()
        return int(row["state"]) if row is not None else None

    def record_receipt_reaction(
        self,
        outbound_request_id: str,
        *,
        telegram_message_id: str,
        forum_chat_id: str,
        telegram_thread_id: str,
        state: int,
        emoji: str,
        now: float | int | None = None,
    ) -> bool:
        """Commit one successfully applied reaction monotonically."""

        if not outbound_request_id.strip():
            raise LedgerError("receipt_reaction_request_required")
        if not telegram_message_id.isdigit() or int(telegram_message_id) <= 0:
            raise LedgerError("receipt_reaction_message_invalid")
        if not forum_chat_id.startswith("-") or not telegram_thread_id.isdigit():
            raise LedgerError("receipt_reaction_route_invalid")
        if int(state) not in {3, 4, 5} or not emoji.strip():
            raise LedgerError("receipt_reaction_state_invalid")
        existing = self.receipt_reaction_state(outbound_request_id)
        if existing is not None and int(state) <= existing:
            return False
        self.connection.execute(
            """INSERT INTO mirror_outbound_receipt_reactions(
                   outbound_request_id, telegram_message_id, forum_chat_id,
                   telegram_thread_id, state, emoji, applied_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(outbound_request_id) DO UPDATE SET
                   telegram_message_id=excluded.telegram_message_id,
                   forum_chat_id=excluded.forum_chat_id,
                   telegram_thread_id=excluded.telegram_thread_id,
                   state=excluded.state, emoji=excluded.emoji,
                   applied_at=excluded.applied_at""",
            (
                outbound_request_id,
                telegram_message_id,
                forum_chat_id,
                telegram_thread_id,
                int(state),
                emoji,
                _now_seconds(now),
            ),
        )
        self._harden_files()
        return True

    def quarantine_source_item(
        self,
        *,
        adapter_id: str,
        source_item_id: str,
        payload: object,
        error_code: str,
        privacy_scope: str,
        now: float | int | None = None,
    ) -> bool:
        """Durably retain one permanently invalid source record before ACK."""

        if not adapter_id.strip() or not source_item_id.strip():
            raise LedgerError("source_quarantine_identity_required")
        if privacy_scope not in PRIVACY_SCOPES:
            raise LedgerError("source_quarantine_scope_invalid")
        if not error_code or not error_code.replace("_", "").isalnum():
            raise LedgerError("source_quarantine_error_invalid")
        try:
            payload_json = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            raise LedgerError("source_quarantine_payload_invalid") from None
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        source_item_ref = opaque_ref(
            "source-item", f"{adapter_id}\x1f{source_item_id}"
        )
        existing = self.connection.execute(
            """SELECT payload_hash, error_code, privacy_scope
               FROM mirror_source_quarantine
               WHERE adapter_id = ? AND source_item_ref = ?""",
            (adapter_id, source_item_ref),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["payload_hash"]) != payload_hash
                or str(existing["error_code"]) != error_code
                or str(existing["privacy_scope"]) != privacy_scope
            ):
                raise EventConflictError()
            return False
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                """INSERT INTO mirror_source_quarantine(
                       adapter_id, source_item_ref, payload_hash, payload_json,
                       error_code, privacy_scope, quarantined_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    adapter_id,
                    source_item_ref,
                    payload_hash,
                    payload_json,
                    error_code,
                    privacy_scope,
                    _now_seconds(now),
                ),
            )
            self.connection.execute("COMMIT")
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
        self._harden_files()
        return True

    def record_event(self, event: InboundEvent, *, now: float | int | None = None) -> bool:
        payload_hash = event.payload_hash()
        existing = self.connection.execute(
            "SELECT payload_hash FROM mirror_events WHERE event_id = ?", (event.event_id,)
        ).fetchone()
        if existing:
            if str(existing["payload_hash"]) != payload_hash:
                raise EventConflictError()
            return False
        try:
            self.connection.execute(
                """
                INSERT INTO mirror_events(
                    event_id, payload_hash, source, conversation_id,
                    occurred_at, privacy_scope, payload_json, captured_at,
                    source_profile_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    payload_hash,
                    event.source,
                    event.conversation_id,
                    event.occurred_at,
                    event.privacy_scope,
                    event.storage_json(),
                    _now_seconds(now),
                    event.source_profile_id,
                ),
            )
        except sqlite3.IntegrityError:
            existing = self.connection.execute(
                "SELECT payload_hash FROM mirror_events WHERE event_id = ?", (event.event_id,)
            ).fetchone()
            if existing and str(existing["payload_hash"]) == payload_hash:
                return False
            raise EventConflictError() from None
        self._harden_files()
        return True

    def load_event(self, event_id: str) -> InboundEvent:
        row = self.connection.execute(
            "SELECT payload_json FROM mirror_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if not row:
            raise LedgerError("event_missing")
        return InboundEvent.from_storage_json(str(row["payload_json"]))

    def capture_event(
        self, event: InboundEvent, *, now: float | int | None = None
    ) -> tuple[bool, int | None, str | None]:
        """Atomically append, inventory media, and enqueue one inbound event.

        A missing route is committed as a visible block rather than rolling
        back the captured event.  This producer path deliberately does not
        acquire the delivery worker's runtime lease.
        """

        timestamp = _now_seconds(now)
        owns_transaction = not self.connection.in_transaction
        if owns_transaction:
            self.connection.execute("BEGIN IMMEDIATE")
        try:
            inserted = self.record_event(event, now=timestamp)
            for media in event.media:
                if not media.managed_temp:
                    continue
                existing = self.connection.execute(
                    "SELECT event_id FROM mirror_managed_media WHERE path = ?",
                    (media.path,),
                ).fetchone()
                if existing and str(existing["event_id"]) != event.event_id:
                    raise LedgerError("managed_media_path_conflict")
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO mirror_managed_media(
                        path, event_id, state, cleanup_attempts,
                        next_attempt_at, created_at, updated_at, size_bytes
                    ) VALUES (?, ?, 'active', 0, 0, ?, ?, ?)
                    """,
                    (
                        media.path,
                        event.event_id,
                        timestamp,
                        timestamp,
                        max(0, int(media.size_bytes)),
                    ),
                )
            delivery_id: int | None = None
            blocked_reason: str | None = None
            try:
                delivery_id = self.enqueue(event.event_id, now=timestamp)
            except RouteMissingError as exc:
                blocked_reason = exc.code
            if owns_transaction:
                self.connection.execute("COMMIT")
        except BaseException:
            if owns_transaction and self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
        self._harden_files()
        return inserted, delivery_id, blocked_reason

    def set_route(
        self,
        route: Route,
        *,
        watermark_event_id: str | None = None,
        allow_update: bool = False,
        now: float | int | None = None,
    ) -> bool:
        timestamp = _now_seconds(now)
        watermark_hash = _opaque_hash(watermark_event_id) if watermark_event_id else None
        existing = self.connection.execute(
            "SELECT * FROM mirror_routes WHERE conversation_id = ?", (route.conversation_id,)
        ).fetchone()
        if not existing:
            self.connection.execute(
                """
                INSERT INTO mirror_routes(
                    conversation_id, chat_id, thread_id, enabled,
                    watermark_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    route.conversation_id,
                    route.chat_id,
                    route.thread_id,
                    int(route.enabled),
                    watermark_hash,
                    timestamp,
                    timestamp,
                ),
            )
            self._harden_files()
            return True
        same_destination = (
            str(existing["chat_id"]) == route.chat_id
            and str(existing["thread_id"]) == route.thread_id
            and bool(existing["enabled"]) == route.enabled
        )
        if not same_destination and not allow_update:
            raise RouteConflictError()
        next_watermark = watermark_hash or existing["watermark_hash"]
        if same_destination and next_watermark == existing["watermark_hash"]:
            return False
        self.connection.execute(
            """
            UPDATE mirror_routes
            SET chat_id = ?, thread_id = ?, enabled = ?, watermark_hash = ?, updated_at = ?
            WHERE conversation_id = ?
            """,
            (
                route.chat_id,
                route.thread_id,
                int(route.enabled),
                next_watermark,
                timestamp,
                route.conversation_id,
            ),
        )
        self._harden_files()
        return True

    def get_route(self, conversation_id: str) -> Route | None:
        row = self.connection.execute(
            "SELECT conversation_id, chat_id, thread_id, enabled FROM mirror_routes WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if not row:
            return None
        return Route(
            conversation_id=str(row["conversation_id"]),
            chat_id=str(row["chat_id"]),
            thread_id=str(row["thread_id"]),
            enabled=bool(row["enabled"]),
        )

    def get_conversation_scope(self, conversation_id: str) -> str:
        row = self.connection.execute(
            "SELECT privacy_scope FROM mirror_conversation_policies WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        return str(row["privacy_scope"]) if row else "owner_private"

    def resolve_conversation_alias(self, conversation_id: str) -> str:
        row = self.connection.execute(
            "SELECT canonical_id FROM mirror_conversation_aliases WHERE alias_id = ?",
            (conversation_id,),
        ).fetchone()
        return str(row["canonical_id"]) if row else conversation_id

    def set_conversation_alias(
        self,
        alias_id: str,
        canonical_id: str,
        *,
        allow_update: bool = False,
        now: float | int | None = None,
    ) -> bool:
        for name, value in (("alias_id", alias_id), ("canonical_id", canonical_id)):
            if (
                not isinstance(value, str)
                or not value.startswith("conversation:")
                or len(value) != len("conversation:") + 64
                or any(char not in "0123456789abcdef" for char in value.split(":", 1)[1])
            ):
                raise ValueError(f"invalid_{name}")
        timestamp = _now_seconds(now)
        row = self.connection.execute(
            "SELECT canonical_id FROM mirror_conversation_aliases WHERE alias_id = ?",
            (alias_id,),
        ).fetchone()
        old = str(row["canonical_id"]) if row else None
        if old == canonical_id:
            return False
        if old is not None and not allow_update:
            raise RouteConflictError("conversation_alias_conflict")
        owns_transaction = not self.connection.in_transaction
        if owns_transaction:
            self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                """
                INSERT INTO mirror_conversation_aliases(
                    alias_id, canonical_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(alias_id) DO UPDATE SET
                    canonical_id = excluded.canonical_id,
                    updated_at = excluded.updated_at
                """,
                (alias_id, canonical_id, timestamp, timestamp),
            )
            self.connection.execute(
                """INSERT INTO mirror_conversation_alias_audit(
                       alias_id, old_canonical_id, new_canonical_id, changed_at
                   ) VALUES (?, ?, ?, ?)""",
                (alias_id, old, canonical_id, timestamp),
            )
            if owns_transaction:
                self.connection.execute("COMMIT")
        except BaseException:
            if owns_transaction and self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
        self._harden_files()
        return True

    def list_conversation_aliases(self) -> list[tuple[str, str]]:
        return [
            (str(row["alias_id"]), str(row["canonical_id"]))
            for row in self.connection.execute(
                """SELECT alias_id, canonical_id
                   FROM mirror_conversation_aliases ORDER BY alias_id"""
            )
        ]

    def set_conversation_scope(
        self,
        conversation_id: str,
        privacy_scope: str,
        *,
        now: float | int | None = None,
    ) -> bool:
        if privacy_scope not in {
            "area_shared",
            "partnership_restricted",
            "owner_private",
        }:
            raise ValueError("invalid_privacy_scope")
        # Route construction is the canonical opaque-id validator and cannot
        # be used here without a destination; keep the same strict shape.
        if (
            not isinstance(conversation_id, str)
            or not conversation_id.startswith("conversation:")
            or len(conversation_id) != len("conversation:") + 64
            or any(
                char not in "0123456789abcdef"
                for char in conversation_id.split(":", 1)[1]
            )
        ):
            raise ValueError("invalid_conversation_id")
        timestamp = _now_seconds(now)
        row = self.connection.execute(
            "SELECT privacy_scope FROM mirror_conversation_policies WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        old_scope = str(row["privacy_scope"]) if row else None
        if old_scope == privacy_scope:
            return False
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                """
                INSERT INTO mirror_conversation_policies(
                    conversation_id, privacy_scope, created_at, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    privacy_scope = excluded.privacy_scope,
                    updated_at = excluded.updated_at
                """,
                (conversation_id, privacy_scope, timestamp, timestamp),
            )
            self.connection.execute(
                """
                INSERT INTO mirror_conversation_policy_audit(
                    conversation_id, old_scope, new_scope, changed_at
                ) VALUES (?, ?, ?, ?)
                """,
                (conversation_id, old_scope, privacy_scope, timestamp),
            )
            self.connection.execute("COMMIT")
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
        self._harden_files()
        return True

    def list_conversation_scopes(self) -> list[tuple[str, str]]:
        return [
            (str(row["conversation_id"]), str(row["privacy_scope"]))
            for row in self.connection.execute(
                """SELECT conversation_id, privacy_scope
                   FROM mirror_conversation_policies ORDER BY conversation_id"""
            )
        ]

    def list_routes(self, *, enabled_only: bool = False) -> list[Route]:
        where = " WHERE enabled = 1" if enabled_only else ""
        rows = self.connection.execute(
            "SELECT conversation_id, chat_id, thread_id, enabled FROM mirror_routes"
            + where
            + " ORDER BY conversation_id"
        ).fetchall()
        return [
            Route(
                conversation_id=str(row["conversation_id"]),
                chat_id=str(row["chat_id"]),
                thread_id=str(row["thread_id"]),
                enabled=bool(row["enabled"]),
            )
            for row in rows
        ]

    def acquire_runtime_lock(
        self,
        profile_id: str,
        owner_id: str,
        *,
        now: float | int | None = None,
        lease_seconds: int = 120,
    ) -> bool:
        if not isinstance(profile_id, str) or not profile_id.strip():
            raise ValueError("invalid_profile_id")
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("invalid_owner_id")
        if int(lease_seconds) <= 0:
            raise ValueError("invalid_runtime_lease")
        timestamp = _now_seconds(now)
        expires_at = timestamp + int(lease_seconds)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                "DELETE FROM mirror_runtime_locks WHERE expires_at <= ?", (timestamp,)
            )
            row = self.connection.execute(
                "SELECT owner_id FROM mirror_runtime_locks WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
            if row and str(row["owner_id"]) != owner_id:
                self.connection.execute("COMMIT")
                self._harden_files()
                return False
            if row:
                self.connection.execute(
                    """
                    UPDATE mirror_runtime_locks SET expires_at = ?
                    WHERE profile_id = ? AND owner_id = ?
                    """,
                    (expires_at, profile_id, owner_id),
                )
            else:
                self.connection.execute(
                    """
                    INSERT INTO mirror_runtime_locks(
                        profile_id, owner_id, acquired_at, expires_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (profile_id, owner_id, timestamp, expires_at),
                )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        self._harden_files()
        return True

    def renew_runtime_lock(
        self,
        profile_id: str,
        owner_id: str,
        *,
        now: float | int | None = None,
        lease_seconds: int = 120,
    ) -> bool:
        if int(lease_seconds) <= 0:
            raise ValueError("invalid_runtime_lease")
        timestamp = _now_seconds(now)
        result = self.connection.execute(
            """
            UPDATE mirror_runtime_locks SET expires_at = ?
            WHERE profile_id = ? AND owner_id = ? AND expires_at >= ?
            """,
            (timestamp + int(lease_seconds), profile_id, owner_id, timestamp),
        )
        self._harden_files()
        return result.rowcount == 1

    def renew_delivery_lease(
        self,
        delivery_id: int,
        worker_id: str,
        *,
        now: float | int | None = None,
        lease_seconds: int = 60,
    ) -> bool:
        """Extend one still-owned, unexpired delivery lease."""

        if isinstance(delivery_id, bool) or not isinstance(delivery_id, int) or delivery_id <= 0:
            raise ValueError("invalid_delivery_id")
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("invalid_worker_id")
        if int(lease_seconds) <= 0:
            raise ValueError("invalid_lease")
        timestamp = _now_seconds(now)
        result = self.connection.execute(
            """
            UPDATE mirror_leases SET expires_at = ?
            WHERE delivery_id = ? AND worker_id = ? AND expires_at >= ?
            """,
            (
                timestamp + int(lease_seconds),
                delivery_id,
                worker_id,
                timestamp,
            ),
        )
        self._harden_files()
        return result.rowcount == 1

    def release_runtime_lock(self, profile_id: str, owner_id: str) -> bool:
        result = self.connection.execute(
            "DELETE FROM mirror_runtime_locks WHERE profile_id = ? AND owner_id = ?",
            (profile_id, owner_id),
        )
        self._harden_files()
        return result.rowcount == 1

    def add_legacy_delivered_ids(
        self,
        conversation_id: str,
        event_ids: Iterable[str],
        *,
        now: float | int | None = None,
    ) -> int:
        timestamp = _now_seconds(now)
        rows = []
        for item in event_ids:
            value = str(item).strip()
            if value:
                rows.append((conversation_id, _opaque_hash(value), timestamp))
        rows = list(dict.fromkeys(rows))
        before = self.connection.total_changes
        self.connection.executemany(
            "INSERT OR IGNORE INTO mirror_legacy_delivered(conversation_id, event_key_hash, imported_at) VALUES (?, ?, ?)",
            rows,
        )
        self._harden_files()
        return self.connection.total_changes - before

    def is_legacy_delivered(self, conversation_id: str, event_id: str) -> bool:
        return bool(
            self.connection.execute(
                """SELECT 1 FROM mirror_legacy_delivered
                   WHERE conversation_id = ? AND event_key_hash = ?""",
                (conversation_id, _opaque_hash(event_id)),
            ).fetchone()
        )

    def record_legacy_import(
        self, source_hash: str, routes_seen: int, recent_ids_seen: int, *, now: float | int | None = None
    ) -> bool:
        result = self.connection.execute(
            """
            INSERT OR IGNORE INTO mirror_legacy_imports(
                source_hash, routes_seen, recent_ids_seen, imported_at
            ) VALUES (?, ?, ?, ?)
            """,
            (source_hash, routes_seen, recent_ids_seen, _now_seconds(now)),
        )
        self._harden_files()
        return result.rowcount == 1

    def enqueue(self, event_id: str, *, now: float | int | None = None) -> int | None:
        event_row = self.connection.execute(
            "SELECT conversation_id FROM mirror_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if not event_row:
            raise LedgerError("event_missing")
        conversation_id = str(event_row["conversation_id"])
        if self.is_legacy_delivered(conversation_id, event_id):
            return None
        route = self.get_route(conversation_id)
        if route is None or not route.enabled:
            timestamp = _now_seconds(now)
            reason = "route_missing" if route is None else "route_disabled"
            self.connection.execute(
                """
                INSERT INTO mirror_route_blocks(
                    event_id, conversation_id, state, reason, blocked_at, updated_at
                ) VALUES (?, ?, 'blocked_no_route', ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    state = 'blocked_no_route', reason = excluded.reason,
                    updated_at = excluded.updated_at, requeued_at = NULL
                """,
                (event_id, conversation_id, reason, timestamp, timestamp),
            )
            self._harden_files()
            raise RouteMissingError(reason)
        blocked = self.connection.execute(
            """SELECT 1 FROM mirror_route_blocks
               WHERE event_id = ? AND state = 'blocked_no_route'""",
            (event_id,),
        ).fetchone()
        if blocked:
            raise RouteMissingError("route_blocked_requires_reconcile")
        timestamp = _now_seconds(now)
        self._insert_delivery(event_id, route, timestamp)
        row = self.connection.execute(
            "SELECT delivery_id FROM mirror_deliveries WHERE event_id = ?", (event_id,)
        ).fetchone()
        self._harden_files()
        return int(row["delivery_id"])

    def _insert_delivery(self, event_id: str, route: Route, timestamp: int) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO mirror_deliveries(
                event_id, target_chat_id, target_thread_id, state,
                attempts, next_attempt_at, created_at, updated_at
            ) VALUES (?, ?, ?, 'pending', 0, ?, ?, ?)
            """,
            (event_id, route.chat_id, route.thread_id, timestamp, timestamp, timestamp),
        )

    def reconcile_route_blocks(
        self,
        conversation_id: str,
        *,
        limit: int = 500,
        now: float | int | None = None,
    ) -> int:
        """Explicitly requeue held events after an operator provisions a route.

        Creating or enabling a route never causes delivery by itself.  This
        separate operation makes the release auditable and prevents an old
        backlog from being dispatched because of a configuration edit.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("invalid_reconcile_limit")
        route = self.get_route(conversation_id)
        if route is None or not route.enabled:
            raise RouteMissingError("route_missing")
        timestamp = _now_seconds(now)
        count = 0
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            rows = self.connection.execute(
                """
                SELECT event_id FROM mirror_route_blocks
                WHERE conversation_id = ? AND state = 'blocked_no_route'
                ORDER BY blocked_at, event_id LIMIT ?
                """,
                (conversation_id, limit),
            ).fetchall()
            for row in rows:
                event_id = str(row["event_id"])
                if not self.is_legacy_delivered(conversation_id, event_id):
                    self._insert_delivery(event_id, route, timestamp)
                    count += 1
                self.connection.execute(
                    """
                    UPDATE mirror_route_blocks
                    SET state = 'requeued', updated_at = ?, requeued_at = ?
                    WHERE event_id = ? AND state = 'blocked_no_route'
                    """,
                    (timestamp, timestamp, event_id),
                )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        self._harden_files()
        return count

    def list_route_blocks(
        self,
        *,
        state: str = "blocked_no_route",
        limit: int = 500,
    ) -> list[RouteBlock]:
        if state not in {"blocked_no_route", "requeued"}:
            raise ValueError("invalid_route_block_state")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("invalid_route_block_limit")
        rows = self.connection.execute(
            """
            SELECT event_id, conversation_id, state, reason,
                   blocked_at, updated_at, requeued_at
            FROM mirror_route_blocks
            WHERE state = ?
            ORDER BY blocked_at, event_id LIMIT ?
            """,
            (state, limit),
        ).fetchall()
        return [
            RouteBlock(
                event_ref=opaque_ref("event", str(row["event_id"])),
                conversation_id=str(row["conversation_id"]),
                state=str(row["state"]),
                reason=str(row["reason"]),
                blocked_at=int(row["blocked_at"]),
                updated_at=int(row["updated_at"]),
                requeued_at=(
                    int(row["requeued_at"]) if row["requeued_at"] is not None else None
                ),
            )
            for row in rows
        ]

    def get_source_cursor(self, adapter_id: str, source_ref: str) -> tuple[str, int] | None:
        row = self.connection.execute(
            """SELECT generation, byte_offset FROM mirror_source_cursors
               WHERE adapter_id = ? AND source_ref = ?""",
            (adapter_id, source_ref),
        ).fetchone()
        if not row:
            return None
        return str(row["generation"]), int(row["byte_offset"])

    def set_source_cursor(
        self,
        adapter_id: str,
        source_ref: str,
        generation: str,
        byte_offset: int,
        *,
        now: float | int | None = None,
    ) -> None:
        for name, value in (
            ("adapter_id", adapter_id),
            ("source_ref", source_ref),
            ("generation", generation),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"invalid_{name}")
        if isinstance(byte_offset, bool) or not isinstance(byte_offset, int) or byte_offset < 0:
            raise ValueError("invalid_byte_offset")
        existing = self.get_source_cursor(adapter_id, source_ref)
        if existing and existing[0] == generation and byte_offset < existing[1]:
            raise LedgerError("cursor_regression")
        self.connection.execute(
            """
            INSERT INTO mirror_source_cursors(
                adapter_id, source_ref, generation, byte_offset, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(adapter_id, source_ref) DO UPDATE SET
                generation = excluded.generation,
                byte_offset = excluded.byte_offset,
                updated_at = excluded.updated_at
            """,
            (adapter_id, source_ref, generation, byte_offset, _now_seconds(now)),
        )
        self._harden_files()

    def _expire_leases(self, now: int) -> None:
        expired = self.connection.execute(
            "SELECT delivery_id FROM mirror_leases WHERE expires_at <= ?", (now,)
        ).fetchall()
        for row in expired:
            delivery_id = int(row["delivery_id"])
            self.connection.execute(
                """
                UPDATE mirror_delivery_attempts
                SET finished_at = ?, outcome = 'uncertain', error_code = 'lease_expired_uncertain'
                WHERE delivery_id = ? AND outcome = 'started'
                """,
                (now, delivery_id),
            )
            self.connection.execute(
                """
                UPDATE mirror_deliveries SET state = 'uncertain', next_attempt_at = MIN(next_attempt_at, ?),
                    last_error_code = 'lease_expired_uncertain', updated_at = ?
                WHERE delivery_id = ? AND state = 'inflight'
                """,
                (now, now, delivery_id),
            )
        self.connection.execute("DELETE FROM mirror_leases WHERE expires_at <= ?", (now,))

    def claim_next(
        self,
        worker_id: str,
        *,
        source_profile_id: str = DEFAULT_SOURCE_PROFILE_ID,
        now: float | int | None = None,
        lease_seconds: int = 60,
    ) -> DeliveryClaim | None:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("invalid_worker_id")
        if not isinstance(source_profile_id, str) or not source_profile_id.strip():
            raise ValueError("invalid_source_profile_id")
        if int(lease_seconds) <= 0:
            raise ValueError("invalid_lease")
        timestamp = _now_seconds(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self._expire_leases(timestamp)
            # WIP=1 is global for this ledger, not merely per process.
            if self.connection.execute(
                "SELECT 1 FROM mirror_leases WHERE expires_at > ? LIMIT 1", (timestamp,)
            ).fetchone():
                self.connection.execute("COMMIT")
                return None
            self.connection.execute(
                """
                UPDATE mirror_deliveries AS d
                SET state = 'blocked', last_error_code = 'route_changed', updated_at = ?
                WHERE state IN ('pending', 'retry')
                  AND NOT EXISTS (
                    SELECT 1 FROM mirror_events e
                    JOIN mirror_routes r ON r.conversation_id = e.conversation_id
                    WHERE e.event_id = d.event_id AND r.enabled = 1
                      AND r.chat_id = d.target_chat_id
                      AND r.thread_id = d.target_thread_id
                  )
                """,
                (timestamp,),
            )
            row = self.connection.execute(
                """
                SELECT d.delivery_id, d.event_id, d.target_chat_id,
                       d.target_thread_id, d.attempts
                FROM mirror_deliveries d
                JOIN mirror_events e ON e.event_id = d.event_id
                WHERE d.state IN ('pending', 'retry')
                  AND d.next_attempt_at <= ?
                  AND e.source_profile_id = ?
                ORDER BY next_attempt_at, delivery_id
                LIMIT 1
                """,
                (timestamp, source_profile_id),
            ).fetchone()
            if not row:
                self.connection.execute("COMMIT")
                return None
            delivery_id = int(row["delivery_id"])
            attempt_no = int(row["attempts"]) + 1
            lease_expires = timestamp + int(lease_seconds)
            self.connection.execute(
                "UPDATE mirror_deliveries SET state = 'inflight', attempts = ?, updated_at = ? WHERE delivery_id = ?",
                (attempt_no, timestamp, delivery_id),
            )
            self.connection.execute(
                "INSERT INTO mirror_leases(delivery_id, worker_id, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
                (delivery_id, worker_id, timestamp, lease_expires),
            )
            self.connection.execute(
                """
                INSERT INTO mirror_delivery_attempts(delivery_id, attempt_no, started_at, outcome)
                VALUES (?, ?, ?, 'started')
                """,
                (delivery_id, attempt_no, timestamp),
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        self._harden_files()
        event = self.load_event(str(row["event_id"]))
        if event.conversation_kind == "group":
            event = replace(event, actor_display_label=self.participant_label(event))
        route = Route(
            conversation_id=event.conversation_id,
            chat_id=str(row["target_chat_id"]),
            thread_id=str(row["target_thread_id"]),
        )
        return DeliveryClaim(
            delivery_id=delivery_id,
            attempt_no=attempt_no,
            event=event,
            route=route,
            worker_id=worker_id,
            lease_expires_at=lease_expires,
        )

    def _require_lease(self, claim: DeliveryClaim) -> None:
        row = self.connection.execute(
            "SELECT worker_id FROM mirror_leases WHERE delivery_id = ?", (claim.delivery_id,)
        ).fetchone()
        if not row or str(row["worker_id"]) != claim.worker_id:
            raise LeaseLostError()

    def mark_sent(
        self,
        claim: DeliveryClaim,
        remote_ids: Iterable[str] = (),
        *,
        now: float | int | None = None,
        media_retention_seconds: int = 48 * 60 * 60,
    ) -> None:
        timestamp = _now_seconds(now)
        if isinstance(media_retention_seconds, bool) or media_retention_seconds < 0:
            raise ValueError("invalid_media_retention")
        ids = [str(value) for value in remote_ids]
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self._require_lease(claim)
            self.connection.execute(
                """
                UPDATE mirror_deliveries SET state = 'sent', remote_ids_json = ?,
                    last_error_code = NULL, updated_at = ? WHERE delivery_id = ?
                """,
                (json.dumps(ids, separators=(",", ":")), timestamp, claim.delivery_id),
            )
            self.connection.execute(
                """
                UPDATE mirror_delivery_attempts SET finished_at = ?, outcome = 'sent', error_code = NULL
                WHERE delivery_id = ? AND attempt_no = ?
                """,
                (timestamp, claim.delivery_id, claim.attempt_no),
            )
            self.connection.execute("DELETE FROM mirror_leases WHERE delivery_id = ?", (claim.delivery_id,))
            self.connection.execute(
                """
                UPDATE mirror_managed_media
                SET state = 'cleanup_pending', next_attempt_at = ?, updated_at = ?
                WHERE event_id = ? AND state = 'active'
                """,
                (
                    timestamp + int(media_retention_seconds),
                    timestamp,
                    claim.event.event_id,
                ),
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        self._harden_files()

    def list_media_cleanup_due(
        self, *, now: float | int | None = None, limit: int = 100
    ) -> list[str]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("invalid_cleanup_limit")
        rows = self.connection.execute(
            """
            SELECT path FROM mirror_managed_media
            WHERE state = 'cleanup_pending' AND next_attempt_at <= ?
            ORDER BY next_attempt_at, path LIMIT ?
            """,
            (_now_seconds(now), limit),
        ).fetchall()
        return [str(row["path"]) for row in rows]

    def managed_media_paths(self) -> list[str]:
        return [
            str(row["path"])
            for row in self.connection.execute(
                "SELECT path FROM mirror_managed_media ORDER BY path"
            )
        ]

    def mark_media_removed(self, path: str) -> bool:
        result = self.connection.execute(
            "DELETE FROM mirror_managed_media WHERE path = ? AND state = 'cleanup_pending'",
            (path,),
        )
        self._harden_files()
        return result.rowcount == 1

    def mark_media_cleanup_failed(
        self,
        path: str,
        *,
        now: float | int | None = None,
        retry_seconds: int = 60,
    ) -> bool:
        timestamp = _now_seconds(now)
        result = self.connection.execute(
            """
            UPDATE mirror_managed_media
            SET cleanup_attempts = cleanup_attempts + 1,
                next_attempt_at = ?, last_error_code = 'media_cleanup_failed',
                updated_at = ?
            WHERE path = ? AND state = 'cleanup_pending'
            """,
            (timestamp + max(1, int(retry_seconds)), timestamp, path),
        )
        self._harden_files()
        return result.rowcount == 1

    def managed_media_report(self) -> dict[str, object]:
        """Return content-free spool accounting grouped by operational state."""

        rows = self.connection.execute(
            """
            SELECT m.state AS media_state,
                   COALESCE(d.state, rb.state, 'captured') AS owner_state,
                   COUNT(*) AS media_count,
                   COALESCE(SUM(m.size_bytes), 0) AS total_bytes
            FROM mirror_managed_media m
            LEFT JOIN mirror_deliveries d ON d.event_id = m.event_id
            LEFT JOIN mirror_route_blocks rb
                   ON rb.event_id = m.event_id AND rb.state = 'blocked_no_route'
            GROUP BY m.state, COALESCE(d.state, rb.state, 'captured')
            ORDER BY m.state, owner_state
            """
        ).fetchall()
        groups = [
            {
                "media_state": str(row["media_state"]),
                "owner_state": str(row["owner_state"]),
                "media_count": int(row["media_count"]),
                "total_bytes": int(row["total_bytes"]),
            }
            for row in rows
        ]
        return {
            "media_count": sum(int(group["media_count"]) for group in groups),
            "total_bytes": sum(int(group["total_bytes"]) for group in groups),
            "groups": groups,
        }

    def authorize_media_purge(
        self,
        event_id: str,
        *,
        evidence_ref: str,
        now: float | int | None = None,
    ) -> int:
        """Queue terminal or blocked media for local deletion with an audit proof."""

        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError("invalid_event_id")
        if not isinstance(evidence_ref, str) or not evidence_ref.strip():
            raise ValueError("evidence_required")
        timestamp = _now_seconds(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT d.state AS delivery_state, rb.state AS route_state
                FROM mirror_events e
                LEFT JOIN mirror_deliveries d ON d.event_id = e.event_id
                LEFT JOIN mirror_route_blocks rb
                       ON rb.event_id = e.event_id AND rb.state = 'blocked_no_route'
                WHERE e.event_id = ?
                """,
                (event_id,),
            ).fetchone()
            if row is None:
                raise LedgerError("event_missing")
            delivery_state = (
                str(row["delivery_state"])
                if row["delivery_state"] is not None
                else str(row["route_state"] or "captured")
            )
            if delivery_state not in {
                "dead",
                "blocked",
                "uncertain",
                "blocked_no_route",
            }:
                raise LedgerError("media_purge_not_authorized_for_state")
            updated = self.connection.execute(
                """
                UPDATE mirror_managed_media
                SET state = 'cleanup_pending', next_attempt_at = ?, updated_at = ?
                WHERE event_id = ? AND state = 'active'
                """,
                (timestamp, timestamp, event_id),
            )
            media_count = int(updated.rowcount)
            if media_count > 0:
                self.connection.execute(
                    """
                    INSERT INTO mirror_media_purge_audit(
                        event_id, delivery_state, media_count,
                        evidence_hash, changed_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        delivery_state,
                        media_count,
                        _opaque_hash(evidence_ref),
                        timestamp,
                    ),
                )
            self.connection.execute("COMMIT")
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
        self._harden_files()
        return media_count

    def mark_failed(
        self,
        claim: DeliveryClaim,
        *,
        error_code: str,
        retry_at: float | int | None,
        permanent: bool = False,
        max_attempts: int = 5,
        now: float | int | None = None,
    ) -> str:
        timestamp = _now_seconds(now)
        code = error_code if error_code.replace("_", "").isalnum() else "transport_error"
        state = "dead" if permanent or claim.attempt_no >= int(max_attempts) else "retry"
        next_attempt = timestamp if retry_at is None else _now_seconds(retry_at)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self._require_lease(claim)
            self.connection.execute(
                """
                UPDATE mirror_deliveries SET state = ?, next_attempt_at = ?,
                    last_error_code = ?, updated_at = ? WHERE delivery_id = ?
                """,
                (state, next_attempt, code, timestamp, claim.delivery_id),
            )
            self.connection.execute(
                """
                UPDATE mirror_delivery_attempts SET finished_at = ?, outcome = ?, error_code = ?
                WHERE delivery_id = ? AND attempt_no = ?
                """,
                (timestamp, state, code, claim.delivery_id, claim.attempt_no),
            )
            self.connection.execute("DELETE FROM mirror_leases WHERE delivery_id = ?", (claim.delivery_id,))
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        self._harden_files()
        return state

    def mark_uncertain(
        self,
        claim: DeliveryClaim,
        *,
        error_code: str = "delivery_outcome_unknown",
        now: float | int | None = None,
    ) -> None:
        """Quarantine a delivery that may already have reached Telegram.

        Telegram has no idempotency key. An expired in-flight lease, timeout,
        connection loss after dispatch, or unclassified transport exception is
        therefore never retried automatically.
        """
        timestamp = _now_seconds(now)
        code = error_code if error_code.replace("_", "").isalnum() else "delivery_outcome_unknown"
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self._require_lease(claim)
            self.connection.execute(
                """
                UPDATE mirror_deliveries SET state = 'uncertain', last_error_code = ?, updated_at = ?
                WHERE delivery_id = ?
                """,
                (code, timestamp, claim.delivery_id),
            )
            self.connection.execute(
                """
                UPDATE mirror_delivery_attempts SET finished_at = ?, outcome = 'uncertain', error_code = ?
                WHERE delivery_id = ? AND attempt_no = ?
                """,
                (timestamp, code, claim.delivery_id, claim.attempt_no),
            )
            self.connection.execute("DELETE FROM mirror_leases WHERE delivery_id = ?", (claim.delivery_id,))
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        self._harden_files()

    def delivery_state(self, event_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT state FROM mirror_deliveries WHERE event_id = ?", (event_id,)
        ).fetchone()
        return str(row["state"]) if row else None

    def attempt_count(self, event_id: str) -> int:
        row = self.connection.execute(
            "SELECT attempts FROM mirror_deliveries WHERE event_id = ?", (event_id,)
        ).fetchone()
        return int(row["attempts"]) if row else 0

    def list_deliveries(
        self,
        *,
        state: str | None = None,
        limit: int = 500,
    ) -> list[sqlite3.Row]:
        allowed = {
            "pending", "inflight", "retry", "sent", "dead", "blocked", "uncertain"
        }
        if state is not None and state not in allowed:
            raise ValueError("invalid_delivery_state")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("invalid_delivery_limit")
        where = "WHERE d.state = ?" if state else ""
        params: tuple[object, ...] = (state, limit) if state else (limit,)
        return self.connection.execute(
            f"""
            SELECT d.delivery_id, d.event_id, e.conversation_id,
                   d.state, d.attempts, d.last_error_code, d.updated_at
            FROM mirror_deliveries d
            JOIN mirror_events e ON e.event_id = d.event_id
            {where}
            ORDER BY d.updated_at, d.delivery_id LIMIT ?
            """,
            params,
        ).fetchall()

    def reconcile_uncertain(
        self,
        event_id: str,
        *,
        resolution: str,
        evidence_ref: str,
        now: float | int | None = None,
    ) -> bool:
        if resolution not in {"sent", "retry"}:
            raise ValueError("invalid_uncertain_resolution")
        if not isinstance(evidence_ref, str) or not evidence_ref.strip():
            raise ValueError("evidence_required")
        timestamp = _now_seconds(now)
        next_state = "sent" if resolution == "sent" else "retry"
        audit_resolution = (
            "uncertain_mark_sent" if resolution == "sent" else "uncertain_retry"
        )
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT delivery_id, state FROM mirror_deliveries WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if not row or str(row["state"]) != "uncertain":
                raise LedgerError("uncertain_delivery_missing")
            delivery_id = int(row["delivery_id"])
            if next_state == "sent":
                updated = self.connection.execute(
                    """
                    UPDATE mirror_deliveries
                    SET state = 'sent', remote_ids_json = '[]',
                        last_error_code = NULL, updated_at = ?
                    WHERE delivery_id = ? AND state = 'uncertain'
                    """,
                    (timestamp, delivery_id),
                )
                self.connection.execute(
                    """
                    UPDATE mirror_managed_media
                    SET state = 'cleanup_pending', next_attempt_at = ?, updated_at = ?
                    WHERE event_id = ? AND state = 'active'
                    """,
                    (timestamp, timestamp, event_id),
                )
            else:
                updated = self.connection.execute(
                    """
                    UPDATE mirror_deliveries
                    SET state = 'retry', next_attempt_at = ?,
                        last_error_code = 'operator_reconciled_retry', updated_at = ?
                    WHERE delivery_id = ? AND state = 'uncertain'
                    """,
                    (timestamp, timestamp, delivery_id),
                )
            if updated.rowcount != 1:
                raise LedgerError("uncertain_delivery_changed")
            self.connection.execute(
                """
                INSERT INTO mirror_delivery_reconciliation_audit(
                    delivery_id, old_state, new_state, resolution,
                    evidence_hash, changed_at
                ) VALUES (?, 'uncertain', ?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    next_state,
                    audit_resolution,
                    _opaque_hash(evidence_ref),
                    timestamp,
                ),
            )
            self.connection.execute("COMMIT")
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
        self._harden_files()
        return True

    def rebind_route_changed(
        self,
        conversation_id: str,
        *,
        evidence_ref: str,
        limit: int = 500,
        now: float | int | None = None,
    ) -> int:
        if not isinstance(evidence_ref, str) or not evidence_ref.strip():
            raise ValueError("evidence_required")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("invalid_reconcile_limit")
        timestamp = _now_seconds(now)
        updated_count = 0
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            route_row = self.connection.execute(
                """SELECT conversation_id, chat_id, thread_id, enabled
                   FROM mirror_routes WHERE conversation_id = ?""",
                (conversation_id,),
            ).fetchone()
            if route_row is None or not bool(route_row["enabled"]):
                raise RouteMissingError("route_missing")
            route = Route(
                conversation_id=str(route_row["conversation_id"]),
                chat_id=str(route_row["chat_id"]),
                thread_id=str(route_row["thread_id"]),
                enabled=True,
            )
            rows = self.connection.execute(
                """
                SELECT d.delivery_id
                FROM mirror_deliveries d
                JOIN mirror_events e ON e.event_id = d.event_id
                WHERE e.conversation_id = ? AND d.state = 'blocked'
                  AND d.last_error_code = 'route_changed'
                ORDER BY d.updated_at, d.delivery_id LIMIT ?
                """,
                (conversation_id, limit),
            ).fetchall()
            for row in rows:
                delivery_id = int(row["delivery_id"])
                updated = self.connection.execute(
                    """
                    UPDATE mirror_deliveries
                    SET target_chat_id = ?, target_thread_id = ?, state = 'pending',
                        next_attempt_at = ?, last_error_code = NULL, updated_at = ?
                    WHERE delivery_id = ? AND state = 'blocked'
                      AND last_error_code = 'route_changed'
                    """,
                    (
                        route.chat_id,
                        route.thread_id,
                        timestamp,
                        timestamp,
                        delivery_id,
                    ),
                )
                if updated.rowcount != 1:
                    continue
                self.connection.execute(
                    """
                    INSERT INTO mirror_delivery_reconciliation_audit(
                        delivery_id, old_state, new_state, resolution,
                        evidence_hash, changed_at
                    ) VALUES (?, 'blocked', 'pending', 'route_rebind', ?, ?)
                    """,
                    (delivery_id, _opaque_hash(evidence_ref), timestamp),
                )
                updated_count += 1
            self.connection.execute("COMMIT")
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
        self._harden_files()
        return updated_count

    def quick_check(self) -> str:
        return str(self.connection.execute("PRAGMA quick_check").fetchone()[0])

    def health(self) -> dict[str, object]:
        states = {
            str(row["state"]): int(row["count"])
            for row in self.connection.execute(
                "SELECT state, COUNT(*) AS count FROM mirror_deliveries GROUP BY state"
            )
        }
        route_blocks = {
            str(row["state"]): int(row["count"])
            for row in self.connection.execute(
                "SELECT state, COUNT(*) AS count FROM mirror_route_blocks GROUP BY state"
            )
        }
        return {
            "schema_version": int(
                self.connection.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM mirror_schema_migrations"
                ).fetchone()[0]
            ),
            "quick_check": self.quick_check(),
            "events": int(self.connection.execute("SELECT COUNT(*) FROM mirror_events").fetchone()[0]),
            "routes": int(self.connection.execute("SELECT COUNT(*) FROM mirror_routes").fetchone()[0]),
            "active_leases": int(self.connection.execute("SELECT COUNT(*) FROM mirror_leases").fetchone()[0]),
            "runtime_locks": int(
                self.connection.execute("SELECT COUNT(*) FROM mirror_runtime_locks").fetchone()[0]
            ),
            "delivery_states": states,
            "route_blocks": route_blocks,
            "blocked_no_route": route_blocks.get("blocked_no_route", 0),
            "managed_media": self.managed_media_report(),
            "conversation_policies": int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM mirror_conversation_policies"
                ).fetchone()[0]
            ),
            "conversation_aliases": int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM mirror_conversation_aliases"
                ).fetchone()[0]
            ),
            "source_quarantine": int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM mirror_source_quarantine"
                ).fetchone()[0]
            ),
        }
