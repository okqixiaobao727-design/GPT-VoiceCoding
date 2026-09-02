"""The seam contracts, checked against the fakes that stand in for adapters.

Two things are asserted. First, conformance: every fake really does satisfy the
Protocol its seam publishes, member for member, with the same parameter names,
kinds and defaults — so an adapter that quietly drops a keyword argument fails CI
rather than at delivery time. Annotations are deliberately not compared; an
adapter may spell a type differently without breaking the contract.

Second, that the contracts are actually usable end to end without a network, an
audio device or a real agent — ADR 0001's fourth principle, demonstrated rather
than asserted.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import fields
from datetime import UTC, datetime
from typing import Any

import pytest

from fakes import (
    HOUSE_RULES,
    FakeAgent,
    FakeCall,
    FakeCompanionChannel,
    RecordingSink,
)
from gpt_voicecoding.seams.agent import (
    AgentAdapter,
    ApprovalRequest,
    ApprovalVerdict,
    ProgressAvailability,
    ProgressEntry,
    ProgressObservation,
    ProgressOmission,
    ProgressRole,
    RelayRoute,
)
from gpt_voicecoding.seams.call import CallAdapter, CallState
from gpt_voicecoding.seams.companion_channel import CompanionChannel, InboundText
from gpt_voicecoding.seams.delivery import Delivery
from gpt_voicecoding.seams.events import EventSink
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget, new_request_id

CODEX = SessionTarget(agent=AgentKind.CODEX, session_id="abc")
CLAUDE = SessionTarget(agent=AgentKind.CLAUDE, session_id="def", pid=100)


def _members(protocol: type) -> list[str]:
    """The verbs a seam publishes.

    Read off the class body rather than a private CPython attribute, so this
    guard cannot silently become vacuous on a future interpreter.
    """
    members = sorted(
        name
        for name, value in vars(protocol).items()
        if not name.startswith("_") and callable(value)
    )
    assert members, f"{protocol.__name__} publishes no verbs — this check would prove nothing"
    return members


def _shape(function: Any) -> list[tuple[str, Any, Any]]:
    """A signature stripped to what a caller depends on."""
    return [
        (parameter.name, parameter.kind, parameter.default)
        for parameter in inspect.signature(function).parameters.values()
    ]


def assert_implements(protocol: type, implementation: object) -> None:
    """Every verb the seam publishes, present and callable the same way."""
    missing = [name for name in _members(protocol) if not hasattr(implementation, name)]
    assert not missing, f"{type(implementation).__name__} is missing {missing}"

    for name in _members(protocol):
        expected = _shape(getattr(protocol, name))
        actual = _shape(getattr(type(implementation), name))
        assert actual == expected, f"{type(implementation).__name__}.{name} does not match the seam"


CONTRACTS = [
    pytest.param(AgentAdapter, FakeAgent(), id="agent"),
    pytest.param(CallAdapter, FakeCall(), id="call"),
    pytest.param(CompanionChannel, FakeCompanionChannel(), id="companion_channel"),
    pytest.param(EventSink, RecordingSink(), id="event_sink"),
]


@pytest.mark.parametrize(("protocol", "implementation"), CONTRACTS)
def test_the_fake_satisfies_its_seam(protocol: type, implementation: object) -> None:
    assert isinstance(implementation, protocol)
    assert_implements(protocol, implementation)


@pytest.mark.parametrize(("protocol", "implementation"), CONTRACTS[:3])
def test_every_pluggable_seam_can_be_asked_what_it_loaded(
    protocol: type, implementation: Any
) -> None:
    """ADR 0003 generalises past the Companion Channel: `verify` is on every seam."""
    assert "verify" in _members(protocol)
    assert asyncio.run(implementation.verify()).loaded


class TestTheAgentContract:
    def test_progress_observation_names_every_availability_and_omission(self) -> None:
        assert set(ProgressAvailability) == {
            ProgressAvailability.NOT_READ,
            ProgressAvailability.UNREADABLE,
            ProgressAvailability.READABLE,
        }
        assert set(ProgressOmission) == {
            ProgressOmission.NONE,
            ProgressOmission.OLDER,
            ProgressOmission.STATUS_SUMMARY,
            ProgressOmission.NEWEST_OVERSIZE,
            ProgressOmission.OVERSIZE,
        }

    def test_one_entrys_omission_is_never_a_whole_readings(self) -> None:
        """`oversize` names one History page entry; a reading cannot wear it (#171)."""
        with pytest.raises(ValueError, match="one History page entry"):
            ProgressObservation.readable(
                has_history=True,
                read_at=datetime(2026, 9, 1, tzinfo=UTC),
                omission=ProgressOmission.OVERSIZE,
            )

    def test_readable_empty_history_is_the_only_nothing_said_state(self) -> None:
        read_at = datetime(2026, 8, 30, tzinfo=UTC)

        observed = ProgressObservation.readable(
            has_history=False,
            read_at=read_at,
        )

        assert observed.availability is ProgressAvailability.READABLE
        assert observed.has_history is False
        assert observed.recent == ()
        assert observed.omission is ProgressOmission.NONE
        assert observed.read_at == read_at

    def test_history_cannot_disappear_behind_an_empty_unomitted_tail(self) -> None:
        with pytest.raises(ValueError, match="history exists"):
            ProgressObservation.readable(
                has_history=True,
                read_at=datetime(2026, 8, 30, tzinfo=UTC),
            )

    @pytest.mark.parametrize(
        "change",
        [
            {"availability": "readable"},
            {"omission": "none"},
            {"has_history": "yes"},
            {"read_at": "now"},
        ],
    )
    def test_progress_observation_refuses_untyped_vocabulary(
        self, change: dict[str, object]
    ) -> None:
        values: dict[str, object] = {
            "availability": ProgressAvailability.READABLE,
            "has_history": False,
            "omission": ProgressOmission.NONE,
            "read_at": datetime(2026, 8, 30, tzinfo=UTC),
        }
        values.update(change)

        with pytest.raises(ValueError):
            ProgressObservation(**values)  # type: ignore[arg-type]

    def test_an_unreadable_observation_carries_only_its_source_reason(self) -> None:
        observed = ProgressObservation.unreadable("the transcript could not be opened")

        assert observed.availability is ProgressAvailability.UNREADABLE
        assert observed.reason == "the transcript could not be opened"
        assert observed.has_history is None
        assert observed.recent == ()

    def test_a_readable_tail_keeps_roles_order_and_whole_text(self) -> None:
        read_at = datetime(2026, 8, 30, tzinfo=UTC)
        entries = (
            ProgressEntry(ordinal=0, role=ProgressRole.USER, text="do the thing"),
            ProgressEntry(ordinal=0, role=ProgressRole.ASSISTANT, text="done"),
        )

        observed = ProgressObservation.readable(
            has_history=True,
            recent=entries,
            read_at=read_at,
        )

        assert observed.recent == entries

    @pytest.mark.parametrize(
        ("omission", "has_history"),
        [
            (ProgressOmission.NONE, False),
            (ProgressOmission.NEWEST_OVERSIZE, True),
        ],
    )
    def test_one_factory_derives_history_presence_from_a_source_capture(
        self,
        omission: ProgressOmission,
        has_history: bool,
    ) -> None:
        observed = ProgressObservation.from_capture(
            recent=(),
            omission=omission,
            read_at=datetime(2026, 8, 30, tzinfo=UTC),
        )

        assert observed.has_history is has_history

    def test_a_relay_returns_a_receipt_in_the_four_state_vocabulary(self) -> None:
        agent = FakeAgent()
        receipt = asyncio.run(agent.answer_relay(CODEX, "ship it", request_id=new_request_id()))
        assert receipt.outcome in set(Delivery)
        assert receipt.is_delivered is True

    def test_an_adapter_without_the_supplement_route_says_so_and_does_nothing_else(
        self,
    ) -> None:
        agent = FakeAgent(routes=frozenset({RelayRoute.DELIVER}))
        receipt = asyncio.run(
            agent.answer_relay(
                CODEX, "one more thing", request_id=new_request_id(), route=RelayRoute.SUPPLEMENT
            )
        )
        assert receipt.is_delivered is False
        assert "not available" in receipt.reason

    def test_an_adapter_with_both_routes_takes_either(self) -> None:
        agent = FakeAgent(routes=frozenset({RelayRoute.DELIVER, RelayRoute.SUPPLEMENT}))
        receipt = asyncio.run(
            agent.answer_relay(
                CODEX, "one more thing", request_id=new_request_id(), route=RelayRoute.SUPPLEMENT
            )
        )
        assert receipt.is_delivered is True

    def test_a_held_relay_is_never_reported_as_delivered(self) -> None:
        agent = FakeAgent(outcome=Delivery.HELD, reason="parked in front of the human")
        receipt = asyncio.run(
            agent.answer_relay(CLAUDE, "keep this waiting", request_id=new_request_id())
        )
        assert receipt.is_delivered is False

    def test_an_approval_verdict_is_carried_not_decided(self) -> None:
        agent = FakeAgent()
        request = ApprovalRequest(
            approval_id="dialog-1", target=CLAUDE, tool_name="Bash", detail="rm -rf build"
        )
        asyncio.run(
            agent.approval_relay(request, ApprovalVerdict.ALLOW, request_id=new_request_id())
        )
        assert agent.calls[0].verdict is ApprovalVerdict.ALLOW

    def test_a_budget_expiry_has_a_verdict_that_is_not_deny(self) -> None:
        assert ApprovalVerdict.ASK in set(ApprovalVerdict)

    def test_a_fake_holds_no_question_hook_unless_a_test_explicitly_scripts_one(self) -> None:
        agent = FakeAgent()

        assert agent.question_answerable(CLAUDE) is False
        assert asyncio.run(agent.sweep_question_budget(600.0)) == ()


class TestTheCallContract:
    def test_ensuring_a_call_twice_returns_the_same_call(self) -> None:
        call = FakeCall()
        first = asyncio.run(call.ensure_call(HOUSE_RULES))
        second = asyncio.run(call.ensure_call(HOUSE_RULES))
        assert first == second
        assert first.state is CallState.UP

    def test_speaking_with_no_call_up_fails_closed(self) -> None:
        call = FakeCall()
        receipt = asyncio.run(call.speak("you are needed", request_id=new_request_id()))
        assert receipt.is_delivered is False

    def test_ending_a_call_is_idempotent(self) -> None:
        call = FakeCall()
        asyncio.run(call.ensure_call(HOUSE_RULES))
        asyncio.run(call.end_call())
        assert asyncio.run(call.end_call()).state is CallState.DOWN

    def test_the_delegated_turns_model_is_chosen_by_the_caller(self) -> None:
        """The cost lever is a user-facing setting; the seam has no default to override."""
        call = FakeCall()
        reply = asyncio.run(
            call.delegate(
                "summarise the diff",
                model="claude-sonnet-5",
                instructions=HOUSE_RULES,
                request_id=new_request_id(),
            )
        )
        assert reply.model == "claude-sonnet-5"
        assert call.delegated == [("summarise the diff", "claude-sonnet-5")]

    def test_delegate_has_no_default_model(self) -> None:
        parameters = inspect.signature(CallAdapter.delegate).parameters
        assert parameters["model"].default is inspect.Parameter.empty

    def test_instructions_arrive_at_the_call_site(self) -> None:
        """Bridge Core generates them; nothing installs them into an adapter first.

        Both verbs that start a thread take them, and neither has a default —
        an adapter that could fall back to instructions of its own would be a
        second source for the one thing the hub is the only source of.
        """
        for verb in (CallAdapter.ensure_call, CallAdapter.delegate):
            parameters = inspect.signature(verb).parameters
            assert parameters["instructions"].default is inspect.Parameter.empty

    def test_the_house_rules_a_call_opened_on_came_from_the_caller(self) -> None:
        call = FakeCall()
        asyncio.run(call.ensure_call(HOUSE_RULES))
        assert call.opened_on == [HOUSE_RULES]


class TestTheCompanionChannelContract:
    def test_inbound_text_arrives_unclassified(self) -> None:
        """The channel never decides whether this is a command, an answer or a delegation."""
        event = InboundText(text="turn duty off", origin="chat:1")
        assert {field.name for field in fields(InboundText)} == {"text", "origin"}
        assert event.text == "turn duty off"

    def test_a_failed_push_is_never_mistaken_for_delivery(self) -> None:
        channel = FakeCompanionChannel(outcome=Delivery.FAILED, reason="network down mid-send")
        receipt = asyncio.run(channel.send("you are needed", request_id=new_request_id()))
        assert receipt.is_delivered is False
        assert receipt.reason
