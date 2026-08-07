from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from espelho_zap import MirrorLedger  # noqa: E402
from espelho_zap.receipt_reactions import (  # noqa: E402
    TelegramReceiptReactionProjector,
)


class _ReactionTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def set_message_reaction(self, chat_id: str, message_id: str, emoji: str) -> None:
        self.calls.append((chat_id, message_id, emoji))


class ReceiptReactionProjectorTest(unittest.TestCase):
    def test_reactions_advance_without_text_fallback_or_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mirror_path = root / "mirror.sqlite3"
            outbound_path = root / "outbound.sqlite3"
            connection = sqlite3.connect(outbound_path)
            try:
                connection.execute(
                    """CREATE TABLE hermes_human_outbound(
                       request_id TEXT PRIMARY KEY,
                       telegram_message_id TEXT NOT NULL,
                       forum_chat_id TEXT NOT NULL,
                       telegram_thread_id TEXT NOT NULL,
                       destination TEXT NOT NULL,
                       destination_hash TEXT NOT NULL,
                       payload_sha256 TEXT NOT NULL,
                       text TEXT NOT NULL,
                       media_json TEXT NOT NULL,
                       status TEXT NOT NULL,
                       attempt_count INTEGER NOT NULL DEFAULT 0,
                       next_attempt_at INTEGER NOT NULL DEFAULT 0,
                       remote_message_ids_json TEXT NOT NULL,
                       created_at INTEGER NOT NULL,
                       updated_at INTEGER NOT NULL
                    )"""
                )
                connection.execute(
                    """INSERT INTO hermes_human_outbound VALUES
                       ('req-1','812','-100123','42','wa','hash','payload',
                        'texto','[]','sent',1,0,?,1,1)""",
                    (json.dumps(["wa-1"]),),
                )
                connection.commit()
            finally:
                connection.close()
            transport = _ReactionTransport()
            with MirrorLedger(mirror_path) as mirror:
                mirror.record_outbound_receipt(
                    "wa-1", 3, provider_event="messages.update"
                )
                projector = TelegramReceiptReactionProjector(
                    mirror, outbound_path, transport
                )
                first = projector.apply_once()
                self.assertEqual(1, first.applied)
                self.assertEqual([("-100123", "812", "✅")], transport.calls)

                second = projector.apply_once()
                self.assertEqual(1, second.already_applied)
                self.assertEqual(1, len(transport.calls))

                mirror.record_outbound_receipt(
                    "wa-1", 4, provider_event="messages.update"
                )
                third = projector.apply_once()
                self.assertEqual(1, third.applied)
                self.assertEqual(
                    [("-100123", "812", "✅"), ("-100123", "812", "👀")],
                    transport.calls,
                )


if __name__ == "__main__":
    unittest.main()
