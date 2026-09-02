"""One call at a time — the invariant that sits *above* the Call seam.

Two concurrent calls on shared speakers produce an unbounded assistant-to-
assistant loop. That is not a hypothetical: the reference implementation's
escalation path pressed the GUI toggle while the system already owned a call,
and it happened. ADR 0001 puts the rule here rather than in an adapter, because
an adapter that enforced it would only be enforcing it for *itself* — and the
loop was built out of two different surfaces.

So this is the only door to opening a call, and it **refuses** a second one
rather than quietly returning the call already up. The Call adapter's
`ensure_call` is idempotent, which makes silently absorbing the request the easy
thing to do; absorbing it would also hide the fact that a caller who asked to
open a call needs to make a different decision when one is already there. The
escalation pipeline is that caller, and its different decision — speak into the
existing call — is the whole point of the routing matrix.

Ownership is one flag and one call id, kept current from both directions: this
object's own `open_call` / `end_call`, and the call started / ended / dropped
events the seam raises upward. A call the *user* started is adopted, because
"one voice surface" means one regardless of who pressed the toggle.

The same owner holds the call's last-activity stamp and serialises speech with
ending. That makes "still silent, then end" one operation, as the reference
implementation's Live-Toggle lock did: a notice cannot be handed to a surface
that is being torn down under it.

**Activity is both sides of the conversation.** The user speaking and the
call's own Voice speaking are equally reasons a call is not idle — legacy
counted them with one regex over both roles (`legacy@1d32845:bridge/livecall.py:102-105`),
and the rewrite kept only the user half. So the ceiling reads two things: a
stamp, which both speakers restart, and a flag, which *holds* it while the
Voice is still mid-answer. A stamp alone cannot do it, because an answer
generated in ten seconds and spoken over seventy-five would still lose the call
at seventy (#184).
"""

from __future__ import annotations

import asyncio

from gpt_voicecoding.core.clock import Clock, default_clock
from gpt_voicecoding.core.errors import SecondCallRefused, VoiceInstructionsMissing
from gpt_voicecoding.seams.call import CallAdapter, CallSnapshot
from gpt_voicecoding.seams.delivery import DeliveryReceipt
from gpt_voicecoding.seams.identity import RequestId


class CallInterlock:
    """Whether the system owns a call, and the only way to start owning one."""

    def __init__(self, call: CallAdapter, *, clock: Clock = default_clock) -> None:
        self._call = call
        self._clock = clock
        self._operation_lock = asyncio.Lock()
        self._call_id: str | None = None
        self._last_activity_at: float | None = None
        self._voice_speaking = False
        self._silence_end_attempted_for: str | None = None

    def owns_call(self) -> bool:
        """Whether a Live Call is up and this system is the one holding it."""
        return self._call_id is not None

    def call_id(self) -> str | None:
        """The call the system owns, or None. Opaque; only compared for identity."""
        return self._call_id

    async def open_call(self, instructions: str) -> CallSnapshot:
        """Bring a Live Call up on those house rules. Refuses when one is owned.

        A snapshot that is not UP — CONNECTING, or down — is deliberately *not*
        claimed. Claiming a call that never arrived would bar the retry that
        fixes it, which is the no-loss invariant inverted.

        The instructions are Bridge Core's and are passed straight through: this
        object decides *whether* a call may open, never what it is told — except
        for the one case where there is nothing to tell. Both refusals live here
        because this is the only door, and a rule enforced at each caller
        instead is a rule that grows a second, divergent copy.
        """
        if self._call_id is not None:
            raise SecondCallRefused(self._call_id)
        if not instructions.strip():
            raise VoiceInstructionsMissing()
        snapshot = await self._call.ensure_call(instructions)
        if snapshot.is_up and snapshot.call_id is not None:
            self._call_id = snapshot.call_id
        return snapshot

    async def end_silent_call(self, silence_end_seconds: float) -> bool:
        """Atomically recheck silence and end once. Return whether it ended."""
        async with self._operation_lock:
            if not self._silence_end_due(silence_end_seconds):
                return False
            await self._end()
            return True

    def _silence_end_due(self, silence_end_seconds: float) -> bool:
        """Claim one silence-ending attempt for the current call when due."""
        if self._call_id is None or self._last_activity_at is None:
            return False
        if self._voice_speaking:
            # Not silence. The span is open, so there is no window to measure
            # yet — measuring one from its start would cut the answer in half.
            return False
        if self._silence_end_attempted_for == self._call_id:
            return False
        if self._last_activity_at + silence_end_seconds > self._clock():
            return False
        self._silence_end_attempted_for = self._call_id
        return True

    def note_activity(self) -> None:
        """Restart the silence window when the owned call carried activity."""
        if self._call_id is not None:
            self._last_activity_at = self._clock()

    def note_voice_speech(self, *, speaking: bool) -> None:
        """The Voice started or stopped speaking on the owned call.

        Both edges are activity: the start says the call is not idle, and the
        stop is what the window is then measured from — the end of the answer,
        not the moment before it began.
        """
        if self._call_id is None:
            return
        self._voice_speaking = speaking
        self.note_activity()

    async def speak(self, text: str, *, request_id: RequestId) -> DeliveryReceipt:
        """Speak through the owned call, serialised with ending it.

        The receipt is **not** activity. It says the words were handed to the
        adapter, which is a text hand-over and not a voice; what keeps the call
        alive is the Voice saying them, and that comes back on its own as
        `VoiceSpeech` (#184). Stamping the hand-over as well would restart the
        window from before the answer instead of from the end of it.
        """
        async with self._operation_lock:
            return await self._call.speak(text, request_id=request_id)

    async def end_call(self) -> CallSnapshot:
        """End the call the system owns. Idempotent when none is up."""
        async with self._operation_lock:
            return await self._end()

    async def _end(self) -> CallSnapshot:
        """The one ending implementation, called only while the operation lock is held."""
        snapshot = await self._call.end_call()
        self._clear()
        return snapshot

    def _clear(self) -> None:
        """Forget one call and every piece of state that belongs only to it."""
        self._call_id = None
        self._last_activity_at = None
        self._voice_speaking = False
        self._silence_end_attempted_for = None

    def note_started(self, call_id: str) -> None:
        """Adopt a call the seam reported. One voice surface, whoever opened it."""
        if self._call_id == call_id and self._last_activity_at is not None:
            return
        self._call_id = call_id
        self._last_activity_at = self._clock()
        # A flag belongs to the call it was raised on. Carrying one across would
        # pin the ceiling open on a call nobody is speaking into — the one real
        # hazard the hold introduces, closed in the same place it is opened.
        self._voice_speaking = False
        self._silence_end_attempted_for = None

    def note_ended(self, call_id: str) -> bool:
        """Release if that is the call being reported. True when it cleared.

        A late event about a call that already finished must not unlock the live
        one — that would re-open the exact door this object exists to hold shut.

        The answer is returned rather than swallowed because the interlock
        *clearing* is an outlet transition and a stale event is not one. A
        caller that requested reconciliation on every end event would inspect
        current waits again on news about a call nobody was waiting on.
        """
        if self._call_id != call_id:
            return False
        self._clear()
        return True
