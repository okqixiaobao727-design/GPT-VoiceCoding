"""What Codex Sessions are running: the two readings, and where they meet.

**This module is the I/O half.** It dials the shared daemon, reads the process
table, reads the rollouts on disk, and asks `codex/roster.py` — one pure
function, no client, no clock — what all of that adds up to. The composition
rule lived here until #201, inline across five branches of `discover`, unnamed
and with no test surface of its own; it had been rewritten by #112, #113, #123
and #144, and a wrong rule survived three tickets because nothing could ask it
a question without a fake app-server and a fake process table.

**The shared app-server daemon is the authority when it is up** (#82). It knows
a thread's id, its name, its workspace and what it is doing, and it is the only
route a Relay or an Approval can take. Its roster is `thread/loaded/list`
answering `{"data": [id, …]}`, and each id is described by `thread/read` as
`{"thread": {"id", "name", "cwd", "status", "createdAt"}}` — measured on 0.149.1
by #82's prototype (`661d3d9`), not assumed.

**The process table supplies liveness and place** (#201). A live interactive
`codex` with a controlling terminal says a Session is being sat in; where it is
running says which one it could be sitting in. It never says what a Session
*is*: identity is the daemon's thread id and nothing else.

**The rollouts on disk supply one fact and one only**: whether an exact
`resume <UUID>` process names a thread the user themselves rooted. That is what
lets a TUI started while the daemon was down compose a row at all — #82 proved
such a TUI is never adopted by a daemon that starts later, so it is not a corner
case but the ordinary result.

**Unreachability gets no row and no field.** #68 removed that vocabulary: a
Relay into a Session the daemon cannot load returns the existing `FAILED` grade
with its reason, before the wire (#82). The roster's job is to say what is
proven to exist.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol

from gpt_voicecoding.adapters.agent import _naming
from gpt_voicecoding.adapters.agent._progress import source_degradation
from gpt_voicecoding.adapters.agent._project import ProjectNames
from gpt_voicecoding.adapters.agent.codex import rollouts, roster, thread_tail
from gpt_voicecoding.adapters.agent.codex.processes import Candidate, enumerate_sessions
from gpt_voicecoding.seams.agent import (
    LaneDiscovery,
    ProgressCapture,
    ProgressObservation,
    SessionInspection,
)

_log = logging.getLogger(__name__)

#: The daemon's roster, and the per-thread read.
ROSTER_METHOD: Final = "thread/loaded/list"
READ_METHOD: Final = "thread/read"

#: How much of that first message codex 0.150.0 makes a thread's **provisional**
#: name out of: `THREAD_TITLE_MAX_CHARS`, `rust-v0.150.0:codex-rs/tui/src/app/
#: thread_title.rs:22`, applied by `tui/src/app/thread_routing.rs:1823-1829` as
#: `.chars().take(_)` over the whitespace-collapsed message.
#:
#: **Read as codex's number rather than as a length this product chose**, which
#: is why it is named after the constant it mirrors. If a later codex composes
#: its provisional title differently, this rule stops matching and the daemon's
#: name is kept — the behaviour before #113, not a worse one, and the acceptance
#: reads the Session Name aloud where a person would notice.
PROVISIONAL_TITLE_CHARACTERS: Final = 36

#: How much of a thread id stands in for a name the daemon does not have. Eight
#: characters of a UUID, which is what `codex` itself shows and short enough to
#: say out loud — the fallback task of every unnamed thread (#78). It is
#: composed here, from a fact this lane already holds: nothing is asked of the
#: Session, and the route that used to ask one (`legacy@1d32845:bridge/hook.py:
#: 215-253`, `bridge/daemon.py:1504-1544`) was *dropped* from the #67 port table
#: on 2026-08-25.
SHORT_THREAD_ID_CHARACTERS: Final = 8

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


@dataclass(frozen=True, slots=True)
class ProcessEvidence:
    """The process-table reading and rollout tree that make one evidence source.

    The two inputs are inseparable: an argv thread id can use an explicit user
    rollout to supply root classification when the daemon omits it. Keeping
    them behind one provider prevents a test or caller from injecting one half
    while accidentally reading the machine's real other half.
    """

    list_sessions: ProcessLister = enumerate_sessions
    home: Path | None = None

    async def observations(self) -> tuple[roster.ProcessObservation, ...]:
        """Every live interactive `codex`, with rollout-root evidence where there is any."""
        return _from_processes(await self.list_sessions(), home=self.home)


class TurnCache:
    """Every loaded thread's `ProgressObservation`, read at most once per change (#76).

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

    def __init__(self, *, progress_capture: ProgressCapture) -> None:
        self.capture = progress_capture
        #: thread id → (the `updatedAt` it was read at, what was read).
        self._cache: dict[str, tuple[Any, ProgressObservation]] = {}

    async def progress_for(
        self, client: DaemonClient, thread: dict[str, Any]
    ) -> ProgressObservation:
        """This thread's progress, read or remembered — or `not_read` for a live turn.

        A thread mid-turn is not read on the cadence, for the reason the Claude
        lane does not open a `RUNNING` Session's transcript: it is the expensive
        read, and the roster row is the cheap projection beside the per-target
        verb (#76, advisor ruling Q3).
        """
        thread_id = thread.get("id")
        if not isinstance(thread_id, str) or roster.status_of(thread) == roster.ACTIVE_STATUS:
            return ProgressObservation()
        stamp = thread.get(thread_tail.UPDATED_AT)
        cached = self._cache.get(thread_id)
        if stamp is not None and cached is not None and cached[0] == stamp:
            return cached[1]
        return await self._read_now(client, thread_id, cache_stamp=stamp)

    async def read_now(self, client: DaemonClient, thread_id: str) -> ProgressObservation:
        """Read one Stop now and retain it for the matching later roster row."""
        return await self._read_now(client, thread_id)

    async def _read_now(
        self,
        client: DaemonClient,
        thread_id: str,
        *,
        cache_stamp: Any = None,
    ) -> ProgressObservation:
        """The one deep-read normalization and cache path."""
        reading = await read_thread(client, thread_id, with_turns=True)
        if reading.thread is None:
            assert reading.reason is not None
            return ProgressObservation.unreadable(reading.reason)
        progress = progress_from(reading.thread, capture=self.capture)
        observed_stamp = reading.thread.get(thread_tail.UPDATED_AT, cache_stamp)
        if observed_stamp is not None:
            self._cache[thread_id] = (observed_stamp, progress)
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
    evidence: ProcessEvidence | None = None,
    turns: TurnCache | None = None,
    daemon_note: str = "",
    projects: ProjectNames | None = None,
    reported_non_sessions: set[str] | None = None,
    reported_unheld_terminals: set[int] | None = None,
) -> LaneDiscovery:
    """Every Codex Session on this machine, however well it can be described.

    Four readings and one decision. The process table is read first and always:
    it is the only source that sees a Session the daemon has never heard of, and
    #82 proved that is not a corner case. The daemon is read next, for identity
    and content. `roster.compose` then decides, from those two and nothing else,
    what the rows are — and this function's remaining work is the two things
    that cost I/O and so cannot be decided there: each row's progress and its
    Session Name.

    `reported_non_sessions` holds the ids this lane has already said are not
    Sessions, and it is what makes that sentence one per thread rather than one
    every five seconds. The caller keeps it across ticks; `_threads` prunes it
    back to what the daemon still holds. Given nothing, each pass says it once,
    which is what a one-shot reading wants anyway.

    `reported_unheld_terminals` is the same discipline for the same sentence
    said from the other source (#233): the pids of the live terminals this lane
    has already said answer to nothing on its roster. It is pruned back to the
    pids still running, because a pid is reused and a terminal remembered after
    it exited would silence the next one to land on that number.

    **A drop is logged like an errand, and for the same reason.** A daemon-held
    user root that did not become a row leaves a stated reason (#201): the first
    diagnosis of that bug was wrong precisely because the drop was silent, and
    the engine log showed only the errands, which made the user root's absence
    read as a daemon that never offered it.
    """
    try:
        terminals = await (evidence or ProcessEvidence()).observations()
    except (OSError, TimeoutError) as unreadable:
        return LaneDiscovery(error=f"the process table could not be read: {unreadable}")

    threads, daemon_error = await _threads(client, reported_non_sessions)
    composed = roster.compose(threads, terminals)
    _report(composed.drops, reported_non_sessions)
    # Said only where the daemon was actually read. The sentence itself is about
    # this roster and so is true whatever the daemon did (`NOTHING_TO_VOUCH_FOR`
    # says why), but on a reading with no daemon in it every live terminal on
    # the machine would earn one, and each is remembered per pid — a page of
    # latched lines whose real subject is the one fact `degraded` is already
    # carrying, that the daemon did not answer. The prune below still runs, so
    # the set stays roster-sized across a daemon that is down for a while.
    _report_unheld_terminals(
        composed.unheld if daemon_error is None else (),
        reported_unheld_terminals,
        live={terminal.candidate.pid for terminal in terminals},
    )

    names = projects or ProjectNames()
    rows: list[SessionInspection] = []
    for row in composed.rows:
        inspection, thread = row.inspection, row.thread
        if thread is None:
            rows.append(await _named(inspection, names))
            continue
        if turns is not None and client is not None:
            inspection = replace(inspection, progress=await turns.progress_for(client, thread))
        rows.append(await _named(inspection, names, task=_thread_name(thread)))
    if turns is not None:
        turns.retain({str(thread.get("id")) for thread in threads})

    projected = tuple(rows)
    return LaneDiscovery(
        rows=projected,
        degraded=source_degradation(
            projected,
            _degraded(daemon_error, daemon_note, composed.note),
        ),
    )


