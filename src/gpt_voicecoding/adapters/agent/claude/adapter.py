"""The Claude Agent seam, selecting its private wire. Mechanism only; no queueing.

**What proves delivery on the ordinary route, and it is narrow.** An Answer
Relay ordinarily writes into the inbox socket Claude Code binds for every
Session, and a write that is accepted proves nothing at all: the line was taken
by a socket, not read by a Session.
#71 measured the two things that do prove it — the `held → delivered`
`peer_message_status` receipt, and the target's own transcript entry whose
`origin.from` is our reply address and whose `origin.msg_id` is the id we minted.
Everything weaker is UNKNOWN, and P9 never re-sends an UNKNOWN on this system's
own authority. `inbox.py` holds the wire and the whole argument.

**A late settlement is raised, and that is why the listening continues.** The
wait is bounded, and a spent wait is UNKNOWN. But a *held* Relay is parked in
front of a person and has not finished happening: it settles minutes later to
`delivered` when they release it, or to `denied` / `expired` when they refuse or
never answer — and a held message expires after about five minutes. An engine
that stopped listening would report "parked" for words that were later thrown
away, which is exactly the implied delivery #71 forbade. So a grade that was not
terminal keeps a listener for a second and longer budget, and upstream's own
settlement is raised upward whichever way it went. A grade that *was* terminal
is never revisited.

**Two public verbs over two wires, selected here.** An ordinary Answer Relay
rides the Session's inbox socket. While a Session is parked on its own question,
the same verb selects the held **`PermissionRequest` hook** delegated to
`approval.py`. The Approval Relay uses that hook for permission verdicts too; it
is the route where *we* are the server, with the hook process holding the dialog
open while the response travels back down its connection.

**Words on one wire, authority on the other, and that is structural.** A peer
message is announced to the receiving Session as not typed by its user, and
upstream enforces it — a Session asked to approve a pending dialog on a peer's
say-so refuses and names it permission laundering. So the inbox carries the
user's words and never their authority, and a Session's *question* can never be
answered over it (#71). That is why the hook route exists and why no amount of
later work on this socket could replace it.

The Approval Relay is also the one verb whose route this adapter cannot start.
The hook exists for a Session only when it is installed in that Session's config
directory (ADR 0011, `installation/claude_hooks.py`), and it reaches us only when
this engine published an address for it to dial — so a Session in a directory
this product was never installed into has no Approval Relay, and the honest
report for a verdict aimed at it is a classified failure naming what is not
there.

**The Reply Window is reported from the registry**, by `window.py`. Without it
nothing ever tells Bridge Core that a Claude Session is ready for a user turn, so
every Relay queues against a window that is fail-closed and never observed to
open. That is the mechanism working correctly and a Session nothing can reach;
watching the registry is what makes the two stop being the same thing.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from gpt_voicecoding.adapters.agent._progress import source_degradation
from gpt_voicecoding.adapters.agent._project import ProjectNames
from gpt_voicecoding.adapters.agent.claude import (
    bootstrap,
    children,
    inbox,
    stop_analysis,
    transcript_tail,
)
from gpt_voicecoding.adapters.agent.claude import discovery as claude_discovery
from gpt_voicecoding.adapters.agent.claude.approval import (
    CWD_FIELD,
    MESSAGING_SOCKET_FIELD,
    MESSAGING_TOKEN_FIELD,
    PID_FIELD,
    SESSION_ID_FIELD,
    TRANSCRIPT_PATH_FIELD,
    ApprovalError,
    ApprovalListener,
)
from gpt_voicecoding.adapters.agent.claude.bootstrap import (
    AddressHeld,
    publish_address,
    withdraw_address,
)
from gpt_voicecoding.adapters.agent.claude.inbox import InboxError, ReplyInbox
from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings
from gpt_voicecoding.adapters.agent.claude.transcript import (
    TranscriptReader,
    TranscriptUnavailable,
)
from gpt_voicecoding.adapters.agent.claude.window import ReplyWindowWatcher, StopReading
from gpt_voicecoding.installation import claude_hooks
from gpt_voicecoding.seams.agent import (
    AgentEvent,
    ApprovalRequest,
    ApprovalVerdict,
    LaneDiscovery,
    LaneUnavailable,
    ProgressAvailability,
    ProgressCapture,
    ProgressObservation,
    RelayReceipt,
    RelayRoute,
    ReplyWindow,
    SessionEnded,
    SessionInspection,
    SessionLifecycle,
    SessionState,
    WaitingFor,
    WaitingKind,
)
from gpt_voicecoding.seams.delivery import Delivery, DeliveryReceipt
from gpt_voicecoding.seams.events import EventSink
from gpt_voicecoding.seams.identity import AgentKind, RequestId, SessionTarget
from gpt_voicecoding.seams.verify import VerifyOutcome, VerifyResult

_log = logging.getLogger(__name__)

#: What a verdict aimed at a Session with no hook route is told. It names the two
#: things a launch has to have done, because "no request is waiting" read alone
#: looks like a race that was lost rather than a route that was never there.
APPROVAL_UNROUTED = (
    "no permission dialog is parked on this engine for that Session; a Claude Session "
    "answers by voice only when this product is installed in its config directory "
    "(bridge-install status) and this engine has published an approval address"
)
SUPPLEMENT_UNAVAILABLE = (
    "the Claude inbox route has no mid-turn verb: a Session that is mid-turn queues a peer "
    "message until its turn ends, so nothing this adapter can send reaches the turn in flight"
)


@dataclass(frozen=True, slots=True)
class UnpublishedAddress:
    """Why this engine published no Claude approval address (#204).

    One record for all three causes `connect` can end on — the approval socket
    would not bind, a peer engine holds the address (#202), or the address file
    could not be written — because every reader asks the same question of them:
    can a Session reach this engine at all? Both Claude routes read the one
    published address (ADR 0019), so the answer is no, whichever cause it was,
    and the roster is empty *because* of it.

    Kept as one field rather than one per cause: the absence of this record is
    "published", its presence is "not published, because …", and a reader that
    had to check two fields would have two ways to be told the same thing.
    """

    #: The because-clause, rendered by the site that caught the failure and
    #: carrying the path it was about, so the report is actionable without the log.
    reason: str


@dataclass(frozen=True, slots=True)
class _Outstanding:
    """One Relay on the wire, and everything a receipt for it is correlated by.

    The four travelled together through every step of the settlement — the wait,
    the grading, the late listener — because they are one thing: which words went
    where, under which id, and on which socket an answer about them may arrive.
    """

    target: SessionTarget
    replies: ReplyInbox
    msg_id: str
    request_id: RequestId


@dataclass(frozen=True, slots=True)
class SessionReport:
    """What one Session's own `SessionStart` hook said about where it can be reached.

    Every field but the id is optional, because every one of them can honestly
    be absent: a build that does not export the messaging variables, a Session
    whose first turn has not created a transcript, a payload without a cwd. A
    partial report is worth keeping — the fields that did arrive are still the
    ones nothing else carries.
    """

    session_id: str
    #: The `claude` process this Session runs as. Not optional in practice and
    #: optional in the type: it comes from `CLAUDE_PID`, which every build
    #: measured so far exports, and a report without it cannot be turned into a
    #: `SessionTarget` at all (`seams/identity.py:124`).
    pid: int | None = None
    workspace: Path | None = None
    transcript_path: Path | None = None
    messaging_socket: Path | None = None
    messaging_token: str | None = None

    @property
    def target(self) -> SessionTarget | None:
        """The exact Session this report is about, when it said enough to say."""
        if self.pid is None:
            return None
        return SessionTarget(agent=AgentKind.CLAUDE, session_id=self.session_id, pid=self.pid)


@dataclass(frozen=True, slots=True)
class _SessionRead:
    """Everything derived from one opening of a Session transcript."""

    waiting_for: WaitingFor
    progress: ProgressObservation
    last_activity: datetime | None = None
    source_read: bool = False


def _pid_in(payload: dict[str, object], field: str) -> int | None:
    """A pid off the registration wire. Anything that is not a live pid is `None`."""
    value = payload.get(field)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _path_in(payload: dict[str, object], field: str) -> Path | None:
    value = payload.get(field)
    return Path(value) if isinstance(value, str) and value.strip() else None


class ClaudeAgentAdapter:
    """Claude, behind the Agent seam. Implements `AgentAdapter` and `Connectable`."""

    def __init__(
        self,
        *,
        progress_capture: ProgressCapture,
        sink: EventSink | None = None,
        settings: ClaudeSettings | None = None,
        claude_config_directory: Path | None = None,
        installation_base_dir: Path | None = None,
    ) -> None:
        self._sink = sink
        self._settings = settings or ClaudeSettings()
        self._progress_capture = progress_capture
        self._claude_config_directory = (
            claude_config_directory
            if claude_config_directory is not None
            else claude_hooks.default_config_directory(os.environ)
        )
        self._installation_base_dir = installation_base_dir
        #: The inbox socket each registered Session's own `SessionStart` hook
        #: reported. Read, never built: 2.1.245 derives the directory from
        #: `CLAUDE_CODE_TMPDIR` or `$XDG_RUNTIME_DIR` and accepts
        #: `--messaging-socket-path`, so a constructed path is a guess.
        self._inboxes: dict[SessionTarget, Path] = {}
        #: One reply socket per socket *directory*, because that is the namespace
        #: a receipt may be addressed inside. Bound on first use rather than on
        #: `connect`: the directory is a fact about a Session's registration, and
        #: this adapter may be running before any Session has registered.
        self._replies: dict[Path, ReplyInbox] = {}
        self._binding = asyncio.Lock()
        #: Late-receipt listeners in flight, so none outlives this adapter.
        self._listening: set[asyncio.Task[None]] = set()
        #: What each Session has spawned, and what it has finished spawning
        #: (#79). Stateful because a child that is over cannot start again, and
        #: remembering that is what keeps the parent's transcript off the
        #: cadence for a Session whose children are all done.
        self._children = children.Children()
        #: The lane's one opener of a transcript file, shared by what a Session
        #: stopped on (#75) and how far along it is (#76).
        self._transcripts = TranscriptReader()
        #: The project half of every Session Name this lane composes, read once
        #: per workspace and kept for the life of the adapter (#78). Held here
        #: rather than inside `discovery` so the cache outlives one tick.
        self._projects = ProjectNames()
        self._windows = ReplyWindowWatcher(
            settings=self._settings, emit=self._emit, stopped_on=self.stop_reading
        )
        #: The socket this adapter owns: hook processes dial in here holding a
        #: dialog open, so this adapter is the server on this route.
        self._approvals = ApprovalListener(
            settings=self._settings,
            resolve=self._registered_as,
            emit=self._emit,
            register=self._session_started,
        )
        #: What each Session's own `SessionStart` hook reported, by session id.
        #: **Not a roster**: `claude agents --json` is the roster, and it sees
        #: Sessions whose hook never ran. This is the two things that command
        #: does not carry — the inbox socket, and the transcript path (#71).
        self._reported: dict[SessionTarget, SessionReport] = {}
        #: Why `connect` published no approval address, if it published none
        #: (#204, generalising #202). `None` is both "this engine published it"
        #: and "connect has not run", which is the same thing to every reader:
        #: there is no failure to name.
        self._unpublished: UnpublishedAddress | None = None

    # -- lifecycle --------------------------------------------------------

    async def connect(self) -> None:
        """Start watching Reply Windows, and open the socket dialogs are parked on.

        The approval socket is bound here because it lives in a directory of
        this engine's own, and its address has to be knowable *before* any Session
        launches: the launch is what carries it to the hook. A socket bound on
        first use would be one no launch could ever have named.

        A socket that will not bind is logged and not raised. It costs the
        Approval Relay and nothing else — every verdict aimed at this adapter is
        then a classified failure — and taking the whole Agent seam down over it
        would trade one lost route for two working ones.
        """
        await self._windows.start()
        try:
            await self._approvals.start()
        except ApprovalError as refused:
            # Recorded, not merely logged (#204): nothing is published after
            # this, so no Session can register with this engine either, and
            # `verify` is where ADR 0003 says an operator finds that out.
            self._unpublished = UnpublishedAddress(
                reason=f"the approval socket at {self.approval_socket_path()} would not bind "
                f"({refused})"
            )
            _log.warning("no Approval Relay this run: %s", refused)
            return
        # Only now, and only if it bound: a published address nobody is listening
        # on costs every permission dialog in this config directory a full dial
        # timeout, which is worse than the silence of publishing nothing.
        try:
            published = publish_address(self.approval_socket_path(), self._settings)
        except AddressHeld as held:
            # First live engine wins (#202). The address is one file per user per
            # machine and the hook can read no other, so displacing a peer that
            # is still answering would silently move every permission dialog on
            # this machine onto this engine.
            self._unpublished = UnpublishedAddress(
                reason=f"another engine is listening at {held.holder}"
            )
            _log.warning("no Approval Relay this run: %s", held)
        except OSError as refused:
            self._unpublished = UnpublishedAddress(
                reason=f"the approval address at {bootstrap.address_path()} could not be written "
                f"({refused})"
            )
            _log.warning("the approval address could not be published: %s", refused)
        else:
            _log.info("approval address published at %s", published)

    async def aclose(self) -> None:
        """Stop everything this adapter started, and take its sockets back out.

        A Session's inbox is Claude Code's. Closing this adapter lets go of
        connections to it and nothing more — there is nothing there to reap.
        What *is* ours is the reply socket bound beside it and the key published
        for it, both in directories belonging to somebody else, so both are
        removed here rather than left for the next process to trip over.

        **Each cancelled task is waited for, not merely cancelled.** A
        cancellation is a request, delivered the next time the task runs, so
        cancelling and returning would make `aclose` mean "asked them to stop"
        while a listener was still holding a connection to a Session's own
        process. Waiting is what makes it mean "they have stopped": nothing this
        adapter started is still running when this returns.
        """
        listening = list(self._listening)
        for task in listening:
            task.cancel()
        for task in listening:
            with suppress(asyncio.CancelledError):
                await task
        self._listening.clear()
        await self._windows.aclose()
        # Closes last, and releases every parked dialog to its human
        # on the way out: an engine shutting down must never be the reason a
        # permission prompt resolves without the person it was asked of.
        await self._approvals.aclose()
        # And the address goes with it. A published address nobody answers is a
        # dial into nothing, paid by every permission dialog in this config
        # directory until something else publishes over it.
        withdraw_address(self.approval_socket_path())
        for replies in self._replies.values():
            await replies.aclose()
        self._replies.clear()
        self._inboxes.clear()

    # -- the Session roster this adapter can reach ------------------------

    def register_session(self, target: SessionTarget, socket_path: Path) -> None:
        """Record where one Session's inbox listens.

        The path is the registration this adapter needs and cannot discover:
        Claude Code chooses it inside the Session's own process, from
        `CLAUDE_CODE_TMPDIR`, `$XDG_RUNTIME_DIR` or `--messaging-socket-path`,
        so it is read and never built. It arrives from that Session's
        `SessionStart` hook (`_session_started`) — the reference implementation
        got it from a launch wrapper it owned, and v1.0 launches nothing (#72).
        """
        if target.agent is not AgentKind.CLAUDE:
            raise ValueError(f"{target.agent} sessions are not this adapter's to reach")
        self._inboxes[target] = socket_path
        # Registering is also what starts reporting this Session's Reply Window,
        # which is what makes it reachable at all: until a window is observed,
        # Bridge Core holds every Relay against the fail-closed default.
        self._windows.watch(target)
        _log.info(
            "registered Session inbox agent=%s session_id=%s pid=%s socket=%s",
            target.agent,
            target.session_id,
            target.pid,
            socket_path,
        )

    def _session_started(self, payload: dict[str, object]) -> None:
        """One Session's `SessionStart` hook reported where it can be reached.

        **This adds no roster row.** A Session appears in the roster because
        `claude agents --json` lists it, which it does whether or not this hook
        ran — including for every Session that started before this engine did.
        What lands here is what that command cannot say: the Session's own inbox
        socket, and the `transcript_path` #75 and #76 read.

        **This is where a Session becomes reachable**, and it is the only place
        left that can be. The address of a Session's inbox is generated by
        Claude Code inside that Session's own process, so nothing outside it can
        discover one; the reference implementation learned it from a launch
        wrapper it owned, and v1.0 does not launch Sessions (#72). The hook is
        the Session telling us itself.

        **Keyed by the exact target, never by the session id.** `--resume` forks
        a second process under one session id, and those two processes have two
        inbox sockets. Storing the last report to arrive under the shared id
        would put one process's socket behind the other's pid — a Relay
        delivered, successfully, into the wrong conversation. The pid arrives
        from `CLAUDE_PID` (`registration.PID_VARIABLE`) for exactly this reason.
        """
        session_id = payload.get(SESSION_ID_FIELD)
        if not isinstance(session_id, str) or not session_id.strip():
            return
        report = SessionReport(
            session_id=session_id.strip(),
            pid=_pid_in(payload, PID_FIELD),
            workspace=_path_in(payload, CWD_FIELD),
            transcript_path=_path_in(payload, TRANSCRIPT_PATH_FIELD),
            messaging_socket=_path_in(payload, MESSAGING_SOCKET_FIELD),
            messaging_token=payload.get(MESSAGING_TOKEN_FIELD)
            if isinstance(payload.get(MESSAGING_TOKEN_FIELD), str)
            else None,
        )
        target = report.target
        if target is None:
            # Nothing can be done with it: a Claude target needs a pid, so this
            # report cannot be attached to a Session even to record its
            # transcript path. Said out loud because it means a build that does
            # not export `CLAUDE_PID`, which is news about the far side.
            _log.warning(
                "a registration for session_id=%s carried no pid, so it names no Session; "
                "the Session stays listed and unreachable",
                report.session_id,
            )
            return
        self._reported[target] = report
        _log.info(
            "registration received for session_id=%s pid=%s workspace=%s transcript=%s socket=%s",
            report.session_id,
            report.pid,
            report.workspace,
            report.transcript_path,
            report.messaging_socket,
        )
        if report.messaging_socket is not None:
            self.register_session(target, report.messaging_socket)

    def reported(self, target: SessionTarget) -> SessionReport | None:
        """What that Session's own hook said about itself, if it ever ran."""
        return self._reported.get(target)

    def forget_session(self, target: SessionTarget) -> None:
        """Stop holding a route to one Session. The Session itself is untouched.

        Called by `_emit` whenever this adapter reports a Session ended, which is
        what keeps the caches below bounded by the machine's live Sessions rather
        than by everything that has ever registered (#98).
        """
        self._inboxes.pop(target, None)
        self._approvals.clear_released_question(target)
        self._windows.forget(target)
        report = self._reported.pop(target, None)
        if report is not None:
            self._transcripts.forget(report.transcript_path)

    def reachable(self) -> tuple[SessionTarget, ...]:
        """Every Session this adapter holds an inbox address for."""
        return tuple(self._inboxes)

    def reply_window(self, target: SessionTarget) -> ReplyWindow:
        """Where this Session's Reply Window stands right now, read from the registry.

        The seam's level query, and how a Session's *starting* window reaches
        Bridge Core at all: registration cannot announce it, because it runs
        before Bridge Core holds the Session (#27), so Bridge Core asks instead.

        A Session this adapter holds no inbox address for is CLOSED, whatever
        its registry record happens to say. The window is a claim about
        reachability, and reading someone else's record is not the same as being
        able to reach them — the same fail-closed rule the whole seam runs on.
        """
        if target not in self._inboxes:
            return ReplyWindow.CLOSED
        return self._windows.level(target)

    def question_answerable(self, target: SessionTarget) -> bool:
        """Whether the exact question hook for this Session is still parked."""
        return self._approvals.question_answerable(target)

    async def sweep_question_budget(
        self, budget_seconds: float
    ) -> tuple[tuple[SessionTarget, WaitingFor], ...]:
        """Release Claude questions past Core's configured budget."""
        return await self._approvals.sweep_question_budget(budget_seconds)

    # -- the seam ---------------------------------------------------------

    async def discover(self) -> LaneDiscovery:
        """Every Claude Session running, from Claude Code's own roster.

        The roster is `discovery.py`'s whole business — it owns the command and
        the mapping. Nothing about being *reachable* enters there: a Session is
        listed because it exists, and whether this adapter holds an inbox address
        for it is a question `answer_relay` answers with a receipt (#68).

        What is added here is the one thing the roster cannot say: **what a
        stopped Session stopped on** (#75). The roster reports `waiting` without
        naming a tool, a dialog or a prompt, so a row that stopped is read
        against its own transcript and against any dialog parked on this
        engine's approval socket. It happens on this verb rather than on
        `inspect` because this is the verb Bridge Core actually calls — every
        five seconds, for the whole machine (`core/bridge.py:442`) — and
        `inspect` reads the same rows.

        The other thing the roster cannot say is **what a Session has spawned**
        (#79). A Task subagent is not a process and is not on the official
        roster — measured, `children.py` — so its rows are found from the
        parent's own transcript tree and listed straight after it. The two reads
        are mutually exclusive and that is what keeps both off the hot path: a
        stopped Session gets its transcript read and can have no live child,
        while a `RUNNING` one is never opened and is the only kind that can.
        """
        lane = await claude_discovery.discover(projects=self._projects)
        if not lane.enumerated:
            return lane
        rows: list[SessionInspection] = []
        for row in lane.rows:
            rows.append(self._row_with_stop(row))
            rows.extend(self._children_under(row))
        projected = tuple(rows)
        return replace(
            lane,
            rows=projected,
            degraded=source_degradation(projected, lane.degraded),
        )

    def _children_under(self, row: SessionInspection) -> tuple[SessionInspection, ...]:
        """Every Child Process this row is running, or none because it is not running.

        The first of #79's two liveness conditions, held here because this is
        where the parent's state is: a Session that is not mid-turn has no child
        mid-turn, and a child whose file has no ending — the parent was
        interrupted — stops being listed the moment its parent stops working.
        The second condition is the child's own last record (`children.py`).
        """
        if row.state is not SessionState.RUNNING:
            return ()
        return self._children.under(row, self._transcript_path(row.target))

    def _row_with_stop(self, row: SessionInspection) -> SessionInspection:
        """One roster row, with everything its own transcript says about it.

        **A Session mid-turn is not stopped on anything**, so a `RUNNING` row is
        returned untouched and its transcript is never opened. That is what keeps
        this off the hot path: on a machine of busy Sessions, the five-second
        cadence costs one roster command and no file reads at all. #76 rides on
        the same gate rather than a wider one of its own — a roster row is the
        cheap projection, and the `progress` verb is where a running Session can
        still be asked (`inspect`).
        """
        if row.state is SessionState.RUNNING:
            return row
        return self._read_into(row)

    def _read_into(self, row: SessionInspection) -> SessionInspection:
        """One row, filled from one read of the transcript its Session named.

        **One read answers both questions.** What the Session stopped on (#75)
        and how far along it is (#76) come from the same records at the same
        moment; two reads would be two whole-file parses per tick describing two
        moments of a file the Session is still appending to.

        **Nothing here bypasses the reader's cache, and nothing needs to.** That
        cache is keyed on the file's own identity as the filesystem reports it
        (`transcript.py:36-40`), so a hit means the Session has not written a
        byte since — and a transcript that has not changed cannot have changed
        what its tail says. Forgetting the parse first would buy no freshness and
        cost a second pass over a file measured at 186 MB on this machine.

        **A `RUNNING` row is read for progress and never for a stop.** The two
        questions do not have the same answer mid-turn: an outstanding `tool_use`
        with no result yet is a tool *running*, and reading it as a stop would
        report a working Session as one waiting on the user's permission. Only
        `inspect` gets here with a `RUNNING` row at all — the cadence returns one
        untouched — and that is the row the gate exists for.
        """
        reading = self._read_session(row.target, row.waiting_for, state=row.state)
        return replace(
            row,
            waiting_for=reading.waiting_for,
            progress=reading.progress,
            last_activity=reading.last_activity if reading.source_read else row.last_activity,
        )

    def _read_session(
        self,
        target: SessionTarget,
        base: WaitingFor,
        *,
        state: SessionState,
    ) -> _SessionRead:
        """One transcript opening, shared by roster, inspect, and Stop events."""
        try:
            records = self._transcripts.records(self._transcript_path(target))
        except TranscriptUnavailable as unreadable:
            waiting = base if state is SessionState.RUNNING else self._overlay(target, base, base)
            return _SessionRead(
                waiting_for=waiting,
                progress=ProgressObservation.unreadable(str(unreadable)),
            )
        if records is None:
            waiting = base if state is SessionState.RUNNING else self._overlay(target, base, base)
            return _SessionRead(waiting_for=waiting, progress=ProgressObservation())

        found = base if state is SessionState.RUNNING else stop_analysis.analyse(records)
        waiting = base if state is SessionState.RUNNING else self._overlay(target, base, found)
        anchored_question = (
            found
            if waiting.kind is WaitingKind.QUESTION
            and found.kind is WaitingKind.QUESTION
            and replace(waiting, approval_id=None) == replace(found, approval_id=None)
            else None
        )
        if anchored_question is None:
            entries, omission, moved = transcript_tail.recent(
                records,
                capture=self._progress_capture,
            )
        else:
            entries, omission, moved = transcript_tail.recent_before_question(
                records,
                question=anchored_question,
                capture=self._progress_capture,
            )
        return _SessionRead(
            waiting_for=waiting,
            progress=ProgressObservation.from_capture(
                recent=entries,
                omission=omission,
                read_at=datetime.now(UTC),
            ),
            last_activity=moved,
            source_read=True,
        )

    def _transcript_path(self, target: SessionTarget) -> Path | None:
        """Where this Session's own record is, as its registration named it."""
        report = self._reported.get(target)
        return report.transcript_path if report else None

    def stop_reading(self, target: SessionTarget, roster: WaitingFor | None = None) -> StopReading:
        """What one Stop is about and what was said, from one transcript read."""
        base = roster if roster is not None else WaitingFor()
        reading = self._read_session(target, base, state=SessionState.IDLE)
        return StopReading(waiting_for=reading.waiting_for, progress=reading.progress)

    def _overlay(
        self,
        target: SessionTarget,
        base: WaitingFor,
        found: WaitingFor,
    ) -> WaitingFor:
        """What the transcript analysis and any parked dialog say this Session stopped on.

        Two sources, and they are ranked rather than merged, because they can
        disagree and the ranking is the behaviour (#75):

        0. **A question parked on the approval socket wins over everything**
           (#77). `AskUserQuestion` raises the same `PermissionRequest` hook a
           `Write` does (measured on 2.1.246), so the dialog arrives here with
           the whole prompt and its options — while the transcript says nothing about the call
           until it has flushed, and by then the person at the keyboard has
           usually answered it. The hook's question is the thing itself and the
           record's is a reconstruction of it, so the hook wins whether the two
           name the same prompt or different ones. #128 addresses the held writer
           through the Answer Relay, with Claude's `prompt_id` when supplied or
           a listener-private correlator otherwise.
        1. **A readable question wins outright.** The reference implementation's
           precedence — a decision only the user can supply outranks a permission
           call beside it (`legacy@1d32845:bridge/transcript.py:1691-1692`) — and
           a hook holding a dialog open never overrides one.
        2. **A permission read from the transcript takes the `approval_id` from
           the dialog**, which is the only place a handle exists at all — and
           the dialog is then also the authority on which call that handle
           belongs to (`_announced_as`).
        3. **A dialog with nothing readable behind it is still a stop.** This is
           the first-turn case: `PermissionRequest` fires when the dialog opens,
           before the `tool_use` record is flushed. The reference implementation
           scraped the tool name out of an English Notification sentence
           (`legacy@1d32845:bridge/daemon.py:143-145,213-223,2049-2051`);
           **adapted**, because v2's hook carries the same fact plus the handle,
           delivered by the process holding the dialog. `caught_up` is `True`
           here: the seam's rule is that a reader claiming a kind must have read
           the record that says so, and the hook payload is that record.
        4. **Nothing said and no dialog** leaves the roster's own word standing —
           `NONE` for a finished turn, and `UNKNOWN` with `caught_up=False` for a
           Session the roster calls `waiting`, which is the seam's way of saying
           *ask again, never guess*.
        """
        parked_question = self._approvals.newest_question_for(target)
        if parked_question is not None:
            return parked_question
        if found.kind is WaitingKind.NONE:
            # The transcript is not held up on anything, which the roster may
            # still know to be a Session waiting on the user. Its word stands.
            found = base
        dialog = self._approvals.newest_for(target)
        if dialog is None or found.kind is WaitingKind.QUESTION:
            if found.kind is not WaitingKind.QUESTION:
                self._approvals.clear_released_question(target)
            return found
        self._approvals.clear_released_question(target)
        if found.kind is WaitingKind.PERMISSION:
            tool_name, detail = _announced_as(found, dialog)
            return replace(
                found, tool_name=tool_name, detail=detail, approval_id=dialog.approval_id
            )
        _log.info(
            "the transcript for %s has not flushed the call behind the dialog parked here; "
            "naming it from the hook payload instead: tool=%s approval=%s",
            target,
            dialog.tool_name,
            dialog.approval_id,
        )
        return WaitingFor(
            kind=WaitingKind.PERMISSION,
            tool_name=dialog.tool_name or None,
            detail=dialog.detail or None,
            approval_id=dialog.approval_id,
        )

    async def inspect(self, target: SessionTarget) -> SessionInspection:
        """One Session, freshly read from the same roster `discover` reads.

        Answering from the roster rather than from anything held here is what
        keeps this the *same* value `discover` yields — one reader, one shape.
        A Session the roster no longer lists reads as `ENDED`, which is the
        honest answer to "what is it doing" for a Session that is not there.

        **This one is asked about a running Session too, and `discover` is not.**
        The cadence skips a `RUNNING` row because a Session mid-turn has not
        stopped on anything; but "how far along is it" is the question a user
        asks *precisely* while it works, so the per-target read has no such gate
        (#76, the `progress` verb). A stopped row was already read by the
        `discover` inside this call and is returned as that one reading; a
        running row still says `not_read` and is read exactly once here. The
        reader's file-identity cache remains the proof that an unchanged file
        need not be parsed again across separate calls.

        **A roster that could not be read is not a roster that lists nothing.**
        The two are one value apart here and a whole verdict apart for the
        caller, so a failed enumeration raises rather than answering `ENDED`.
        """
        lane = await self.discover()
        if not lane.enumerated:
            assert lane.error is not None  # `enumerated` is exactly this test
            raise LaneUnavailable(AgentKind.CLAUDE, lane.error)
        for row in lane.rows:
            if row.target == target:
                if row.progress.availability is not ProgressAvailability.NOT_READ:
                    return row
                return self._read_into(row)
        return SessionInspection(
            target=target,
            workspace=Path(),
            lifecycle=SessionLifecycle.ENDED,
            state=SessionState.IDLE,
        )

    def supported_routes(self) -> frozenset[RelayRoute]:
        """Deliver only, and honestly so: the channel is refused mid-turn."""
        return frozenset({RelayRoute.DELIVER})

    async def answer_relay(
        self,
        target: SessionTarget,
        text: str,
        *,
        request_id: RequestId,
        route: RelayRoute = RelayRoute.DELIVER,
    ) -> DeliveryReceipt:
        """Carry the user's own words in, with the user's authority."""
        if route is RelayRoute.SUPPLEMENT:
            # Bridge Core decides what to do instead — queueing is its policy,
            # never this adapter's.
            return _failed(request_id, SUPPLEMENT_UNAVAILABLE)
        held_question = self._approvals.held_question_for(target)
        if held_question is not None:
            question_id, _ = held_question
            return await self._approvals.answer_question(question_id, text, request_id=request_id)
        released_reason = self._approvals.released_question_reason(target)
        if released_reason is not None:
            return _failed(
                request_id,
                released_reason,
            )
        return await self._deliver(target, text, request_id=request_id)

    async def approval_relay(
        self, request: ApprovalRequest, verdict: ApprovalVerdict, *, request_id: RequestId
    ) -> DeliveryReceipt:
        """Carry one verdict into the hook that is holding the dialog open.

        A verdict for a dialog nothing is parked on is a failure and not a
        silence, because there are several ways to arrive here with nothing
        waiting — the human answered on screen, the request already resolved,
        or the Session never had a hook route at all — and Bridge Core can only
        record what it is told.
        """
        if request.target.agent is not AgentKind.CLAUDE:
            return _failed(
                request_id, f"{request.target.agent} sessions are not this adapter's to reach"
            )
        if not self._approvals.listening:
            return _failed(request_id, APPROVAL_UNROUTED)
        return await self._approvals.answer(request.approval_id, verdict, request_id=request_id)

    def registry_directory(self) -> Path:
        """Where launches find the Session records this adapter later observes."""
        return self._settings.registry_directory

    def approval_socket_path(self) -> Path:
        """Where a launch should tell this Session's hook to find us.

        Answered whether or not the socket is bound, because it is derived rather
        than discovered and a launcher asking where to point a hook is asking a
        question about this engine's identity, not about its current state.
        """
        return self._approvals.path

    def _registered_as(self, session_id: str) -> SessionTarget | None:
        """The authority check behind the approval socket, in this adapter's own terms.

        A Claude target is addressed by pid and an *approval* payload carries
        only a session id, so the two are matched here against the channels the
        Sessions' own `SessionStart` hooks registered — the same roster every
        other verb addresses. A session id this adapter holds no channel for is
        not this engine's Session, and its dialog is not this engine's to
        answer.

        **An ambiguous session id is refused rather than guessed.** `--resume`
        forks a second process under one session id, which is the whole reason a
        Claude target carries a pid, and a hook payload carries no pid to break
        the tie with. Answering anyway would still deliver the verdict — it
        travels back down the hook's own connection either way — but it would
        announce the dialog against the wrong process, and a notice naming the
        wrong Session is the truthfulness failure, not the lost approval. The
        dialog stays with its human, which is the safe direction to be wrong in.
        """
        matches = [target for target in self._inboxes if target.session_id == session_id]
        if len(matches) == 1:
            return matches[0]
        if matches:
            _log.warning(
                "session id %s names %d registered Claude processes; refusing to guess which "
                "one raised the dialog",
                session_id,
                len(matches),
            )
        return None

    async def verify(self) -> VerifyResult:
        """Report what is installed and loaded, and why no address was published.

        The outcome is the routes' own answer and an unpublished address never
        changes it (#202, #204): an engine without the approval route is still
        loaded, configured and reaching Sessions on its other route, which is
        degraded rather than the unreachable far side ADR 0003's #159 amendment
        fails. What the failure does is get *named*, here as well as in the log,
        because ADR 0003 makes this report the authority on what the engine
        actually loaded — and "no Approval Relay, because the socket would not
        bind" is exactly the kind of thing configuration cannot tell an operator.

        The one exception is the empty-roster PASS, below, and it keys on "no
        address published" rather than on any one cause of it: all three read the
        same to a Session trying to register (#204).
        """
        result = await self._verify_routes()
        if self._unpublished is None:
            return result
        unpublished = (
            f"this engine published no Claude approval address: {self._unpublished.reason}, "
            f"so it runs without the Approval Relay"
        )
        if result.outcome is VerifyOutcome.PASS and not self._inboxes:
            # Both Claude routes read the one published address — the
            # `PermissionRequest` hook (`approval_hook.py`) and the `SessionStart`
            # registration hook (`registration.py`) — so an engine that published
            # none is not merely missing approvals: no Session can register with
            # it either. This one branch would otherwise report PASS, "no Claude
            # Session is registered, so there is no inbox to reach", which is the
            # guard that says nothing while the route is dead that ADR 0003
            # exists to prevent. **Only** this one: every other answer is a
            # reason of its own — a missing hook block, a registry outside the
            # config directory, an inbox that stopped answering — and replacing
            # those would hide a real failure behind this one.
            return VerifyResult(
                outcome=VerifyOutcome.FAIL,
                loaded=result.loaded,
                detail=f"{unpublished}, and no Claude Session can register with an engine that "
                f"published no address, which is why its roster is empty",
            )
        return replace(
            result, detail=f"{unpublished}; {result.detail}" if result.detail else unpublished
        )

    async def _verify_routes(self) -> VerifyResult:
        """Report what is installed and loaded, then whether an inbox answers.

        Ask Installation first, dial second. A missing hook block explains why
        the inbox roster may be empty and fails without trying any registered
        address. A dial is an immediate close: anything written here would land
        in a real conversation. It proves only that somebody still listens at the
        address a Session's registration reported.
        """
        loaded = f"{type(self).__module__}:{type(self).__name__}"
        registry_directory = self._settings.registry_directory
        if not registry_directory.is_relative_to(self._claude_config_directory):
            return VerifyResult(
                outcome=VerifyOutcome.FAIL,
                loaded=loaded,
                detail=(
                    f"Claude hooks are checked under {self._claude_config_directory}, but "
                    f"the Session registry is at {registry_directory}"
                ),
            )
        reach = claude_hooks.reach(
            self._claude_config_directory,
            base_dir=self._installation_base_dir,
        )
        if not reach.installed:
            return VerifyResult(
                outcome=VerifyOutcome.FAIL,
                loaded=loaded,
                detail=reach.note,
            )
        if not self._inboxes:
            return VerifyResult(
                outcome=VerifyOutcome.PASS,
                loaded=loaded,
                detail=(
                    f"{reach.note}; no Claude Session is registered, so there is no inbox to reach"
                ),
            )

        answered: list[SessionTarget] = []
        refusals: list[str] = []
        for target, path in self._inboxes.items():
            try:
                await inbox.dial(path, timeout=self._settings.request_timeout_seconds)
            except InboxError as unreachable:
                refusals.append(f"{target.session_id}: {unreachable}")
                continue
            answered.append(target)

        if not answered:
            return VerifyResult(
                outcome=VerifyOutcome.FAIL,
                loaded=loaded,
                detail="no registered Claude Session inbox answered: " + "; ".join(refusals),
            )
        return VerifyResult(
            outcome=VerifyOutcome.PASS,
            loaded=loaded,
            detail=f"{len(answered)} of {len(self._inboxes)} Claude Session inbox(es) answered",
        )

    # -- carrying words ---------------------------------------------------

    async def _deliver(
        self, target: SessionTarget, text: str, *, request_id: RequestId
    ) -> DeliveryReceipt:
        """One attempt, classified into the hub's four states and nothing else."""
        socket_path = self._inboxes.get(target)
        if socket_path is None:
            return _failed(request_id, f"no Claude Session is registered as {target}")
        spent = len(text.encode("utf-8"))
        if spent > self._settings.max_text_bytes:
            return _failed(
                request_id,
                f"the words are {spent} bytes and this engine caps one Relay at "
                f"{self._settings.max_text_bytes}",
            )
        try:
            replies = await self._reply_inbox(socket_path.parent)
        except InboxError as unbindable:
            # Nothing has been sent, so this is proven non-delivery. It is a
            # refusal rather than a best effort because a Relay written with no
            # reply address is a Relay no receipt can ever settle: on a receiver
            # that holds, it would be parked and never heard of again.
            return _failed(
                request_id, f"no reply inbox could be bound for this Relay: {unbindable}"
            )

        msg_id = inbox.new_message_id()
        frames: list[dict[str, object]] = []
        token = self._messaging_token(target)
        if token is not None:
            frames.append(inbox.auth_frame(token))
        frames.append(inbox.user_frame(text, msg_id=msg_id, reply_to=replies.address))

        # Everything up to and including the dial happens before a byte of the
        # user's words is on the wire, so a failure here proves they never left.
        try:
            await inbox.send(
                socket_path, tuple(frames), timeout=self._settings.request_timeout_seconds
            )
        except InboxError as unreachable:
            return _failed(request_id, f"the Session's inbox is unreachable: {unreachable}")
        except (OSError, ConnectionError) as broken:
            # Past the connection: the line may or may not have been read.
            return _unknown(request_id, f"the write to the Session's inbox failed: {broken}")

        outstanding = _Outstanding(
            target=target, replies=replies, msg_id=msg_id, request_id=request_id
        )
        receipt = await self._await_receipt(outstanding)
        if receipt.outcome in (Delivery.HELD, Delivery.UNKNOWN):
            self._listen_late(outstanding)
        return receipt

    def _messaging_token(self, target: SessionTarget) -> str | None:
        """The Session's own inbox token, as its `SessionStart` hook reported it."""
        report = self._reported.get(target)
        return report.messaging_token if report else None

    async def _reply_inbox(self, directory: Path) -> ReplyInbox:
        """This engine's reply socket inside one Session's socket namespace.

        Bound once per directory and kept: binding it is a `ps` call and a file
        written into somebody else's directory, and doing that per Relay would
        publish and withdraw a key on every sentence the user speaks.
        """
        async with self._binding:
            existing = self._replies.get(directory)
            if existing is not None:
                return existing
            replies = ReplyInbox(
                directory=directory, registry_directory=self._settings.registry_directory
            )
            await replies.start()
            self._replies[directory] = replies
            _log.info("reply inbox bound at %s", replies.address)
            return replies

    async def _await_receipt(self, outstanding: _Outstanding) -> DeliveryReceipt:
        """Wait, bounded, for one of the two things that prove this Relay arrived.

        Polled rather than awaited on an event, because the two sources are not
        one wire: a status frame lands on our own socket, and the transcript is a
        file the Session appends to with nothing to notify us. A spent wait is
        UNKNOWN and never DELIVERED — which on an accepting receiver, where
        nothing is ever held and therefore nothing is ever receipted, is a real
        and frequent answer rather than an edge.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.ack_timeout_seconds
        while True:
            graded = self._graded(outstanding)
            if graded is not None:
                return graded
            if loop.time() >= deadline:
                return _unknown(
                    outstanding.request_id,
                    "nothing proved the words arrived within "
                    f"{self._settings.ack_timeout_seconds:.0f}s: no receipt, and the "
                    "Session's own record does not carry them yet",
                )
            await asyncio.sleep(self._settings.receipt_poll_seconds)

    def _graded(self, outstanding: _Outstanding) -> DeliveryReceipt | None:
        """What the two sources say right now, or `None` for "keep waiting".

        A settlement wins over the transcript, and the transcript wins over
        `held` — which is upstream saying it has not finished happening.
        """
        settled = self._settled(outstanding)
        if settled is not None and settled.outcome is not Delivery.HELD:
            return settled
        try:
            records = self._transcripts.records(self._transcript_path(outstanding.target))
        except TranscriptUnavailable:
            records = None
        if inbox.correlated(
            records, msg_id=outstanding.msg_id, address=outstanding.replies.address
        ):
            return DeliveryReceipt(request_id=outstanding.request_id, outcome=Delivery.DELIVERED)
        return settled

    def _settled(self, outstanding: _Outstanding) -> DeliveryReceipt | None:
        """What upstream's own status frames say about this message, ranked.

        **Ranked, not taken in arrival order**, because one message's statuses are
        a sequence and `held` is the first of them: a `held` that has since become
        `delivered` must read as delivered, and reading the frames in the order
        they landed would freeze it at the first.

        `held` is returned rather than waited out — the person it is parked in
        front of may take minutes, and "parked for someone to release" is the true
        answer for that whole time — but it is the weakest thing here, so a caller
        that has a better source may still prefer it.
        """
        parked = False
        for frame in outstanding.replies.statuses(outstanding.msg_id):
            status = frame.get("status")
            if status == inbox.DELIVERED_STATUS:
                return DeliveryReceipt(
                    request_id=outstanding.request_id, outcome=Delivery.DELIVERED
                )
            if status in inbox.REFUSED_STATUSES:
                return DeliveryReceipt(
                    request_id=outstanding.request_id,
                    outcome=Delivery.FAILED,
                    reason=f"the Session's inbox settled this message as {status}",
                )
            parked = parked or status == inbox.HELD_STATUS
        if parked:
            return DeliveryReceipt(
                request_id=outstanding.request_id,
                outcome=Delivery.HELD,
                reason="the words are parked for the person at that Session to release",
            )
        return None

    def _listen_late(self, outstanding: _Outstanding) -> None:
        """Keep watching a Relay whose grade was not terminal, so its end is heard."""
        task = asyncio.ensure_future(self._late(outstanding))
        self._listening.add(task)
        task.add_done_callback(self._listening.discard)

    async def _late(self, outstanding: _Outstanding) -> None:
        """One unsettled Relay's second budget, which is the hold's own lifetime.

        Started only for HELD and UNKNOWN, and those are the two grades that have
        not finished happening. A held message settles when the person answers,
        expires after about five minutes, or is dropped when the hold queue passes
        a hundred — so `late_ack_timeout_seconds` is that lifetime and not a guess.

        **It watches both sources, not only the receipts.** An earlier draft here
        read the status frames alone, on the argument that the `origin` record is
        written at injection and so arrives at once or never. That is true only of
        a Session that was idle: a Relay sent into one that has since started a
        turn is injected when the turn *ends*, which can be minutes — and on an
        accepting receiver there are no status frames at all, so dropping the
        transcript would leave those UNKNOWN for ever and the hub would say the
        words again. The read is the shared one, cached on the file's own
        identity, so a poll that finds the transcript unchanged costs one `stat`.

        **Both directions are raised, and that is the point.** A late `delivered`
        stops the hub re-sending words that provably arrived; a late `denied` or
        `expired` is proven non-delivery, which is the one grade P9 permits
        another attempt for. Reporting only the first would leave a Relay recorded
        as parked long after it was thrown away — the implied delivery #71
        forbade. A grade that was already terminal never gets here, so nothing
        this raises re-grades a settled attempt.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.late_ack_timeout_seconds
        try:
            while loop.time() < deadline:
                await asyncio.sleep(self._settings.receipt_poll_seconds)
                settled = self._graded(outstanding)
                if settled is None or settled.outcome is Delivery.HELD:
                    continue
                self._emit(RelayReceipt(target=outstanding.target, receipt=settled))
                return
        except asyncio.CancelledError:
            raise

    def _emit(self, event: AgentEvent) -> None:
        """Raise one event upward, and let go of a Session that has ended (#98).

        **The adapter that says a Session ended is the one that forgets it.**
        `forget_session` had no caller: it is not on the `AgentAdapter` seam, and
        Bridge Core's `_session_ended` only marks state — so on an engine that
        starts at login, every Session that ever registered kept its inbox
        address, its window and its parsed transcript for the life of the
        process, and those records are of files measured at 186 MB on this
        machine. It is done here rather than behind a new seam method because
        the knowledge is already here, and rather than behind a timer because a
        death is an observation and not an age.

        Forgetting happens *before* the sink sees the event, so this adapter is
        never holding a route to a Session it has already declared ended: a
        Relay aimed at one has to fail on the way out rather than be written
        into an inbox still listed here.
        """
        if isinstance(event, SessionEnded):
            self.forget_session(event.target)
        if self._sink is not None:
            self._sink.emit(event)


def _announced_as(found: WaitingFor, dialog: ApprovalRequest) -> tuple[str | None, str | None]:
    """The tool name and detail the announcement uses, when a dialog is parked here.

    **The hook payload is the authority on what is parked** (advisor, 2026-08-26,
    #98). One assistant message may carry several `tool_use` blocks; the
    transcript reading is held up on the newest outstanding one, while the dialog
    is parked on whichever call the far side actually stopped to ask about. The
    two can name different tools, and the `approval_id` beside them belongs to
    the dialog's — so a reading that kept the transcript's name would announce
    one tool and carry the user's verdict to another, which is an Approval
    delivered to a call they were never shown.

    So the dialog's `tool_name` wins whenever it has one, and `detail` follows
    the name it describes: **the record's summary is kept only while the two
    agree on the call.** "Agree" means the record named the same tool, not
    merely that it named none — a record that could not read the tool's name
    could still summarise its input, and that summary describes whichever call
    the record was held up on rather than the one on screen.
    """
    if not dialog.tool_name:
        # The hook payload named no tool, so it contradicts nothing the record
        # said, and #75's fill-a-gap-never-overwrite rule stands whole.
        return found.tool_name or None, found.detail or dialog.detail or None
    if found.tool_name == dialog.tool_name:
        # One call, read twice. The record read the call's whole input, so its
        # summary is the fuller of the two and the dialog's fills a gap.
        return dialog.tool_name, found.detail or dialog.detail or None
    return dialog.tool_name, dialog.detail or None


def _failed(request_id: RequestId, reason: str) -> DeliveryReceipt:
    return DeliveryReceipt(request_id=request_id, outcome=Delivery.FAILED, reason=reason)


def _unknown(request_id: RequestId, reason: str) -> DeliveryReceipt:
    return DeliveryReceipt(request_id=request_id, outcome=Delivery.UNKNOWN, reason=reason)
