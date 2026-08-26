"""What Codex Sessions are running, from the shared daemon and from the machine.

Two sources, and the merge between them is the whole module.

**The shared app-server daemon is the authority when it is up** (#82). It knows a
thread's id, its name, its workspace and what it is doing, and it is the only
route a Relay or an Approval can take. Its roster is `thread/loaded/list`
answering `{"data": [id, …]}`, and each id is described by `thread/read` as
`{"thread": {"id", "name", "cwd", "status"}}` — measured on 0.149.1 by #82's
prototype (`661d3d9`), not assumed.

**The process table is what is left when it is not** — and it is not, often. A
TUI that started while the daemon was down is never adopted by a daemon that
starts later (#82, measured), so "the daemon is up" and "the daemon knows about
this Session" are different questions and the second one has to keep an answer.
Those rows are `degraded`, not an error: they are true, they are just thinner.

**A Session that has not been spoken to has no id at all**, because `codex`
writes the rollout carrying one at its first *turn* (#73). Such a row is
addressed by its pid alone, and gains its id later without becoming a second row
— see `core.sessions._better_known`.

**Unreachability gets no row and no field.** #68 removed that vocabulary: a
process-table row is listed like any other, and a Relay into a Session the
daemon cannot load returns the existing `FAILED` grade with its reason, before
the wire (#82). The roster's job is to say what exists.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol

from gpt_voicecoding.adapters.agent.codex import rollouts, thread_tail
from gpt_voicecoding.adapters.agent.codex.processes import Candidate, enumerate_sessions
from gpt_voicecoding.seams.agent import (
    LaneDiscovery,
    Progress,
    SessionInspection,
    SessionLifecycle,
    SessionState,
    WaitingFor,
    WaitingKind,
)
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget

_log = logging.getLogger(__name__)

#: The daemon's roster, and the per-thread read.
ROSTER_METHOD: Final = "thread/loaded/list"
READ_METHOD: Final = "thread/read"

#: What a thread's status is called while a turn is running. Named because two
#: rules turn on it: the row reads as `RUNNING`, and the cadence does not read
#: its turns.
ACTIVE_STATUS: Final = "active"

#: What a thread's `status.type` can be, as the Codex spoke has observed it.
#: `systemError` is a thread whose turn ended badly — still reachable, still
#: able to take the next Relay, which is why it reads as idle rather than as
#: something that must be waited out.
STATUS_TYPES: Final = {
    "idle": SessionState.IDLE,
    ACTIVE_STATUS: SessionState.RUNNING,
    "systemError": SessionState.IDLE,
}

#: What every degraded reading ends with: where the rows actually came from.
#: The *reason* is a separate sentence, because there are two of them and they
#: are not the same fact.
FROM_THE_MACHINE = "so these rows come from the process table and the rollouts on disk"

#: This engine holds no connection, and nothing recorded why. **The fallback
#: sentence, and it should be the rarest of the three** — `SharedDaemon.client()`
#: sets a reason on `note` on every path that answers `None`, and `_degraded`
#: prefers that reason over this one because the dial's own words are always more
#: precise than this is.
#:
#: **It says only what this process can see**, which is the reading the Advisor
#: fixed on #96 ("consequence for part B", 2026-08-26): a client `None` means
#: there is no connection *here*, and it may not be spelled as a claim about
#: whether the daemon was dialled, answered, or exists. That is `NO_DAEMON`'s
#: sentence, and it is only ever said after a request was really made.
#:
#: **What this used to say, and why it may not say it again** (#96). Until #76,
#: `CodexAgentAdapter._shared_daemon()` returned `None` unconditionally, and this
#: sentence was the same one as `NO_DAEMON` below — so a roster reading "the
#: daemon did not answer" was produced without a single byte being sent, while
#: `bridge-install status` on the same machine at the same moment really did dial
#: the daemon and really did get an answer. The two readings looked like a
#: contradiction to be investigated, and a session went and investigated it. #76
#: builds the client, so "nothing was dialled" is no longer true either; what
#: survives from #96 is its rule, which is the one that mattered: **never claim
#: the daemon was silent, and never claim anything about it this build did not
#: observe.**
NO_CLIENT = (
    f"this engine holds no connection to the shared Codex app-server daemon, {FROM_THE_MACHINE}"
)

#: The daemon was dialled and did not answer. Only ever said after a request was
#: actually made — which is what makes it different from `NO_CLIENT`.
NO_DAEMON = f"the shared Codex app-server daemon did not answer, {FROM_THE_MACHINE}"

#: The daemon answered, and this build could not read what it said. A third
#: sentence rather than a parenthesis on the second, for the reason the second
#: exists at all: "did not answer (it answered a shape this build cannot read)"
#: contradicts itself inside one sentence, and the half a reader carries away is
#: the half that blames the daemon. The fault here is this build's.
UNREADABLE_ROSTER = (
    f"the shared Codex app-server daemon answered {ROSTER_METHOD} in a shape this build "
    f"cannot read, {FROM_THE_MACHINE}"
)


class DaemonClient(Protocol):
    """The one verb this module needs of a connection to the shared daemon."""

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call one method and wait for its answer, or raise saying why not."""
        ...


