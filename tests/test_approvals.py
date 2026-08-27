"""The Approval Relay budget, its fallback, and the closing notice.

Three locked rules, and each one is a way the obvious implementation goes wrong:

- **Never deny on timeout.** A budget that runs out means the user was not
  reachable, not that they said no. It answers `ask` and the on-screen dialog
  takes over.
- **The notification fires immediately, in parallel with the voice attempt.**
  A pending dialog is a stall the user may be nowhere near, so the push does not
  wait to see whether the call worked.
- **A closing notice absorbs the duplicate** — and it fires on *every*
  resolution, expiry included. Without it, the user who got the push is left
  believing a decision is still wanted from them, which is exactly the state a
  never-deny fallback creates.

Delivery rides the Stop Notice escalation pipeline, because a pending dialog is
one more attention-needing stall and not a bespoke flow.
"""

from __future__ import annotations

import asyncio

from fakes import HOUSE_RULES, FakeAgent, FakeCall, FakeCompanionChannel
from gpt_voicecoding.core.adjudication import SwitchAdjudicator
from gpt_voicecoding.core.approvals import (
    CLOSING_NOTICES,
    UNCONFIRMED_NOTICES,
    ApprovalPipeline,
)
from gpt_voicecoding.core.escalation import EscalationPipeline
from gpt_voicecoding.core.interlock import CallInterlock
from gpt_voicecoding.core.lifecycle import Lifecycle
from gpt_voicecoding.core.policy import CorePolicy
from gpt_voicecoding.core.switches import Switchboard, SwitchName
from gpt_voicecoding.seams.agent import (
    ApprovalRequest,
    ApprovalVerdict,
    ApprovalVerdictKind,
    WaitingKind,
)
from gpt_voicecoding.seams.delivery import Delivery
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget

CODEX = SessionTarget(agent=AgentKind.CODEX, session_id="abc")

TEN_MINUTES = 600.0


def request(approval_id: str = "approval-1") -> ApprovalRequest:
    return ApprovalRequest(
        approval_id=approval_id,
        target=CODEX,
        tool_name="Bash",
        kind=WaitingKind.PERMISSION,
        detail="rm -rf build",
    )


def question_request(approval_id: str = "approval-1") -> ApprovalRequest:
    return ApprovalRequest(
        approval_id=approval_id,
        target=CODEX,
        tool_name="AskUserQuestion",
        kind=WaitingKind.QUESTION,
        prompt="Tabs or spaces?",
        options=("tabs", "spaces"),
    )


class Harness:
    """An approval pipeline riding a real escalation pipeline over fakes."""

    def __init__(
        self,
        *,
        duty: bool = True,
        voice: bool = True,
        message: bool = True,
        budget: float = TEN_MINUTES,
        outcome: Delivery = Delivery.DELIVERED,
    ) -> None:
        self.now = 1_000.0
        self.switches = Switchboard()
        self.switches.flip(SwitchName.DUTY, duty)
        self.switches.flip(SwitchName.VOICE, voice)
        self.switches.flip(SwitchName.MESSAGE, message)

        self.call = FakeCall()
        self.channel = FakeCompanionChannel()
        self.agent = FakeAgent(outcome=outcome, reason="the fake carried nothing")
        self.escalation = EscalationPipeline(
            call=self.call,
            channel=self.channel,
            interlock=CallInterlock(self.call),
            adjudicator=SwitchAdjudicator(self.switches),
            voice_instructions=HOUSE_RULES,
        )
        self.pipeline = ApprovalPipeline(
            agents={AgentKind.CODEX: self.agent},
            escalation=self.escalation,
            policy=CorePolicy(approval_budget_seconds=budget),
            clock=lambda: self.now,
        )

    def opened(self, req: ApprovalRequest | None = None, spoken_as: str | None = None) -> object:
        return asyncio.run(self.pipeline.opened(req or request(), spoken_as))

    def answer(self, verdict: ApprovalVerdict, approval_id: str = "approval-1") -> object:
        return asyncio.run(self.pipeline.answer(approval_id, verdict))

    def sweep(self) -> object:
        return asyncio.run(self.pipeline.sweep_expired())

    @property
    def verdicts(self) -> list[ApprovalVerdict | None]:
        return [call.verdict for call in self.agent.calls]


