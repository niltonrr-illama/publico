"""Deterministic, optional consumers for the immutable mirror ledger.

Capture and Telegram delivery do not import this module.  Daily Notes, claims,
search projections, and reports can therefore fail or be disabled without
stopping the data plane.  Every consumer owns an independent cursor for each
privacy scope and advances it only after its durable output is published.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping, Sequence, TextIO

from .ledger import MirrorLedger
from .models import InboundEvent, PRIVACY_SCOPES


CONSUMER_SCHEMA_VERSION = 1
_CONSUMER_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_CLAIM_ID = re.compile(r"^claim:[0-9a-f]{64}$")
_DAY_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:T| |$)")
_PRIVACY_RANK = {
    "area_shared": 0,
    "partnership_restricted": 1,
    "owner_private": 2,
}


class ConsumerError(RuntimeError):
    """A fail-closed error isolated from capture and delivery."""

    code = "consumer_error"

    def __init__(self, code: str | None = None):
        self.code = code or self.code
        super().__init__(self.code)


class ConsumerCursorConflictError(ConsumerError):
    code = "consumer_cursor_conflict"


class ClaimConflictError(ConsumerError):
    code = "claim_conflict"


class PrivacyScopeError(ConsumerError):
    code = "privacy_scope_downgrade"


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    claim_id: str
    text: str
    privacy_scope: str
    evidence_event_ids: tuple[str, ...]
    supersedes: tuple[str, ...]
    created_at: int
    active: bool = True


@dataclass(frozen=True, slots=True)
class DailyNotesResult:
    consumer_id: str
    processed_events: int
    files: tuple[Path, ...]
    cursors: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class SearchExportResult:
    path: Path
    event_documents: int
    claim_documents: int
    sha256: str
    size_bytes: int


def _now_seconds(value: float | int | None = None) -> int:
    return int(time.time() if value is None else value)


def _opaque_event_ref(event_id: str) -> str:
    return "event:" + hashlib.sha256(event_id.encode("utf-8")).hexdigest()


def _require_consumer_id(value: str) -> str:
    if not isinstance(value, str) or not _CONSUMER_ID.fullmatch(value):
        raise ValueError("invalid_consumer_id")
    return value


def _require_scope(value: str) -> str:
    if not isinstance(value, str) or value not in PRIVACY_SCOPES:
        raise ValueError("invalid_privacy_scope")
    return value


def _normalize_scopes(
    values: Iterable[str] | None, *, require_explicit: bool = False
) -> tuple[str, ...]:
    if values is None:
        if require_explicit:
            raise ValueError("allowed_scopes_required")
        return tuple(sorted(PRIVACY_SCOPES, key=_PRIVACY_RANK.get))
    result = tuple(sorted({_require_scope(item) for item in values}, key=_PRIVACY_RANK.get))
    if not result:
        raise ValueError("allowed_scopes_required")
    return result


def _unique_strings(
    values: Iterable[str], *, field: str, allow_empty_collection: bool
) -> tuple[str, ...]:
    result: set[str] = set()
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"invalid_{field}")
        result.add(item.strip())
    if not result and not allow_empty_collection:
        raise ValueError(f"{field}_required")
    return tuple(sorted(result))


def _event_day(occurred_at: str) -> str:
    match = _DAY_PREFIX.match(occurred_at)
    if not match:
        raise ConsumerError("invalid_event_occurred_at")
    value = match.group(1)
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ConsumerError("invalid_event_occurred_at") from None
    return value


def _atomic_write(path: Path, callback: Callable[[TextIO], None]) -> None:
    """Publish one file atomically in its destination directory."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            callback(handle)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
        temporary = None
        try:
            directory_flag = getattr(os, "O_DIRECTORY", 0)
            descriptor = os.open(path.parent, os.O_RDONLY | directory_flag)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