#: How the process table is read. Injected so the merge can be tested.
ProcessLister = Callable[[], Awaitable[tuple[Candidate, ...]]]


class TurnCache:
    """Every loaded thread's `Progress`, read at most once per change (#76).

    **The cache is the whole reason this class exists, and it was measured.** A
    `thread/read` with `includeTurns: true` answered **558,875 bytes** for a
    thread of two turns against the real daemon on codex 0.149.1 (2026-08-26),
    where the same read without turns answered 3,600. `numTurns` is not a
    parameter of this method — passing it changed nothing — so there is no way
    to ask for a smaller answer. On a five-second cadence over a machine of
    stopped Sessions, reading turns every tick would be megabytes a minute for
    an answer that had not changed.

    **Keyed on the thread's own `updatedAt`,** which is the same argument the
    Claude lane's transcript cache makes from `(size, mtime)`: a thread that has
    not been touched cannot have changed what its tail says, and the key is the
    far side's own account rather than a clock this module would have to guess
    the right length for. A thread that names no `updatedAt` is never cached —
    without a time there is nothing to say it has not moved.

    **`updatedAt` is epoch seconds, so its resolution is one second**, and two
    changes to a thread inside the same second are one change to this cache. That
    is written down rather than defended against: the rows it keys are *stopped*
    threads, so the next thing that moves one moves the second too, and a row
    somebody asks about through the `progress` verb is read live regardless.

    `read_at` is kept from the reading that was taken, not refreshed on a hit.
    That is what it means: when this was true.
    """

    def __init__(self) -> None:
        #: thread id → (the `updatedAt` it was read at, what was read).
        self._cache: dict[str, tuple[Any, Progress]] = {}

    async def progress_for(self, client: DaemonClient, thread: dict[str, Any]) -> Progress | None:
        """This thread's progress, read or remembered — or `None` for a live turn.

        A thread mid-turn is not read on the cadence, for the reason the Claude
        lane does not open a `RUNNING` Session's transcript: it is the expensive
        read, and the roster row is the cheap projection beside the per-target
        verb (#76, advisor ruling Q3).
        """
        thread_id = thread.get("id")
        if not isinstance(thread_id, str) or _status_of(thread) == ACTIVE_STATUS:
            return None
        stamp = thread.get(thread_tail.UPDATED_AT)
        cached = self._cache.get(thread_id)
        if stamp is not None and cached is not None and cached[0] == stamp:
            return cached[1]
        described = await read_thread(client, thread_id, with_turns=True)
        if described is None:
            return None
        progress = progress_from(described)
        if stamp is not None:
            self._cache[thread_id] = (stamp, progress)
        return progress

    def forget(self, thread_id: str | None) -> None:
        """Drop one thread's remembered reading, so the next look is a fresh one.

        The per-target verb's doing (#76). It is how "read this Session now"
        stays **one** read: the enumeration that follows takes the fresh deep
        read for this thread, instead of the cadence answering from the cache
        and the verb then reading a second time.
        """
        if thread_id is not None:
            self._cache.pop(thread_id, None)

    def retain(self, thread_ids: set[str]) -> None:
        """Forget every thread the daemon no longer holds, so this stays roster-sized."""
        for gone in set(self._cache) - thread_ids:
            del self._cache[gone]


