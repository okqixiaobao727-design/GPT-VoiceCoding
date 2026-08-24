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
from pathlib import Path
from typing import Any

import pytest

from fakes import (
    HOUSE_RULES,
    FakeAgent,
    FakeCall,
    FakeCompanionChannel,
    FakeSessionLauncher,
    RecordingSink,
)
from gpt_voicecoding.seams.agent import (
    AgentAdapter,
    ApprovalRequest,
    ApprovalVerdict,
    RelayRoute,
)
from gpt_voicecoding.seams.call import CallAdapter, CallState
from gpt_voicecoding.seams.companion_channel import CompanionChannel, InboundText
from gpt_voicecoding.seams.delivery import Delivery
from gpt_voicecoding.seams.events import EventSink
from gpt_voicecoding.seams.identity import (
    AgentKind,
    SessionLabel,
    SessionTarget,
    new_request_id,
)
from gpt_voicecoding.seams.session_launcher import (
    CloseRequest,
    CloseStatus,
    LaunchRequest,
    LaunchStatus,
    SessionLauncher,
)

CODEX = SessionTarget(agent=AgentKind.CODEX, session_id="abc")
CLAUDE = SessionTarget(agent=AgentKind.CLAUDE, session_id="def", pid=100)
WORKSPACE = Path(__file__).resolve().parents[1]


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
    pytest.param(SessionLauncher, FakeSessionLauncher(), id="session_launcher"),
    pytest.param(EventSink, RecordingSink(), id="event_sink"),
]


@pytest.mark.parametrize(("protocol", "implementation"), CONTRACTS)
def test_the_fake_satisfies_its_seam(protocol: type, implementation: object) -> None:
    assert isinstance(implementation, protocol)
    assert_implements(protocol, implementation)


@pytest.mark.parametrize(("protocol", "implementation"), CONTRACTS[:4])
def test_every_pluggable_seam_can_be_asked_what_it_loaded(
    protocol: type, implementation: Any
) -> None:
    """ADR 0003 generalises past the Companion Channel: `verify` is on every seam."""
    assert "verify" in _members(protocol)
    assert asyncio.run(implementation.verify()).loaded


class TestTheAgentContract:
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


class TestTheSessionLauncherContract:
    def test_a_launch_returns_the_exact_identity_core_will_register(self) -> None:
        launcher = FakeSessionLauncher(targets=[CLAUDE])
        outcome = asyncio.run(launcher.launch(self.request()))
        assert outcome.status is LaunchStatus.LAUNCHED
        assert outcome.target == CLAUDE

    def test_repeating_a_launch_request_id_yields_one_child_and_one_outcome(self) -> None:
        launcher = FakeSessionLauncher(targets=[CLAUDE, CODEX])
        request = self.request()
        first = asyncio.run(launcher.launch(request))
        second = asyncio.run(launcher.launch(request))
        assert first == second
        assert launcher.targets == [CODEX]

    def test_a_failed_launch_registers_nothing_and_carries_the_real_error(self) -> None:
        launcher = FakeSessionLauncher(targets=[])
        outcome = asyncio.run(launcher.launch(self.request()))
        assert outcome.status is LaunchStatus.FAILED
        assert outcome.target is None
        assert outcome.detail

    def test_an_unavailable_launcher_is_not_a_failed_launch(self) -> None:
        launcher = FakeSessionLauncher(targets=[CLAUDE], available=False)
        assert asyncio.run(launcher.launch(self.request())).status is LaunchStatus.UNAVAILABLE

    def test_the_launcher_is_handed_exactly_the_environment_it_should_set(self) -> None:
        launcher = FakeSessionLauncher(targets=[CLAUDE])
        asyncio.run(launcher.launch(self.request(env={"CLAUDE_BG_BACKEND": "daemon"})))
        assert launcher.environments == [{"CLAUDE_BG_BACKEND": "daemon"}]

    def test_closing_twice_is_idempotent_and_says_which_it_was(self) -> None:
        launcher = FakeSessionLauncher(targets=[CLAUDE])
        asyncio.run(launcher.launch(self.request()))

        first = asyncio.run(
            launcher.close(CloseRequest(request_id=new_request_id(), target=CLAUDE))
        )
        second = asyncio.run(
            launcher.close(CloseRequest(request_id=new_request_id(), target=CLAUDE))
        )
        assert first.status is CloseStatus.CLOSED
        assert second.status is CloseStatus.ALREADY_CLOSED

    def test_closing_an_identity_that_was_never_launched_fails_closed(self) -> None:
        """Locked semantics: fail closed on a missing or stale identity."""
        launcher = FakeSessionLauncher(targets=[CLAUDE])
        outcome = asyncio.run(
            launcher.close(CloseRequest(request_id=new_request_id(), target=CODEX))
        )
        assert outcome.status is CloseStatus.FAILED
        assert outcome.detail
        assert CODEX not in launcher.closed

    def test_a_close_request_carries_a_target_and_never_a_label(self) -> None:
        assert "label" not in inspect.signature(CloseRequest).parameters

    @staticmethod
    def request(env: dict[str, str] | None = None) -> LaunchRequest:
        return LaunchRequest(
            request_id=new_request_id(),
            agent=AgentKind.CLAUDE,
            workspace=WORKSPACE,
            label=SessionLabel("GPT-VoiceCoding", "a task"),
            env=env or {},
        )
