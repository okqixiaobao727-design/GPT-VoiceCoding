"""Bridge Core assembled, driven end to end by events, against fakes only.

ADR 0001 principle 4 in one file: a fake call, fake agents and a fake channel,
no network and no audio, and every one of the five pipelines reachable from an
event an adapter could actually raise.

The cases here are the ones the pipelines issue was asked to prove, and each is
a defect that has happened rather than one that might: a stop arriving while the
system owns a call, a notice with nowhere to go, an approval nobody answered, a
Relay whose Session never opened its window, and Duty going off in the middle of
all of it while the control plane keeps answering.
"""

from __future__ import annotations

import asyncio

import pytest

from gpt_voicecoding.core.bridge import (
    NO_CONTROL_SURFACE,
    NO_DELEGATE_HANDLER,
)
from gpt_voicecoding.core.errors import VoiceInstructionsMissing
from gpt_voicecoding.core.router import Classification
from gpt_voicecoding.core.sessions import SessionState
from gpt_voicecoding.core.switches import SwitchName
from gpt_voicecoding.seams.agent import (
    ApprovalRequest,
    ApprovalVerdict,
    AwaitingApproval,
    ReplyWindow,
    ReplyWindowChanged,
    SessionEnded,
    SessionStopped,
)
from gpt_voicecoding.seams.call import CallDropped, CallStarted, CallState, UserSpeech
from gpt_voicecoding.seams.companion_channel import InboundText
from gpt_voicecoding.seams.delivery import Delivery
from hub import CLAUDE, CODEX, TEN_MINUTES, Hub


class TestTheStopNoticePipelineEndToEnd:
    def test_a_stopped_session_is_announced_by_its_label(self) -> None:
        hub = Hub()

        hub.emit(SessionStopped(target=CODEX))

        assert "port the log" in hub.call.spoken[0]

    def test_a_stop_while_the_system_owns_a_call_opens_no_second_call(self) -> None:
        """The reference implementation's loop, made unreachable."""
        hub = Hub()
        started = hub.toggle()
        assert started.call_id is not None
        hub.emit(CallStarted(call_id=started.call_id))

        hub.emit(SessionStopped(target=CODEX))

        assert hub.call.calls_started == 1
        assert hub.call.spoken

    def test_with_no_call_and_message_on_the_channel_send_is_actually_invoked(self) -> None:
        hub = Hub(voice=False)

        hub.emit(SessionStopped(target=CODEX))

        assert hub.channel.sent
        assert hub.call.calls_started == 0

    def test_an_unreachable_channel_retains_the_notice_and_never_marks_it_delivered(
        self,
    ) -> None:
        hub = Hub(voice=False)
        hub.channel.outcome = Delivery.FAILED
        hub.channel.reason = "the chat is unreachable"

        hub.emit(SessionStopped(target=CODEX))

        (waiting,) = hub.state.relays.pending()
        assert waiting.outcome.is_delivered is False

    def test_a_retained_notice_surfaces_when_a_call_comes_up(self) -> None:
        hub = Hub(duty=False)
        hub.emit(SessionStopped(target=CODEX))
        assert len(hub.state.relays.pending()) == 1

        hub.flip(SwitchName.DUTY, True)

        assert hub.call.spoken
        assert hub.state.relays.pending() == ()

    def test_message_off_and_voice_on_never_pushes_text(self) -> None:
        hub = Hub(message=False)

        hub.emit(SessionStopped(target=CODEX))

        assert hub.channel.sent == []


class TestTheApprovalPipelineEndToEnd:
    def test_an_awaiting_approval_event_announces_on_every_outlet(self) -> None:
        hub = Hub()

        hub.emit(AwaitingApproval(request=ApprovalRequest("a1", CODEX, "Bash")))

        assert hub.call.spoken
        assert hub.channel.sent

    def test_an_unanswered_approval_expires_to_ask_and_never_to_deny(self) -> None:
        hub = Hub()
        hub.emit(AwaitingApproval(request=ApprovalRequest("a1", CODEX, "Bash")))

        hub.now += TEN_MINUTES
        hub.tick()

        assert [call.verdict for call in hub.agent.calls] == [ApprovalVerdict.ASK]

    def test_a_verdict_after_expiry_is_discarded_safely(self) -> None:
        hub = Hub()
        hub.emit(AwaitingApproval(request=ApprovalRequest("a1", CODEX, "Bash")))
        hub.now += TEN_MINUTES
        hub.tick()

        late = asyncio.run(hub.core.approvals.answer("a1", ApprovalVerdict.ALLOW))

        assert late is None
        assert [call.verdict for call in hub.agent.calls] == [ApprovalVerdict.ASK]