async def discover(
    client: DaemonClient | None = None,
    *,
    processes: ProcessLister = enumerate_sessions,
    home: Path | None = None,
    turns: TurnCache | None = None,
    daemon_note: str = "",
) -> LaneDiscovery:
    """Every Codex Session on this machine, however well it can be described.

    The process table is read first and always: it is the only source that sees
    a Session the daemon has never heard of, and #82 proved that is not a corner
    case but the ordinary result of starting a TUI while the daemon is down.
    """
    try:
        candidates = await processes()
    except (OSError, TimeoutError) as unreadable:
        return LaneDiscovery(error=f"the process table could not be read: {unreadable}")

    threads, daemon_error = await _threads(client)
    rows: list[SessionInspection] = []
    claimed: set[int] = set()

    for thread in threads:
        pid = _pid_for(thread, candidates, claimed)
        if pid is not None:
            claimed.add(pid)
        progress = (
            await turns.progress_for(client, thread)
            if turns is not None and client is not None
            else None
        )
        rows.append(_from_thread(thread, pid, progress))
    if turns is not None:
        turns.retain({str(thread.get("id")) for thread in threads})

    rows.extend(
        _from_process(candidate, home=home)
        for candidate in candidates
        if candidate.pid not in claimed
    )
    return LaneDiscovery(rows=tuple(rows), degraded=_degraded(daemon_error, daemon_note))


def progress_from(thread: Mapping[str, Any]) -> Progress:
    """One `thread/read` answer, as the seam holds it.

    The one place a thread document becomes a `Progress`, so the cadence's cached
    read and the verb's live one cannot come back describing it two ways.
    `read_at` is stamped here because it belongs to the *reading*: it is when this
    was true, and a value carried forward from a cache hit keeps its own moment.
    """
    entries, truncated = thread_tail.recent(thread)
    return Progress(recent=entries, truncated=truncated, read_at=datetime.now(UTC))


def _degraded(daemon_error: str | None, note: str) -> str | None:
    """Why these rows are thinner than usual — from both things that can say so.

    A lane can be reading from the process table *and* joined to a daemon whose
    version disagrees with the CLI's, and neither fact is allowed to hide the
    other. There is deliberately no `error` path here: a missing daemon has
    never been a reason to report no Sessions (#74).

    **A dial that failed says why in its own words, once.** When there is no
    client and the dial left a reason, that reason replaces `NO_CLIENT` rather
    than following it: "holds no connection; codex did not answer within 10
    seconds" is two sentences making one claim, and #96 is the record of what a
    roster that makes more claims than it observed costs to read.
    """
    if daemon_error == NO_CLIENT and note:
        return f"{note}, {FROM_THE_MACHINE}"
    reasons = [reason for reason in (daemon_error, note) if reason]
    return "; ".join(reasons) or None


async def _threads(client: DaemonClient | None) -> tuple[list[dict[str, Any]], str | None]:
    """Every thread the daemon holds, or the reason there are none to hold.

    A daemon that is absent, refusing or answering nonsense all mean one thing
    to this lane: the rows will be thinner than usual. None of them is a reason
    to report no Sessions, because the process table has already been read.

    **There are three ways to end up thin here, and three sentences.** Nothing
    was dialled; the daemon was dialled and did not answer; the daemon answered
    and this build could not read it. The consequence is the same every time and
    the causes are not, and reporting the second when the first or the third is
    true is a false claim about the daemon's health — one that contradicts every
    other surface that really does dial it (#96).
    """
    if client is None:
        return [], NO_CLIENT
    try:
        answer = await client.request(ROSTER_METHOD, {})
    except Exception as unreachable:  # noqa: BLE001 - any failure is the same fact here
        _log.info("the shared Codex daemon did not answer %s: %s", ROSTER_METHOD, unreachable)
        return [], f"{NO_DAEMON} ({unreachable})"

    ids = answer.get("data") if isinstance(answer, dict) else None
    if not isinstance(ids, list):
        return [], UNREADABLE_ROSTER

    found: list[dict[str, Any]] = []
    for thread_id in ids:
        if not isinstance(thread_id, str) or not thread_id.strip():
            continue
        described = await read_thread(client, thread_id.strip())
        if described is not None:
            found.append(described)
    return found, None


async def read_thread(
    client: DaemonClient, thread_id: str, *, with_turns: bool = False
) -> dict[str, Any] | None:
    """One thread as the daemon describes it, or `None` if it cannot describe it.

    `with_turns` is the expensive half and is asked for only by `TurnCache`,
    which is where the measurement justifying that word lives.
    """
    try:
        answer = await client.request(
            READ_METHOD, {"threadId": thread_id, "includeTurns": with_turns}
        )
    except Exception as unreadable:  # noqa: BLE001 - one bad thread is not a bad roster
        _log.info("the daemon could not describe thread %s: %s", thread_id, unreadable)
        return None
    thread = answer.get("thread") if isinstance(answer, dict) else None
    if not isinstance(thread, dict) or thread.get("id") != thread_id:
        _log.info("%s answered about a different thread than %s", READ_METHOD, thread_id)
        return None
    return thread


