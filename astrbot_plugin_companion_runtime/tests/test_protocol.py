"""Protocol-level tests: parsing, serialization, and defensive behaviour."""

from __future__ import annotations

import unittest

from companion_runtime.protocol import (
    ACTION_RENDER,
    ACTION_SEND,
    PROTOCOL_VERSION,
    STATUS_OK,
    ActionReport,
    AuthorizeDecision,
    AuthorizeRequest,
    ContextRequest,
    ContextSnapshot,
    EventEnvelope,
    EventRecord,
    LeaseRequest,
    LeasedAction,
    truncate_error,
)


class EventRecordTests(unittest.TestCase):
    def test_generates_identity_and_timestamp(self) -> None:
        record = EventRecord(kind="user_message", session="webchat:FriendMessage:u1")
        self.assertTrue(record.event_id.startswith("evt_"))
        self.assertTrue(record.occurred_at.endswith("Z"))

    def test_optional_extra_is_omitted_when_empty(self) -> None:
        record = EventRecord(kind="user_message", session="s")
        self.assertNotIn("extra", record.to_wire())

    def test_envelope_carries_protocol_version(self) -> None:
        envelope = EventEnvelope(
            adapter_id="default",
            events=[EventRecord(kind="user_message", session="s", text="hi")],
        )
        body = envelope.to_wire()
        self.assertEqual(body["protocol_version"], PROTOCOL_VERSION)
        self.assertEqual(body["adapter_id"], "default")
        self.assertEqual(len(body["events"]), 1)
        self.assertEqual(body["events"][0]["text"], "hi")
        self.assertIn("sent_at", body)


class ContextSnapshotTests(unittest.TestCase):
    def test_prefers_runtime_authored_text(self) -> None:
        snapshot = ContextSnapshot.from_wire({"text": "  【当前心理状态】\n平静  "})
        assert snapshot is not None
        self.assertEqual(snapshot.render(), "【当前心理状态】\n平静")

    def test_assembles_sections_when_no_text(self) -> None:
        snapshot = ContextSnapshot.from_wire(
            {"version": "42", "sections": {"当前最终意图": "想问面试结果", "  ": "ignored"}},
        )
        assert snapshot is not None
        self.assertEqual(snapshot.version, "42")
        self.assertEqual(snapshot.render(), "【当前最终意图】\n想问面试结果")

    def test_rejects_non_mapping(self) -> None:
        self.assertIsNone(ContextSnapshot.from_wire(["nope"]))
        self.assertIsNone(ContextSnapshot.from_wire(None))

    def test_empty_detection(self) -> None:
        snapshot = ContextSnapshot.from_wire({})
        assert snapshot is not None
        self.assertTrue(snapshot.is_empty())
        self.assertEqual(snapshot.render(), "")

    def test_coerces_string_ttl(self) -> None:
        snapshot = ContextSnapshot.from_wire({"text": "x", "ttl_ms": "1500"})
        assert snapshot is not None
        self.assertEqual(snapshot.ttl_ms, 1500)


class LeasedActionTests(unittest.TestCase):
    def test_parses_a_well_formed_action(self) -> None:
        action = LeasedAction.from_wire(
            {
                "action_id": "act_1",
                "action_type": "RENDER",
                "lease_id": "lease_9",
                "session": "webchat:FriendMessage:u1",
                "attempt_id": "att_2",
                "lease_ttl_ms": "30000",
                "payload": {"prompt": "hi"},
            },
        )
        assert action is not None
        self.assertEqual(action.action_type, ACTION_RENDER)
        self.assertEqual(action.lease_ttl_ms, 30000)
        self.assertEqual(action.payload, {"prompt": "hi"})
        self.assertEqual(action.key, ("act_1", "att_2"))

    def test_missing_identifiers_are_rejected(self) -> None:
        self.assertIsNone(LeasedAction.from_wire({"action_type": ACTION_SEND}))
        self.assertIsNone(LeasedAction.from_wire({"action_id": "act_1"}))
        self.assertIsNone(LeasedAction.from_wire("nope"))

    def test_unknown_action_type_is_kept_for_explicit_reporting(self) -> None:
        action = LeasedAction.from_wire(
            {"action_id": "a", "lease_id": "l", "action_type": "teleport"},
        )
        assert action is not None
        self.assertEqual(action.action_type, "teleport")

    def test_key_falls_back_to_lease_id(self) -> None:
        action = LeasedAction.from_wire({"action_id": "a", "lease_id": "l"})
        assert action is not None
        self.assertEqual(action.key, ("a", "l"))

    def test_non_mapping_payload_is_replaced(self) -> None:
        action = LeasedAction.from_wire(
            {"action_id": "a", "lease_id": "l", "payload": ["not", "a", "mapping"]},
        )
        assert action is not None
        self.assertEqual(action.payload, {})


