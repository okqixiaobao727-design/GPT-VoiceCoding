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
"""

from __future__ import annotations

from gpt_voicecoding.core.errors import SecondCallRefused, VoiceInstructionsMissing
from gpt_voicecoding.seams.call import CallAdapter, CallSnapshot


class CallInterlock:
    """Whether the system owns a call, and the only way to start owning one."""

    def __init__(self, call: CallAdapter) -> None:
        self._call = call
        self._call_id: str | None = None

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

    async def end_call(self) -> CallSnapshot:
        """End the call the system owns. Idempotent when none is up."""
        snapshot = await self._call.end_call()
        self._call_id = None
        return snapshot

    def note_started(self, call_id: str) -> None:
        """Adopt a call the seam reported. One voice surface, whoever opened it."""
        self._call_id = call_id

    def note_ended(self, call_id: str) -> bool:
        """Release if that is the call being reported. True when it cleared.

        A late event about a call that already finished must not unlock the live
        one — that would re-open the exact door this object exists to hold shut.

        The answer is returned rather than swallowed because the interlock
        *clearing* is an outlet transition and a stale event is not one. A
        caller that swept on every end event would attempt a retained notice
        again on news about a call nobody was waiting on.
        """
        if self._call_id != call_id:
            return False
        self._call_id = None
        return True