class TestAnnouncingAPendingDialog:
    def test_the_push_and_the_voice_attempt_both_fire(self) -> None:
        harness = Harness()

        harness.opened()

        assert harness.call.spoken
        assert harness.channel.sent

    def test_the_announcement_names_the_tool_being_asked_about(self) -> None:
        harness = Harness()

        harness.opened()

        assert "Bash" in harness.call.spoken[0]

    def test_the_announcement_names_the_session_its_caller_gave_it(self) -> None:
        """#109. Bridge Core holds the roster; this pipeline is handed the answer."""
        harness = Harness()

        harness.opened(spoken_as="GPT-VoiceCoding \u00b7 port the log")

        assert harness.call.spoken[0].startswith("GPT-VoiceCoding \u00b7 port the log is waiting")

    def test_a_question_announcement_keeps_the_sessions_recommendation(self) -> None:
        harness = Harness()
        pending_question = ApprovalRequest(
            approval_id="approval-1",
            target=CODEX,
            tool_name="AskUserQuestion",
            kind=WaitingKind.QUESTION,
            prompt="Tabs or spaces?",
            options=("tabs", "spaces"),
            recommendation="tabs",
        )

        harness.opened(pending_question)

        assert "It recommends tabs" in harness.call.spoken[0]

    def test_a_caller_with_no_name_to_give_still_names_the_session(self) -> None:
        """The address is the floor `spoken_name` itself falls back to.

        The parameter is optional, and what makes that safe is that its default
        is not "say nothing": a notice reading "a session is waiting for your
        permission" is the one this pipeline may never send, because it is the
        one the user cannot act on (#109).
        """
        harness = Harness()

        harness.opened()

        assert harness.call.spoken[0].startswith("codex abc is waiting")

    def test_no_outlet_drops_the_announcement_but_keeps_the_request_pending(self) -> None:
        harness = Harness(duty=False)

        waiting, outcome = harness.opened()

        assert waiting.request.approval_id == "approval-1"
        assert outcome.state is Lifecycle.DROPPED
        assert [one.request.approval_id for one in harness.pipeline.pending()] == ["approval-1"]
        assert harness.call.spoken == []
        assert harness.channel.sent == []

    def test_message_off_announces_by_voice_alone(self) -> None:
        harness = Harness(message=False)

        harness.opened()

        assert harness.call.spoken
        assert harness.channel.sent == []

    def test_voice_off_announces_by_text_alone(self) -> None:
        harness = Harness(voice=False)

        harness.opened()

        assert harness.call.spoken == []
        assert harness.channel.sent

    def test_the_request_is_pending_from_the_moment_it_was_announced(self) -> None:
        harness = Harness()

        harness.opened()

        assert [one.request.approval_id for one in harness.pipeline.pending()] == ["approval-1"]


class TestCarryingTheVerdict:
    def test_an_allow_is_carried_to_the_session(self) -> None:
        harness = Harness()
        harness.opened()

        outcome = harness.answer(ApprovalVerdict.ALLOW)

        assert harness.verdicts == [ApprovalVerdict.ALLOW]
        assert outcome.state is Lifecycle.DELIVERED
        assert outcome.verdict is ApprovalVerdict.ALLOW

    def test_a_deny_is_carried_too_because_the_user_decides(self) -> None:
        harness = Harness()
        harness.opened()

        harness.answer(ApprovalVerdict.DENY)

        assert harness.verdicts == [ApprovalVerdict.DENY]

    def test_answering_clears_the_pending_request(self) -> None:
        harness = Harness()
        harness.opened()

        harness.answer(ApprovalVerdict.ALLOW)

        assert harness.pipeline.pending() == ()

    def test_no_operation_class_is_barred_from_voice(self) -> None:
        """The mechanism ceiling is the adapters'; Core bars nothing."""
        harness = Harness()
        harness.opened(
            ApprovalRequest(
                approval_id="approval-1",
                target=CODEX,
                tool_name="Write",
                kind=WaitingKind.PERMISSION,
            )
        )

        harness.answer(ApprovalVerdict.ALLOW)

        assert harness.verdicts == [ApprovalVerdict.ALLOW]


