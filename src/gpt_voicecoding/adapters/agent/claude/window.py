"""Whether a Claude Session can take a user turn right now, read from the registry.

Without this, nothing ever reports a Claude Session's Reply Window, `Session.
reply_window` stays at its fail-closed default of CLOSED, and Bridge Core queues
the user's words forever instead of delivering them. That is the mechanism
working as designed — a window nobody has observed is not one anything may claim
is open — but it leaves a Claude Session unreachable in the assembled engine, so
observing it is this module's whole job.

**A Reply Window is a level, not an event.** Claude Code publishes exactly that
in its registry record: a `status` field carrying `idle`, `busy`, `shell` or
`waiting`, rewritten sub-second. Reading the field that *is* the level beats
reconstructing it from a stream of events.

**`shell` is `idle` with a background task still running, and it is OPEN**
(#154). The build rewrites `idle` to it for the pid-file write when a
`local_bash` task outlives the turn, and only this reader ever sees the word —
`claude agents --json` maps it back to `busy`. Reading it as a state nobody had
looked at made it CLOSED by the whitelist's own correct rule, and left a Session
that was accepting input unreachable until its background task finished. The
window is OPEN because a Relay delivered during `shell` was *measured* to be
acted on as the next turn, not because the two words looked alike; the
measurement is beside `registry.PROVEN_AGAINST_VERSION`, and without it this
would have stayed CLOSED.

**Legacy has no behaviour to port here, and that is the citation** (ADR 0010).
Legacy learned a finished turn from the `Stop` hook event rather than from any
polled status word (`legacy@1d32845:bridge/daemon.py:194-211`, whose rule is
"`Stop` is a finished turn"), so it had no registry `status` to read, no fourth
word to miss, and a background shell could not delay a stop it was told about by
an event. The lateness this fixes is a v2 defect introduced when the rewrite
swapped that event for a poll — *dropped, because* the source of truth changed
and the polled vocabulary was never completed.

**`waiting` is CLOSED.** It means *a* dialog is on screen, and a dialog blocks
every Relay there is — verified live: three routes, all enqueued, none
delivered, none starting a turn, until a human answered the dialog. A Session in
that state is the opposite of one awaiting the user's next instruction. Surfacing
the dialog itself is the Approval Relay's job, not this one's, so nothing about
it leaks out of here as a window state.

**Which dialog it is matters everywhere except here** (#150). `waiting` has five
causes and only one of them is a permission prompt; a `/model` picker writes it
too. For the window they are one answer — every dialog takes no Relay — so this
level is unchanged. For the *Stop* below they are three answers, read off the
`waitingFor` label the same record carries (`waiting_labels.py`).

**Anything unrecognised is CLOSED**, including a record that has vanished or
cannot be read. The whole vocabulary of this seam rests on never claiming
readiness that has not been observed, and a status string this build has never
seen is not an observation of readiness.

**Transitions are what get emitted, and nothing else.** Registration seeds the
baseline this watcher compares against and stays silent; Bridge Core learns the
starting level by *asking* — the Agent seam's `reply_window`, answered here by
`level` — at the moment it enters the Session in its roster.

**A turn stop is an observed active status reaching an open one.** `idle` is
one such word and `shell` is the other, because the turn has equally ended in
both — waiting for the background task to drain before saying so delayed the
notice for exactly as long as that task ran (#154). Both `busy` and `waiting`
belong to the same active turn: `waiting` is that turn paused at a permission
dialog, not the next user prompt. `shell → idle` is therefore not a second stop:
the turn was already over, so nothing is left active for that edge to end.

A missing, torn or unrecognised record does not prove activity and does not
erase activity already observed.
That distinction prevents the first `idle` record written after registration
from being announced as a turn that stopped, while still surviving an ordinary
mid-turn registry rewrite. A whole active-to-idle cycle that begins and ends
between two polls remains invisible, the same sampling limit this level watcher
already has for Reply Window transitions.

**A Session *entering* `waiting` is the other stop, and it is a different fact**
(#77). The turn has not ended — that is why `waiting` stays active above — but a
dialog is on screen and the Session is stalled on the person. Announcing only
the first left a *question* with no route to any outlet: by the time the Session
reached `idle` the user had answered it at the keyboard, the transcript carried
the `tool_result`, and the reader correctly reported that it was waiting on
nothing. What a permission's Stop produces is Bridge Core's policy and not this
module's: since #191 this is the only event a dialog travels on, and the parked
hook's handle rides on it.

**But not every `waiting` is that stop** (#150). Reading the bare status as one
called the user about a slash-command picker they were looking at, and said only
"it has not said what it is waiting for yet" when it did. `_settle` below asks
the label which of the three a wait is: never a Stop, a Stop that can be named
now, or a Stop nothing can name yet — and the third is *held* and re-read on
this sweep's own cadence until it can be, or until a configured budget is spent.
That is `caught_up=False`'s documented meaning, *ask again, never guess*,
implemented at last.

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
import time
from collections.abc import Callable
from dataclasses import dataclass

from gpt_voicecoding.adapters.agent.claude import waiting_labels
from gpt_voicecoding.adapters.agent.claude.registry import (
    RegistryError,
    SessionRecord,
    pid_is_live,
    read_record,
)
from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings
from gpt_voicecoding.adapters.agent.claude.waiting_labels import (
    NOTHING_READ_YET,
    StopDisposition,
)
from gpt_voicecoding.seams.agent import (
    AgentEvent,
    ProgressObservation,
    ReplyWindow,
    ReplyWindowChanged,
    SessionEnded,
    SessionStopped,
    WaitingFor,
    WaitingKind,
)
from gpt_voicecoding.seams.identity import SessionTarget

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StopReading:
    """One transcript read's two answers for the Stop it produced (#151)."""

    waiting_for: WaitingFor
    progress: ProgressObservation


