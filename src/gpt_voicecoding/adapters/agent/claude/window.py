"""Whether a Claude Session can take a user turn right now, read from the registry.

Without this, nothing ever reports a Claude Session's Reply Window, `Session.
reply_window` stays at its fail-closed default of CLOSED, and Bridge Core queues
the user's words forever instead of delivering them. That is the mechanism
working as designed — a window nobody has observed is not one anything may claim
is open — but it leaves a Claude Session unreachable in the assembled engine, so
observing it is this module's whole job.

**A Reply Window is a level, not an event.** Claude Code publishes exactly that,
in the same registry record the Notice Relay already reads: a `status` field
carrying `idle`, `busy` or `waiting`, rewritten sub-second. Reading a level from
the field that *is* the level beats reconstructing it from a stream of transcript
events, which is why this watches the registry rather than the transcript the
rest of this spoke reads.

**`waiting` is CLOSED.** It means a permission dialog is on screen, and a dialog
blocks every Relay there is — verified live: three routes, all enqueued, none
delivered, none starting a turn, until a human answered the dialog. A Session in
that state is the opposite of one awaiting the user's next instruction. Surfacing
the dialog itself is the Approval Relay's job, not this one's, so nothing about
it leaks out of here as a window state.

**Anything unrecognised is CLOSED**, including a record that has vanished or
cannot be read. The whole vocabulary of this seam rests on never claiming
readiness that has not been observed, and a status string this build has never
seen is not an observation of readiness.

Transitions are what get emitted, plus one report at registration so Bridge Core
learns the current level immediately rather than sitting at the default until the
Session happens to change state.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from gpt_voicecoding.adapters.agent.claude.registry import (
    RegistryError,
    SessionRecord,
    pid_is_live,
    read_record,
)
from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings
from gpt_voicecoding.seams.agent import ReplyWindow, ReplyWindowChanged
from gpt_voicecoding.seams.identity import SessionTarget

_log = logging.getLogger(__name__)

#: The one registry status that means "awaiting the user's next instruction".
#: A whitelist rather than a blacklist: a new status this build has never seen
#: must read as CLOSED, and a blacklist would let it read as OPEN.
STATUS_MEANING_OPEN = "idle"


def window_for(record: SessionRecord | None) -> ReplyWindow:
    """What one registry record says about a Session's willingness to take a turn."""
    if record is None or record.status != STATUS_MEANING_OPEN:
        return ReplyWindow.CLOSED
    return ReplyWindow.OPEN


class ReplyWindowWatcher:
    """Watches every registered Claude Session's registry record, and reports changes."""

    def __init__(
        self,
        *,
        settings: ClaudeSettings,
        emit: Callable[[ReplyWindowChanged], None],
    ) -> None:
        self._settings = settings
        self._emit = emit
        #: The last level reported for each target, so only transitions are sent.
        self._reported: dict[SessionTarget, ReplyWindow] = {}
        self._polling: asyncio.Task[None] | None = None

    def watch(self, target: SessionTarget) -> None:
        """Start watching one Session, reporting where its window stands right now.

        The immediate report is not an optimisation. Bridge Core starts every
        Session at CLOSED, so without it a Session that is already idle — the
        common case, since a Session is usually registered the moment it comes up
        — would stay unreachable until it next changed state.
        """
        if target in self._reported:
            return
        window = window_for(self._look(target))
        self._reported[target] = window
        self._emit(ReplyWindowChanged(target=target, window=window))

    def forget(self, target: SessionTarget) -> None:
        """Stop watching one Session. Its own process is untouched."""
        self._reported.pop(target, None)

    @property
    def watching(self) -> tuple[SessionTarget, ...]:
        return tuple(self._reported)

    async def start(self) -> None:
        """Begin polling. Idempotent, so a second connect does not double the reads."""
        if self._polling is None:
            self._polling = asyncio.ensure_future(self._poll())

    async def aclose(self) -> None:
        """Stop polling, and wait for it — a cancellation is only a request to stop."""
        polling, self._polling = self._polling, None
        if polling is not None:
            polling.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await polling
        self._reported.clear()

    def poll_once(self) -> None:
        """One sweep of every watched Session. Emits only where the level moved."""
        for target in tuple(self._reported):
            window = window_for(self._look(target))
            if self._reported.get(target) != window:
                self._reported[target] = window
                self._emit(ReplyWindowChanged(target=target, window=window))

    def _look(self, target: SessionTarget) -> SessionRecord | None:
        """One Session's record, or `None` for every reason there might not be one."""
        if target.pid is None:  # pragma: no cover - SessionTarget refuses this already
            return None
        try:
            record = read_record(self._settings.registry_directory, target.pid)
        except RegistryError:
            return None
        if record.session_id != target.session_id or not pid_is_live(record.pid):
            # The pid has been recycled onto something else, or the process is
            # gone. Either way this record is not this Session's.
            return None
        return record

    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(self._settings.reply_window_poll_seconds)
            try:
                self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # One unreadable sweep must not end the watch: the registry is
                # another program's file and a momentary bad read is ordinary.
                _log.exception("a Reply Window sweep failed; the watch continues")