class TestTheClosingNotice:
    def test_a_voice_approval_closes_the_loop_on_the_channel_too(self) -> None:
        """The closing notice is what absorbs the duplicate push."""
        harness = Harness()
        harness.opened()
        pushed = len(harness.channel.sent)

        outcome = harness.answer(ApprovalVerdict.ALLOW)

        assert len(harness.channel.sent) == pushed + 1
        assert outcome.closing_notice in harness.channel.sent[-1]

    def test_the_closing_notice_says_which_way_it_went(self) -> None:
        harness = Harness()
        harness.opened()

        allowed = harness.answer(ApprovalVerdict.ALLOW)

        harness.opened(request("approval-2"))
        denied = harness.answer(ApprovalVerdict.DENY, "approval-2")

        assert allowed.closing_notice != denied.closing_notice

    def test_exactly_one_closing_notice_per_resolution(self) -> None:
        harness = Harness()
        harness.opened()
        pushed = len(harness.channel.sent)

        harness.answer(ApprovalVerdict.ALLOW)
        harness.answer(ApprovalVerdict.ALLOW)

        assert len(harness.channel.sent) == pushed + 1


class TestResolvingAnUnannouncedRequest:
    def test_resolution_removes_the_request_without_creating_a_notice_ledger(self) -> None:
        harness = Harness(duty=False)
        harness.opened()

        harness.answer(ApprovalVerdict.ALLOW)

        assert harness.pipeline.pending() == ()
        assert harness.verdicts == [ApprovalVerdict.ALLOW]
        assert harness.call.spoken == []
        assert harness.channel.sent == []

    def test_expiry_removes_the_request_without_creating_a_notice_ledger(self) -> None:
        harness = Harness(duty=False)
        harness.opened()

        harness.now += TEN_MINUTES
        (outcome,) = harness.sweep()

        assert outcome.verdict is ApprovalVerdict.ASK
        assert harness.pipeline.pending() == ()
        assert harness.call.spoken == []
        assert harness.channel.sent == []


class TestTheBudget:
    def test_nothing_expires_before_the_budget_runs_out(self) -> None:
        harness = Harness()
        harness.opened()

        harness.now += TEN_MINUTES - 1

        assert harness.sweep() == ()
        assert len(harness.pipeline.pending()) == 1

    def test_expiry_answers_ask_and_never_deny(self) -> None:
        harness = Harness()
        harness.opened()

        harness.now += TEN_MINUTES
        (outcome,) = harness.sweep()

        assert harness.verdicts == [ApprovalVerdict.ASK]
        assert outcome.verdict is ApprovalVerdict.ASK
        assert outcome.verdict != ApprovalVerdict.DENY

    def test_expiry_reports_the_fallback_rather_than_leaving_the_user_hanging(self) -> None:
        harness = Harness()
        harness.opened()
        pushed = len(harness.channel.sent)

        harness.now += TEN_MINUTES
        (outcome,) = harness.sweep()

        assert outcome.state is Lifecycle.REPORTED_FAILED
        assert len(harness.channel.sent) == pushed + 1
        assert outcome.closing_notice in harness.channel.sent[-1]

    def test_the_budget_is_configurable(self) -> None:
        harness = Harness(budget=30.0)
        harness.opened()

        harness.now += 30.0

        assert len(harness.sweep()) == 1

    def test_the_budget_ticks_even_when_no_outlet_ever_took_the_notification(self) -> None:
        """Duty off means nobody was told — the dialog is still stalled, so it expires."""
        harness = Harness(duty=False)
        harness.opened()

        harness.now += TEN_MINUTES
        (outcome,) = harness.sweep()

        assert harness.verdicts == [ApprovalVerdict.ASK]
        assert outcome.state is Lifecycle.REPORTED_FAILED

    def test_a_request_expires_exactly_once(self) -> None:
        harness = Harness()
        harness.opened()
        harness.now += TEN_MINUTES

        assert len(harness.sweep()) == 1
        assert harness.sweep() == ()


