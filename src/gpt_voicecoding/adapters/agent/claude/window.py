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

**This sweep is also the only observer of a Claude Session's death** (#20).
Nothing else on the Claude side ever raises `SessionEnded`, while Bridge Core's
consumer of it — mark the Session ENDED, close its window, answer every Relay
queued against it — has been complete and tested since the pipelines landed. The
producer is the missing half, and the evidence it needs is already computed here
on every sweep and thrown away one step before it could be reported: the same
registry lookup that decides a window separates "gone" from "busy" and then
flattens both into CLOSED.

**Death is reported only from positive evidence, and the asymmetry is the whole
design.** Two facts qualify — the target's process is no longer alive, or a
readable record for that pid names a *different* session id, so the pid has been
recycled onto something else. Everything else keeps reporting CLOSED exactly as
before, and in particular a record that is absent, half-written or unparseable
while the process is still alive is an ordinary momentary state: the registry
belongs to another program and is rewritten live. A **false** death is
destructive — it marks a living Session ENDED and answers away every Relay
waiting for it — while a **missed** death costs nothing beyond the behaviour this
module already had. So the rule is to fail toward "still alive", which is the
same fail-closed reasoning the Reply Window runs on, pointed at another question.

Because of that, liveness is probed for the *target's* pid on every sweep, not
only when a record happened to parse. A dead Session whose record Claude Code
deleted, and one whose record it left behind, are both deaths, and neither is
observable from the record alone.

Death is reported **once**, from the sweep and never from registration's
immediate report — raising it there would race Bridge Core's own registration of
the Session, whose handler drops an event for a Session it does not yet know — and
it is **not** paired with a `ReplyWindowChanged(CLOSED)`, because ending a Session
already closes its window in core state and emitting both would report one fact
twice in two vocabularies. After it fires, the target stops being watched.
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
from gpt_voicecoding.seams.agent import (
    AgentEvent,
    ReplyWindow,
    ReplyWindowChanged,
    SessionEnded,
)
from gpt_voicecoding.seams.identity import SessionTarget

_log = logging.getLogger(__name__)

#: The one registry status that means "awaiting the user's next instruction".
#: A whitelist rather than a blacklist: a new status this build has never seen
#: must read as CLOSED, and a blacklist would let it read as OPEN.
STATUS_MEANING_OPEN = "idle"

#: The two `SessionEnded.detail` strings, one per qualifying fact, so a reader can
#: tell a vanished process from a pid that now belongs to somebody else.
PROCESS_GONE = "the Session's process is no longer alive"
PID_RECYCLED = "pid {pid} now carries a different Claude Session"


def window_for(record: SessionRecord | None) -> ReplyWindow:
    """What one registry record says about a Session's willingness to take a turn."""
    if record is None or record.status != STATUS_MEANING_OPEN:
        return ReplyWindow.CLOSED
    return ReplyWindow.OPEN


def death_for(target: SessionTarget, record: SessionRecord | None, *, alive: bool) -> str | None:
    """Which evidence says this Session is gone, or `None` for "no evidence, so alive".

    `record` is `None` for every reason there might not be a readable one, and
    that on its own is never death: only `alive` can settle a missing record, and
    only a record that *does* parse can accuse the pid of belonging to somebody
    else. Pure, and told its evidence rather than fetching any — the sweep reads
    the registry once and probes liveness once, then asks both questions of the
    same facts.
    """
    if not alive:
        return PROCESS_GONE
    if record is not None and record.session_id != target.session_id:
        return PID_RECYCLED.format(pid=record.pid)
    return None


def _record_for_live_target(
    target: SessionTarget, record: SessionRecord | None, *, alive: bool
) -> SessionRecord | None:
    """That record, but only if it really is this live Session's — else `None`.

    A record for a recycled pid, or one belonging to a process that is gone, says
    nothing about the target's willingness to take a turn, and `window_for` must
    not be shown it.
    """
    if record is None or record.session_id != target.session_id or not alive:
        return None
    return record


class ReplyWindowWatcher:
    """Watches every registered Claude Session's registry record, and reports changes.

    Two reports come out of one sweep: the Reply Window as a level, and the
    Session's death as a one-shot event. They share a sweep because they share
    their evidence, and reusing the one poll interval is why death needs no
    configuration of its own.
    """

    def __init__(
        self,
        *,
        settings: ClaudeSettings,
        emit: Callable[[AgentEvent], None],
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

        Death is deliberately not reported from here even when the evidence is
        already in. Bridge Core registers the Session itself, and a `SessionEnded`
        for a Session it does not yet know is dropped with a log line — so a death
        raised at registration is a death silently lost. A target that is already
        dead reports CLOSED now and is reported ended on the first sweep.
        """
        if target in self._reported:
            return
        record, alive = self._observe(target)
        window = window_for(_record_for_live_target(target, record, alive=alive))
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
        """One sweep of every watched Session: death first, then the level.

        Death first because it is terminal. A target proved gone is reported once
        and dropped, and reporting a window for it in the same breath would say a
        fact Bridge Core has already drawn from the ending.
        """
        for target in tuple(self._reported):
            record, alive = self._observe(target)
            death = death_for(target, record, alive=alive)
            if death is not None:
                self._reported.pop(target, None)
                self._emit(SessionEnded(target=target, detail=death))
                continue
            window = window_for(_record_for_live_target(target, record, alive=alive))
            if self._reported.get(target) != window:
                self._reported[target] = window
                self._emit(ReplyWindowChanged(target=target, window=window))

    def _observe(self, target: SessionTarget) -> tuple[SessionRecord | None, bool]:
        """The sweep's only evidence: one registry read and one liveness probe.

        Gathered here, once per target, and handed to the two pure translators —
        so neither of them reaches for the filesystem, and the window and the
        death are decided from the same instant's facts rather than two.

        Liveness is asked of the *target's* pid rather than a parsed record's,
        because a dead Session whose record was deleted and one whose record was
        left behind are both deaths, and the record can prove neither.
        """
        if target.pid is None:  # pragma: no cover - SessionTarget refuses this already
            # No pid is no evidence, and no evidence must never read as death.
            return None, True
        alive = pid_is_live(target.pid)
        try:
            return read_record(self._settings.registry_directory, target.pid), alive
        except RegistryError:
            return None, alive

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