def _pid_for(
    thread: dict[str, Any], candidates: tuple[Candidate, ...], claimed: set[int]
) -> int | None:
    """The TUI running this thread, when exactly one process can be it.

    Joined on the workspace, because that is the only field the two sources
    share — the daemon does not report a pid and the process table does not
    report a thread. Two unclaimed TUIs in one directory cannot be told apart,
    so neither is claimed: a row addressed by the wrong pid is worse than one
    addressed by its thread id alone, which still reaches the daemon.
    """
    cwd = thread.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        return None
    wanted = os.path.realpath(cwd)
    matches = [
        candidate.pid
        for candidate in candidates
        if candidate.pid not in claimed and os.path.realpath(candidate.workspace) == wanted
    ]
    return matches[0] if len(matches) == 1 else None


def _status_of(thread: Mapping[str, Any]) -> str | None:
    """What this thread says it is doing, in its own word."""
    status = thread.get("status")
    kind = status.get("type") if isinstance(status, Mapping) else None
    return kind if isinstance(kind, str) else None


def _from_thread(
    thread: dict[str, Any], pid: int | None, progress: Progress | None = None
) -> SessionInspection:
    """One daemon-held thread as the seam holds it."""
    kind = _status_of(thread)
    state = STATUS_TYPES.get(str(kind), SessionState.RUNNING)
    cwd = thread.get("cwd")
    name = thread.get("name")
    return SessionInspection(
        target=SessionTarget(agent=AgentKind.CODEX, session_id=str(thread["id"]), pid=pid),
        workspace=Path(str(cwd)) if isinstance(cwd, str) and cwd.strip() else Path(),
        lifecycle=SessionLifecycle.LIVE,
        state=state,
        # The status says whether a turn is running, never what a stopped thread
        # stopped on. #75 reads that; a `systemError` is flagged for it to look.
        waiting_for=(
            WaitingFor(kind=WaitingKind.UNKNOWN, caught_up=False)
            if kind == "systemError"
            else WaitingFor()
        ),
        progress=progress,
        # Free on the cheap read, and honest for a thread mid-turn too: it is
        # the thread's own account of when it last moved, which is exactly the
        # case `last_activity` exists to answer when nothing was said (#76).
        last_activity=thread_tail.last_activity(thread),
        name=name.strip() if isinstance(name, str) and name.strip() else None,
    )


def _from_process(candidate: Candidate, *, home: Path | None) -> SessionInspection:
    """One running TUI the daemon does not hold, named as well as disk allows.

    Its thread id comes from the newest rollout whose own `cwd` is this
    workspace **and which was written after this process started**, and only
    once the Session has taken a turn — before that there is genuinely no id,
    and the row is addressed by pid.

    The start-time bound is what keeps the workspace join honest. A workspace
    outlives the Sessions run in it and rollouts stay on disk, so the newest one
    in a directory is only *this* Session's if this Session could have written
    it. Without the bound a fresh TUI adopts the id of whatever ran there last,
    and `core.sessions._better_known` — which refuses to let a known id be
    overwritten by `None` — then protects that wrong id from every later,
    honest reading.

    **The state is `RUNNING` because nothing here can see one.** A process is
    not evidence of a Reply Window, and `RUNNING` is the reading that holds a
    Relay rather than delivering it into a Session that may be mid-turn. That
    matters more here than anywhere: this Session's Relay would fail at the wire
    anyway (#82), and a held Relay is one the user gets back.
    """
    rollout = rollouts.newest_for(candidate.workspace, home=home, since=candidate.started_at)
    meta = rollouts.session_meta(rollout) if rollout is not None else None
    return SessionInspection(
        target=SessionTarget(
            agent=AgentKind.CODEX,
            session_id=rollouts.session_id_in(meta) if meta is not None else None,
            pid=candidate.pid,
        ),
        workspace=candidate.workspace,
        lifecycle=SessionLifecycle.LIVE,
        state=SessionState.RUNNING,
        waiting_for=WaitingFor(),
    )
