"""The Agent seam, over the Claude Session Channel. Mechanism only; no queueing.

**What proves delivery.** The channel route cannot mint the hub's `request_id`
into the transcript the way `turn/start` does on the Codex side, so the proof is
the other direction: the correlation id rides inside the message, and the
Session calls `acknowledge_answer` with it. That tool call is the only positive
proof there is. The channel accepting the line is not — it says the notification
was pushed, not that the Session read it.

**A late acknowledgement is an upgrade, and that is why it exists.** The wait is
bounded, and a spent wait is UNKNOWN, never DELIVERED. But Bridge Core retains
anything not proven delivered and sends it again when the Reply Window next
opens — so an acknowledgement that arrives after the wait and is thrown away
becomes the hub re-delivering words that provably arrived. The connection is
therefore kept, for a second and longer budget, purely so a late receipt can be
raised upward and the hub can drop what it was holding. Only DELIVERED is ever
raised that way; a late anything-else is logged and changes nothing.

**Two verbs over two different wires.** This package is the shared Claude
adapter. The Answer Relay rides the MCP Session Channel, described above. The
Approval Relay rides the **`PermissionRequest` hook**, delegated to
`approval.py`, and it is the route where *we* are the server: a hook process
dials in holding a displayed dialog open, and the verdict travels back down the
connection it is waiting on.

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
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from gpt_voicecoding.adapters.agent.claude import discovery as claude_discovery
from gpt_voicecoding.adapters.agent.claude import stop_analysis, transcript_tail
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
    bootstrap_value,
    publish_address,
    withdraw_address,
)
from gpt_voicecoding.adapters.agent.claude.protocol import (
    ACKNOWLEDGED,
    CHANNEL_ERROR,
    KIND_FIELD,
    QUEUED,
    REQUEST_ID_FIELD,
    TEXT_FIELD,
    channel_kind_for,
)
from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings
from gpt_voicecoding.adapters.agent.claude.transcript import Record, TranscriptReader
from gpt_voicecoding.adapters.agent.claude.window import ReplyWindowWatcher
from gpt_voicecoding.adapters.agent.claude.wire import (
    ChannelConnection,
    ChannelError,
)
from gpt_voicecoding.seams.agent import (
    AgentEvent,
    ApprovalRequest,
    ApprovalVerdict,
    LaneDiscovery,
    LaneUnavailable,
    Progress,
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
    "the Claude Session Channel has no mid-turn route: a channel message delivered inside "
    "a turn is framed as not being from the user and is refused"
)


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
        self, *, sink: EventSink | None = None, settings: ClaudeSettings | None = None
    ) -> None:
        self._sink = sink
        self._settings = settings or ClaudeSettings()
        #: The channel socket each registered Session's launch wrapper reported.
        self._channels: dict[SessionTarget, Path] = {}
        #: Late-receipt listeners in flight, so none outlives this adapter.
        self._listening: set[asyncio.Task[None]] = set()
        #: The lane's one opener of a transcript file, shared by what a Session
        #: stopped on (#75) and how far along it is (#76).
        self._transcripts = TranscriptReader()
        self._windows = ReplyWindowWatcher(
            settings=self._settings, emit=self._emit, stopped_on=self.stopped_on
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
            _log.warning("no Approval Relay this run: %s", refused)
            return
        # Only now, and only if it bound: a published address nobody is listening
        # on costs every permission dialog in this config directory a full dial
        # timeout, which is worse than the silence of publishing nothing.
        try:
            published = publish_address(self.approval_socket_path(), self._settings)
        except OSError as refused:
            _log.warning("the approval address could not be published: %s", refused)
        else:
            _log.info("approval address published at %s", published)

    async def aclose(self) -> None:
        """Stop everything this adapter started, and take its socket back out.

        A channel is a process Claude Code owns. Closing this adapter lets go of
        connections to it and nothing more — there is nothing there to reap.

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
        withdraw_address()
        self._channels.clear()

    # -- the Session roster this adapter can reach ------------------------

    def register_session(self, target: SessionTarget, socket_path: Path) -> None:
        """Record where one Session's channel listens.

        The path is the registration this adapter needs and cannot discover: the
        channel is spawned by Claude Code inside the Session's own process, so
        its address exists nowhere outside it. It arrives from that Session's
        `SessionStart` hook (`_session_started`) — the reference implementation
        got it from a launch wrapper it owned, and v1.0 launches nothing (#72).
        """
        if target.agent is not AgentKind.CLAUDE:
            raise ValueError(f"{target.agent} sessions are not this adapter's to reach")
        self._channels[target] = socket_path
        # Registering is also what starts reporting this Session's Reply Window,
        # which is what makes it reachable at all: until a window is observed,
        # Bridge Core holds every Relay against the fail-closed default.
        self._windows.watch(target)
        _log.info(
            "registered Session channel agent=%s session_id=%s pid=%s socket=%s",
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
        self._channels.pop(target, None)
        self._windows.forget(target)
        report = self._reported.pop(target, None)
        if report is not None:
            self._transcripts.forget(report.transcript_path)

    def reachable(self) -> tuple[SessionTarget, ...]:
        """Every Session this adapter holds a channel address for."""
        return tuple(self._channels)

    def reply_window(self, target: SessionTarget) -> ReplyWindow:
        """Where this Session's Reply Window stands right now, read from the registry.

        The seam's level query, and how a Session's *starting* window reaches
        Bridge Core at all: registration cannot announce it, because it runs
        before Bridge Core holds the Session (#27), so Bridge Core asks instead.

        A Session this adapter holds no channel for is CLOSED, whatever its
        registry record happens to say. The window is a claim about reachability,
        and reading someone else's record is not the same as being able to reach
        them — the same fail-closed rule the whole seam runs on.
        """
        if target not in self._channels:
            return ReplyWindow.CLOSED
        return self._windows.level(target)

    # -- the seam ---------------------------------------------------------

    async def discover(self) -> LaneDiscovery:
        """Every Claude Session running, from Claude Code's own roster.

        The roster is `discovery.py`'s whole business — it owns the command and
        the mapping. Nothing about being *reachable* enters there: a Session is
        listed because it exists, and whether this adapter holds a channel to it
        is a question `answer_relay` answers with a receipt (#68).

        What is added here is the one thing the roster cannot say: **what a
        stopped Session stopped on** (#75). The roster reports `waiting` without
        naming a tool, a dialog or a prompt, so a row that stopped is read
        against its own transcript and against any dialog parked on this
        engine's approval socket. It happens on this verb rather than on
        `inspect` because this is the verb Bridge Core actually calls — every
        five seconds, for the whole machine (`core/bridge.py:442`) — and
        `inspect` reads the same rows.
        """
        lane = await claude_discovery.discover()
        if not lane.enumerated:
            return lane
        return replace(lane, rows=tuple(self._row_with_stop(row) for row in lane.rows))

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
        records = self._transcripts.records(self._transcript_path(row.target))
        waiting = (
            row.waiting_for
            if row.state is SessionState.RUNNING
            else self._overlay(row.target, row.waiting_for, records)
        )
        if records is None:
            # No path, no file yet, or a read that failed. The roster's own word
            # stands, and `progress` stays `None` — "not read", never "read and
            # found nothing".
            return row if waiting == row.waiting_for else replace(row, waiting_for=waiting)
        entries, truncated, moved = transcript_tail.recent(records)
        return replace(
            row,
            waiting_for=waiting,
            progress=Progress(recent=entries, truncated=truncated, read_at=datetime.now(UTC)),
            last_activity=moved,
        )

    def _transcript_path(self, target: SessionTarget) -> Path | None:
        """Where this Session's own record is, as its registration named it."""
        report = self._reported.get(target)
        return report.transcript_path if report else None

    def stopped_on(self, target: SessionTarget, roster: WaitingFor | None = None) -> WaitingFor:
        """What one Session stopped on, from its transcript and any parked dialog.

        The Reply Window watcher's route to the same answer (`window.py:308-311`),
        and the reason the overlay below is a method rather than inline: a stop
        raised by the watcher and a stop read off the roster must be the one
        reading, not two that agree most of the time.
        """
        records = self._transcripts.records(self._transcript_path(target))
        return self._overlay(target, roster if roster is not None else WaitingFor(), records)

    def _overlay(
        self, target: SessionTarget, base: WaitingFor, records: tuple[Record, ...] | None
    ) -> WaitingFor:
        """What these records and any parked dialog say this Session stopped on.

        Two sources, and they are ranked rather than merged, because they can
        disagree and the ranking is the behaviour (#75):

        0. **A question parked on the approval socket wins over everything**
           (#77). `AskUserQuestion` raises the same `PermissionRequest` hook a
           `Write` does (measured on 2.1.246), so the dialog arrives here with
           the whole prompt, its options and the `prompt_id` a verdict would be
           addressed with — while the transcript says nothing about the call
           until it has flushed, and by then the person at the keyboard has
           usually answered it. The hook's question is the thing itself and the
           record's is a reconstruction of it, so the hook wins whether the two
           name the same prompt or different ones. Nothing carries a verdict
           into it: `as_approval_request` still answers `None` for a QUESTION,
           and the notice sends the user to their own terminal until #103.
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
        found = base if records is None else stop_analysis.analyse(records)
        if found.kind is WaitingKind.NONE:
            # The transcript is not held up on anything, which the roster may
            # still know to be a Session waiting on the user. Its word stands.
            found = base
        dialog = self._approvals.newest_for(target)
        if dialog is None or found.kind is WaitingKind.QUESTION:
            return found
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
        (#76, the `progress` verb), and every row that reaches it is read here
        rather than taken from the cadence's pass. What that costs is one pure
        walk of records already in memory: the file itself is opened again only
        if the Session has written to it since, because the reader's cache is
        keyed on the file's own identity. Bypassing that cache is deliberately
        *not* done — a hit is the proof of freshness, not a stale answer.

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
        return await self._deliver(target, text, request_id=request_id, verb="answer_relay")

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

    def launch_bootstrap(self, channel_socket_path: Path) -> str:
        """What one launch must set the bootstrap variable to for this engine.

        The Session Launcher owns the child environment but not the contents of
        this value: the byte budgets inside it are this adapter's settings, and
        the approval address is this adapter's socket. So the launcher says where
        the channel should listen — that path is per-launch and only it can mint
        one — and asks here for everything else.

        Answered before anything is bound, for the same reason
        `approval_socket_path` is: a launch has to carry the address into the
        Session that will dial it, so the address must exist first.
        """
        return bootstrap_value(
            channel_socket_path,
            self._settings,
            approval_socket_path=self.approval_socket_path(),
        )

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
        matches = [target for target in self._channels if target.session_id == session_id]
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
        """Report what is loaded, and whether any registered channel really answers."""
        loaded = f"{type(self).__module__}:{type(self).__name__}"
        if not self._channels:
            return VerifyResult(
                outcome=VerifyOutcome.PASS,
                loaded=loaded,
                detail="no Claude Session is registered, so there is no channel to reach",
            )

        answered: list[SessionTarget] = []
        refusals: list[str] = []
        for target, path in self._channels.items():
            try:
                connection = await self._dial(path)
            except ChannelError as unreachable:
                refusals.append(f"{target.session_id}: {unreachable}")
                continue
            answered.append(target)
            await connection.aclose()

        if not answered:
            return VerifyResult(
                outcome=VerifyOutcome.FAIL,
                loaded=loaded,
                detail="no registered Claude Session Channel answered: " + "; ".join(refusals),
            )
        return VerifyResult(
            outcome=VerifyOutcome.PASS,
            loaded=loaded,
            detail=f"{len(answered)} of {len(self._channels)} Claude Session Channel(s) answered",
        )

    # -- carrying words ---------------------------------------------------

    async def _deliver(
        self, target: SessionTarget, text: str, *, request_id: RequestId, verb: str
    ) -> DeliveryReceipt:
        """One attempt, classified into the hub's four states and nothing else."""
        socket_path = self._channels.get(target)
        if socket_path is None:
            return _failed(request_id, f"no Claude Session is registered as {target}")
        spent = len(text.encode("utf-8"))
        if spent > self._settings.max_text_bytes:
            return _failed(
                request_id,
                f"the words are {spent} bytes and both ends of the channel cap one Relay at "
                f"{self._settings.max_text_bytes}",
            )

        # Everything up to and including the dial happens before a byte of the
        # user's words is on the wire, so a failure here proves they never left.
        try:
            connection = await self._dial(socket_path)
        except ChannelError as unreachable:
            return _failed(request_id, f"the Session's channel is unreachable: {unreachable}")

        keep_open = False
        try:
            message = {
                REQUEST_ID_FIELD: str(request_id),
                KIND_FIELD: channel_kind_for(verb),
                TEXT_FIELD: text,
            }
            try:
                await connection.send(message)
            except ChannelError as broken:
                # Past the write: the line may or may not have been read.
                return _unknown(request_id, f"the channel write failed: {broken}")

            receipt = await self._await_acknowledgement(connection, request_id)
            keep_open = receipt.outcome is Delivery.UNKNOWN and receipt.request_id == request_id
            if keep_open:
                self._listen_late(target, connection, request_id)
            return receipt
        finally:
            if not keep_open:
                await connection.aclose()

    async def _await_acknowledgement(
        self, connection: ChannelConnection, request_id: RequestId
    ) -> DeliveryReceipt:
        """Wait, bounded, for the Session to say it has the words.

        `queued_for_claude` is not an answer, so it keeps waiting. A reply about
        a *different* request id contradicts the attempt, and a contradiction is
        UNKNOWN rather than a failure: these words may well have arrived.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.ack_timeout_seconds
        while True:
            try:
                reply = await connection.read_message(
                    timeout_seconds=deadline - loop.time(),
                )
            except TimeoutError:
                return _unknown(
                    request_id,
                    "the Session did not acknowledge the words within "
                    f"{self._settings.ack_timeout_seconds:.0f}s",
                )
            except ChannelError as broken:
                return _unknown(request_id, f"the channel stopped answering: {broken}")

            outcome = _classify(reply, request_id)
            if outcome is not None:
                return outcome

    def _listen_late(
        self, target: SessionTarget, connection: ChannelConnection, request_id: RequestId
    ) -> None:
        """Keep listening on a spent attempt, so a late acknowledgement is still heard."""
        task = asyncio.ensure_future(self._late(target, connection, request_id))
        self._listening.add(task)
        task.add_done_callback(self._listening.discard)

    async def _late(
        self, target: SessionTarget, connection: ChannelConnection, request_id: RequestId
    ) -> None:
        """One spent attempt's second budget. Only DELIVERED is ever raised from here."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.late_ack_timeout_seconds
        try:
            while True:
                try:
                    reply = await connection.read_message(
                        timeout_seconds=deadline - loop.time(),
                    )
                except (TimeoutError, ChannelError):
                    return
                outcome = _classify(reply, request_id)
                if outcome is None:
                    continue
                if outcome.outcome is Delivery.DELIVERED:
                    self._emit(RelayReceipt(target=target, receipt=outcome))
                    return
                # An upgrade is the only thing a late receipt may be. Anything
                # else would re-grade an attempt Bridge Core has already
                # recorded, from a connection nobody is waiting on.
                _log.info(
                    "a late channel reply for %s said %s; the recorded grade stands",
                    request_id,
                    outcome.outcome,
                )
                return
        except asyncio.CancelledError:
            raise
        finally:
            await connection.aclose()

    async def _dial(self, socket_path: Path) -> ChannelConnection:
        return await ChannelConnection.dial(
            socket_path,
            timeout_seconds=self._settings.request_timeout_seconds,
            max_message_bytes=self._settings.max_message_bytes,
        )

    def _emit(self, event: AgentEvent) -> None:
        """Raise one event upward, and let go of a Session that has ended (#98).

        **The adapter that says a Session ended is the one that forgets it.**
        `forget_session` had no caller: it is not on the `AgentAdapter` seam, and
        Bridge Core's `_session_ended` only marks state — so on an engine that
        starts at login, every Session that ever registered kept its channel
        address, its window and its parsed transcript for the life of the
        process, and those records are of files measured at 186 MB on this
        machine. It is done here rather than behind a new seam method because
        the knowledge is already here, and rather than behind a timer because a
        death is an observation and not an age.

        Forgetting happens *before* the sink sees the event, so this adapter is
        never holding a route to a Session it has already declared ended: a
        Relay aimed at one has to fail on the way out rather than be written
        into a channel still listed here.
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


def _classify(reply: dict[str, object], request_id: RequestId) -> DeliveryReceipt | None:
    """What one channel reply says about this attempt, or `None` for "keep waiting"."""
    kind = reply.get("type")
    answered = reply.get(REQUEST_ID_FIELD)

    if kind == CHANNEL_ERROR:
        # This channel only ever refuses a line *before* pushing it, so its
        # refusal is proof of non-delivery rather than the reference
        # implementation's ambiguous "it failed, possibly after arriving".
        return _failed(request_id, f"the channel refused the words: {reply.get('message')}")
    if kind == QUEUED:
        if answered != str(request_id):
            return _unknown(request_id, "the channel queued a different request")
        return None
    if kind == ACKNOWLEDGED:
        if answered != str(request_id):
            return _unknown(request_id, "the channel acknowledged a different request")
        return DeliveryReceipt(request_id=request_id, outcome=Delivery.DELIVERED)
    return _unknown(request_id, f"the channel said something unexpected: {kind!r}")


def _failed(request_id: RequestId, reason: str) -> DeliveryReceipt:
    return DeliveryReceipt(request_id=request_id, outcome=Delivery.FAILED, reason=reason)


def _unknown(request_id: RequestId, reason: str) -> DeliveryReceipt:
    return DeliveryReceipt(request_id=request_id, outcome=Delivery.UNKNOWN, reason=reason)