class TestTheRelayPipelineEndToEnd:
    def test_words_for_a_busy_session_wait_and_are_confirmed_once(self) -> None:
        hub = Hub()

        hub.emit(InboundText(text="ship it"))

        assert hub.agent.calls == []
        assert hub.channel.sent == ["got it, it'll go when this turn ends"]

    def test_the_open_window_delivers_them_without_announcing_again(self) -> None:
        hub = Hub()
        hub.emit(InboundText(text="ship it"))
        confirmations = len(hub.channel.sent)

        hub.emit(ReplyWindowChanged(target=CODEX, window=ReplyWindow.OPEN))

        assert [call.text for call in hub.agent.calls] == ["ship it"]
        assert len(hub.channel.sent) == confirmations

    def test_ten_minutes_of_waiting_becomes_one_reported_failure(self) -> None:
        hub = Hub()
        hub.emit(InboundText(text="ship it"))

        hub.now += TEN_MINUTES
        hub.tick()

        assert hub.state.relays.pending() == ()
        assert any("never reached the session" in spoken for spoken in hub.call.spoken)

    def test_a_session_that_ends_reports_the_words_still_waiting_for_it(self) -> None:
        hub = Hub()
        hub.emit(InboundText(text="ship it"))

        hub.emit(SessionEnded(target=CODEX))

        assert hub.state.relays.pending() == ()
        assert hub.state.sessions.all()[0].state is SessionState.ENDED


class TestTheInboundRouterEndToEnd:
    def test_a_command_reaches_the_wired_control_surface(self) -> None:
        seen: list[Classification] = []

        async def control(found: Classification) -> str:
            seen.append(found)
            return "duty is on"

        hub = Hub(control=control)

        hub.emit(InboundText(text="/status"))

        assert [found.command for found in seen] == ["status"]
        assert hub.channel.sent == ["duty is on"]

    def test_a_command_with_nothing_wired_says_so_rather_than_guessing(self) -> None:
        hub = Hub()

        hub.emit(InboundText(text="/status"))

        assert hub.channel.sent == [NO_CONTROL_SURFACE]

    def test_a_delegation_reaches_the_wired_handler(self) -> None:
        async def delegate(found: Classification) -> str:
            return f"about {found.text}: it says so"

        hub = Hub(delegate=delegate)

        hub.emit(InboundText(text=">what does ADR 0002 say"))

        assert hub.channel.sent == ["about what does ADR 0002 say: it says so"]

    def test_a_delegation_with_nothing_wired_says_so(self) -> None:
        hub = Hub()

        hub.emit(InboundText(text=">summarise the diff"))

        assert hub.channel.sent == [NO_DELEGATE_HANDLER]

    def test_unknown_input_fails_closed_with_an_honest_reply(self) -> None:
        hub = Hub(sessions=((CODEX, "port the log"), (CLAUDE, "build the shell")))

        hub.emit(InboundText(text="ship it"))

        assert hub.agent.calls == []
        (reply,) = hub.channel.sent
        assert "port the log" in reply
        assert "build the shell" in reply

    def test_the_reply_goes_out_with_every_switch_off(self) -> None:
        """A reply is not a push. ADR 0002 covers the surface it came in on."""
        hub = Hub(duty=False, voice=False, message=False)

        hub.emit(InboundText(text="/status"))

        assert hub.channel.sent == [NO_CONTROL_SURFACE]

    def test_words_for_a_session_that_ended_are_refused_not_queued(self) -> None:
        hub = Hub()
        hub.emit(SessionEnded(target=CODEX))

        hub.emit(InboundText(text="ship it"))

        assert hub.state.relays.pending() == ()
        assert hub.channel.sent