class TestAVerdictThatArrivesTooLate:
    def test_it_is_discarded_rather_than_carried(self) -> None:
        harness = Harness()
        harness.opened()
        harness.now += TEN_MINUTES
        harness.sweep()

        outcome = harness.answer(ApprovalVerdict.ALLOW)

        assert outcome is None
        assert harness.verdicts == [ApprovalVerdict.ASK]

    def test_it_emits_nothing_because_the_loop_was_already_closed(self) -> None:
        harness = Harness()
        harness.opened()
        harness.now += TEN_MINUTES
        harness.sweep()
        pushed = len(harness.channel.sent)

        harness.answer(ApprovalVerdict.ALLOW)

        assert len(harness.channel.sent) == pushed

    def test_a_verdict_for_a_request_that_never_existed_is_discarded_safely(self) -> None:
        harness = Harness()

        assert harness.answer(ApprovalVerdict.ALLOW, "never-heard-of-it") is None


class TestWhatTheClosingNoticeMayClaim:
    """P14: only a `DELIVERED` receipt earns the word "approved" or "denied".

    Legacy never claimed a verdict had reached a Session because it never
    carried one — it detected a pending request and sent the user to the screen
    (`legacy@1d32845:bridge/transcript.py:1633-1713`;
    `legacy@1d32845:bridge/daemon.py:1901-2052`). v1 does carry verdicts, so it
    inherits the obligation legacy never had: a receipt that proves nothing is
    told to the user as proving nothing, and the on-screen dialog — which is
    still the thing that can actually resolve it — is where they are pointed.
    """

    def test_an_allow_the_session_never_confirmed_is_not_announced_as_approved(self) -> None:
        harness = Harness(outcome=Delivery.UNKNOWN)
        harness.opened()

        outcome = harness.answer(ApprovalVerdict.ALLOW)

        assert outcome.closing_notice == UNCONFIRMED_NOTICES[ApprovalVerdictKind.ALLOW]
        assert outcome.closing_notice != CLOSING_NOTICES[ApprovalVerdictKind.ALLOW]

    def test_a_deny_the_session_never_confirmed_is_not_announced_as_denied(self) -> None:
        harness = Harness(outcome=Delivery.FAILED)
        harness.opened()

        outcome = harness.answer(ApprovalVerdict.DENY)

        assert outcome.closing_notice == UNCONFIRMED_NOTICES[ApprovalVerdictKind.DENY]

    def test_a_held_verdict_is_not_confirmed_either(self) -> None:
        """HELD is parked in front of the human. It is the dialog's, not ours."""
        harness = Harness(outcome=Delivery.HELD)
        harness.opened()

        outcome = harness.answer(ApprovalVerdict.ALLOW)

        assert outcome.closing_notice == UNCONFIRMED_NOTICES[ApprovalVerdictKind.ALLOW]

    def test_an_unconfirmed_answer_points_at_the_on_screen_dialog(self) -> None:
        harness = Harness(outcome=Delivery.UNKNOWN)
        harness.opened(question_request())

        outcome = harness.answer(ApprovalVerdict.answer("tabs"))

        assert outcome.closing_notice == (
            "your answer was not confirmed to have reached the session — if the dialog is "
            "still on screen, answer it there"
        )

    def test_the_unconfirmed_wording_points_at_the_dialog_that_can_still_resolve_it(
        self,
    ) -> None:
        for wording in UNCONFIRMED_NOTICES.values():
            assert "dialog" in wording

    def test_the_unconfirmed_wording_reaches_the_user_and_not_only_the_outcome(self) -> None:
        harness = Harness(outcome=Delivery.UNKNOWN)
        harness.opened()

        outcome = harness.answer(ApprovalVerdict.ALLOW)

        assert outcome.closing_notice in harness.channel.sent[-1]

    def test_a_delivered_verdict_still_says_which_way_it_went(self) -> None:
        harness = Harness()
        harness.opened()

        outcome = harness.answer(ApprovalVerdict.ALLOW)

        assert outcome.closing_notice == CLOSING_NOTICES[ApprovalVerdictKind.ALLOW]

    def test_an_expiry_says_the_same_thing_on_every_grade(self) -> None:
        """`ask` carried no verdict, so no receipt can fail to confirm one."""
        assert (
            UNCONFIRMED_NOTICES[ApprovalVerdictKind.ASK] == CLOSING_NOTICES[ApprovalVerdictKind.ASK]
        )

    def test_every_verdict_has_an_unconfirmed_wording(self) -> None:
        assert set(UNCONFIRMED_NOTICES) == set(ApprovalVerdictKind)
        assert set(CLOSING_NOTICES) == set(ApprovalVerdictKind)
