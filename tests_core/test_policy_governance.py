from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from espelho_zap.ledger import LedgerError, MirrorLedger  # noqa: E402
from espelho_zap.models import InboundEvent, Route, opaque_ref
from espelho_zap.policy import (
    GovernanceError,
    GroupAdmission,
    GroupGrill,
    HumanCanary,
    ReceiptState,
    advance_receipt,
    installation_state,
    validate_participant_label,
)
from espelho_zap.routing import telegram_text  # noqa: E402


PROFILE = opaque_ref("profile", "test")
CONVERSATION = opaque_ref("conversation", "group")
ACTOR = opaque_ref("actor", "person")


def grill() -> GroupGrill:
    return GroupGrill.from_mapping(
        {
            "agent_name": "QAAgent",
            "mission": "Apoiar o QA",
            "audience": "Gestores",
            "authoritative_sources": "Documentacao aprovada",
            "activation_triggers": "Mencao explicita",
            "allowed_actions": "Responder no grupo",
            "forbidden_actions": "Nao aprovar sozinha",
            "approval_and_escalation": "Escalar ao gestor",
            "tone_and_sla": "Objetivo, ate um dia util",
            "acceptance_examples": "Com mencao responde; sem mencao observa",
        }
    )


class GovernancePolicyTest(unittest.TestCase):
    def test_group_agent_requires_complete_grill(self) -> None:
        with self.assertRaisesRegex(GovernanceError, "group_grill_required"):
            GroupAdmission(
                CONVERSATION, PROFILE, "-1001", "22", "area_shared", True,
                agent_mode="mention_only",
            )
        self.assertEqual("QAAgent", grill().agent_name)

    def test_raw_identifiers_are_not_participant_labels(self) -> None:
        with self.assertRaisesRegex(GovernanceError, "identity_unsafe"):
            validate_participant_label("@119799707365464")
        self.assertEqual("Maria Silva", validate_participant_label(" Maria   Silva "))

    def test_receipts_are_monotonic(self) -> None:
        self.assertEqual(ReceiptState.DELIVERED, advance_receipt("sent", 3))
        self.assertEqual(ReceiptState.READ, advance_receipt("read", "delivered"))

    def test_ledger_receipt_never_downgrades(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with MirrorLedger(Path(temporary) / "mirror.sqlite3") as ledger:
                self.assertEqual(
                    4,
                    ledger.record_outbound_receipt(
                        "outbound:one", 4, provider_event="messages.update"
                    ),
                )
                self.assertEqual(
                    4,
                    ledger.record_outbound_receipt(
                        "outbound:one", 3,
                        provider_event="message-receipt.update",
                    ),
                )

    def test_group_render_has_human_label_not_raw_identifier(self) -> None:
        event = InboundEvent(
            "evt", "whatsapp", CONVERSATION, "2026-08-05T00:00:00Z", ACTOR,
            source_profile_id=PROFILE, text="Bom dia", conversation_kind="group",
            actor_display_label="Maria Silva",
        )
        self.assertEqual("👤 Maria Silva\n\nBom dia", telegram_text(event))

    def test_manual_group_identity_survives_event_without_label(self) -> None:
        event = InboundEvent(
            "evt", "whatsapp", CONVERSATION, "2026-08-05T00:00:00Z", ACTOR,
            source_profile_id=PROFILE, text="Bom dia", conversation_kind="group",
        )
        with tempfile.TemporaryDirectory() as temporary:
            with MirrorLedger(Path(temporary) / "mirror.sqlite3") as ledger:
                ledger.set_route(Route(CONVERSATION, "-1001", "22"))
                ledger.approve_group(CONVERSATION, PROFILE)
                ledger.set_participant_identity(
                    PROFILE, CONVERSATION, ACTOR, "Maria Silva", label_source="manual"
                )
                ledger.authorize_event(event)
                self.assertEqual("Maria Silva", ledger.participant_label(event))

    def test_installation_requires_real_bidirectional_media_matrix(self) -> None:
        flags = dict(
            exact_route=True, single_delivery=True, no_dm_fallback=True,
            integrity_ok=True, no_enrichment=True, human_confirmed=True,
        )
        canaries = tuple(
            HumanCanary(direction, kind, **flags)
            for direction in ("inbound", "outbound")
            for kind in ("text", "image", "audio")
        )
        self.assertEqual("prepared", installation_state(canaries[:-1]))
        self.assertEqual("installed_success", installation_state(canaries))

    def test_unapproved_group_is_blocked_before_event_persistence(self) -> None:
        event = InboundEvent(
            "evt", "whatsapp", CONVERSATION, "2026-08-05T00:00:00Z", ACTOR,
            source_profile_id=PROFILE, text="teste", conversation_kind="group",
            actor_display_label="Maria Silva",
        )
        with tempfile.TemporaryDirectory() as temporary:
            with MirrorLedger(Path(temporary) / "mirror.sqlite3") as ledger:
                with self.assertRaisesRegex(LedgerError, "group_not_approved"):
                    ledger.authorize_event(event)
                self.assertIsNone(
                    ledger.connection.execute(
                        "SELECT 1 FROM mirror_events WHERE event_id=?", (event.event_id,)
                    ).fetchone()
                )
                row = ledger.connection.execute(
                    "SELECT blocked_count FROM mirror_admission_blocks"
                ).fetchone()
                self.assertEqual(1, int(row["blocked_count"]))

    def test_approved_group_has_exact_route_and_identity(self) -> None:
        event = InboundEvent(
            "evt", "whatsapp", CONVERSATION, "2026-08-05T00:00:00Z", ACTOR,
            source_profile_id=PROFILE, text="teste", conversation_kind="group",
            actor_display_label="Maria Silva",
        )
        with tempfile.TemporaryDirectory() as temporary:
            with MirrorLedger(Path(temporary) / "mirror.sqlite3") as ledger:
                ledger.set_route(Route(CONVERSATION, "-1001", "22"))
                ledger.approve_group(CONVERSATION, PROFILE)
                ledger.authorize_event(event)
                self.assertEqual("Maria Silva", ledger.participant_label(event))


if __name__ == "__main__":
    unittest.main()