def _report(drops: tuple[roster.Drop, ...], reported_non_sessions: set[str] | None) -> None:
    """Say once, per thread, why a thread the daemon holds is not a row.

    Once rather than every pass, and through the same set `_threads` keeps for
    the errand filter, because these are two spellings of one sentence — "the
    daemon holds this and you will not see it" — and a reason repeated every
    five seconds is a reason nobody reads.
    """
    reported = reported_non_sessions if reported_non_sessions is not None else set()
    for drop in drops:
        if drop.thread_id in reported:
            continue
        reported.add(drop.thread_id)
        _log.info("thread %s is not a Session row: %s", drop.thread_id, drop.reason)


def _report_unheld_terminals(
    unheld: tuple[roster.UnheldTerminal, ...],
    reported_unheld_terminals: set[int] | None,
    *,
    live: set[int],
) -> None:
    """Say once, per pid, that a live `codex` answers to nothing on this roster.

    The same sentence `_report` says about a thread, said about a terminal, and
    it exists because that one could not reach this case: `_report` only speaks
    about threads the daemon holds, and the degradation note only where a user
    root was read in the terminal's workspace — so a TUI outside the daemon
    entirely, which is what a `-c` override makes, produced neither a row nor a
    word (#233).

    **A note and never a row.** A Session this lane cannot identify is
    under-reported and said to be, never invented (ADR 0020); this is the
    saying-so, not a way in.

    Pruned to the pids still running, unlike `_report`'s thread ids, which
    `_threads` prunes for it. Both are the same rule — this set may not grow all
    day — but a pid carries a second reason: the machine reuses it, and a
    terminal remembered past its exit would silence its successor.
    """
    if reported_unheld_terminals is None:
        reported = set()
    else:
        reported_unheld_terminals &= live
        reported = reported_unheld_terminals
    for terminal in unheld:
        if terminal.pid in reported:
            continue
        reported.add(terminal.pid)
        _log.info(
            "a live codex terminal (pid %s) in %s is not a Session row: %s",
            terminal.pid,
            terminal.workspace,
            roster.NOTHING_TO_VOUCH_FOR,
        )