#: The registry statuses that mean "awaiting the user's next instruction", and
#: therefore also "the turn has ended". A whitelist rather than a blacklist: a
#: new status this build has never seen must read as CLOSED, and a blacklist
#: would let it read as OPEN.
#:
#: `shell` is `idle` wearing a second hat (#154): Claude Code rewrites the word
#: to it when the Session is idle *and* a `local_bash` background task is still
#: running. The turn has ended and the Session takes the next one, which is why
#: both words sit in one set rather than `shell` being a status of its own. The
#: measurement that says so is beside `registry.PROVEN_AGAINST_VERSION`.
#: Legacy read no polled status at all, so there is nothing to port (ADR
#: 0010; see this module's docstring).
#:
#: An empty status is the whitelist's rule holding, measured (#157). It is not a
#: fifth word but a record whose creating write carried no `status` key yet, and
#: it reads CLOSED like anything else outside the set. Nothing is missed by
#: that: the finished-turn edge below needs `was_active`, and a record can only
#: be statusless before its Session's first status write — so no sweep can have
#: seen that pid in a turn, and this cannot announce late the way `shell` did.
#: The measurement is beside `registry.PROVEN_AGAINST_VERSION`.
STATUSES_MEANING_OPEN = frozenset(("idle", "shell"))

#: Registry statuses that prove a turn is still in progress. `waiting` is the
#: permission-dialog pause described above, so it remains part of that turn.
STATUSES_MEANING_TURN_ACTIVE = frozenset(("busy", "waiting"))

#: The one status that means a dialog is on screen and the Session is stalled on
#: the person, not on the machine. Part of the turn for the Reply Window's
#: purposes (above) and a **Stop** for the Stop Notice's — see `poll_once`.
STATUS_MEANING_STALLED_ON_THE_USER = "waiting"


@dataclass(slots=True)
class _Dialog:
    """One wait, from the sweep that first saw it until the Session leaves it.

    `deadline` is when the catch-up budget runs out, fixed at the moment the
    wait was first seen so an unreadable record in the middle neither restarts
    it nor spends it twice. `settled` is what keeps a dialog that stands for as
    long as a person takes from becoming a stream of notices: it says this
    dialog has had its decision, which is as true of one announced as of one
    deliberately passed over in silence.
    """

    deadline: float
    settled: bool = False


#: The two `SessionEnded.detail` strings, one per qualifying fact, so a reader can
#: tell a vanished process from a pid that now belongs to somebody else.
PROCESS_GONE = "the Session's process is no longer alive"
PID_RECYCLED = "pid {pid} now carries a different Claude Session"


def _is_a_turn_in_progress(record: SessionRecord) -> bool:
    """Whether this record is evidence of a turn that is running.

    `busy` always is. `waiting` is too — a dialog is that turn paused on the
    person — **but only a dialog somebody has been told about** (#150). The
    finished-turn Stop exists to say a turn the user was following has ended,
    and a wait that was never announced started no such thing: a slash-command
    picker is opened from an idle Session by the person sitting in front of it,
    and a wait still inside its catch-up budget has deliberately said nothing
    yet. Counting either as a turn made the `idle` that followed a Stop with
    the emptiest notice there is — which is the reported symptom again, one
    transition later.

    So only a wait this reader can name on sight starts a turn here; a held one
    starts it in `_settle`, at the moment it is announced, and a `dialog open`
    never does. A turn already observed is never *erased* by any of this, only
    not started: a Session that went `busy` and then opened a picker is still
    mid-turn, and only `idle` ends it.
    """
    if record.status not in STATUSES_MEANING_TURN_ACTIVE:
        return False
    if record.status != STATUS_MEANING_STALLED_ON_THE_USER:
        return True
    return (
        waiting_labels.classify(record.waiting_for_label).disposition is StopDisposition.NAMED_NOW
    )


