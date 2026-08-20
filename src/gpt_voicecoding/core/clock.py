"""Where Bridge Core's policy gets the time from.

Two of the locked numbers are durations — the Relay queue's ten-minute ceiling
and the Approval Relay's budget — so every pipeline that owns one takes a clock
by injection rather than calling the module-level `time` functions. A test that
has to sleep ten minutes is a test nobody runs.

The default is `time.monotonic`, not `time.time`, because these are *elapsed*
questions: "has this waited past its ceiling" must not be answerable differently
because the system clock stepped, and a laptop that sleeps and wakes must not
report a queued Relay as ten minutes stale when it was thirty seconds.

**Nothing durable may be stamped from this clock.** A monotonic reading means
nothing after a restart, and the durable subset — switch state and the Session
registry — outlives the process. Those timestamps come from the surface that
records them; this clock measures durations inside one engine run.
"""

from __future__ import annotations

import time
from collections.abc import Callable

#: Reads the current elapsed-seconds value. Injected everywhere it is needed.
Clock = Callable[[], float]

#: The one Bridge Core uses when nothing overrides it.
default_clock: Clock = time.monotonic
