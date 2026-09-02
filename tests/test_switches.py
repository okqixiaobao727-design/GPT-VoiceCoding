"""Switch state: the hierarchy, and what "effective" means under it.

`CONTEXT.md` fixes the shape — Duty is master, Voice and Message sit beneath it,
Auto Hang-up stands beside it, Feature Switches are flat booleans under a
parent, every switch has exactly two states, and every switch under Duty is
effective only while Duty is on. These tests pin that hierarchy; ADR 0002's "the
control plane is never gated" is a policy test and belongs to the pipelines
issue, not here.
"""

from __future__ import annotations

import pytest

from gpt_voicecoding.core.errors import UnknownSwitchError
from gpt_voicecoding.core.switches import (
    FeatureSwitch,
    Switchboard,
    SwitchName,
    SwitchSnapshot,
)


def test_duty_voice_and_message_exist_and_start_off() -> None:
    board = Switchboard()
    for name in (SwitchName.DUTY, SwitchName.VOICE, SwitchName.MESSAGE):
        assert board.is_set(name) is False


def test_auto_hangup_starts_on() -> None:
    """The Silence Ceiling is on unless the user turns it off (`CONTEXT.md`)."""
    assert Switchboard().is_set(SwitchName.AUTO_HANGUP) is True


def test_auto_hangup_stands_beside_duty_rather_than_under_it() -> None:
    """The ceiling is the call's own limit, so Duty does not reach it (`CONTEXT.md`)."""
    board = Switchboard()
    assert board.is_set(SwitchName.DUTY) is False
    assert board.is_effective(SwitchName.AUTO_HANGUP) is True

    board.flip(SwitchName.AUTO_HANGUP, False)
    assert board.is_effective(SwitchName.AUTO_HANGUP) is False


def test_flipping_a_switch_reports_the_previous_state() -> None:
    board = Switchboard()
    assert board.flip(SwitchName.DUTY, True) is False
    assert board.flip(SwitchName.DUTY, True) is True
    assert board.is_set(SwitchName.DUTY) is True


def test_voice_is_only_effective_while_duty_is_on() -> None:
    board = Switchboard()
    board.flip(SwitchName.VOICE, True)

    assert board.is_set(SwitchName.VOICE) is True
    assert board.is_effective(SwitchName.VOICE) is False

    board.flip(SwitchName.DUTY, True)
    assert board.is_effective(SwitchName.VOICE) is True


def test_duty_is_effective_on_its_own() -> None:
    board = Switchboard()
    board.flip(SwitchName.DUTY, True)
    assert board.is_effective(SwitchName.DUTY) is True


def test_voice_and_message_are_independent() -> None:
    """Messages-only operation is a supported state (`CONTEXT.md`)."""
    board = Switchboard()
    board.flip(SwitchName.DUTY, True)
    board.flip(SwitchName.MESSAGE, True)

    assert board.is_effective(SwitchName.MESSAGE) is True
    assert board.is_effective(SwitchName.VOICE) is False


def test_flipping_duty_off_does_not_forget_the_switches_beneath_it() -> None:
    board = Switchboard()
    board.flip(SwitchName.DUTY, True)
    board.flip(SwitchName.VOICE, True)
    board.flip(SwitchName.DUTY, False)

    assert board.is_set(SwitchName.VOICE) is True
    assert board.is_effective(SwitchName.VOICE) is False

    board.flip(SwitchName.DUTY, True)
    assert board.is_effective(SwitchName.VOICE) is True


def test_feature_switches_are_registered_not_hard_coded() -> None:
    board = Switchboard(
        features=[
            FeatureSwitch(name="stop_notice", parent=SwitchName.VOICE, default=True),
            FeatureSwitch(name="approval_relay", parent=SwitchName.VOICE),
        ]
    )
    assert board.is_set("stop_notice") is True
    assert board.is_set("approval_relay") is False