class TestTheOneCallInvariantEndToEnd:
    def test_the_live_toggle_opens_a_call_when_none_is_up(self) -> None:
        hub = Hub()

        snapshot = hub.toggle()

        assert snapshot.state is CallState.UP
        assert hub.core.interlock.owns_call() is True

    def test_the_live_toggle_ends_the_call_the_system_owns(self) -> None:
        hub = Hub()
        hub.toggle()

        assert hub.toggle().state is CallState.DOWN
        assert hub.core.interlock.owns_call() is False

    def test_a_hub_that_generated_no_house_rules_opens_no_call(self) -> None:
        """The refusal comes from the interlock, which is the one door.

        The hub does not carry its own copy of this check, so what a caller sees
        here is the same refusal, worded once, that the escalation pipeline sees.
        """
        hub = Hub(instructions=False)

        with pytest.raises(VoiceInstructionsMissing):
            hub.toggle()

        assert hub.call.calls_started == 0

    def test_ending_a_call_never_needs_house_rules(self) -> None:
        """Opening is refusable; ending is not. A call that is up must be endable."""
        hub = Hub(instructions=False)
        hub.core.interlock.note_started("call-1")

        assert hub.toggle().state is CallState.DOWN

    def test_the_live_toggle_works_with_every_switch_off(self) -> None:
        """It is a control-plane action: the user touching the call, not the system."""
        hub = Hub(duty=False, voice=False, message=False)

        assert hub.toggle().state is CallState.UP
        assert hub.toggle().state is CallState.DOWN

    def test_the_toggle_ends_a_call_the_user_started_rather_than_opening_a_second(
        self,
    ) -> None:
        """One toggle, one voice surface — whoever brought the call up."""
        hub = Hub()
        started = hub.toggle()
        assert started.call_id is not None
        hub.emit(CallStarted(call_id=started.call_id))

        assert hub.toggle().state is CallState.DOWN
        assert hub.call.calls_started == 1

    def test_a_dropped_call_frees_escalation_to_open_the_next_one(self) -> None:
        hub = Hub()
        hub.emit(CallStarted(call_id="call-1"))
        hub.emit(CallDropped(call_id="call-1", detail="the network went away"))

        hub.emit(SessionStopped(target=CODEX))

        assert hub.call.calls_started == 1

    def test_a_notice_that_failed_is_retried_once_the_interlock_clears(self) -> None:
        """The locked sequence: notice fails → retained → retried after it clears."""
        hub = Hub(message=False)
        hub.emit(CallStarted(call_id="call-the-user-started"))

        hub.emit(SessionStopped(target=CODEX))
        assert hub.call.spoken == []
        assert len(hub.state.relays.pending()) == 1

        hub.emit(CallDropped(call_id="call-the-user-started", detail="the network went away"))

        assert hub.call.spoken
        assert hub.state.relays.pending() == ()

    def test_a_stale_call_event_re_offers_nothing(self) -> None:
        """Only the interlock actually clearing is an outlet transition."""
        hub = Hub(message=False)
        hub.emit(CallStarted(call_id="current-call"))
        hub.emit(SessionStopped(target=CODEX))
        assert len(hub.state.relays.pending()) == 1

        hub.emit(CallDropped(call_id="stale-old-call", detail="a late report"))

        assert hub.call.calls_started == 0
        assert hub.core.interlock.call_id() == "current-call"
        assert len(hub.state.relays.pending()) == 1

    def test_a_channel_that_came_back_re_offers_what_was_retained(self) -> None:
        """The one outlet transition no event announces."""
        hub = Hub(voice=False)
        hub.channel.outcome = Delivery.FAILED
        hub.channel.reason = "the chat is unreachable"
        hub.emit(SessionStopped(target=CODEX))
        assert len(hub.state.relays.pending()) == 1

        hub.channel.outcome = Delivery.DELIVERED
        hub.channel.reason = "back"
        asyncio.run(hub.core.outlets_changed())

        assert hub.state.relays.pending() == ()

    def test_a_call_coming_up_re_offers_what_was_retained(self) -> None:
        hub = Hub(duty=False)
        hub.emit(SessionStopped(target=CODEX))
        hub.flip(SwitchName.VOICE, True)
        hub.flip(SwitchName.MESSAGE, False)
        assert len(hub.state.relays.pending()) == 1

        hub.flip(SwitchName.DUTY, True)

        assert hub.call.spoken
        assert hub.state.relays.pending() == ()