def progress_from(thread: Mapping[str, Any], *, capture: ProgressCapture) -> ProgressObservation:
    """One `thread/read` answer, as the seam holds it.

    The one place a thread document becomes a `ProgressObservation`, so the cadence's cached
    read and the verb's live one cannot come back describing it two ways.
    `read_at` is stamped here because it belongs to the *reading*: it is when this
    was true, and a value carried forward from a cache hit keeps its own moment.
    """
    entries, omission = thread_tail.recent(thread, capture=capture)
    return ProgressObservation.from_capture(
        recent=entries,
        omission=omission,
        read_at=datetime.now(UTC),
    )


def _degraded(
    daemon_error: str | None, note: str, under_reporting: str | None = None
) -> str | None:
    """Why these rows are thinner than usual — from all three things that say so.

    A lane can be reading from the process table *and* joined to a daemon whose
    version disagrees with the CLI's *and* holding a live terminal it cannot
    match to any thread (#201), and no fact is allowed to hide another. There is
    deliberately no `error` path here: a missing daemon has never been a reason
    to report no Sessions (#74).

    **A dial that failed says why in its own words, once.** When there is no
    client and the dial left a reason, that reason replaces `NO_CLIENT` rather
    than following it: "holds no connection; codex did not answer within 10
    seconds" is two sentences making one claim, and #96 is the record of what a
    roster that makes more claims than it observed costs to read.
    """
    if daemon_error == NO_CLIENT and note:
        reasons = [f"{note}, {FROM_THE_MACHINE}"]
    else:
        reasons = [reason for reason in (daemon_error, note) if reason]
    if under_reporting:
        reasons.append(under_reporting)
    return "; ".join(reasons) or None


