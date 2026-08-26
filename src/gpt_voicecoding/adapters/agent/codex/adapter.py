"""The Agent seam, over ``codex app-server``. Mechanism only; no queueing.

**Where the words go.** A Relay is a `turn/start` carrying the hub's `request_id`
as `clientUserMessageId`, and it counts as delivered only when a `thread/read`
readback shows exactly one `userMessage` with that `clientId`. A `turn/start`
that returned successfully is *not* proof: it says the request was accepted, not
that the thread holds the words. That distinction is the whole reason this
adapter polls rather than trusting its own call, and it survives codex 0.148.0
unchanged.

**Supplement is `turn/steer`, not the queue.** The route that means "the agent is
working and I want to add something now" is `turn/steer`, which is stable in
0.148.0, carries the same `clientUserMessageId`, and takes an `expectedTurnId`
precondition so a turn that ended between the user speaking and the words landing
fails closed with Codex's own words instead of racing. `thread/queue/add` was the
originally specified backend and is not used: it is experimental-gated, and its
semantics — land *after* the current turn — are what Bridge Core already does as
its fallback for an adapter with no supplement at all, so implementing it here
would put Core's policy inside a spoke.

**This adapter owns no Session's process.** A Codex TUI is a thin client of an
app-server, so the app-server the user's session runs on is spawned by the launch
wrapper in the user's own terminal, and this engine attaches to it as one more
client. The engine spawns exactly one app-server of its own — the one the Call
seam and the Delegated Turn ride on — and never a Session's. An engine restart
must not close the user's coding sessions.

**Approvals fan out; `ask` is silence.** Codex delivers one permission prompt to
every subscribed client under the same id, first answer wins, and the losers get
`serverRequest/resolved`. So the on-screen dialog and this adapter both hold the
same prompt, and handing it back is implemented by not answering — which is what
lets a budget expiry answer `ask` without it becoming a denial.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from gpt_voicecoding import __version__
from gpt_voicecoding.adapters.agent.codex import approvals as approval_wire
from gpt_voicecoding.adapters.agent.codex import discovery as codex_discovery
from gpt_voicecoding.adapters.agent.codex import thread_tail
from gpt_voicecoding.adapters.agent.codex.shared_daemon import SharedDaemon
from gpt_voicecoding.adapters.agent.codex.threads import (
    PINNED_POLICY,
    USER_REVIEWER,
    ApprovalRouting,
    PendingApproval,
    WatchedThread,
)
from gpt_voicecoding.adapters.codex_app_server.process import (
    AppServerError,
    OwnedAppServer,
    attach,
)
from gpt_voicecoding.adapters.codex_app_server.settings import CodexSettings
from gpt_voicecoding.adapters.codex_app_server.wire import (
    Message,
    RemoteError,
    WireError,
)
from gpt_voicecoding.seams.agent import (
    ApprovalRequest,
    ApprovalVerdict,
    AwaitingApproval,
    LaneDiscovery,
    LaneUnavailable,
    RelayRoute,
    ReplyWindow,
    ReplyWindowChanged,
    SessionEnded,
    SessionInspection,
    SessionLifecycle,
    SessionState,
    SessionStopped,
    WaitingFor,
    WaitingKind,
)
from gpt_voicecoding.seams.delivery import Delivery, DeliveryReceipt
from gpt_voicecoding.seams.events import EventSink
from gpt_voicecoding.seams.identity import AgentKind, RequestId, SessionTarget
from gpt_voicecoding.seams.verify import VerifyOutcome, VerifyResult

_log = logging.getLogger(__name__)

#: What codex says about a thread that exists but has never done anything. It
#: has no rollout file yet, so there is nothing to resume — a state the thread
#: grows out of the moment it does any work.
NO_ROLLOUT_YET = "no rollout found"


def default_own_socket_path(settings: CodexSettings) -> Path:
    """Where the engine's own app-server listens, when nothing states otherwise.

    Per-uid, and inside a directory of its own, for the two reasons `config.py`
    gives its control socket exactly this shape: two accounts on one machine
    each get their own rather than the second finding the first still listening,
    and a socket is only as private as the directory holding it — the runtime
    root is shared `/tmp`, which cannot be made private to anyone.
    """
    return settings.socket_directory / f"gpt-voicecoding-{os.geteuid()}" / "codex-app-server.sock"


class CodexAgentAdapter:
    """Codex, behind the Agent seam. Implements `AgentAdapter` and `Connectable`."""

    def __init__(
        self,
        *,
        sink: EventSink | None = None,
        settings: CodexSettings | None = None,
        own_socket_path: Path | None = None,
        own_log_path: Path | None = None,
        daemon: SharedDaemon | None = None,
        processes: codex_discovery.ProcessLister | None = None,
    ) -> None:
        self._sink = sink
        self._settings = settings or CodexSettings()
        self._threads: dict[SessionTarget, WatchedThread] = {}
        #: Prompts already resolved by somebody else, so a late verdict is
        #: refused rather than answered into a closed request.
        self._resolved: set[Any] = set()
        #: Subscription retries in flight, so none outlives this adapter.
        self._retrying: set[asyncio.Task[None]] = set()
        self._own = OwnedAppServer(
            settings=self._settings,
            socket_path=own_socket_path or default_own_socket_path(self._settings),
            log_path=own_log_path,
            version=__version__,
        )
        #: This engine's client of the daemon somebody else owns. Joined lazily,
        #: on the first discovery that needs it, because an engine must start on
        #: a machine whose daemon is not up yet. Injectable for the same reason
        #: the app-server's socket path is: a test that dialled the real one
        #: would be a test talking to the Sessions of whoever ran it.
        self._daemon = daemon or SharedDaemon(settings=self._settings, version=__version__)
        #: What each loaded thread last said, read at most once per change.
        self._turns = codex_discovery.TurnCache()
        #: How the machine's own process table is read. Injected for the reason
        #: the daemon is: a test that shelled out to `ps` would be a test whose
        #: answer depends on what the person running it happens to have open.
        self._processes = processes or codex_discovery.enumerate_sessions
        self._opened = False

    # -- the connection this engine owns ----------------------------------

    @property
    def app_server(self) -> OwnedAppServer:
        """The engine's own app-server, for the Call adapter to consume.

        Deliberately exposed as the component rather than as a raw connection:
        the Call adapter needs the thing that is *owned*, not a socket it might
        be tempted to reopen.
        """
        return self._own

    async def connect(self) -> None:
        """Start the engine's own app-server. Idempotent (`Connectable`)."""
        if self._opened:
            return
        await self._own.start()
        self._opened = True

    async def aclose(self) -> None:
        """Detach from every Session, then stop what this engine owns.

        In that order, and every thread gets its turn even if one objects: the
        Sessions belong to the user and are merely being let go of, while the
        engine's own app-server is the only process here that must actually die.

        **The detaching happens all at once**, because it is what stands between
        a SIGTERM and that app-server actually being told to go. Sequentially,
        the wait before the one process that must die grew with the number of
        Sessions the user happened to have open — so the shutdown budget held on
        a machine with two and not on a machine with nine, which is the kind of
        bound that passes every test and fails on somebody's Tuesday (#96).
        """
        for task in list(self._retrying):
            task.cancel()
        self._retrying.clear()
        watching = list(self._threads.values())
        self._threads.clear()
        await asyncio.gather(*(self._detached(watched) for watched in watching))
        # Only this engine's end of the shared daemon: the daemon itself carries
        # on, because the user's `codex` TUIs are its clients and stopping it
        # would end their Sessions (#83's written rule, ADR 0012). It never waits
        # on a dial in flight — #96's shutdown budget is derived from the phases
        # it bounds, and a phase that could sit out a ten-second lookup is not
        # one of them (`shared_daemon.SharedDaemon.aclose`).
        await self._daemon.aclose()
        await self._own.aclose()
        self._opened = False

    async def _detached(self, watched: WatchedThread) -> None:
        """Let go of one Session, saying so if it objects. Never raises."""
        try:
            await watched.connection.aclose()
        except Exception:  # a connection objecting must not strand the rest
            _log.exception("closing the connection to %s raised", watched.socket_path)

    # -- the Session roster this adapter watches --------------------------

    async def register_session(self, target: SessionTarget, socket_path: Path) -> None:
        """Watch one Session, on the app-server its launch wrapper owns.

        The socket path is the registration this adapter needs and cannot
        discover: a TUI's app-server is spawned outside this process, so its
        address arrives from the launcher rather than from anything here.
        """
        if target.agent is not AgentKind.CODEX:
            raise ValueError(f"{target.agent} sessions are not this adapter's to watch")
        if target in self._threads:
            return

        connection = await attach(
            socket_path,
            version=__version__,
            settings=self._settings,
            on_notification=self._heard,
            on_server_request=self._asked,
            on_closed=lambda reason, held=target: self._connection_lost(held, reason),
        )
        watched = WatchedThread(target=target, socket_path=socket_path, connection=connection)
        self._threads[target] = watched
        try:
            await self._subscribe(watched)
        except (WireError, AppServerError):
            self._threads.pop(target, None)
            await connection.aclose()
            raise
        _log.info(
            "registered Session channel agent=%s session_id=%s pid=%s socket=%s",
            target.agent,
            target.session_id,
            target.pid,
            socket_path,
        )

    def reply_window(self, target: SessionTarget) -> ReplyWindow:
        """Where this Session's Reply Window stands right now, from what has been observed.

        The seam's level query (#27). Nothing is probed: a thread's status
        arrives as a notification, so the freshest answer available is the one
        `_note_status` last wrote down, and asking the app-server here would make
        a synchronous verb wait on a wire.

        Both of the ways this can have nothing to report are already CLOSED, and
        deliberately so. A target this adapter does not watch is not one it can
        claim anything about; and a watched thread that has not yet reported a
        status carries `WatchedThread.reply_window`'s own fail-closed default,
        which is exactly the right answer — a window nobody has observed is not
        one anything may claim is open. In that second case the level is
        *provisional rather than wrong*: `observed` is still False, so the first
        status to arrive is emitted as a `ReplyWindowChanged` and corrects it.
        """
        watched = self._threads.get(target)
        if watched is None:
            return ReplyWindow.CLOSED
        return watched.reply_window

    async def forget_session(self, target: SessionTarget) -> None:
        """Stop watching one Session. The Session itself is left running."""
        watched = self._threads.pop(target, None)
        if watched is not None:
            await watched.connection.aclose()

    def watching(self) -> tuple[SessionTarget, ...]:
        """Every Session this adapter currently holds a connection for."""
        return tuple(self._threads)

    # -- the seam ---------------------------------------------------------

    async def discover(self) -> LaneDiscovery:
        """Every Codex Session on this machine, from the daemon and from the machine.

        Delegated whole to `discovery.py`. The client handed over is this
        engine's connection to the shared daemon — `None` when the daemon is not
        answering, which is not a gap in the roster: those Sessions are listed
        from the process table, the lane says so on `degraded`, and a Relay into
        one fails at the wire with its reason (#82).
        """
        client = await self._shared_daemon()
        return await codex_discovery.discover(
            client,
            processes=self._processes,
            turns=self._turns,
            daemon_note=self._daemon.note,
        )

    async def inspect(self, target: SessionTarget) -> SessionInspection:
        """One Session, freshly read from the same sources `discover` reads.

        Matched on the whole target where it can be, and on the pid otherwise:
        a Codex Session gains its thread id at its first turn (#73), so the two
        readings of one process may name it differently.

        **One live read, and exactly one** (#76). Dropping this thread's
        remembered turns *before* enumerating is what makes that true: the
        enumeration below then takes one fresh deep read for it, where leaving
        the cache alone would have the cadence answer from memory and this verb
        read a second time — two 558,875-byte reads for one question. A thread
        mid-turn is gated out of that read and is picked up by `_turns_into`
        instead, which is the other half of the same one-read rule.

        **A lane that could not look raises**, rather than reporting a Session
        it never saw as `ENDED` — the same rule `SessionRegistry.observe`
        follows for `LaneDiscovery.error`, for the same reason.
        """
        self._turns.forget(target.session_id)
        lane = await self.discover()
        if not lane.enumerated:
            assert lane.error is not None  # `enumerated` is exactly this test
            raise LaneUnavailable(AgentKind.CODEX, lane.error)
        for row in lane.rows:
            if row.target == target or (target.pid is not None and row.target.pid == target.pid):
                return await self._turns_into(row)
        return SessionInspection(
            target=target,
            workspace=Path(),
            lifecycle=SessionLifecycle.ENDED,
            state=SessionState.IDLE,
        )

    async def _shared_daemon(self) -> codex_discovery.DaemonClient | None:
        """This engine's connection to the shared app-server daemon, if it has one.

        The lane's one door to the daemon, and the reason it is a method rather
        than a field: #77 takes its Relay and Approval routes through this same
        connection, and its pre-wire `FAILED` is what `None` here means. A second
        caller opening its own client would be a second thing for the daemon to
        drop and a second thing to notice it had.

        `None` is honest and ordinary: the daemon may not be up yet, and the lane
        then reads the machine instead — which is exactly what it does for a TUI
        the daemon never adopted anyway (#82).

        **`None` no longer means "nothing was dialled", and the roster had to
        stop saying it did.** #96 split that sentence out precisely because this
        method returned `None` without a byte being sent, so a roster reading
        "the daemon did not answer" contradicted `bridge-install status` dialling
        the same daemon on the same machine and getting an answer. #76 builds the
        client, so something is now always attempted — and what the roster says
        is the dial's own reason (`SharedDaemon.note`), which is more precise
        than either sentence #96 had to choose between.
        """
        return await self._daemon.client()

    async def _turns_into(self, row: SessionInspection) -> SessionInspection:
        """One row with its turns read live, for the verb that asks about one Session.

        **The cadence's gate is not this verb's** (#76). A thread mid-turn carries
        no progress on the roster, because reading turns costs 558,875 bytes per
        thread per read (measured, `discovery.TurnCache`) and a working Session is
        not stopped on anything — but "how far along is it" is the question a user
        asks *precisely* while it works, so here it is read.

        **A row that already carries a reading keeps it, and that is the one-read
        rule rather than a shortcut.** `inspect` dropped this thread's cached
        turns before enumerating, so a reading on the row is one taken moments
        ago in this same call. Reading again would be the second of two
        half-megabyte reads for one question.

        **An unattached row keeps `progress=None`, and never a guessed one.** Its
        rollout is on disk and reading it would be a second source answering the
        same question with worse evidence — the port table left exactly that
        behind (P6, P13). Bridge Core turns that `None` into the honest error #76
        asks for; it is not this adapter's to invent one.
        """
        if row.progress is not None or row.target.session_id is None:
            return row
        client = await self._shared_daemon()
        if client is None:
            return row
        described = await codex_discovery.read_thread(
            client, row.target.session_id, with_turns=True
        )
        if described is None:
            return row
        return replace(
            row,
            progress=codex_discovery.progress_from(described),
            last_activity=thread_tail.last_activity(described) or row.last_activity,
        )

    def supported_routes(self) -> frozenset[RelayRoute]:
        """Both. `turn/steer` is stable in the codex this adapter is built against."""
        return frozenset({RelayRoute.DELIVER, RelayRoute.SUPPLEMENT})

    async def answer_relay(
        self,
        target: SessionTarget,
        text: str,
        *,
        request_id: RequestId,
        route: RelayRoute = RelayRoute.DELIVER,
    ) -> DeliveryReceipt:
        """Carry the user's own words in, with the user's authority."""
        return await self._relay(target, text, request_id=request_id, route=route)

    async def approval_relay(
        self, request: ApprovalRequest, verdict: ApprovalVerdict, *, request_id: RequestId
    ) -> DeliveryReceipt:
        """Carry one verdict — or, for `ask`, deliberately carry nothing."""
        watched = self._threads.get(request.target)
        if watched is None:
            return _failed(request_id, f"no Codex Session is registered as {request.target}")

        refusal = self._misrouted(watched)
        if refusal:
            return _failed(request_id, refusal)

        pending = watched.pending.get(request.approval_id)
        if pending is None:
            if request.approval_id in watched.answered_elsewhere:
                return _failed(
                    request_id,
                    "the on-screen dialog already answered that request",
                )
            return _failed(
                request_id,
                f"no permission request {request.approval_id} is waiting on this Session",
            )

        if verdict is ApprovalVerdict.ASK:
            watched.pending.pop(request.approval_id, None)
            return DeliveryReceipt(
                request_id=request_id,
                outcome=Delivery.HELD,
                reason="handed back to the on-screen dialog, which still holds it",
            )

        if not approval_wire.carries_a_decision(pending.method):
            watched.pending.pop(request.approval_id, None)
            return DeliveryReceipt(
                request_id=request_id,
                outcome=Delivery.HELD,
                reason=(
                    f"{pending.method} is answered with a permission profile, which this "
                    "adapter has none of; it is waiting at the on-screen dialog"
                ),
            )

        if pending.wire_id in self._resolved:
            watched.pending.pop(request.approval_id, None)
            return _failed(request_id, "the on-screen dialog already answered that request")

        answer = approval_wire.answer_for(verdict)
        assert answer is not None  # ASK is the only verdict answered by silence
        try:
            await watched.connection.respond(pending.wire_id, answer)
        except WireError as unreachable:
            return _failed(request_id, f"the verdict could not be sent: {unreachable}")

        watched.pending.pop(request.approval_id, None)
        if await self._resolution_seen(pending.wire_id):
            return DeliveryReceipt(request_id=request_id, outcome=Delivery.DELIVERED)
        return DeliveryReceipt(
            request_id=request_id,
            outcome=Delivery.UNKNOWN,
            reason="the verdict was sent but codex never reported the request resolved",
        )

    async def verify(self) -> VerifyResult:
        """Report what is loaded, and whether the engine's own app-server answers."""
        loaded = f"{type(self).__module__}:{type(self).__name__}"
        if not self._opened:
            return VerifyResult(
                outcome=VerifyOutcome.FAIL,
                loaded=loaded,
                detail="this adapter has not been connected",
            )
        try:
            await self._own.connection.request(
                "thread/loaded/list", {}, timeout_seconds=self._settings.request_timeout_seconds
            )
        except (WireError, AppServerError) as unreachable:
            return VerifyResult(
                outcome=VerifyOutcome.FAIL,
                loaded=loaded,
                detail=f"the codex app-server did not answer: {unreachable}",
            )
        return VerifyResult(
            outcome=VerifyOutcome.PASS,
            loaded=loaded,
            detail=f"{len(self._threads)} Codex Session(s) watched",
        )

    # -- carrying words ---------------------------------------------------

    async def _relay(
        self, target: SessionTarget, text: str, *, request_id: RequestId, route: RelayRoute
    ) -> DeliveryReceipt:
        """One attempt, classified into the hub's four states and nothing else."""
        watched = self._threads.get(target)
        if watched is None:
            return _failed(request_id, f"no Codex Session is registered as {target}")

        refusal = self._misrouted(watched)
        if refusal:
            return _failed(request_id, refusal)

        # Everything up to here happens before any of the user's words are on
        # the wire, so a failure in it proves the thread never saw them.
        try:
            await self._subscribe(watched)
        except (WireError, AppServerError) as unreachable:
            return _failed(request_id, f"the Session's app-server is unreachable: {unreachable}")
        if not watched.subscribed:
            return _failed(
                request_id,
                "that Session has not started work yet, so codex cannot resume it: "
                f"{watched.subscribe_blocked}",
            )

        if route is RelayRoute.SUPPLEMENT:
            return await self._steer(watched, text, request_id=request_id)
        return await self._start_turn(watched, text, request_id=request_id)

    async def _start_turn(
        self, watched: WatchedThread, text: str, *, request_id: RequestId
    ) -> DeliveryReceipt:
        try:
            await watched.connection.request(
                "turn/start",
                {
                    "threadId": watched.thread_id,
                    "clientUserMessageId": str(request_id),
                    "approvalPolicy": PINNED_POLICY,
                    "approvalsReviewer": USER_REVIEWER,
                    "input": [{"type": "text", "text": text}],
                },
            )
        except RemoteError as refused:
            # A rejected request never started a turn, so the words did not land.
            return _failed(request_id, f"codex refused the turn: {refused.remote_message}")
        except WireError as lost:
            # Past the dial with no answer: the words may or may not have gone.
            return _unknown(request_id, f"codex never answered the turn: {lost}")

        watched.assert_pinned()
        receipt = await self._await_receipt(watched, request_id)
        await self._read_routing_back(watched)
        return receipt

    async def _steer(
        self, watched: WatchedThread, text: str, *, request_id: RequestId
    ) -> DeliveryReceipt:
        """Mid-turn, into the turn that is actually running — or fail closed."""
        turn_id = watched.active_turn_id
        if turn_id is None:
            return _failed(
                request_id,
                "no turn is running on that Session, so there is nothing to supplement",
            )
        try:
            await watched.connection.request(
                "turn/steer",
                {
                    "threadId": watched.thread_id,
                    "expectedTurnId": turn_id,
                    "clientUserMessageId": str(request_id),
                    "input": [{"type": "text", "text": text}],
                },
            )
        except RemoteError as refused:
            # The turn ended between the user speaking and the words landing.
            # Codex names the turn it actually found, which is worth quoting.
            return _failed(request_id, f"codex refused the supplement: {refused.remote_message}")
        except WireError as lost:
            return _unknown(request_id, f"codex never answered the supplement: {lost}")

        receipt = await self._await_receipt(watched, request_id)
        await self._read_routing_back(watched)
        return receipt

    async def _await_receipt(
        self, watched: WatchedThread, request_id: RequestId
    ) -> DeliveryReceipt:
        """Poll the thread until it shows the words, or until the wait is spent.

        Exactly one matching `userMessage` is delivery. None yet is not an
        answer, so it keeps waiting. More than one, or a readback that cannot be
        counted at all, *contradicts* the attempt — and a contradiction is
        UNKNOWN, never a failure: the words may well be in there.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.receipt_timeout_seconds
        while True:
            try:
                readback = await watched.connection.request(
                    "thread/read", {"threadId": watched.thread_id, "includeTurns": True}
                )
            except WireError as unreadable:
                return _unknown(request_id, f"the thread could not be read back: {unreadable}")

            found = _receipts_in(readback, watched.thread_id, str(request_id))
            if found == 1:
                return DeliveryReceipt(request_id=request_id, outcome=Delivery.DELIVERED)
            if found is None or found > 1:
                return _unknown(
                    request_id,
                    "the codex readback contradicted the attempt: it holds "
                    f"{'an uncountable number of' if found is None else found} copies",
                )
            if loop.time() >= deadline:
                return _unknown(
                    request_id,
                    "codex never showed the words in the thread within "
                    f"{self._settings.receipt_timeout_seconds:.0f}s",
                )
            await asyncio.sleep(self._settings.receipt_poll_seconds)

    async def _resolution_seen(self, wire_id: Any) -> bool:
        """Wait, bounded, for codex to say that request is resolved."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.verdict_timeout_seconds
        while loop.time() < deadline:
            if wire_id in self._resolved:
                return True
            await asyncio.sleep(0.02)
        return wire_id in self._resolved

    # -- subscribing and listening ----------------------------------------

    async def _subscribe(self, watched: WatchedThread) -> None:
        """Resume the thread, which is what puts its event stream on this socket.

        The approval routing is *read* here and never asserted: probing codex
        0.148.0 showed a resume-time override is accepted and silently ignored
        on a live thread. Asserting it is `turn/start`'s job.

        The turn history comes back with it. `excludeTurns` would trim it, but
        that parameter is gated behind `experimentalApi`, and a connection that
        only watches a user's Session has no business claiming that capability
        just to make one response smaller — the frame limit is what bounds the
        size. Found by the bare-terminal proof against a real app-server, which
        is the only place the gate shows up.
        """
        if watched.subscribed:
            return
        try:
            echo = await watched.connection.request(
                "thread/resume", {"threadId": watched.thread_id}
            )
        except RemoteError as refused:
            if NO_ROLLOUT_YET not in refused.remote_message:
                raise
            # A Session the user launched a moment ago has no rollout on disk
            # until it has done something, and a thread with no rollout cannot
            # be resumed. That is a state it grows out of, so it is recorded
            # and retried when the thread next reports activity — the
            # thread-lifecycle notifications arrive whether or not this client
            # is subscribed, which is what makes the retry possible at all.
            watched.subscribe_blocked = refused.remote_message
            return
        thread = echo.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != watched.thread_id:
            raise WireError("codex resumed a different thread than the one asked for")
        watched.subscribed = True
        watched.subscribe_blocked = ""
        watched.read_routing(echo)
        self._note_status(watched, thread.get("status"))

    async def _read_routing_back(self, watched: WatchedThread) -> None:
        """Ask codex where this thread's approvals now go, having just said.

        `turn/start` answers with the turn and nothing else, so the assertion it
        carried is unverified until something echoes it. A
        `thread/settings/updated` notification does arrive — but a notification
        is not something a caller can wait for, and a mis-route that is only
        noticed if a message happens to land in time is a mis-route that is
        sometimes not noticed at all. `thread/resume` echoes the settings, is
        non-destructive on a live thread, and is already how this adapter reads
        them, so it is asked directly.

        The Relay's own receipt is not touched by what comes back. The words
        either reached the thread or did not, and that is what a receipt grades;
        a disagreement here is recorded against the thread, and the *next* Relay
        or verdict is the one that refuses. Grading arrived words FAILED would
        make Bridge Core re-deliver them, which is how duplicates get made.
        """
        try:
            echo = await watched.connection.request(
                "thread/resume", {"threadId": watched.thread_id}
            )
        except (WireError, AppServerError) as unreadable:
            _log.info(
                "could not read back where %s routes approvals: %s",
                watched.thread_id,
                unreadable,
            )
            return
        watched.read_routing(echo)
        if watched.routing is ApprovalRouting.MISROUTED:
            _log.warning(
                "codex ignored the approval pin on %s: %s",
                watched.thread_id,
                watched.routing_detail,
            )

    def _misrouted(self, watched: WatchedThread) -> str:
        """The refusal for a thread whose approvals provably do not reach the user."""
        if watched.routing is not ApprovalRouting.MISROUTED:
            return ""
        return (
            "this Session's permission requests are routed away from the user, so a Relay "
            f"into it cannot be answered by voice: {watched.routing_detail}"
        )

    def _heard(self, message: Message) -> None:
        """One notification. Never blocks: the event sink is non-blocking by contract."""
        method = message.get("method")
        params = message.get("params")
        if not isinstance(params, dict):
            return
        watched = self._thread_for(params.get("threadId"))

        if method == "serverRequest/resolved":
            self._resolved.add(params.get("requestId"))
            if watched is not None:
                self._retire_resolved(watched, params.get("requestId"))
            return
        if watched is None:
            return

        match method:
            case "thread/status/changed":
                self._note_status(watched, params.get("status"))
                if not watched.subscribed:
                    self._spawn(self._resubscribe(watched))
            case "turn/started":
                turn = params.get("turn")
                watched.active_turn_id = turn.get("id") if isinstance(turn, dict) else None
            case "turn/completed":
                watched.active_turn_id = None
            case "thread/settings/updated":
                settings = params.get("threadSettings")
                if isinstance(settings, dict):
                    watched.read_routing(settings)
            case "thread/closed" | "thread/deleted":
                self._emit(SessionEnded(target=watched.target, detail=str(method)))

    def _asked(self, message: Message) -> None:
        """One server request. Only the permission prompts are ours to hold."""
        method = message.get("method")
        params = message.get("params")
        if method not in approval_wire.APPROVAL_METHODS or not isinstance(params, dict):
            # Anything else is somebody else's to answer — most likely the TUI's,
            # which holds the same request. Staying silent is what lets it.
            return
        watched = self._thread_for(params.get("threadId"))
        if watched is None:
            return

        request = approval_wire.request_from(method, params, target=watched.target)
        watched.pending[request.approval_id] = PendingApproval(
            approval_id=request.approval_id, wire_id=message.get("id"), method=str(method)
        )
        self._emit(AwaitingApproval(request=request))

    def _retire_resolved(self, watched: WatchedThread, wire_id: Any) -> None:
        """Drop a prompt somebody else answered, so no verdict lands on a closed one."""
        for approval_id, pending in list(watched.pending.items()):
            if pending.wire_id == wire_id:
                del watched.pending[approval_id]
                watched.answered_elsewhere.add(approval_id)

    def _note_status(self, watched: WatchedThread, status: Any) -> None:
        """Map a thread status onto the Reply Window, and onto having stopped.

        Only a transition *out of* active is a stop. The first `idle` a thread
        reports is it sitting there having done nothing, and announcing that as
        "a Session stopped and may need you" would make every registration ring.
        """
        kind = status.get("type") if isinstance(status, dict) else None
        if kind not in ("idle", "active", "systemError"):
            return
        window = ReplyWindow.CLOSED if kind == "active" else ReplyWindow.OPEN
        first_look = not watched.observed
        was_running = watched.observed and watched.reply_window is ReplyWindow.CLOSED
        watched.observed = True

        if first_look or window is not watched.reply_window:
            watched.reply_window = window
            self._emit(ReplyWindowChanged(target=watched.target, window=window))
        if kind == "active":
            return
        if was_running:
            watched.active_turn_id = None
            self._emit(
                SessionStopped(
                    target=watched.target,
                    waiting_for=(
                        WaitingFor(kind=WaitingKind.UNKNOWN, caught_up=False)
                        if kind == "systemError"
                        else WaitingFor()
                    ),
                )
            )

    def _connection_lost(self, target: SessionTarget, reason: str) -> None:
        """The app-server holding that Session went away. Say so, exactly once.

        A Codex TUI is a thin client of its app-server, so an app-server that is
        gone means the Session on it is gone too — there is no surviving process
        for it to be a session of. That makes `SessionEnded` the honest event
        rather than a guess, and it is what stops the hub from holding a target
        it can never reach: the Relay queue answers a Session that ended, while
        a Session merely marked unreachable would keep words waiting for a
        window that will never open.

        No reconnect is attempted, and that is the "no retry storm" half of the
        contract: the socket belongs to a process that is not this engine's to
        restart, and dialling a dead one in a loop would be this adapter
        inventing a recovery it cannot perform.
        """
        watched = self._threads.pop(target, None)
        if watched is None:
            return
        watched.subscribed = False
        watched.pending.clear()
        self._emit(SessionEnded(target=target, detail=reason))

    async def _resubscribe(self, watched: WatchedThread) -> None:
        """Try again to resume a thread that had nothing to resume."""
        try:
            await self._subscribe(watched)
        except (WireError, AppServerError) as unreachable:
            _log.info("could not subscribe to %s: %s", watched.thread_id, unreachable)

    def _spawn(self, work: Any) -> None:
        """Run a retry without letting it outlive the adapter that started it."""
        task = asyncio.ensure_future(work)
        self._retrying.add(task)
        task.add_done_callback(self._retrying.discard)

    def _thread_for(self, thread_id: Any) -> WatchedThread | None:
        if not isinstance(thread_id, str):
            return None
        for watched in self._threads.values():
            if watched.thread_id == thread_id:
                return watched
        return None

    def _emit(self, event: Any) -> None:
        if self._sink is not None:
            self._sink.emit(event)


def _receipts_in(readback: Message, thread_id: str, request_id: str) -> int | None:
    """How many delivery receipts this readback holds, or `None` if it cannot be counted.

    `None` is a third answer and is kept apart from zero on purpose: a readback
    describing a different thread, or one whose shape is not what the protocol
    promises, says nothing about what arrived — while zero says the words are
    not there *yet*.
    """
    thread = readback.get("thread")
    if not isinstance(thread, dict) or thread.get("id") != thread_id:
        return None
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return None

    found = 0
    for turn in turns:
        if not isinstance(turn, dict):
            return None
        items = turn.get("items")
        if items is None:
            continue
        if not isinstance(items, list):
            return None
        found += sum(
            1
            for item in items
            if isinstance(item, dict)
            and item.get("type") == "userMessage"
            and item.get("clientId") == request_id
        )
    return found


def _failed(request_id: RequestId, reason: str) -> DeliveryReceipt:
    return DeliveryReceipt(request_id=request_id, outcome=Delivery.FAILED, reason=reason)


def _unknown(request_id: RequestId, reason: str) -> DeliveryReceipt:
    return DeliveryReceipt(request_id=request_id, outcome=Delivery.UNKNOWN, reason=reason)
