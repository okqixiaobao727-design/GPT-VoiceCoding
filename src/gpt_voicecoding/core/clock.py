"""Where Bridge Core's policy gets the time from.

Three of the locked numbers are durations — the Relay queue's ten-minute
ceiling, the Approval Relay's budget, and the Live Call's silence ceiling — so
every component that owns one takes a clock by injection rather than calling
the module-level `time` functions. A test that has to sleep ten minutes is a
test nobody runs.

The default is `time.monotonic`, not `time.time`, because these are *elapsed*
questions: "has this waited past its ceiling" must not be answerable differently
because the system clock stepped, and a laptop that sleeps and wakes must not
report a queued Relay as ten minutes stale when it was thirty seconds.

**Nothing that will be read outside this process may be stamped from this
clock.** A monotonic reading is an offset from an origin only this process
knows, so it survives neither a restart nor the trip across the control plane,
and on the far side it names no moment at all. This clock measures durations
inside one engine run and answers no question asked from outside it.

So there are two, and they are never interchangeable: `default_clock` measures
elapsed time inside one run, and `wall_clock` stamps a fact read outside it.
A Session's `first_seen` is stamped from the second: the roster is not
written to disk (#74), but every row of it travels to every surface in the
`sessions` payload, and `first_seen` is the one field on that row this engine
authored rather than observed.
"""

from __future__ import annotations

import time
from collections.abc import Callable

#: Reads the current elapsed-seconds value. Injected everywhere it is needed.
Clock = Callable[[], float]

#: The one Bridge Core uses when nothing overrides it. Elapsed time only.
default_clock: Clock = time.monotonic

#: The one used to stamp anything that outlives this process. Never for durations.
wall_clock: Clock = time.time
