"""The wire vocabulary: closed, and the same shape in both directions.

These are the assertions the Swift shell will implement against, so they are
about the *contract* — the action set, the error set, and the fact that a
request or reply survives the round trip through a plain JSON document — never
about sockets, which are `control_plane/`'s business.
"""

from __future__ import annotations

import json

import pytest

from gpt_voicecoding.seams.control_plane import (
    MAX_REQUEST_BYTES,
    PROTOCOL_VERSION,
    Action,
    ErrorCode,
    MalformedRequest,
    Reply,
    Request,
)


class TestTheActionSet:
    def test_the_action_set_is_exactly_these(self) -> None:
        assert {str(action) for action in Action} == {
            "status",
            "switch",
            "brief",
            "history",
            "live",
            "relay",
            "approve",
            "verify",
        }

    def test_the_history_page_replacing_exact_progress_moves_the_protocol_to_seven(
        self,
    ) -> None:
        """A v6 surface would send `progress` to an engine that has no such action.

        The Swift shell compares this number and nothing else, so an action set
        that changed under an unchanged number is a gate that lies (#171).
        """
        assert PROTOCOL_VERSION == 7

    def test_the_retired_exact_progress_verb_is_not_an_action_this_engine_has(self) -> None:
        """Retired with the History page, and its absence asserted, not assumed."""
        assert "progress" not in {str(action) for action in Action}

    def test_the_retired_roster_verb_is_not_an_action_this_engine_has(self) -> None:
        """Retired with the Briefing verb, and its absence asserted, not assumed."""
        assert "sessions" not in {str(action) for action in Action}

    def test_launching_and_closing_are_not_actions_this_engine_has(self) -> None:
        """Parked with the launcher (#72), and their absence is asserted, not assumed.

        An action that quietly came back would be a wire this engine answers on
        with nothing behind it. A surface still sending either gets
        `unknown_action`, which is the honest answer and the reason the protocol
        version moved with them.
        """
        assert {"launch", "close"} & {str(action) for action in Action} == set()
        assert {"launch_failed", "close_failed"} & {str(code) for code in ErrorCode} == set()

    def test_no_legacy_alias_survives(self) -> None:
        """The old CLI carried both a legacy and a current Stop command."""
        assert "duty_toggle" not in {str(action) for action in Action}
        assert "overview" not in {str(action) for action in Action}


class TestARequestOnTheWire:
    def test_survives_the_round_trip(self) -> None:
        request = Request(action=Action.SWITCH, payload={"name": "duty", "on": True})
        assert Request.of(json.loads(json.dumps(request.as_document()))) == request

    def test_an_unknown_action_is_refused_rather_than_carried(self) -> None:
        with pytest.raises(MalformedRequest) as refusal:
            Request.of({"action": "duty_toggle"})
        assert refusal.value.code is ErrorCode.UNKNOWN_ACTION

    def test_a_document_that_is_not_an_object_is_refused(self) -> None:
        with pytest.raises(MalformedRequest) as refusal:
            Request.of(["status"])
        assert refusal.value.code is ErrorCode.MALFORMED_REQUEST

    def test_a_payload_that_is_not_an_object_is_refused(self) -> None:
        with pytest.raises(MalformedRequest) as refusal:
            Request.of({"action": "status", "payload": "duty"})
        assert refusal.value.code is ErrorCode.MALFORMED_REQUEST

    def test_a_missing_payload_is_an_empty_one(self) -> None:
        assert Request.of({"action": "status"}).payload == {}


class TestAReplyOnTheWire:
    def test_the_current_protocol_includes_the_version_four_action_removal(self) -> None:
        """Version 4 removed launch/close; every version since retains that."""
        assert PROTOCOL_VERSION >= 4
        assert {"launch", "close"} & {str(action) for action in Action} == set()

    def test_an_answer_carries_the_action_it_answers_and_the_protocol_version(self) -> None:
        document = Reply.answered(Action.STATUS, {"call_id": None}).as_document()
        assert document["ok"] is True
        assert document["action"] == "status"
        assert document["protocol"] == PROTOCOL_VERSION
        assert document["data"] == {"call_id": None}

    def test_a_refusal_carries_the_code_and_the_refusals_own_words(self) -> None:
        document = Reply.refused(
            Action.SWITCH, ErrorCode.UNKNOWN_SWITCH, "unknown switch: 'sound'"
        ).as_document()
        assert document["ok"] is False
        assert document["error"] == {
            "code": "unknown_switch",
            "message": "unknown switch: 'sound'",
        }
        assert "data" not in document

    def test_a_refusal_may_answer_no_action_at_all(self) -> None:
        """A line that was never valid JSON names no action, and says so."""
        document = Reply.refused(None, ErrorCode.MALFORMED_REQUEST, "not JSON").as_document()
        assert document["action"] is None

    def test_a_refusal_must_carry_words_to_render(self) -> None:
        with pytest.raises(ValueError):
            Reply.refused(Action.STATUS, ErrorCode.REFUSED, "   ")

    def test_a_reply_survives_the_round_trip(self) -> None:
        reply = Reply.refused(Action.RELAY, ErrorCode.STALE_SESSION, "that Session is ended")
        assert Reply.of(json.loads(json.dumps(reply.as_document()))) == reply


class TestTheBound:
    def test_the_request_bound_is_stated_on_the_contract(self) -> None:
        """Both sides must agree on it, so neither side may invent it."""
        assert MAX_REQUEST_BYTES > 0