def window_for(record: SessionRecord | None) -> ReplyWindow:
    """What one registry record says about a Session's willingness to take a turn."""
    if record is None or record.status not in STATUSES_MEANING_OPEN:
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
        stopped_on: Callable[[SessionTarget, WaitingFor | None], StopReading] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._emit = emit
        #: The catch-up budget is measured in seconds, so something has to
        #: supply them. Injected because the sweep is driven by `poll_once` and
        #: a test that had to spend the real budget in real time would either
        #: be slow or prove nothing about it.
        self._clock = clock
        #: The progress and waiting facts for a Session that just stopped
        #: (#75, #151). Injected rather than read here: this class watches the
        #: registry, while both facts live in the Session's own transcript and
        #: the dialogs parked on the approval socket.
        self._stopped_on = stopped_on
        #: The last level reported for each target, so only transitions are sent.
        self._reported: dict[SessionTarget, ReplyWindow] = {}
        #: Targets whose current turn has been observed in progress. Kept across
        #: unreadable records because absence is not evidence that a turn ended.
        self._active_turns: set[SessionTarget] = set()
        #: The dialog each target was last seen holding, with its catch-up
        #: budget and whether it has been decided. Kept across unreadable
        #: records for the same reason `_active_turns` is: an unreadable record
        #: is not the dialog closing, and treating it as one would announce the
        #: same dialog again on the next readable sweep.
        self._at_a_dialog: dict[SessionTarget, _Dialog] = {}
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
        if live_record is not None and _is_a_turn_in_progress(live_record):
            self._active_turns.add(target)

    def forget(self, target: SessionTarget) -> None:
        """Stop watching one Session. Its own process is untouched."""
        self._reported.pop(target, None)
        self._active_turns.discard(target)
        self._at_a_dialog.pop(target, None)

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
        self._at_a_dialog.clear()

    def poll_once(self) -> None:
        """One sweep of every watched Session: death first, then the level, then the stops.

        The finished-turn edge tests membership in `STATUSES_MEANING_OPEN`
        rather than equality with `idle`, so a turn ending into `shell`
        announces at that transition (#154) and the `shell → idle` that follows
        finds no active turn left to end.

        Death first because it is terminal. A target proved gone is reported once
        and dropped, and reporting a window for it in the same breath would say a
        fact Bridge Core has already drawn from the ending.

        **Two things are a Stop, and they are different facts** (#77, from #75's
        review). A turn reaching `idle` is the one this watcher always had. The
        other is a Session *entering* `waiting`: a dialog is on screen and the
        Session is stalled on the person rather than on the machine, which is
        precisely what a Stop Notice exists to say. Announcing only the first
        meant a question reached no outlet at all — by the time the Session went
        `idle` the user had already answered it by hand, the transcript carried
        the `tool_result`, and the reader correctly reported that it was waiting
        on nothing.

        `waiting` stays inside `STATUSES_MEANING_TURN_ACTIVE`, because for the
        *Reply Window* it is still mid-turn: a dialog takes no Relay. The two
        questions are asked of one status and answered differently, which is why
        this is two lines here rather than a change to that set.

        **One dialog, one decision**, tracked on `_at_a_dialog`. A dialog waits
        for a human, so it is readable on every sweep for as long as the person
        takes; announcing it each time would turn one decision into a stream of
        notices. So every sweep that reads `waiting` goes to `_settle`, which
        decides at most once per dialog — the sweeps before that decision are
        the re-reads #150's catch-up budget is made of. The dialog is held from
        the last *readable* status for the same reason `_active_turns` is: an
        unreadable record is the registry being rewritten, not a dialog closing
        and reopening.

        What the user hears about a permission is Bridge Core's policy
        (`core/bridge.py`), not this adapter's; this module supplies the wait,
        handle included.
        """
        for target in tuple(self._reported):
            record, alive = self._observe(target)
            death = death_for(target, record, alive=alive)
            if death is not None:
                self._reported.pop(target, None)
                self._active_turns.discard(target)
                self._at_a_dialog.pop(target, None)
                self._emit(SessionEnded(target=target, detail=death))
                continue
            live_record = _record_for_live_target(target, record, alive=alive)
            was_active = target in self._active_turns
            if live_record is not None:
                if _is_a_turn_in_progress(live_record):
                    self._active_turns.add(target)
                elif live_record.status in STATUSES_MEANING_OPEN:
                    self._active_turns.discard(target)
                if live_record.status == STATUS_MEANING_STALLED_ON_THE_USER:
                    self._at_a_dialog.setdefault(
                        target,
                        _Dialog(
                            deadline=self._clock() + self._settings.stop_catch_up_budget_seconds
                        ),
                    )
                else:
                    self._at_a_dialog.pop(target, None)
            window = window_for(live_record)
            previous_window = self._reported.get(target)
            if previous_window != window:
                self._reported[target] = window
                self._emit(ReplyWindowChanged(target=target, window=window))
            if live_record is None:
                continue
            if live_record.status == STATUS_MEANING_STALLED_ON_THE_USER:
                self._settle(target, live_record)
            elif was_active and live_record.status in STATUSES_MEANING_OPEN:
                self._emit(self._stopped_event(target, self._read_stop(target)))

    def _settle(self, target: SessionTarget, record: SessionRecord) -> None:
        """Whether this wait is a Stop, and whether it can be said yet.

        Called on every sweep the record says `waiting`, not only on the edge
        into it, because two of the three answers are *not yet* and a *not yet*
        has to be asked again.

        **The label decides which question this is** (#150,
        `waiting_labels.py`). A `dialog open` is the user driving their own TUI
        and is settled by saying nothing at all. A `permission prompt` is
        announced the moment it is seen, exactly as every `waiting` was before
        this. Everything else — a question whose text is still in flight, a
        label nobody has measured, no label at all — is real and not yet
        nameable, and *that* is what the catch-up budget is for.

        **Holding is the whole fix.** `caught_up=False` has always meant *ask
        again, never guess*, and nothing implemented it: the sweep announced the
        `UNKNOWN` immediately and Bridge Core rendered "it has not said what it
        is waiting for yet" about a Session that was not waiting for anything.
        Here the re-read happens on the sweep's own cadence — the sweep re-reads
        the record every tick regardless, and the transcript read behind
        `_read_stop` is cached on the file's identity — until the reading is
        complete or the budget is spent. Only then, and once, does the honest
        `UNKNOWN` go out (`legacy@1d32845:bridge/daemon.py:1933-1936,
        2116-2160`, **ported** onto this sweep).
        """
        dialog = self._at_a_dialog[target]
        if dialog.settled:
            return
        reading = waiting_labels.classify(record.waiting_for_label)
        if reading.disposition is StopDisposition.NEVER_A_STOP:
            # Decided, and decided to say nothing. Marked so the label going
            # unreadable on a later sweep cannot turn it into a notice.
            dialog.settled = True
            return
        if reading.disposition is StopDisposition.NAMED_NOW:
            self._announce(target, dialog, self._read_stop(target, reading.waiting_for))
            return
        found = self._read_stop(target, NOTHING_READ_YET)
        if found.waiting_for.caught_up and found.waiting_for.kind is not WaitingKind.NONE:
            self._announce(target, dialog, found)
            return
        if self._clock() >= dialog.deadline:
            self._announce(
                target,
                dialog,
                StopReading(waiting_for=NOTHING_READ_YET, progress=found.progress),
            )

    def _announce(self, target: SessionTarget, dialog: _Dialog, reading: StopReading) -> None:
        """Say one dialog once, and let the turn it belongs to end afterwards.

        The turn is marked in progress *here* rather than from the record,
        because a wait nobody has been told about is not a turn anybody is
        following — see `_is_a_turn_in_progress`.
        """
        dialog.settled = True
        self._active_turns.add(target)
        self._emit(self._stopped_event(target, reading))

    @staticmethod
    def _stopped_event(target: SessionTarget, reading: StopReading) -> SessionStopped:
        return SessionStopped(
            target=target,
            progress=reading.progress,
            waiting_for=reading.waiting_for,
        )

    def _read_stop(self, target: SessionTarget, roster: WaitingFor | None = None) -> StopReading:
        """Both facts from the one reader call that observed this Stop."""
        fallback = StopReading(
            waiting_for=roster if roster is not None else WaitingFor(),
            progress=ProgressObservation(),
        )
        if self._stopped_on is None:
            return fallback
        try:
            return self._stopped_on(target, roster)
        except Exception:  # noqa: BLE001 - a poorer notice beats no notice
            _log.exception("could not read what %s stopped on; announcing it without", target)
            return fallback

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
