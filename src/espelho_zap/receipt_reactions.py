"""Project WhatsApp provider receipts onto the original Telegram message.

The WhatsApp provider receipt is authoritative for delivery/read state.  This
module only adds or advances one bot reaction on the operator's original
Telegram message; it never edits that message and never emits a status reply.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3

from .ledger import LedgerError, MirrorLedger
from .telegram import TelegramBotTransport


@dataclass(frozen=True, slots=True)
class ReceiptReactionResult:
    candidates: int = 0
    applied: int = 0
    already_applied: int = 0
    invalid: int = 0
    failed: int = 0


def _read_only_connection(path: Path) -> sqlite3.Connection:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise LedgerError("outbound_ledger_invalid")
    uri = path.resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _remote_ids(value: object) -> tuple[str, ...]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(
        item.strip()
        for item in parsed
        if isinstance(item, str) and item.strip()
    )


def _emoji_for_state(state: int) -> str:
    # State 3 is provider-confirmed delivery; states 4/5 are read/played.
    return "👀" if state >= 4 else "✅"


class TelegramReceiptReactionProjector:
    """Apply bounded, idempotent receipt reactions for one observer cycle."""

    def __init__(
        self,
        mirror_ledger: MirrorLedger,
        outbound_ledger_path: str | Path,
        transport: TelegramBotTransport,
        *,
        max_items: int = 100,
    ):
        if mirror_ledger.read_only:
            raise ValueError("receipt_reaction_ledger_must_be_writable")
        if max_items <= 0:
            raise ValueError("receipt_reaction_limit_invalid")
        self.mirror_ledger = mirror_ledger
        self.outbound_ledger_path = Path(outbound_ledger_path)
        self.transport = transport
        self.max_items = int(max_items)

    def apply_once(self) -> ReceiptReactionResult:
        receipts = {
            str(row["outbound_ref"]): int(row["state"])
            for row in self.mirror_ledger.connection.execute(
                """SELECT outbound_ref, state FROM mirror_outbound_receipts
                   WHERE state >= 3"""
            ).fetchall()
        }
        if not receipts:
            return ReceiptReactionResult()

        connection = _read_only_connection(self.outbound_ledger_path)
        try:
            rows = connection.execute(
                """SELECT request_id, telegram_message_id, forum_chat_id,
                          telegram_thread_id, remote_message_ids_json
                   FROM hermes_human_outbound
                   WHERE status IN ('sent', 'uncertain')
                     AND remote_message_ids_json <> '[]'
                   ORDER BY updated_at, request_id"""
            ).fetchall()
        finally:
            connection.close()

        candidates = 0
        applied = 0
        already_applied = 0
        invalid = 0
        failed = 0
        for row in rows:
            if candidates >= self.max_items:
                break
            remote_ids = _remote_ids(row["remote_message_ids_json"])
            states = [receipts[item] for item in remote_ids if item in receipts]
            if not states:
                continue
            candidates += 1
            state = max(states)
            request_id = str(row["request_id"])
            previous = self.mirror_ledger.receipt_reaction_state(request_id)
            if previous is not None and state <= previous:
                already_applied += 1
                continue
            telegram_message_id = str(row["telegram_message_id"])
            forum_chat_id = str(row["forum_chat_id"])
            telegram_thread_id = str(row["telegram_thread_id"])
            emoji = _emoji_for_state(state)
            try:
                self.transport.set_message_reaction(
                    forum_chat_id, telegram_message_id, emoji
                )
                if self.mirror_ledger.record_receipt_reaction(
                    request_id,
                    telegram_message_id=telegram_message_id,
                    forum_chat_id=forum_chat_id,
                    telegram_thread_id=telegram_thread_id,
                    state=state,
                    emoji=emoji,
                ):
                    applied += 1
                else:
                    already_applied += 1
            except (ValueError, LedgerError):
                invalid += 1
            except Exception:
                # Do not mark the state applied when Telegram rejected or
                # ambiguously failed.  The next bounded cycle may retry it.
                failed += 1
        return ReceiptReactionResult(
            candidates=candidates,
            applied=applied,
            already_applied=already_applied,
            invalid=invalid,
            failed=failed,
        )


__all__ = ["ReceiptReactionResult", "TelegramReceiptReactionProjector"]
