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
from typing import Any, get_args

import pytest

from fakes import (
    CALL_AGENT_INSTRUCTIONS,
    FakeAgent,
    FakeCall,
    FakeCompanionChannel,
    RecordingSink,
)
from fakes import (
    dial as _dial,
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
from gpt_voicecoding.seams.call import (
    CODEX_BYTES_PER_TOKEN,
    HANDOVER_BUDGET_BYTES,
    MAX_HANDOVER_ITEMS,
    WIRE_INITIAL_ITEMS_TOKEN_CAP,
    WIRE_LINE_OVERHEAD_BYTES,
    CallAdapter,
    CallDropped,
    CallEnded,
    CallEvent,
    CallStarted,
    CallState,
    Cue,
    Dial,
    DialReason,
    SpokenBrief,
    UserSpeaking,
    UserSpeech,
    VoiceSpeech,
)
from gpt_voicecoding.seams.companion_channel import CompanionChannel, InboundText
from gpt_voicecoding.seams.delivery import Delivery
from gpt_voicecoding.seams.events import EventSink
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget, new_request_id


def _brief(**overrides: object) -> SpokenBrief:
    """One `SpokenBrief`, filled the way Briefing fills it — words, not values."""
    fields: dict[str, Any] = {
        "name": "repo · task",
        "agent": "claude",
        "state": "waiting for your decision",
        "newest": "it said something",
        "decision": ("asked: which one?",),
        "answerable_here": "from here",
        "last_activity_at": "not read",
    }
    fields.update(overrides)
    return SpokenBrief(**fields)  # type: ignore[arg-type]


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

    def test_handing_a_dialog_back_has_a_verdict_that_is_not_deny(self) -> None:
        """`ask` is the one verdict said by saying nothing, and it stays."""
        assert ApprovalVerdict.ASK in set(ApprovalVerdict)

    def test_a_fake_holds_no_question_hook_unless_a_test_explicitly_scripts_one(self) -> None:
        agent = FakeAgent()

        assert agent.question_answerable(CLAUDE) is False

    def test_the_seam_asks_no_adapter_to_keep_a_clock(self) -> None:
        """The wire bounds a held hook, so no verb hands an adapter a budget (#191)."""
        assert not hasattr(FakeAgent(), "sweep_question_budget")
        assert not hasattr(AgentAdapter, "sweep_question_budget")


class TestTheCallContract:
    def test_ensuring_a_call_twice_returns_the_same_call(self) -> None:
        call = FakeCall()
        first = asyncio.run(call.ensure_call(_dial()))
        second = asyncio.run(call.ensure_call(_dial()))
        assert first == second
        assert first.state is CallState.UP

    def test_speaking_with_no_call_up_fails_closed(self) -> None:
        call = FakeCall()
        receipt = asyncio.run(call.speak(_brief(), request_id=new_request_id()))
        assert receipt.is_delivered is False

    def test_ending_a_call_is_idempotent(self) -> None:
        call = FakeCall()
        asyncio.run(call.ensure_call(_dial()))
        asyncio.run(call.end_call())
        assert asyncio.run(call.end_call()).state is CallState.DOWN

    def test_the_delegated_turns_model_is_chosen_by_the_caller(self) -> None:
        """The cost lever is a user-facing setting; the seam has no default to override."""
        call = FakeCall()
        reply = asyncio.run(
            call.delegate(
                "summarise the diff",
                model="claude-sonnet-5",
                instructions=CALL_AGENT_INSTRUCTIONS,
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
        second source for the one thing the hub is the only source of. The two
        verbs no longer take them under one name: `ensure_call` takes a `Dial`,
        because a call addresses two audiences and a Delegated Turn addresses
        one (ADR 0018). What is asserted is unchanged — the payload is the
        caller's, and there is no default to fall back on.
        """
        assert (
            inspect.signature(CallAdapter.ensure_call).parameters["dial"].default
            is inspect.Parameter.empty
        )
        assert (
            inspect.signature(CallAdapter.delegate).parameters["instructions"].default
            is inspect.Parameter.empty
        )

    def test_the_house_rules_a_call_opened_on_came_from_the_caller(self) -> None:
        call = FakeCall()
        asyncio.run(call.ensure_call(_dial()))
        assert [dialled.agent for dialled in call.opened_on] == [CALL_AGENT_INSTRUCTIONS]

    def test_both_sides_speaking_states_are_events_the_seam_raises(self) -> None:
        """Each side of the conversation crosses the seam as a state (#184, #195).

        A state rather than a tick, because the two readers want different
        questions answered from it: the Silence Ceiling asks "was there
        activity", and it also has to *hold* while an utterance is still being
        spoken — a span, which no bare edge describes.

        **Two of them, one per side.** The user's half used to arrive only as
        the finished `UserSpeech(text)`, which since #194 often lands at hand-off
        or teardown — so a user who talked for a whole ceiling without the Voice
        answering was judged silent (#195). The transcript stays: it is what the
        engine writes down, and a span carries no words.
        """
        assert set(get_args(CallEvent)) == {
            UserSpeech,
            UserSpeaking,
            VoiceSpeech,
            CallStarted,
            CallEnded,
            CallDropped,
        }
        assert {field.name for field in fields(VoiceSpeech)} == {"speaking"}
        assert {field.name for field in fields(UserSpeaking)} == {"speaking"}
        assert VoiceSpeech(speaking=True).speaking is True
        assert UserSpeaking(speaking=True).speaking is True

    def test_an_adapter_can_raise_both_sides_edges_with_no_audio(self) -> None:
        """The fake emits them, so every consumer above the seam is testable dry."""
        sink = RecordingSink()
        call = FakeCall(sink=sink)
        asyncio.run(call.ensure_call(_dial()))

        call.voice_speech(speaking=True)
        call.voice_speech(speaking=False)
        call.user_speaking(speaking=True)
        call.user_speaking(speaking=False)

        assert sink.events == [
            VoiceSpeech(speaking=True),
            VoiceSpeech(speaking=False),
            UserSpeaking(speaking=True),
            UserSpeaking(speaking=False),
        ]

    def test_the_seam_names_the_moment_a_cue_marks_and_never_the_sound(self) -> None:
        """One verb, three moments (#186). The notes are the adapter's own.

        `play_cue` rather than `play_tone` or three verbs: what varies between a
        call coming up and a call going down is which moment it is, and an
        adapter that could not make a sound at all still knows what happened.
        """
        assert "play_cue" in _members(CallAdapter)
        assert set(Cue) == {Cue.CONNECTED, Cue.ENDED, Cue.EVENT}
        assert [str(cue) for cue in Cue] == ["connected", "ended", "event"]

    def test_a_cue_tells_the_caller_nothing_back(self) -> None:
        """The span a cue occupies stays behind the seam, where #145 will read it.

        A verb that handed a span upward would be Bridge Core holding a fact
        about an audio device — and the one consumer of that fact is the capture
        side, which is on the adapter's own side of this line.
        """
        assert inspect.signature(CallAdapter.play_cue).return_annotation == "None"

    def test_the_fake_records_the_cues_it_was_asked_for_in_order(self) -> None:
        """So a test above the seam can grade the order with no audio anywhere."""
        call = FakeCall()
        asyncio.run(call.play_cue(Cue.CONNECTED))
        asyncio.run(call.play_cue(Cue.ENDED))
        assert call.cues == [Cue.CONNECTED, Cue.ENDED]


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


class TestTheDial:
    """The three payloads one dial carries, and the two it refuses to go without."""

    def test_a_dial_refuses_prose_the_voice_would_not_get(self) -> None:
        with pytest.raises(ValueError):
            Dial(voice="   ", agent=CALL_AGENT_INSTRUCTIONS)

    def test_a_dial_refuses_rules_the_call_agent_would_not_get(self) -> None:
        with pytest.raises(ValueError):
            Dial(voice="speak plainly", agent="")

    def test_a_dial_names_its_audiences_and_never_a_wire_slot(self) -> None:
        assert {field.name for field in fields(Dial)} == {"voice", "agent", "hand_over"}

    def test_a_spoken_brief_carries_the_session_briefs_own_fields(self) -> None:
        assert {field.name for field in fields(SpokenBrief)} == {
            "name",
            "agent",
            "state",
            "newest",
            "decision",
            "answerable_here",
            "last_activity_at",
        }

    def test_a_hand_over_over_the_item_cap_is_refused_here(self) -> None:
        with pytest.raises(ValueError):
            Dial(
                voice="speak plainly",
                agent=CALL_AGENT_INSTRUCTIONS,
                hand_over=tuple(
                    DialReason(text=f"item {n}") for n in range(MAX_HANDOVER_ITEMS + 1)
                ),
            )

    def test_a_hand_over_one_byte_over_the_budget_is_refused_here(self) -> None:
        """One byte over, counted the way the seam counts it — labels included.

        A body of `HANDOVER_BUDGET_BYTES + 1` characters would overshoot by
        twenty-five once `WIRE_LINE_OVERHEAD_BYTES` is charged on it, and an
        off-by-one in the comparison would slip through that.
        """
        text = "x" * (HANDOVER_BUDGET_BYTES - WIRE_LINE_OVERHEAD_BYTES + 1)

        with pytest.raises(ValueError):
            Dial(
                voice="speak plainly",
                agent=CALL_AGENT_INSTRUCTIONS,
                hand_over=(DialReason(text=text),),
            )

    def test_the_budget_is_the_wires_own_ceiling_converted_by_codexs_own_estimate(
        self,
    ) -> None:
        """#215: the figure is derived, not chosen — so it is asserted as the product.

        codex refuses the request itself, before the backend sees it, counting
        `ceil(bytes / 4)` against a cap of 8,192 estimated tokens
        (`realtime_conversation.rs:102,1374-1392` and `truncate.rs:4,71-74` at tag
        `rust-v0.152.1`). A literal here would be the old allowance wearing a new
        number; the product is the thing to re-read when codex is upgraded.
        """
        assert HANDOVER_BUDGET_BYTES == WIRE_INITIAL_ITEMS_TOKEN_CAP * CODEX_BYTES_PER_TOKEN

    def test_a_hand_over_of_exactly_the_budget_is_accepted_here(self) -> None:
        """The ceiling is inclusive, at the new figure as at the old one."""
        text = "x" * (HANDOVER_BUDGET_BYTES - WIRE_LINE_OVERHEAD_BYTES)
        dial = Dial(
            voice="speak plainly",
            agent=CALL_AGENT_INSTRUCTIONS,
            hand_over=(DialReason(text=text),),
        )

        assert dial.hand_over_size_in_bytes == HANDOVER_BUDGET_BYTES

    def test_a_chinese_hand_over_the_old_budget_refused_now_fits(self) -> None:
        """The concrete loosening #215 exists for, in the language it was cited about.

        Ten thousand Chinese characters are thirty thousand UTF-8 bytes and so
        7,500 estimated tokens — inside the wire's cap all along, and four times
        over the allowance that used to stand here.
        """
        dial = Dial(
            voice="speak plainly",
            agent=CALL_AGENT_INSTRUCTIONS,
            hand_over=(DialReason(text="简" * 10_000),),
        )

        assert dial.hand_over_size_in_bytes > 8192
        assert dial.hand_over_size_in_bytes <= HANDOVER_BUDGET_BYTES

    def test_the_fake_records_the_dial_it_was_given(self) -> None:
        call = FakeCall()
        dial = Dial(
            voice="speak plainly",
            agent=CALL_AGENT_INSTRUCTIONS,
            hand_over=(DialReason(text="opened by the user"),),
        )

        asyncio.run(call.ensure_call(dial))

        assert call.opened_on == [dial]

    def test_the_fake_records_the_brief_it_was_asked_to_speak(self) -> None:
        call = FakeCall()
        brief = _brief()
        asyncio.run(call.ensure_call(_dial()))

        asyncio.run(call.speak(brief, request_id=new_request_id()))

        assert call.spoken == [brief]