def test_a_feature_switch_needs_its_whole_ancestry_on() -> None:
    board = Switchboard(
        features=[FeatureSwitch(name="stop_notice", parent=SwitchName.VOICE, default=True)]
    )
    assert board.is_effective("stop_notice") is False

    board.flip(SwitchName.DUTY, True)
    assert board.is_effective("stop_notice") is False

    board.flip(SwitchName.VOICE, True)
    assert board.is_effective("stop_notice") is True

    board.flip("stop_notice", False)
    assert board.is_effective("stop_notice") is False


def test_a_switch_refuses_anything_that_is_not_on_or_off() -> None:
    """A string "false" is truthy: the switch would look off and behave on."""
    board = Switchboard()
    with pytest.raises(TypeError):
        board.flip(SwitchName.DUTY, "false")
    assert board.is_set(SwitchName.DUTY) is False


def test_a_feature_switch_default_must_also_be_one_of_two_states() -> None:
    with pytest.raises(TypeError):
        FeatureSwitch(name="stop_notice", parent=SwitchName.VOICE, default="on")


def test_a_snapshot_carrying_something_that_is_not_a_state_is_refused() -> None:
    board = Switchboard()
    with pytest.raises(TypeError):
        board.restore(SwitchSnapshot.of({"duty": "false"}))


def test_an_unknown_switch_fails_closed() -> None:
    board = Switchboard()
    with pytest.raises(UnknownSwitchError):
        board.is_set("no_such_switch")
    with pytest.raises(UnknownSwitchError):
        board.flip("no_such_switch", True)
    with pytest.raises(UnknownSwitchError):
        board.is_effective("no_such_switch")


def test_a_feature_may_not_shadow_a_named_switch() -> None:
    with pytest.raises(ValueError):
        Switchboard(features=[FeatureSwitch(name="duty", parent=SwitchName.VOICE)])


def test_two_features_may_not_share_a_name() -> None:
    with pytest.raises(ValueError):
        Switchboard(
            features=[
                FeatureSwitch(name="stop_notice", parent=SwitchName.VOICE),
                FeatureSwitch(name="stop_notice", parent=SwitchName.MESSAGE),
            ]
        )


def test_a_snapshot_round_trips_every_switch() -> None:
    board = Switchboard(
        features=[FeatureSwitch(name="stop_notice", parent=SwitchName.VOICE, default=True)]
    )
    board.flip(SwitchName.DUTY, True)
    board.flip("stop_notice", False)

    restored = Switchboard(
        features=[FeatureSwitch(name="stop_notice", parent=SwitchName.VOICE, default=True)]
    )
    restored.restore(board.snapshot())

    assert restored.snapshot() == board.snapshot()
    assert restored.is_set(SwitchName.DUTY) is True
    assert restored.is_set("stop_notice") is False


def test_a_snapshot_round_trips_the_auto_hangup_position() -> None:
    board = Switchboard()
    board.flip(SwitchName.AUTO_HANGUP, False)

    restored = Switchboard()
    restored.restore(board.snapshot())

    assert restored.is_set(SwitchName.AUTO_HANGUP) is False


def test_a_snapshot_written_before_auto_hangup_existed_leaves_it_on() -> None:
    """An engine that predates the switch wrote three keys; the fourth keeps its default.

    Restoring one must not read the absent key as off — that would turn the
    Silence Ceiling off on the first restart after an upgrade.
    """
    board = Switchboard()
    board.restore(SwitchSnapshot.of({"duty": True, "voice": True, "message": False}))

    assert board.is_set(SwitchName.AUTO_HANGUP) is True


def test_restoring_a_snapshot_naming_an_unregistered_feature_fails_closed() -> None:
    """A feature dropped from config must not silently resurrect from disk."""
    board = Switchboard(features=[FeatureSwitch(name="stop_notice", parent=SwitchName.VOICE)])
    snapshot = board.snapshot()

    with pytest.raises(UnknownSwitchError):
        Switchboard().restore(snapshot)