class MirrorConsumers:
    """Independent projections over :class:`MirrorLedger`.

    The class shares the ledger connection but owns only ``mirror_consumer_*``,
    ``mirror_daily_*``, and ``mirror_claim_*`` tables.  It never modifies an
    event, delivery, route, source cursor, or ``PRAGMA user_version``.
    """

    def __init__(self, ledger: MirrorLedger):
        if not isinstance(ledger, MirrorLedger):
            raise TypeError("ledger_required")
        self.ledger = ledger
        self.connection = ledger.connection
        self._migrate()

    def _migrate(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS mirror_consumer_schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at INTEGER NOT NULL
            )
            """
        )
        current = int(
            self.connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM mirror_consumer_schema_migrations"
            ).fetchone()[0]
        )
        if current > CONSUMER_SCHEMA_VERSION:
            raise ConsumerError("unsupported_consumer_schema")
        if current == CONSUMER_SCHEMA_VERSION:
            return
        try:
            self.connection.executescript(
                """
                BEGIN IMMEDIATE;

                CREATE TABLE mirror_consumer_event_index (
                    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE REFERENCES mirror_events(event_id),
                    captured_at INTEGER NOT NULL
                );

                CREATE TABLE mirror_consumer_cursors (
                    consumer_id TEXT NOT NULL,
                    privacy_scope TEXT NOT NULL CHECK (
                        privacy_scope IN ('area_shared', 'partnership_restricted', 'owner_private')
                    ),
                    last_event_seq INTEGER NOT NULL DEFAULT 0 CHECK (last_event_seq >= 0),
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(consumer_id, privacy_scope)
                );

                CREATE TABLE mirror_daily_note_events (
                    consumer_id TEXT NOT NULL,
                    privacy_scope TEXT NOT NULL CHECK (
                        privacy_scope IN ('area_shared', 'partnership_restricted', 'owner_private')
                    ),
                    event_id TEXT NOT NULL REFERENCES mirror_events(event_id),
                    event_seq INTEGER NOT NULL REFERENCES mirror_consumer_event_index(event_seq),
                    event_day TEXT NOT NULL,
                    projected_at INTEGER NOT NULL,
                    PRIMARY KEY(consumer_id, privacy_scope, event_id),
                    UNIQUE(consumer_id, privacy_scope, event_seq)
                );
                CREATE INDEX mirror_daily_note_day
                ON mirror_daily_note_events(consumer_id, privacy_scope, event_day, event_seq);

                CREATE TABLE mirror_claims (
                    claim_id TEXT PRIMARY KEY,
                    claim_hash TEXT NOT NULL UNIQUE,
                    claim_text TEXT NOT NULL,
                    privacy_scope TEXT NOT NULL CHECK (
                        privacy_scope IN ('area_shared', 'partnership_restricted', 'owner_private')
                    ),
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE mirror_claim_evidence (
                    claim_id TEXT NOT NULL REFERENCES mirror_claims(claim_id),
                    event_id TEXT NOT NULL REFERENCES mirror_events(event_id),
                    event_payload_hash TEXT NOT NULL,
                    evidence_privacy_scope TEXT NOT NULL CHECK (
                        evidence_privacy_scope IN ('area_shared', 'partnership_restricted', 'owner_private')
                    ),
                    added_at INTEGER NOT NULL,
                    PRIMARY KEY(claim_id, event_id)
                );

                CREATE TABLE mirror_claim_supersessions (
                    old_claim_id TEXT PRIMARY KEY REFERENCES mirror_claims(claim_id),
                    new_claim_id TEXT NOT NULL REFERENCES mirror_claims(claim_id),
                    created_at INTEGER NOT NULL,
                    CHECK(old_claim_id <> new_claim_id)
                );
                CREATE INDEX mirror_claim_supersessions_new
                ON mirror_claim_supersessions(new_claim_id);

                CREATE TRIGGER mirror_claims_immutable_update
                BEFORE UPDATE ON mirror_claims BEGIN
                    SELECT RAISE(ABORT, 'immutable_claim');
                END;
                CREATE TRIGGER mirror_claims_immutable_delete
                BEFORE DELETE ON mirror_claims BEGIN
                    SELECT RAISE(ABORT, 'immutable_claim');
                END;
                CREATE TRIGGER mirror_claim_evidence_immutable_update
                BEFORE UPDATE ON mirror_claim_evidence BEGIN
                    SELECT RAISE(ABORT, 'immutable_claim_evidence');
                END;
                CREATE TRIGGER mirror_claim_evidence_immutable_delete
                BEFORE DELETE ON mirror_claim_evidence BEGIN
                    SELECT RAISE(ABORT, 'immutable_claim_evidence');
                END;
                CREATE TRIGGER mirror_claim_supersessions_immutable_update
                BEFORE UPDATE ON mirror_claim_supersessions BEGIN
                    SELECT RAISE(ABORT, 'immutable_claim_supersession');
                END;
                CREATE TRIGGER mirror_claim_supersessions_immutable_delete
                BEFORE DELETE ON mirror_claim_supersessions BEGIN
                    SELECT RAISE(ABORT, 'immutable_claim_supersession');
                END;
                """
            )
            self.connection.execute(
                "INSERT INTO mirror_consumer_schema_migrations(version, applied_at) VALUES (?, ?)",
                (CONSUMER_SCHEMA_VERSION, _now_seconds()),
            )
            self.connection.execute("COMMIT")
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
        self.ledger._harden_files()

    def _index_new_events(self) -> None:
        """Assign a durable local sequence without changing ``mirror_events``."""

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                """
                INSERT INTO mirror_consumer_event_index(event_id, captured_at)
                SELECT e.event_id, e.captured_at
                FROM mirror_events AS e
                WHERE NOT EXISTS (
                    SELECT 1 FROM mirror_consumer_event_index AS i
                    WHERE i.event_id = e.event_id
                )
                ORDER BY e.rowid
                """
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def get_cursor(self, consumer_id: str, privacy_scope: str) -> int:
        _require_consumer_id(consumer_id)
        _require_scope(privacy_scope)
        row = self.connection.execute(
            """
            SELECT last_event_seq FROM mirror_consumer_cursors
            WHERE consumer_id = ? AND privacy_scope = ?
            """,
            (consumer_id, privacy_scope),
        ).fetchone()
        return int(row["last_event_seq"]) if row else 0

    def _next_rows(
        self, consumer_id: str, privacy_scope: str, *, limit: int
    ) -> tuple[int, list[Mapping[str, object]]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0 or limit > 10_000:
            raise ValueError("invalid_batch_limit")
        self._index_new_events()
        cursor = self.get_cursor(consumer_id, privacy_scope)
        rows = self.connection.execute(
            """
            SELECT i.event_seq, e.event_id, e.occurred_at, e.payload_json
            FROM mirror_consumer_event_index AS i
            JOIN mirror_events AS e ON e.event_id = i.event_id
            WHERE i.event_seq > ? AND e.privacy_scope = ?
            ORDER BY i.event_seq
            LIMIT ?
            """,
            (cursor, privacy_scope, limit),
        ).fetchall()
        return cursor, [dict(row) for row in rows]

    def project_daily_notes(
        self,
        output_dir: str | Path,
        *,
        consumer_id: str = "daily-notes",
        allowed_scopes: Iterable[str],
        batch_limit: int = 500,
        now: float | int | None = None,
    ) -> DailyNotesResult:
        """Project at most ``batch_limit`` events per scope into Daily Notes.

        A cursor is committed only after every touched day has been atomically
        replaced.  A crash after a replace but before the commit is safe: the
        same deterministic file is rebuilt on the next run.
        """

        _require_consumer_id(consumer_id)
        scopes = _normalize_scopes(allowed_scopes, require_explicit=True)
        root = Path(output_dir)
        processed = 0
        files: list[Path] = []
        cursors: dict[str, int] = {}
        for scope in scopes:
            count, touched, cursor = self._project_daily_scope(
                root,
                consumer_id=consumer_id,
                privacy_scope=scope,
                batch_limit=batch_limit,
                now=now,
            )
            processed += count
            files.extend(touched)
            cursors[scope] = cursor
        return DailyNotesResult(
            consumer_id=consumer_id,
            processed_events=processed,
            files=tuple(files),
            cursors=cursors,
        )

    def _project_daily_scope(
        self,
        output_dir: Path,
        *,
        consumer_id: str,
        privacy_scope: str,
        batch_limit: int,
        now: float | int | None,
    ) -> tuple[int, list[Path], int]:
        start_cursor, rows = self._next_rows(
            consumer_id, privacy_scope, limit=batch_limit
        )
        if not rows:
            return 0, [], start_cursor

        batch_by_day: dict[str, list[tuple[int, InboundEvent]]] = {}
        for row in rows:
            event = InboundEvent.from_storage_json(str(row["payload_json"]))
            day = _event_day(event.occurred_at)
            batch_by_day.setdefault(day, []).append((int(row["event_seq"]), event))

        touched: list[Path] = []
        for day in sorted(batch_by_day):
            existing = self.connection.execute(
                """
                SELECT p.event_seq, e.payload_json
                FROM mirror_daily_note_events AS p
                JOIN mirror_events AS e ON e.event_id = p.event_id
                WHERE p.consumer_id = ? AND p.privacy_scope = ? AND p.event_day = ?
                ORDER BY p.event_seq
                """,
                (consumer_id, privacy_scope, day),
            ).fetchall()
            combined: dict[str, tuple[int, InboundEvent]] = {
                event.event_id: (int(row["event_seq"]), event)
                for row in existing
                for event in (InboundEvent.from_storage_json(str(row["payload_json"])),)
            }
            for event_seq, event in batch_by_day[day]:
                combined[event.event_id] = (event_seq, event)
            ordered = sorted(
                combined.values(), key=lambda item: (item[1].occurred_at, item[1].event_id)
            )
            target = output_dir / privacy_scope / f"{day}.md"
            content = self._render_daily_note(day, privacy_scope, ordered)
            _atomic_write(target, lambda handle, value=content: handle.write(value))
            touched.append(target)

        end_cursor = int(rows[-1]["event_seq"])
        timestamp = _now_seconds(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if self.get_cursor(consumer_id, privacy_scope) != start_cursor:
                raise ConsumerCursorConflictError()
            self.connection.executemany(
                """
                INSERT OR IGNORE INTO mirror_daily_note_events(
                    consumer_id, privacy_scope, event_id, event_seq, event_day, projected_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        consumer_id,
                        privacy_scope,
                        event.event_id,
                        event_seq,
                        day,
                        timestamp,
                    )
                    for day, values in batch_by_day.items()
                    for event_seq, event in values
                ],
            )
            self.connection.execute(
                """
                INSERT INTO mirror_consumer_cursors(
                    consumer_id, privacy_scope, last_event_seq, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(consumer_id, privacy_scope) DO UPDATE SET
                    last_event_seq = excluded.last_event_seq,
                    updated_at = excluded.updated_at
                """,
                (consumer_id, privacy_scope, end_cursor, timestamp),
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        self.ledger._harden_files()
        return len(rows), touched, end_cursor

    @staticmethod
    def _render_daily_note(
        day: str,
        privacy_scope: str,
        events: Sequence[tuple[int, InboundEvent]],
    ) -> str:
        lines = [
            f"# Daily Notes — {day}",
            "",
            f"- privacy_scope: `{privacy_scope}`",
            f"- events: {len(events)}",
            "- projection: deterministic-v1",
            "",
        ]
        for _, event in events:
            lines.extend(
                [
                    f"## {event.occurred_at} · {_opaque_event_ref(event.event_id)}",
                    "",
                    f"- source: `{event.source}`",
                    f"- conversation: `{event.conversation_id}`",
                    f"- actor: `{event.actor_ref}`",
                ]
            )
            context = event.context_text or event.text
            if context:
                lines.extend(["- text:", *[f"  > {line}" for line in context.splitlines()]])
            for media in event.media:
                detail = f"{media.kind}; mime={media.mime_type or 'unknown'}; bytes={media.size_bytes}"
                if media.sha256:
                    detail += f"; sha256={media.sha256}"
                lines.append(f"- media: `{detail}`")
                if media.caption:
                    lines.extend(
                        ["  - original_caption:", *[f"    > {line}" for line in media.caption.splitlines()]]
                    )
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def add_claim(
        self,
        text: str,
        evidence_event_ids: Iterable[str],
        *,
        privacy_scope: str,
        supersedes: Iterable[str] = (),
        claim_id: str | None = None,
        now: float | int | None = None,
    ) -> tuple[ClaimRecord, bool]:
        """Add an immutable, evidence-backed claim.

        A claim can only keep or increase the restriction of all evidence and
        every claim it supersedes.  Supersession is an immutable edge, never a
        destructive update of history.
        """

        if not isinstance(text, str) or not text.strip():
            raise ValueError("invalid_claim_text")
        scope = _require_scope(privacy_scope)
        evidence_ids = _unique_strings(
            evidence_event_ids,
            field="claim_evidence",
            allow_empty_collection=False,
        )
        old_ids = _unique_strings(
            supersedes,
            field="superseded_claim",
            allow_empty_collection=True,
        )
        timestamp = _now_seconds(now)

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            evidence_rows = []
            for event_id in evidence_ids:
                row = self.connection.execute(
                    """
                    SELECT payload_hash, privacy_scope FROM mirror_events
                    WHERE event_id = ?
                    """,
                    (event_id,),
                ).fetchone()
                if not row:
                    raise ConsumerError("claim_evidence_missing")
                evidence_rows.append(
                    (event_id, str(row["payload_hash"]), str(row["privacy_scope"]))
                )

            old_rows = []
            for old_id in old_ids:
                row = self.connection.execute(
                    "SELECT claim_hash, privacy_scope FROM mirror_claims WHERE claim_id = ?",
                    (old_id,),
                ).fetchone()
                if not row:
                    raise ConsumerError("superseded_claim_missing")
                old_rows.append((old_id, str(row["claim_hash"]), str(row["privacy_scope"])))

            required_rank = max(
                [_PRIVACY_RANK[item[2]] for item in evidence_rows + old_rows],
                default=0,
            )
            if _PRIVACY_RANK[scope] < required_rank:
                raise PrivacyScopeError()

            canonical = {
                "schema_version": 1,
                "text": text,
                "privacy_scope": scope,
                "evidence": evidence_rows,
                "supersedes": old_rows,
            }
            claim_hash = hashlib.sha256(
                json.dumps(
                    canonical,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            effective_id = claim_id or f"claim:{claim_hash}"
            if not isinstance(effective_id, str) or not _CLAIM_ID.fullmatch(effective_id):
                raise ValueError("invalid_claim_id")
            if effective_id in old_ids:
                raise ClaimConflictError("claim_self_supersession")

            existing = self.connection.execute(
                "SELECT claim_hash FROM mirror_claims WHERE claim_id = ?", (effective_id,)
            ).fetchone()
            if existing:
                if str(existing["claim_hash"]) != claim_hash:
                    raise ClaimConflictError()
                self.connection.execute("COMMIT")
                return self.get_claim(effective_id), False
            duplicate = self.connection.execute(
                "SELECT claim_id FROM mirror_claims WHERE claim_hash = ?", (claim_hash,)
            ).fetchone()
            if duplicate:
                self.connection.execute("COMMIT")
                return self.get_claim(str(duplicate["claim_id"])), False

            for old_id in old_ids:
                edge = self.connection.execute(
                    "SELECT new_claim_id FROM mirror_claim_supersessions WHERE old_claim_id = ?",
                    (old_id,),
                ).fetchone()
                if edge and str(edge["new_claim_id"]) != effective_id:
                    raise ClaimConflictError("claim_already_superseded")

            self.connection.execute(
                """
                INSERT INTO mirror_claims(
                    claim_id, claim_hash, claim_text, privacy_scope, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (effective_id, claim_hash, text, scope, timestamp),
            )
            self.connection.executemany(
                """
                INSERT INTO mirror_claim_evidence(
                    claim_id, event_id, event_payload_hash,
                    evidence_privacy_scope, added_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (effective_id, event_id, payload_hash, event_scope, timestamp)
                    for event_id, payload_hash, event_scope in evidence_rows
                ],
            )
            self.connection.executemany(
                """
                INSERT INTO mirror_claim_supersessions(
                    old_claim_id, new_claim_id, created_at
                ) VALUES (?, ?, ?)
                """,
                [(old_id, effective_id, timestamp) for old_id in old_ids],
            )
            self.connection.execute("COMMIT")
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
        self.ledger._harden_files()
        return self.get_claim(effective_id), True

    def get_claim(self, claim_id: str) -> ClaimRecord:
        row = self.connection.execute(
            """
            SELECT c.claim_id, c.claim_text, c.privacy_scope, c.created_at,
                   NOT EXISTS (
                       SELECT 1 FROM mirror_claim_supersessions s
                       WHERE s.old_claim_id = c.claim_id
                   ) AS active
            FROM mirror_claims c WHERE c.claim_id = ?
            """,
            (claim_id,),
        ).fetchone()
        if not row:
            raise ConsumerError("claim_missing")
        evidence = tuple(
            str(item["event_id"])
            for item in self.connection.execute(
                """
                SELECT event_id FROM mirror_claim_evidence
                WHERE claim_id = ? ORDER BY event_id
                """,
                (claim_id,),
            )
        )
        supersedes = tuple(
            str(item["old_claim_id"])
            for item in self.connection.execute(
                """
                SELECT old_claim_id FROM mirror_claim_supersessions
                WHERE new_claim_id = ? ORDER BY old_claim_id
                """,
                (claim_id,),
            )
        )
        return ClaimRecord(
            claim_id=str(row["claim_id"]),
            text=str(row["claim_text"]),
            privacy_scope=str(row["privacy_scope"]),
            evidence_event_ids=evidence,
            supersedes=supersedes,
            created_at=int(row["created_at"]),
            active=bool(row["active"]),
        )

    def export_search_projection(
        self,
        output_path: str | Path,
        *,
        allowed_scopes: Iterable[str],
        include_events: bool = True,
        include_claims: bool = True,
        active_claims_only: bool = True,
    ) -> SearchExportResult:
        """Write deterministic JSONL for any local or remote search provider.

        This method does no network I/O and imports no GBrain/provider module.
        The caller explicitly selects every privacy scope included in the file.
        """

        scopes = _normalize_scopes(allowed_scopes, require_explicit=True)
        if not include_events and not include_claims:
            raise ValueError("empty_search_projection")
        placeholders = ",".join("?" for _ in scopes)
        target = Path(output_path)
        counts = {"events": 0, "claims": 0}

        def write(handle: TextIO) -> None:
            if include_events:
                cursor = self.connection.execute(
                    f"""
                    SELECT event_id, payload_json FROM mirror_events
                    WHERE privacy_scope IN ({placeholders})
                    ORDER BY occurred_at, event_id
                    """,
                    scopes,
                )
                while True:
                    rows = cursor.fetchmany(500)
                    if not rows:
                        break
                    for row in rows:
                        event = InboundEvent.from_storage_json(str(row["payload_json"]))
                        document = event.payload_dict()
                        document.update(
                            {
                                "document_id": _opaque_event_ref(event.event_id),
                                "kind": "event",
                            }
                        )
                        handle.write(
                            json.dumps(
                                document,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                            + "\n"
                        )
                        counts["events"] += 1
            if include_claims:
                active_clause = (
                    "AND NOT EXISTS (SELECT 1 FROM mirror_claim_supersessions s "
                    "WHERE s.old_claim_id = c.claim_id)"
                    if active_claims_only
                    else ""
                )
                rows = self.connection.execute(
                    f"""
                    SELECT c.claim_id, c.claim_text, c.privacy_scope, c.created_at
                    FROM mirror_claims c
                    WHERE c.privacy_scope IN ({placeholders}) {active_clause}
                    ORDER BY c.created_at, c.claim_id
                    """,
                    scopes,
                )
                for row in rows:
                    claim_id = str(row["claim_id"])
                    evidence = [
                        _opaque_event_ref(str(item["event_id"]))
                        for item in self.connection.execute(
                            """
                            SELECT event_id FROM mirror_claim_evidence
                            WHERE claim_id = ? ORDER BY event_id
                            """,
                            (claim_id,),
                        )
                    ]
                    supersedes = [
                        str(item["old_claim_id"])
                        for item in self.connection.execute(
                            """
                            SELECT old_claim_id FROM mirror_claim_supersessions
                            WHERE new_claim_id = ? ORDER BY old_claim_id
                            """,
                            (claim_id,),
                        )
                    ]
                    document = {
                        "created_at": int(row["created_at"]),
                        "document_id": claim_id,
                        "evidence": evidence,
                        "kind": "claim",
                        "privacy_scope": str(row["privacy_scope"]),
                        "supersedes": supersedes,
                        "text": str(row["claim_text"]),
                    }
                    handle.write(
                        json.dumps(
                            document,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                    counts["claims"] += 1

        _atomic_write(target, write)
        digest = hashlib.sha256()
        size = 0
        with target.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        return SearchExportResult(
            path=target,
            event_documents=counts["events"],
            claim_documents=counts["claims"],
            sha256=digest.hexdigest(),
            size_bytes=size,
        )

    def aggregate_report(
        self, *, allowed_scopes: Iterable[str]
    ) -> dict[str, object]:
        """Return counts and states only; no message or claim content."""

        scopes = _normalize_scopes(allowed_scopes, require_explicit=True)
        placeholders = ",".join("?" for _ in scopes)

        def counts(query: str) -> dict[str, int]:
            return {str(row[0]): int(row[1]) for row in self.connection.execute(query, scopes)}

        event_total, conversations = self.connection.execute(
            f"""
            SELECT COUNT(*), COUNT(DISTINCT conversation_id) FROM mirror_events
            WHERE privacy_scope IN ({placeholders})
            """,
            scopes,
        ).fetchone()
        claim_total, active_claims = self.connection.execute(
            f"""
            SELECT COUNT(*), SUM(CASE WHEN NOT EXISTS (
                SELECT 1 FROM mirror_claim_supersessions s
                WHERE s.old_claim_id = c.claim_id
            ) THEN 1 ELSE 0 END)
            FROM mirror_claims c WHERE c.privacy_scope IN ({placeholders})
            """,
            scopes,
        ).fetchone()
        return {
            "quick_check": self.ledger.quick_check(),
            "events": int(event_total),
            "conversations": int(conversations),
            "events_by_scope": counts(
                f"""SELECT privacy_scope, COUNT(*) FROM mirror_events
                    WHERE privacy_scope IN ({placeholders}) GROUP BY privacy_scope"""
            ),
            "events_by_source": counts(
                f"""SELECT source, COUNT(*) FROM mirror_events
                    WHERE privacy_scope IN ({placeholders}) GROUP BY source"""
            ),
            "delivery_states": counts(
                f"""SELECT d.state, COUNT(*) FROM mirror_deliveries d
                    JOIN mirror_events e ON e.event_id = d.event_id
                    WHERE e.privacy_scope IN ({placeholders}) GROUP BY d.state"""
            ),
            "route_blocks": counts(
                f"""SELECT b.state, COUNT(*) FROM mirror_route_blocks b
                    JOIN mirror_events e ON e.event_id = b.event_id
                    WHERE e.privacy_scope IN ({placeholders}) GROUP BY b.state"""
            ),
            "claims": int(claim_total),
            "active_claims": int(active_claims or 0),
            "consumer_cursors": int(
                self.connection.execute(
                    f"""SELECT COUNT(*) FROM mirror_consumer_cursors
                        WHERE privacy_scope IN ({placeholders})""",
                    scopes,
                ).fetchone()[0]
            ),
        }

    def pending_report(
        self,
        *,
        allowed_scopes: Iterable[str],
        include_content: bool = False,
        limit: int = 100,
    ) -> dict[str, object]:
        """Report pending/failed delivery metadata; content is opt-in."""

        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0 or limit > 1_000:
            raise ValueError("invalid_report_limit")
        scopes = _normalize_scopes(allowed_scopes, require_explicit=True)
        placeholders = ",".join("?" for _ in scopes)
        rows = self.connection.execute(
            f"""
            SELECT d.state, d.attempts, d.last_error_code,
                   e.event_id, e.source, e.conversation_id, e.occurred_at,
                   e.privacy_scope, e.payload_json
            FROM mirror_deliveries d
            JOIN mirror_events e ON e.event_id = d.event_id
            WHERE d.state <> 'sent' AND e.privacy_scope IN ({placeholders})
            ORDER BY d.updated_at, d.delivery_id LIMIT ?
            """,
            (*scopes, limit),
        ).fetchall()
        items = []
        for row in rows:
            item: dict[str, object] = {
                "kind": "delivery",
                "event_ref": _opaque_event_ref(str(row["event_id"])),
                "state": str(row["state"]),
                "attempts": int(row["attempts"]),
                "error_code": str(row["last_error_code"] or ""),
                "source": str(row["source"]),
                "conversation_ref": str(row["conversation_id"]),
                "occurred_at": str(row["occurred_at"]),
                "privacy_scope": str(row["privacy_scope"]),
            }
            if include_content:
                event = InboundEvent.from_storage_json(str(row["payload_json"]))
                item["text"] = event.context_text or event.text
                item["media"] = [media.payload_dict() for media in event.media]
            items.append(item)
        remaining = max(0, limit - len(items))
        if remaining:
            blocked = self.connection.execute(
                f"""
                SELECT b.reason, e.event_id, e.source, e.conversation_id,
                       e.occurred_at, e.privacy_scope, e.payload_json
                FROM mirror_route_blocks b
                JOIN mirror_events e ON e.event_id = b.event_id
                WHERE b.state = 'blocked_no_route'
                  AND e.privacy_scope IN ({placeholders})
                ORDER BY b.blocked_at, b.event_id LIMIT ?
                """,
                (*scopes, remaining),
            ).fetchall()
            for row in blocked:
                item = {
                    "kind": "route_block",
                    "event_ref": _opaque_event_ref(str(row["event_id"])),
                    "state": "blocked_no_route",
                    "error_code": str(row["reason"]),
                    "source": str(row["source"]),
                    "conversation_ref": str(row["conversation_id"]),
                    "occurred_at": str(row["occurred_at"]),
                    "privacy_scope": str(row["privacy_scope"]),
                }
                if include_content:
                    event = InboundEvent.from_storage_json(str(row["payload_json"]))
                    item["text"] = event.context_text or event.text
                    item["media"] = [media.payload_dict() for media in event.media]
                items.append(item)
        summary: dict[str, int] = {}
        for item in items:
            state = str(item["state"])
            summary[state] = summary.get(state, 0) + 1
        return {
            "include_content": include_content,
            "returned": len(items),
            "states": summary,
            "items": items,
        }