async def _threads(
    client: DaemonClient | None, reported_non_sessions: set[str] | None = None
) -> tuple[list[dict[str, Any]], str | None]:
    """Every thread the daemon holds, or the reason there are none to hold.

    A daemon that is absent, refusing or answering nonsense all mean one thing
    to this lane: the rows will be thinner than usual. None of them is a reason
    to report no Sessions, because the process table has already been read.

    **Errands are read and returned like everything else** (#201). Deciding
    that a thread is the daemon's own is part of the composition rule, and the
    rule lives in one place; a caller that filtered first would be a second
    copy of it, and the version of this function that did exactly that is why
    `roster.compose` could be handed a list whose shape it could not check.

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
    held: set[str] = set()
    for listed in ids:
        if not isinstance(listed, str) or not listed.strip():
            continue
        thread_id = listed.strip()
        held.add(thread_id)
        reading = await read_thread(client, thread_id)
        if reading.thread is not None:
            found.append(reading.thread)
    # Every id the daemon still holds, so a thread it has let go stops being
    # remembered as one this lane has already explained. `compose` decides what
    # each of these documents is; this function only reads them.
    if reported_non_sessions is not None:
        reported_non_sessions &= held
    return found, None


@dataclass(frozen=True, slots=True)
class ThreadRead:
    """One daemon read, preserving why no authoritative document arrived."""

    thread: dict[str, Any] | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if (self.thread is None) == (self.reason is None):
            raise ValueError("a thread read carries exactly one of a document or a reason")


async def read_thread(
    client: DaemonClient, thread_id: str, *, with_turns: bool = False
) -> ThreadRead:
    """One thread as the daemon describes it, or `None` if it cannot describe it.

    `with_turns` is the expensive half and is asked for only by `TurnCache`,
    which is where the measurement justifying that word lives.
    """
    try:
        answer = await client.request(
            READ_METHOD, {"threadId": thread_id, "includeTurns": with_turns}
        )
    except Exception as unreadable:  # noqa: BLE001 - one bad thread is not a bad roster
        reason = f"the daemon could not describe thread {thread_id}: {unreadable}"
        _log.info("%s", reason)
        return ThreadRead(reason=reason)
    thread = answer.get("thread") if isinstance(answer, dict) else None
    if not isinstance(thread, dict) or thread.get("id") != thread_id:
        reason = f"{READ_METHOD} answered about a different thread than {thread_id}"
        _log.info("%s", reason)
        return ThreadRead(reason=reason)
    return ThreadRead(thread=thread)


def _thread_name(thread: Mapping[str, Any]) -> str | None:
    """What the daemon calls this thread, when it calls it anything.

    **A name that is only the user's own prompt read back is not a name** (#113).
    codex 0.150.0 names an unnamed thread the moment its first `UserMessage`
    completes, and that first name is the message itself: whitespace-collapsed
    and cut to 36 characters, mid-word (`rust-v0.150.0:codex-rs/tui/src/app/
    thread_routing.rs:1800-1854`, `tui/src/app/thread_title.rs:22`). A generated
    title then replaces it — measured on the run of record's own thread, which
    the daemon now calls `回复 READY` and the product froze as `Reply with the
    single word READY. Do` (#113). *How long the first name is live is not
    measured; it is observed on #80's run of record*, whose `stop notice` step
    already reads the Session Name aloud. It cannot be read back from the daemon:
    the hidden thread that generates the title is ephemeral, and an ephemeral
    thread's `createdAt`/`updatedAt` are stamped at read time
    (`rust-v0.150.0:codex-rs/app-server/src/request_processors/thread_processor.
    rs:5999-6016`), so the daemon keeps no record of when the swap happened.

    That unmeasured window is exactly what this rule makes not matter. #78 froze
    the first name a target accepted, so the product kept the fragment for the
    Session's whole life and said it back in every Stop Notice; refusing it here
    lets the freeze land on something sayable — the id-prefix fallback below,
    exactly as when the daemon offers nothing — until the real title arrives.

    **The test is the daemon's own account of the prompt, not the shape of the
    string.** `Thread.preview` is "usually the first user message in the thread"
    (`rust-v0.150.0:codex-rs/app-server-protocol/src/protocol/v2/thread_data.rs:
    211`, and identically at `@0.149.1:thread_data.rs:209`), written from that
    same first message and never overwritten by a later one
    (`codex-rs/thread-store/src/thread_metadata_sync.rs:316-324`). So this asks
    codex whether the name it just gave is the prompt, and codex answers from the
    field it already put on this document — no second request, no `includeTurns`,
    no rollout. That is not a convenience: the provisional name appears *during*
    the first turn, which is precisely when `TurnCache` declines to read turns at
    all, so a rule that needed them could not see the name it exists to catch.

    **Upstream draws the same line in its own house**: resuming a thread whose
    stored title equals its preview leaves `name` unset rather than showing it
    (`rust-v0.150.0:codex-rs/app-server/src/request_processors/thread_processor.
    rs:5783-5788`).

    **A daemon that states no preview keeps its name.** Absent is not a claim —
    `SESSION_THREAD_SOURCES` above makes the same reading of the same silence,
    and an older daemon that records no preview must not lose every name it has.
    The rule only ever fires on a positive match, so where the two sides strip a
    prompt differently — the title drops IDE context, the preview strips a
    message prefix — it declines to fire and the name stands.
    """
    name = thread.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    if _is_the_prompt_back(name, thread.get(roster.PREVIEW)):
        # Debug rather than info, and for `core.sessions._named_as`'s reason: this
        # is a decision taken again on every five-second tick for as long as the
        # daemon holds that name, and at info a thread whose generated title never
        # arrived would be one steady line per tick saying nothing new.
        _log.debug("thread %s is named its own first prompt; leaving it unnamed", thread.get("id"))
        return None
    return name.strip()


def _is_the_prompt_back(name: str, preview: Any) -> bool:
    """Whether this name is *the* provisional title codex composes from the prompt.

    **The title codex composes, and not merely a prefix of the prompt.** The
    provisional name is one expression — the first message collapsed by
    `split_whitespace().join(" ")` and cut to `PROVISIONAL_TITLE_CHARACTERS` —
    so this recomposes that expression and compares. A looser "the prompt starts
    with this name" would reach names codex never composed: a generated title
    opens with an imperative verb (`tui/src/app/thread_title.rs:206-214`) and a
    prompt very often does too, so `Fix the login bug` would be thrown away as a
    prefix of `Fix the login bug in the auth module` — a good name lost to a rule
    meant to catch a bad one.

    Collapsed on both sides because that is the only form the two are comparable
    in: the preview carries the message with its newlines intact. The cut is
    collapsed again after it is made, because cutting at a character count can
    leave the trailing space of the word it stopped after. Case is kept — the
    name is a verbatim slice, so a case-insensitive test would only reach names
    that are not one.
    """
    prompt = _collapsed(preview)
    if not prompt:
        return False
    return _collapsed(name) == _collapsed(prompt[:PROVISIONAL_TITLE_CHARACTERS])


def _collapsed(value: Any) -> str:
    """One line of single-spaced words, or nothing at all."""
    return " ".join(value.split()) if isinstance(value, str) else ""


async def _named(
    row: SessionInspection, projects: ProjectNames, *, task: str | None = None
) -> SessionInspection:
    """The same row, carrying its Session Name.

    One rule for both sources, because there is one kind of Codex Session: the
    task is the daemon's `Thread.name` when the daemon has one, and the first
    `SHORT_THREAD_ID_CHARACTERS` of the thread id when it does not. A row read
    off the process table simply never has the first, so it takes the second —
    and a Session that has not taken its first turn has neither, because it has
    no thread id yet (#73), so it stays unnamed until it does. That is not a
    reach problem: an unnamed row is listed, and what it can be addressed by is
    its target, which it has had all along.

    **A Child Process is the one row that takes no name at all** (#78, #79). A
    Session Name is what the user says to reach a Session, and there is nothing
    here to reach; the registry drops one anyway (`core/sessions.py:_named_as`),
    so composing it would be composing something nobody ever sees. Not
    composing it is the same rule said where the row is made — and it keeps the
    daemon's own `Thread.name` for a subagent out of a roster the user reads.
    """
    if not row.child.is_main:
        return row
    chosen = task or _short_thread_id(row.target.session_id)
    if chosen is None:
        return row
    project = await projects.of(row.workspace)
    if project is None:
        return row
    return replace(row, name=_naming.compose(project, chosen))


def _short_thread_id(thread_id: str | None) -> str | None:
    """The head of a thread id, as a name for a thread nobody named."""
    if thread_id is None:
        return None
    short = thread_id.strip()[:SHORT_THREAD_ID_CHARACTERS]
    return short or None


def _from_processes(
    candidates: tuple[Candidate, ...], *, home: Path | None
) -> tuple[roster.ProcessObservation, ...]:
    """Every live terminal, with explicit rollout-root evidence where there is any.

    **Every candidate is kept** (#201). A candidate with no thread id in its
    argv used to be dropped right here, which is why no hand-started `codex`
    could ever reach the roster: only `codex resume <UUID>` carries one, and the
    acceptance harness — like every real user — starts `codex "<prompt>"`. Such
    a candidate is liveness and place, which is exactly the half `roster.compose`
    needs of this source.

    The rollout lookup answers one question about the other kind of candidate:
    whether an exact argv id names a thread the user themselves rooted. It
    requires the same id in argv, filename and `session_meta`, the same real
    workspace, and explicit `thread_source=user`. No count, timestamp, or
    workspace-only observation can manufacture the shared key (#144).
    """
    observed: list[roster.ProcessObservation] = []
    for candidate in candidates:
        thread_id = candidate.session_id
        if thread_id is None:
            observed.append(roster.ProcessObservation(candidate=candidate))
            continue
        located = rollouts.locate(thread_id, home=home)
        meta = rollouts.session_meta(located) if isinstance(located, Path) else None
        rollout_root = (
            meta is not None
            and rollouts.session_id_in(meta) == thread_id
            and meta.get("thread_source") == rollouts.USER_THREAD_SOURCE
            and (workspace := rollouts.workspace_in(meta)) is not None
            and os.path.realpath(workspace) == os.path.realpath(candidate.workspace)
        )
        observed.append(roster.ProcessObservation(candidate=candidate, rollout_root=rollout_root))
    return tuple(observed)
