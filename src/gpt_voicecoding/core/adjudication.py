"""Switch adjudication — the one place a switch decides anything.

`switches` holds flags and answers "is this one effective". That is state. This
is the policy read off it: *may this outward action happen right now?* Every
pipeline asks here rather than reading the board itself, so "Duty off means the
system does not speak, does not push, and does not touch the Live Call" is one
implementation instead of one per pipeline.

Three of the four questions here are about reaching the user. The fourth — may
the Silence Ceiling end a silent call — is about the call's own limit, and its
switch answers it alone; nothing above it may veto it.

The Voice and Message answers are computed independently, all the way down. The
Voice Switch is the whole Live Call — speaking into it, opening it, ending it —
because `CONTEXT.md` defines it that way, and the Message Switch is text reach.
Messages-only is a supported state, so nothing here may make one answer depend
on the other.

**Switches constrain the system's own reach; user-initiated control-plane
actions are never adjudicated.** That boundary is what resolves every apparent
conflict between a switch and a command. Duty, Voice and Message answer "may the
system do this unbidden" — escalate into a call, push a notice, open a voice
surface nobody asked for. They do not answer "may the user do this", so a status
query, a switch flip and the Live Toggle all pass without consulting anything
here. The alternative is indefensible: Voice flipped off while a call is up, and
the user's own "end this call" refused by the switch that says the system should
be quiet.

**ADR 0002 lives here as an absence.** There is no verb for a status query and
no verb for a switch flip, because the control plane never consults this object.
Adding one would be the first step toward gating the one surface that must
answer with every switch off, so `tests/test_adjudication.py` pins the verb list.
"""

from __future__ import annotations

from enum import StrEnum

from gpt_voicecoding.core.switches import Switchboard, SwitchName


class Outlet(StrEnum):
    """A way the system can reach the user right now.

    Ordered as the escalation pipeline prefers them: a Live Call the user is
    already in beats a push they have to go and look at.
    """

    #: The Live Call — the system's one voice surface.
    VOICE = "voice"
    #: The Companion Channel — text reach when no call is up.
    MESSAGE = "message"


class SwitchAdjudicator:
    """Answers what the switches permit. Holds no state of its own."""

    def __init__(self, switches: Switchboard) -> None:
        #: Held by reference, never copied: Bridge Core's truth is one object,
        #: and a snapshot taken here would go stale the moment a switch flipped.
        self._switches = switches

    def may_touch_call(self) -> bool:
        """Whether the system may speak into, open, or end the Live Call."""
        return self._switches.is_effective(SwitchName.VOICE)

    def may_push(self) -> bool:
        """Whether the system may push text through the Companion Channel."""
        return self._switches.is_effective(SwitchName.MESSAGE)

    def may_auto_hangup(self) -> bool:
        """Whether the Silence Ceiling may end the call it is measuring.

        The Auto Hang-up Switch answers this alone. It stands beside Duty rather
        than under it (`CONTEXT.md`), because the ceiling is the call's own limit
        and not an act toward the user — so the answer holds with Duty and Voice
        off, and on a call the user opened. `is_effective` is asked rather than
        `is_set` for the same reason every other verb here does: the ancestry
        question is the board's to answer, not this module's to assume away.
        """
        return self._switches.is_effective(SwitchName.AUTO_HANGUP)

    def may_use(self, feature: str) -> bool:
        """Whether one Feature Switch is on *and* everything above it is.

        Fails closed on a name this board does not have: an unknown capability
        is not a permitted one.
        """
        return self._switches.is_effective(feature)

    def outlets(self) -> tuple[Outlet, ...]:
        """Every outlet open right now, in escalation preference order."""
        open_now = []
        if self.may_touch_call():
            open_now.append(Outlet.VOICE)
        if self.may_push():
            open_now.append(Outlet.MESSAGE)
        return tuple(open_now)
