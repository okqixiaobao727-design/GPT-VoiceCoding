"""One launch per request identity, and one truthful outcome — for either adapter.

The two adapters differ in how a process comes into being and in nothing else,
so the bookkeeping that makes the seam's promises true lives here once. That is
not only tidiness: the promises are the kind that are wrong in the same way in
both places when they are written twice.

**Exactly one launch per request id, under concurrency too.** Reading a cache and
then awaiting a launch is not enough: two callers arriving with the same request
id both find the cache empty, because nothing has finished yet, and both start a
child. The identity has to be claimed *before* the first await, which is what
holding the in-flight launch itself — rather than only its result — does. Both
callers then await the same launch and receive the same outcome, which is what
"one child, one outcome" means when the two requests overlap rather than queue.

**A close that did not fully happen is not recorded as one that did.** A Session
whose window went but whose app-server survived is still this launcher's to
answer for, so it stays live and a repeat may try again. Forgetting it would
turn the next `close` into a cheerful `already_closed` about a process that is
still running.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from gpt_voicecoding.seams.identity import RequestId, SessionTarget
from gpt_voicecoding.seams.session_launcher import LaunchOutcome, LaunchRequest


class LaunchRegistry:
    """What each adapter remembers: launches by identity, and Sessions by target."""

    def __init__(self) -> None:
        self._outcomes: dict[RequestId, LaunchOutcome] = {}
        #: Launches that have started and not yet finished, held by identity. The
        #: reason a second caller cannot start a second child.
        self._inflight: dict[RequestId, asyncio.Task[LaunchOutcome]] = {}
        self._closed: set[SessionTarget] = set()

    async def once(
        self, request: LaunchRequest, launching: Callable[[], Awaitable[LaunchOutcome]]
    ) -> LaunchOutcome:
        """Run `launching` at most once for this request id, whoever asks."""
        identity = request.request_id
        settled = self._outcomes.get(identity)
        if settled is not None:
            return settled

        running = self._inflight.get(identity)
        if running is None:
            # Created before the first await, so an identity is claimed the
            # moment it is seen rather than when its launch completes.
            running = asyncio.ensure_future(launching())
            self._inflight[identity] = running

        try:
            outcome = await asyncio.shield(running)
        finally:
            if running.done():
                self._inflight.pop(identity, None)
        self._outcomes[identity] = outcome
        return outcome

    def is_closed(self, target: SessionTarget) -> bool:
        """Whether this launcher has already fully closed that Session."""
        return target in self._closed

    def forget(self, target: SessionTarget) -> None:
        """Record a Session as fully closed. Only ever called when it really is."""
        self._closed.add(target)
