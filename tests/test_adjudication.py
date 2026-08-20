"""Switch adjudication — the one place a switch is allowed to decide anything.

`test_switches.py` pins the *hierarchy*; this pins the *policy* read off it: may
the system touch the Live Call, may it push text, and — the locked one that is
easiest to get wrong — are those two answers independent of each other.

ADR 0002 is tested here as an absence. The adjudicator has no verb for a status
query and no verb for a switch flip, because the control plane never asks it
anything. A test asserts that, so growing a `may_answer_status()` fails CI
rather than quietly gating the one surface that must never be gated.
"""

from __future__ import annotations

import pytest

from gpt_voicecoding.core.adjudication import Outlet, SwitchAdjudicator
from gpt_voicecoding.core.errors import UnknownSwitchError
from gpt_voicecoding.core.switches import FeatureSwitch, Switchboard, SwitchName


def board(**flips: bool) -> Switchboard:
    """A board with the named switches flipped on, everything else off."""
    switches = Switchboard(
        features=(FeatureSwitch(name="stop_notices", parent=SwitchName.VOICE, default=True),)
    )
    for name, state in flips.items():
        switches.flip(name, state)
    return switches


class TestTheTwoOutwardQuestions:
    def test_nothing_is_allowed_with_every_switch_off(self) -> None:
        adjudicator = SwitchAdjudicator(board())

        assert adjudicator.may_touch_call() is False
        assert adjudicator.may_push() is False
        assert adjudicator.outlets() == ()

    def test_duty_alone_allows_nothing(self) -> None:
        """Duty is permission for the switches beneath it, not an action of its own."""
        adjudicator = SwitchAdjudicator(board(duty=True))

        assert adjudicator.may_touch_call() is False
        assert adjudicator.may_push() is False

    def test_voice_under_duty_allows_the_call_and_nothing_else(self) -> None:
        adjudicator = SwitchAdjudicator(board(duty=True, voice=True))

        assert adjudicator.may_touch_call() is True
        assert adjudicator.may_push() is False
        assert adjudicator.outlets() == (Outlet.VOICE,)

    def test_message_under_duty_allows_text_and_nothing_else(self) -> None:
        """Messages-only is a supported state, not a degraded one."""
        adjudicator = SwitchAdjudicator(board(duty=True, message=True))

        assert adjudicator.may_push() is True
        assert adjudicator.may_touch_call() is False
        assert adjudicator.outlets() == (Outlet.MESSAGE,)

    def test_both_under_duty_allow_both_outlets_voice_first(self) -> None:
        """Order is the escalation preference: speak into the call before pushing."""
        adjudicator = SwitchAdjudicator(board(duty=True, voice=True, message=True))

        assert adjudicator.outlets() == (Outlet.VOICE, Outlet.MESSAGE)

    def test_duty_off_silences_both_without_rewriting_either(self) -> None:
        """Flipping Duty back on restores what the user chose, not a default."""
        switches = board(duty=True, voice=True, message=True)
        adjudicator = SwitchAdjudicator(switches)
        switches.flip(SwitchName.DUTY, False)

        assert adjudicator.outlets() == ()
        assert switches.is_set(SwitchName.VOICE) is True

        switches.flip(SwitchName.DUTY, True)
        assert adjudicator.outlets() == (Outlet.VOICE, Outlet.MESSAGE)

    def test_the_adjudicator_reads_live_state_rather_than_a_copy(self) -> None:
        """Bridge Core's truth is one object; nothing here may snapshot it."""
        switches = board()
        adjudicator = SwitchAdjudicator(switches)
        assert adjudicator.may_push() is False

        switches.flip(SwitchName.DUTY, True)
        switches.flip(SwitchName.MESSAGE, True)
        assert adjudicator.may_push() is True


class TestFeatureSwitches:
    def test_a_feature_is_effective_only_under_its_parent(self) -> None:
        switches = board(duty=True)
        adjudicator = SwitchAdjudicator(switches)
        assert adjudicator.may_use("stop_notices") is False

        switches.flip(SwitchName.VOICE, True)
        assert adjudicator.may_use("stop_notices") is True

    def test_an_unknown_feature_fails_closed_rather_than_defaulting_on(self) -> None:
        with pytest.raises(UnknownSwitchError):
            SwitchAdjudicator(board(duty=True, voice=True)).may_use("no_such_feature")


class TestAdr0002:
    def test_the_adjudicator_has_no_verb_the_control_plane_could_consult(self) -> None:
        """ADR 0002 is absolute, so it is enforced by shape and not by discipline.

        Adding a status or flip verb here is the exact first step toward gating
        the one surface that must answer with every switch off.
        """
        verbs = {name for name in dir(SwitchAdjudicator) if not name.startswith("_")}

        assert verbs == {"may_touch_call", "may_push", "may_use", "outlets"}
