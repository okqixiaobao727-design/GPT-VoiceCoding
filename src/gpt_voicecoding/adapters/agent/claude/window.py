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

**Transitions are what get emitted, and nothing else.** Registration seeds the
baseline this watcher compares against and stays silent; Bridge Core learns the
starting level by *asking* — the Agent seam's `reply_window`, answered here by
`level` — at the moment it enters the Session in its roster.

**A turn stop is an observed active status reaching `idle`.** Both `busy` and
`waiting` belong to the same active turn: `waiting` is that turn paused at a
permission dialog, not the next user prompt. A missing, torn or unrecognised
record does not prove activity and does not erase activity already observed.
That distinction prevents the first `idle` record written after registration
from being announced as a turn that stopped, while still surviving an ordinary
mid-turn registry rewrite. A whole active-to-idle cycle that begins and ends
between two polls remains invisible, the same sampling limit this level watcher
already has for Reply Window transitions.

That split is #27's. Registration announced the starting level once, and the
announcement was always dropped: an adapter is registered before Bridge Core
holds the Session, so the report arrived for a Session nobody knew, while having
been recorded here as sent — which left the sweep, whose whole rule is to emit
only on a change, unable to repeat it. A Session that was already idle when it
was registered therefore sat at Bridge Core's fail-closed CLOSED for the rest of
its life. A level has to be *pulled* to be bootstrapped; only its changes can be
pushed.

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

Death is reported **once**, from the sweep and never from registration — raising
it there would lose it to Bridge Core's own registration of the Session, whose
handler drops an event for a Session it does not yet know, which is the same
ordering that took the Reply Window's starting level above — and
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
    SessionStopped,
)
from gpt_voicecoding.seams.identity import SessionTarget

_log = logging.getLogger(__name__)

#: The one registry status that means "awaiting the user's next instruction".
#: A whitelist rather than a blacklist: a new status this build has never seen
#: must read as CLOSED, and a blacklist would let it read as OPEN.
STATUS_MEANING_OPEN = "idle"

#: Registry statuses that prove a turn is still in progress. `waiting` is the
#: permission-dialog pause described above, so it remains part of that turn.
STATUSES_MEANING_TURN_ACTIVE = frozenset(("busy", "waiting"))

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

    Three reports come out of one sweep: the Reply Window as a level, a turn
    stopping as an event, and the Session's death as a terminal event. They share
    a sweep because they share their evidence, and reuse one poll interval.
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
        #: Targets whose current turn has been observed in progress. Kept across
        #: unreadable records because absence is not evidence that a turn ended.
        self._active_turns: set[SessionTarget] = set()
        self._polling: asyncio.Task[None] | None = None

    def level(self, target: SessionTarget) -> ReplyWindow:
        """Where one Session's window stands right now, read fresh and reported to nobody.

        The answer to the Agent seam's `reply_window`, and the only way a
        Session's *first* level reaches Bridge Core. Pure query: it emits
        nothing, records nothing, and does not make `target` watched.
        """
        record, alive = self._observe(target)
        return window_for(_record_for_live_target(target, record, alive=alive))

    def watch(self, target: SessionTarget) -> None:
        """Start watching one Session, from where its window stands right now.

        **Seeded, not announced — and the difference is #27.** This records the
        current level so the sweep has a baseline to compare against, and emits
        nothing at all.

        It used to emit, on the argument that Bridge Core starts every Session at
        CLOSED and an already-idle Session would otherwise stay unreachable until
        it next changed state. The need was real; the mechanism could not meet
        it. Registration runs *before* Bridge Core holds the Session, so that
        report was dropped as belonging to a Session nobody knew — and because it
        had already been recorded here as sent, the sweep's `if self._reported.
        get(target) != window` could never repeat it. The report written to
        prevent a stuck-CLOSED Session was discarded every time, and the Session
        it was written for stayed CLOSED for the rest of its life.

        Bridge Core now *asks* — `level` above, called the instant the roster
        holds the Session — so the need is met by a question that cannot be
        dropped rather than by an announcement that always was. Emitting here as
        well would put a Reply Window changed on an unknown Session line in the
        log of every healthy launch, which would cost that line the evidential
        weight it earned in #21.

        Death is deliberately not reported from here either, and for the same
        underlying reason: a `SessionEnded` for a Session Bridge Core does not yet
        know is dropped with a log line, so a death raised at registration is a
        death silently lost. A target that is already dead seeds CLOSED now and is
        reported ended on the first sweep.
        """
        if target in self._reported:
            return
        record, alive = self._observe(target)
        live_record = _record_for_live_target(target, record, alive=alive)
        self._reported[target] = window_for(live_record)
        if live_record is not None and live_record.status in STATUSES_MEANING_TURN_ACTIVE:
            self._active_turns.add(target)

    def forget(self, target: SessionTarget) -> None:
        """Stop watching one Session. Its own process is untouched."""
        self._reported.pop(target, None)
        self._active_turns.discard(target)

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
        self._active_turns.clear()

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
                self._active_turns.discard(target)
                self._emit(SessionEnded(target=target, detail=death))
                continue
            live_record = _record_for_live_target(target, record, alive=alive)
            was_active = target in self._active_turns
            if live_record is not None:
                if live_record.status in STATUSES_MEANING_TURN_ACTIVE:
                    self._active_turns.add(target)
                elif live_record.status == STATUS_MEANING_OPEN:
                    self._active_turns.discard(target)
            window = window_for(live_record)
            previous_window = self._reported.get(target)
            if previous_window != window:
                self._reported[target] = window
                self._emit(ReplyWindowChanged(target=target, window=window))
            if was_active and live_record is not None and live_record.status == STATUS_MEANING_OPEN:
                self._emit(SessionStopped(target=target))

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