class RequestSerializationTests(unittest.TestCase):
    def test_context_request_omits_absent_event_id(self) -> None:
        body = ContextRequest(adapter_id="default", session="s").to_wire()
        self.assertNotIn("last_event_id", body)
        self.assertEqual(body["protocol_version"], PROTOCOL_VERSION)

    def test_context_request_includes_known_event_id(self) -> None:
        body = ContextRequest(
            adapter_id="default",
            session="s",
            last_event_id="evt_1",
        ).to_wire()
        self.assertEqual(body["last_event_id"], "evt_1")

    def test_lease_request_clamps_ttl_and_omits_empty_sessions(self) -> None:
        body = LeaseRequest(adapter_id="default", max_actions=0, lease_ttl_ms=10).to_wire()
        self.assertEqual(body["max_actions"], 1)
        self.assertEqual(body["lease_ttl_ms"], 1000)
        self.assertNotIn("sessions", body)
        self.assertEqual(body["capabilities"], [ACTION_RENDER, ACTION_SEND])

    def test_authorize_request_truncates_preview(self) -> None:
        body = AuthorizeRequest(
            adapter_id="default",
            action_id="a",
            lease_id="l",
            session="s",
            text_preview="x" * 500,
        ).to_wire()
        self.assertEqual(len(body["text_preview"]), 160)

    def test_authorize_decision_denies_on_malformed_input(self) -> None:
        self.assertFalse(AuthorizeDecision.from_wire("nope").authorized)
        self.assertEqual(
            AuthorizeDecision.from_wire(None).reason,
            "malformed_authorize_response",
        )

    def test_authorize_decision_accepts_amended_text(self) -> None:
        decision = AuthorizeDecision.from_wire(
            {"authorized": "true", "text": "改口后的措辞", "reason": "rerender"},
        )
        self.assertTrue(decision.authorized)
        self.assertEqual(decision.text, "改口后的措辞")
        self.assertEqual(decision.reason, "rerender")


class ActionReportTests(unittest.TestCase):
    def test_serializes_result_and_truncates_errors(self) -> None:
        report = ActionReport(
            adapter_id="default",
            action_id="act_1",
            lease_id="lease_1",
            status=STATUS_OK,
            action_type=ACTION_SEND,
            result={"sent": True, "chars": 3},
            error=RuntimeError("x" * 800),
        )
        body = report.to_wire()
        self.assertEqual(body["status"], STATUS_OK)
        self.assertEqual(body["result"], {"sent": True, "chars": 3})
        self.assertLessEqual(len(body["error"]), 400)
        self.assertNotIn("action_id", body)

    def test_empty_result_and_error_are_omitted(self) -> None:
        report = ActionReport(
            adapter_id="default",
            action_id="act_1",
            lease_id="lease_1",
            status=STATUS_OK,
        )
        body = report.to_wire()
        self.assertNotIn("result", body)
        self.assertNotIn("error", body)

    def test_summary_is_single_line(self) -> None:
        report = ActionReport(
            adapter_id="default",
            action_id="act_1",
            lease_id="l",
            status="failed",
            action_type=ACTION_RENDER,
            error="boom",
        )
        self.assertEqual(report.summary(), "render:act_1 -> failed (boom)")


class TruncateErrorTests(unittest.TestCase):
    def test_describes_exception_without_traceback(self) -> None:
        text = truncate_error(ValueError("bad\nvalue"))
        self.assertEqual(text, "ValueError: bad value")

    def test_accepts_plain_strings(self) -> None:
        self.assertEqual(truncate_error("plain"), "plain")

    def test_truncates_long_messages(self) -> None:
        text = truncate_error("y" * 1000, limit=10)
        self.assertEqual(len(text), 10)
        self.assertTrue(text.endswith("…"))


if __name__ == "__main__":
    unittest.main()