class TestSwitchAdjudicationEndToEnd:
    def test_duty_off_neither_speaks_nor_pushes_but_still_records_the_event(self) -> None:
        hub = Hub(duty=False)

        handled = hub.emit(SessionStopped(target=CODEX))

        assert handled == 1
        assert hub.call.spoken == []
        assert hub.channel.sent == []
        assert len(hub.state.relays.pending()) == 1

    def test_the_control_plane_answers_with_every_switch_off(self) -> None:
        """ADR 0002 is absolute."""
        hub = Hub(duty=False, voice=False, message=False)

        status = hub.core.status()

        assert status.switches.as_mapping()[SwitchName.DUTY] is False
        assert len(status.sessions) == 1

    def test_switches_can_be_flipped_back_on_with_duty_off(self) -> None:
        hub = Hub(duty=False, voice=False, message=False)

        assert hub.flip(SwitchName.DUTY, True) is False
        assert hub.core.status().switches.as_mapping()[SwitchName.DUTY] is True

    def test_voice_off_and_message_on_is_a_working_system(self) -> None:
        hub = Hub(voice=False)

        hub.emit(SessionStopped(target=CODEX))
        hub.emit(InboundText(text="ship it"))

        assert hub.channel.sent
        assert hub.call.spoken == []

    def test_duty_flipping_off_mid_escalation_halts_but_keeps_answering(self) -> None:
        """A Stop Notice tries one outlet at a time, so permission is re-read."""
        hub = Hub()
        hub.call.reachable = False
        original = hub.call.ensure_call

        async def go_off_duty_while_connecting(instructions: str) -> object:
            hub.state.switches.flip(SwitchName.DUTY, False)
            return await original(instructions)

        hub.call.ensure_call = go_off_duty_while_connecting  # type: ignore[method-assign]

        handled = hub.emit(SessionStopped(target=CODEX))

        assert hub.channel.sent == []
        assert handled == 1
        assert len(hub.state.relays.pending()) == 1
        assert hub.core.status().sessions

    def test_a_pending_approval_pushes_without_waiting_on_the_voice_attempt(self) -> None:
        """ "In parallel" is literal: a stalled call must not hold the text back."""
        released = asyncio.Event()
        hub = Hub()
        original = hub.call.speak

        async def speak_slowly(text: str, **kwargs: object) -> object:
            await released.wait()
            return await original(text, **kwargs)  # type: ignore[arg-type]

        hub.call.speak = speak_slowly  # type: ignore[method-assign]

        async def watch() -> list[str]:
            escalating = asyncio.ensure_future(
                hub.core.dispatch(AwaitingApproval(request=ApprovalRequest("a1", CODEX, "Bash")))
            )
            # Let the fan-out start both attempts before the call is unblocked.
            for _ in range(4):
                await asyncio.sleep(0)
            pushed = list(hub.channel.sent)
            released.set()
            await escalating
            return pushed

        assert asyncio.run(watch()) == ["a session is waiting for your permission to use Bash"]


class TestEventsThatDecideNothing:
    def test_the_in_call_transcript_is_recorded_and_never_relayed(self) -> None:
        """Bridge Core never parses speech; the voice thread acts through commands."""
        hub = Hub()

        handled = hub.emit(UserSpeech(text="stop the log one"))

        assert handled == 1
        assert hub.agent.calls == []
        assert hub.channel.sent == []

    def test_events_arrive_once_and_in_order(self) -> None:
        hub = Hub(voice=False)

        hub.emit(
            SessionStopped(target=CODEX, detail="first"),
            SessionStopped(target=CODEX, detail="second"),
        )

        assert [sent.endswith("first") for sent in hub.channel.sent] == [True, False]
        assert hub.channel.sent[1].endswith("second")
