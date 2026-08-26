"""Bridge Core's single source of truth, assembled — and its one persistence path.

Three pieces of state, held here and nowhere else: switch state, the Session
registry, and the undelivered Relay queue. No module keeps a copy; every surface
queries the hub.

This object exists to make "one persistence path" structural. The store is
reachable from here and from nothing else, so there is exactly one place that
decides what is durable, and the answer is: switch state, and nothing else. The
Relay queue is deliberately not persisted — see `relay_queue` — and neither is
the Session roster, which discovery re-reads on a cadence (#74).

Policy lives in the pipelines, not here. This holds truth and writes it down.
"""

from __future__ import annotations

from gpt_voicecoding.core.errors import BridgeCoreError, StateFormatError
from gpt_voicecoding.core.persistence import PersistedState, StateStore
from gpt_voicecoding.core.relay_queue import RelayQueue
from gpt_voicecoding.core.sessions import SessionRegistry
from gpt_voicecoding.core.switches import Switchboard


class BridgeState:
    """The hub's truth, and the only thing that touches the store."""

    def __init__(
        self,
        *,
        switches: Switchboard,
        sessions: SessionRegistry,
        relays: RelayQueue,
        store: StateStore | None = None,
    ) -> None:
        self._switches = switches
        self._sessions = sessions
        self._relays = relays
        self._store = store

    @property
    def switches(self) -> Switchboard:
        return self._switches

    @property
    def sessions(self) -> SessionRegistry:
        return self._sessions

    @property
    def relays(self) -> RelayQueue:
        return self._relays

    def persist(self) -> None:
        """Write the durable subset down. A no-op when running without a store."""
        if self._store is None:
            return
        self._store.save(PersistedState(switches=self._switches.snapshot()))

    def restore(self) -> bool:
        """Adopt what was written down. False means first run, not failure.

        Anything the file describes that this engine cannot honour — a switch
        configuration no longer declares — fails closed. Starting blank would
        look identical to the system quietly deciding to stop speaking.

        **The Session roster is not adopted, because it is not written** (#74).
        A restart comes back with an empty roster and fills it from the first
        discovery, which is the only source that can be right about what is
        running now.
        """
        if self._store is None:
            return False
        state = self._store.load()
        if state is None:
            return False

        try:
            self._switches.restore(state.switches)
        except BridgeCoreError as error:
            raise StateFormatError(self._store.path, str(error)) from error
        return True
