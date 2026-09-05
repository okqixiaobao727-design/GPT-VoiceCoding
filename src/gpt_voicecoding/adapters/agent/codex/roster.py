"""What a Codex Session is, decided from two readings and nothing else.

**The rule, in one sentence.** A Codex Session is a daemon-held user root thread
that a live terminal in its own workspace vouches for.

- The **daemon** supplies identity and content: the thread id — which *is* the
  row's identity — its name, its state, its tree links and its progress.
- The **process table** supplies liveness and place only: a live interactive
  `codex` with a controlling terminal, and its working directory compared by
  realpath.
- A user root no live terminal vouches for is not a row, however recently the
  daemon loaded it. A terminal that vouches for no thread is not a row either.

**Why the two halves cannot be collapsed into one source, measured** (#201, on
codex-cli 0.152.1 with the managed app-server at 0.149.1, 2026-09-02):

1. *The daemon cannot report liveness.* A sweep of the app-server v2 protocol
   finds no way to name the OS process attached to a thread: `osPid` belongs to
   a thread's *background terminal*, `clientId` to remote-control clients and to
   individual items, and `canAcceptDirectInput` is "whether the app server
   accepts direct turn input for this loaded thread" — a property of the server,
   not evidence of an attached terminal.
2. *"Loaded" is not "live".* After its TUI exited, `01a05fc1-b5ca-…` stayed in
   `thread/loaded/list` with `status: idle` for **over thirty minutes** before
   flipping to `notLoaded`. Any rule that reads loadedness as liveness
   reinstates #123's ghost rows.

**What #144 keeps, in full.** A row's identity is always the daemon's thread id;
two observations of one Session never become two rows; a pid is reported only
when exactly one terminal can be it, and is otherwise absent; **workspace never
determines identity**. The single rule #201 changes is that workspace now
determines *liveness*.

**The ghost stays dead by eliminating impossibilities, not by guessing.** A
terminal vouches only for a thread it *could* be attached to: a thread created
before the terminal's own start time cannot be that terminal's, unless the
terminal's argv names it — `resume <UUID>`, which is already an exact match.
This removes candidates; it never manufactures a shared key, so #144's ban on
timestamps-as-identity stands.

**The accepted cost, stated deliberately.** `codex --last` and the interactive
picker resume an older thread with no id in argv, so under this rule they get no
row. This roster may under-report; it must never invent. It is not silent about
it, and since #233 it is not silent about any of it: a live terminal that
vouches for nothing while a user root was read in its workspace is reported
through `note`, and one where none was is carried back in `unheld`. The two
together account for every live terminal that composed no row — before #233 the
second kind fell between them and was never mentioned at all. **Read, not
held**, in both sentences: what the split turns on is the user roots this pass
put in `classified`, which is all this function can see (`NOTHING_TO_VOUCH_FOR`
says why the difference matters).

**Every drop leaves a reason.** A daemon-held user root that does not become a
row is returned in `drops` with why, on the principle `errand_of` already
follows — the reason is carried back rather than a bare `False`, because a row
that stops appearing is a row somebody comes looking for. The absence of that
reason is why #201's first diagnosis was wrong: the user root passed the errand
filter and was dropped later, at row composition, silently.

**This module performs no I/O, holds no daemon client, and reads no clock.**
Everything it decides it decides from its two arguments, which is what makes the
rule testable with dictionaries in and rows out (`tests/test_codex_roster.py`).
The one filesystem call it makes is `os.path.realpath`, which resolves a path
rather than reading a file, and which the rule names explicitly.

**Legacy citation** (ADR 0010). `legacy@1d32845:bridge/daemon.py:1192-1257` —
**dropped, because** generation 1 only ever knew Sessions it had launched and
wrapped itself, from its own launch records, so it never had to recognise one a
person started. v1.0's promise (#68) is the opposite, and there is no legacy
behaviour to port for it. The one habit that carries over is `pgrep` on an exact
executable name (`legacy@1d32845:bridge/host.py:795`), which `processes.py`
already does.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from gpt_voicecoding.adapters.agent.codex import rollouts, thread_tail
from gpt_voicecoding.adapters.agent.codex.processes import (
    START_TIME_RESOLUTION_SECONDS,
    Candidate,
)
from gpt_voicecoding.seams.agent import (
    MAIN_SESSION,
    ChildClassification,
    ChildKind,
    ProgressObservation,
    SessionInspection,
    SessionLifecycle,
    SessionState,
    WaitingFor,
    WaitingKind,
)
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget

#: What a thread's status is called while a turn is running. Named because two
#: rules turn on it: the row reads as `RUNNING`, and the cadence does not read
#: its turns.
ACTIVE_STATUS: Final = "active"

#: The fourth `status.type` the protocol defines, and the one that is not a
#: state a row can be in: the daemon has let the thread go. It is deliberately
#: absent from `STATUS_TYPES` below — a thread reading this never reaches a row
#: to have a state, because it is dropped first. Before #201 it fell through to
#: the map's `RUNNING` default and read as a Session mid-turn.
NOT_LOADED: Final = "notLoaded"

#: What a thread's `status.type` can be, as the Codex spoke has observed it.
#: `systemError` is a thread whose turn ended badly — still reachable, still
#: able to take the next Relay, which is why it reads as idle rather than as
#: something that must be waited out.
STATUS_TYPES: Final = {
    "idle": SessionState.IDLE,
    ACTIVE_STATUS: SessionState.RUNNING,
    "systemError": SessionState.IDLE,
}

#: When the daemon says this thread was first opened, in epoch seconds — the
#: same units as `updatedAt` and `recencyAt`, measured on 0.149.1 (#76). It is
#: the only fact that can rule a terminal out as a thread's owner, and a thread
#: that states none cannot be vouched for by place.
CREATED_AT: Final = "createdAt"

#: The fields the daemon uses to say a thread is its own errand rather than a
#: person's, and the values of the second that leave a thread a Session (#112).
#:
#: **Both are on the cheap read already.** `Thread` is one struct — `ephemeral`
#: is a plain `bool` and `thread_source` a plain optional string; only `turns`
#: is gated on `includeTurns` (`rust-v0.149.1:codex-rs/app-server-protocol/src/
#: protocol/v2/thread_data.rs:196-266`). So this costs no round trip: the read
#: loop already reads every id.
#:
#: **A keep-list, because the far side's is not a closed set.** `ThreadSource`
#: parses any unrecognised word into `Feature(word)` — `FromStr`'s last arm,
#: `rust-v0.150.0:codex-rs/protocol/src/protocol.rs:2604-2657` — so naming the
#: one value seen so far would leave the next feature string to be found the way
#: this one was: as a phantom Session in somebody's roster (#79's measurement).
#: 0.150.0's title generation starts its thread with exactly `ephemeral: true`
#: and `thread_source: Feature("system")` (`rust-v0.150.0:codex-rs/tui/src/
#: temporary_structured_request.rs:103-104`), and it is the reason this exists.
#:
#: **`subagent` and `guardian_review` are kept on purpose, and this list is
#: #79's vocabulary too** (Advisor, 2026-08-27). They are one delegate class
#: split by a boolean (`rust-v0.150.0:codex-rs/core/src/codex_delegate.rs:111`),
#: and they are #79's Child Process rows — which it cannot classify if this
#: module deletes them first. The two rules meet here and nowhere else.
#:
#: **A thread that names no source is not called an errand**, because absent is
#: not a claim. It remains unclassified when rows are projected: an explicit
#: user rollout may still identify the live TUI, but daemon silence alone cannot
#: confirm a native root (#144). `null` and omission are the same silence.
#:
#: **Legacy has no filter of this kind, and that is the citation.** Its Codex
#: roster was the rollout index (`legacy@1d32845:bridge/codex.py:1020-1026`,
#: `:1063-1129`), and an ephemeral thread writes no rollout — the phantom is a
#: creature of *this* generation's daemon-first roster and could not have
#: appeared there. What is **adapted** from legacy is the technique and not the
#: rule: deciding what a thread is from its own `thread_source`, which
#: `realtime_thread_ids` (`:1020-1026`) does as a keep-list of one value. The
#: nearest legacy *rule*, `thread_source == "subagent"` blocking registration
#: (`legacy@1d32845:bridge/__main__.py:893-898`), belongs to #79 rather than
#: here — and it **excludes** `subagent`, which this keeps, which is exactly why
#: the two are not the same behaviour.
EPHEMERAL: Final = "ephemeral"
THREAD_SOURCE: Final = "threadSource"
SESSION_THREAD_SOURCES: Final = frozenset({"user", "subagent", "guardian_review"})

#: **#79's half of the keep-list above, and derived from it rather than written
#: out beside it.** The rule is one sentence — a thread that reaches the roster
#: and is not the person's own is a Child Process — so the two constants cannot
#: drift into disagreeing about a value: whatever #112 keeps, this classifies.
#: Spelled the other way round, a source added to the keep-list one day would
#: have had to be remembered here on the same day, and the day it was not, a
#: subagent would have become addressable.
#:
#: The two values it currently yields are one delegate class split by a boolean
#: (`rust-v0.150.0:codex-rs/core/src/codex_delegate.rs:111`), which is why one
#: classification covers both. Neither is guessed from a thread's shape:
#: `thread_source` is the daemon's own word for what started a thread.
#:
#: **Adapted from legacy** (ADR 0010). `legacy@1d32845:bridge/__main__.py:
#: 876-899` read the same fact — `session_meta.thread_source` — and refused
#: *registration* on it, so a child had no row at all. v1.0 keeps the safety
#: outcome and drops the invisibility (#67 port table, P11, *adapt*).
CHILD_THREAD_SOURCES: Final = SESSION_THREAD_SOURCES - {rollouts.USER_THREAD_SOURCE}
PARENT_THREAD_ID: Final = "parentThreadId"
SESSION_TREE_ID: Final = "sessionId"

#: What the daemon calls the thread's first user message. On the cheap read like
#: the ones above, and read for one question only: whether the name codex gave
#: this thread is that message read back (#113, `discovery._thread_name`).
PREVIEW: Final = "preview"

#: Why a daemon-held user root left no row, in the words `drops` carries back.
#: Named rather than written inline because a test asks for them and, more to
#: the point, because a person reading an engine log is asking exactly this
#: question and the answer has to be the same sentence every time.
NO_TERMINAL: Final = (
    "no live codex terminal in its workspace could be attached to it, so it is loaded but not live"
)
NO_CREATION_TIME: Final = (
    f"the daemon states no {CREATED_AT}, so no terminal can be shown not to predate it"
)
NOT_CLASSIFIED: Final = (
    f"the daemon states no {THREAD_SOURCE} this build recognises, and nothing else proves a root"
)
NOT_LOADED_REASON: Final = f"its status reads {NOT_LOADED}, so the daemon no longer holds it"
AMBIGUOUS_TERMINAL: Final = (
    "the only live codex terminals that could hold it could equally hold another root the "
    "daemon holds there, so nothing shows this is the one they are in"
)
NO_LIVE_TREE: Final = "no live root holds its Session tree"

#: Why a live terminal composed no row and the degradation note has nothing to
#: say about it either (#233). The note beside it speaks only where a user root
#: was read in the terminal's workspace — a Session that *may* be
#: missing — and a terminal running its own core, the shape a `-c` override
#: gives a TUI, is not that case. Before #233 such a terminal fell between the
#: two rules and was never mentioned; run `20260904T202319Z` had one live for
#: two minutes and the codex engine log has no line about it.
#:
#: **It is a sentence about this roster, not about the daemon's holdings**, and
#: that is deliberate rather than coy. "The daemon holds no user root there" is
#: a claim this function is in no position to make: a thread whose `thread/read`
#: failed is one the daemon holds and this pass never saw, a `notLoaded` root
#: was let go between the two readings, and a thread naming no source this build
#: recognises is not thereby shown to be an errand — each of them leaves a
#: workspace looking emptier here than it is. What `compose` *did* observe in
#: full is its own rows, so that is what this says. Every thread that is not one
#: leaves its own reason in `drops`, which is where a reader goes next (#96:
#: never claim anything about the daemon this build did not observe).
NOTHING_TO_VOUCH_FOR: Final = (
    "no thread the daemon holds in its workspace is a row on this roster, so there is "
    "nothing there for it to vouch for"
)


@dataclass(frozen=True, slots=True)
class ProcessObservation:
    """One live interactive `codex`, and whether its rollout proves a user root.

    **An observation, not an identity**, and the name is the point: most of
    these carry no thread id at all. A hand-started `codex` is pid, workspace
    and start time — liveness and place, which is the whole of what this source
    is allowed to supply. Only `codex resume <UUID>` also carries an id, and
    only then is there anything here to call an identity.

    `rollout_root` answers one question about that second kind: whether the id
    in the argv names a thread the user themselves rooted. It is read from disk
    by the caller — the one piece of evidence here this module could not derive.
    """

    candidate: Candidate
    rollout_root: bool = False


@dataclass(frozen=True, slots=True)
class Drop:
    """One daemon-held thread that did not become a row, and why it did not."""

    thread_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class UnheldTerminal:
    """One live terminal that composed no row and that `note` cannot speak for.

    `Drop`'s counterpart from the other source: that one carries back a thread
    the daemon offered and this rule refused, and this one carries back a
    terminal the machine offered that nothing on this roster answers to. Both
    exist for the same reason — a Session somebody is sitting in and cannot see
    is a Session they come looking for, and the lane has to have said something.

    It carries no reason of its own, unlike `Drop`, because there is only ever
    one: `NOTHING_TO_VOUCH_FOR`, which the caller names where it says it.

    It is a reading, never a row: a Session this lane cannot identify is
    under-reported and said to be, never invented (ADR 0020).

    **Legacy citation** (ADR 0010). `legacy@1d32845:bridge/daemon.py:1192-1257`
    — **dropped, because** generation 1's roster was Sessions registering
    themselves through its hook, so a terminal it had not launched was not
    something it could see, let alone something it could report a gap about.
    Reading the process table for terminals nobody registered is this
    generation's (`processes.py`), and so is the silence this ends.
    """

    pid: int
    workspace: Path


@dataclass(frozen=True, slots=True)
class Row:
    """One composed Session row, beside the daemon document it was read from.

    The document rides along because two things the caller adds afterwards need
    it and cannot be given it any other way: the thread's progress, which costs
    a round trip, and its name, which costs a `git` lookup. Neither is a
    decision this module makes; both are I/O, and I/O is the caller's half.
    A row read from the process table alone carries no document.
    """

    inspection: SessionInspection
    thread: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Roster:
    """Everything the composition rule decided, in one answer."""

    rows: tuple[Row, ...] = ()
    #: Every daemon-held thread that did not become a row, with its reason.
    drops: tuple[Drop, ...] = ()
    #: Why this roster may be under-reporting, or `None` if it is not.
    note: str | None = None
    #: Every live terminal that composed no row and that `note` cannot speak
    #: for, because this pass read no user root where it is running (#233).
    unheld: tuple[UnheldTerminal, ...] = ()


def compose(
    threads: Sequence[Mapping[str, Any]],
    terminals: Sequence[ProcessObservation],
) -> Roster:
    """The Codex lane's Session rows, from the daemon's threads and live terminals.

    `threads` are the documents the daemon answered `thread/read` with, exactly
    as they came back — the errand filter is applied here, not by the caller,
    so that this function really is the whole rule and cannot be handed a
    pre-filtered list that quietly changes what it decides. `terminals` are the
    live interactive `codex` runs the process table holds. Nothing else is
    consulted, and nothing here is read from the machine.
    """
    exact: dict[str, list[ProcessObservation]] = {}
    unnamed: list[ProcessObservation] = []
    for terminal in terminals:
        thread_id = terminal.candidate.session_id
        if thread_id is None:
            unnamed.append(terminal)
        else:
            exact.setdefault(thread_id, []).append(terminal)

    drops: list[Drop] = []
    classified: list[tuple[Mapping[str, Any], ChildClassification]] = []
    for thread in threads:
        thread_id = str(thread["id"])
        errand = errand_of(thread)
        if errand is not None:
            drops.append(Drop(thread_id, errand))
            continue
        if status_of(thread) == NOT_LOADED:
            drops.append(Drop(thread_id, NOT_LOADED_REASON))
            continue
        child = _child_of(thread)
        if child is None and any(match.rollout_root for match in exact.get(thread_id, ())):
            child = MAIN_SESSION
        if child is None:
            drops.append(Drop(thread_id, NOT_CLASSIFIED))
            continue
        classified.append((thread, child))

    could_hold = {
        str(thread["id"]): _could_hold(thread, exact.get(str(thread["id"]), ()), unnamed)
        for thread, child in classified
        if child.is_main
    }
    # A terminal whose argv names no thread and that could equally be sitting in
    # two of these roots proves neither of them. At most one of them is the
    # Session it holds, and choosing would be inventing a row — the refusal
    # #144 already makes when two terminals name one thread, said the other way
    # round. An exact argv match names one thread and is never ambiguous.
    could_be_either = {pid for pid, held in _by_terminal(could_hold).items() if len(held) > 1}
    vouched: dict[str, list[ProcessObservation]] = {}
    for thread, child in classified:
        if not child.is_main:
            continue
        thread_id = str(thread["id"])
        for_this = [
            terminal
            for terminal in could_hold[thread_id]
            if terminal.candidate.pid not in could_be_either
        ]
        if for_this:
            vouched[thread_id] = for_this
        else:
            drops.append(
                Drop(thread_id, _why_no_terminal(thread, ambiguous=bool(could_hold[thread_id])))
            )

    live_tree_ids = {
        tree_id
        for thread, child in classified
        if child.is_main and str(thread["id"]) in vouched
        if (tree_id := _session_tree_id(thread)) is not None
    }

    rows: list[Row] = []
    for thread, child in classified:
        thread_id = str(thread["id"])
        if child.is_main:
            for_this = vouched.get(thread_id)
            if for_this is None:
                continue
            # Every terminal now vouches for at most one root, so this says the
            # whole of #144's pid rule: exactly one terminal can be it, or none
            # is named.
            pid = for_this[0].candidate.pid if len(for_this) == 1 else None
        else:
            if _session_tree_id(thread) not in live_tree_ids:
                drops.append(Drop(thread_id, NO_LIVE_TREE))
                continue
            pid = None
        rows.append(Row(inspection=from_thread(thread, pid, child=child), thread=dict(thread)))

    # A thread the daemon holds has already had its answer here, whatever that
    # answer was — including a drop. Every id it listed is excluded, not only
    # the ones that reached `classified`: a `notLoaded` thread with an exact
    # rollout beside it would otherwise be dropped by the rule above and let
    # back in by the rule below, which is the one row this ADR says never exists.
    daemon_held = {str(thread["id"]) for thread in threads}
    accounted = {terminal.candidate.pid for for_this in vouched.values() for terminal in for_this}
    for thread_id, matches in exact.items():
        if thread_id in daemon_held:
            continue
        if not any(match.rollout_root for match in matches):
            continue
        pid = matches[0].candidate.pid if len(matches) == 1 else None
        rows.append(Row(inspection=_from_process(matches[0].candidate, thread_id, pid)))
        accounted.update(match.candidate.pid for match in matches)

    # The terminals nothing here spoke for, split by the one fact that decides
    # which sentence they get: whether this pass read a user root where they are
    # running. One split rather than a predicate and its negation, so that
    # "exhaustive, and never both" is the shape of the code rather than a claim
    # a comment makes about two list comprehensions (#233).
    beside_a_root, alone = _split_on_roots(
        [terminal for terminal in terminals if terminal.candidate.pid not in accounted],
        _root_workspaces(classified),
    )
    return Roster(
        rows=tuple(_linked_to_their_parents(rows)),
        drops=tuple(drops),
        note=_under_reporting(beside_a_root),
        unheld=tuple(
            UnheldTerminal(pid=terminal.candidate.pid, workspace=terminal.candidate.workspace)
            for terminal in alone
        ),
    )


def _by_terminal(
    could_hold: Mapping[str, Sequence[ProcessObservation]],
) -> dict[int, list[str]]:
    """Which roots each id-less terminal could be attached to, by pid."""
    held: dict[int, list[str]] = {}
    for thread_id, terminals in could_hold.items():
        for terminal in terminals:
            if terminal.candidate.session_id is None:
                held.setdefault(terminal.candidate.pid, []).append(thread_id)
    return held


def _could_hold(
    thread: Mapping[str, Any],
    exact: Sequence[ProcessObservation],
    unnamed: Sequence[ProcessObservation],
) -> list[ProcessObservation]:
    """Every live terminal that could be the one attached to this root.

    An exact argv match is already the shared id #144 requires and needs no
    further evidence. A terminal with no id in its argv — the ordinary case,
    and the shape every hand-started `codex` has — vouches only when it is in
    the thread's own workspace and the thread was not already there before the
    terminal started.
    """
    found = list(exact)
    created = _created_at(thread)
    if created is None:
        return found
    workspace = _workspace_of(thread)
    for terminal in unnamed:
        started = terminal.candidate.started_at
        if started is None or workspace is None:
            continue
        if os.path.realpath(terminal.candidate.workspace) != workspace:
            continue
        if created < started - START_TIME_RESOLUTION_SECONDS:
            continue
        found.append(terminal)
    return found


def _why_no_terminal(thread: Mapping[str, Any], *, ambiguous: bool) -> str:
    """Which of the three ways this root failed to be vouched for it took."""
    if ambiguous:
        return AMBIGUOUS_TERMINAL
    return NO_TERMINAL if _created_at(thread) is not None else NO_CREATION_TIME


def _root_workspaces(
    classified: Sequence[tuple[Mapping[str, Any], ChildClassification]],
) -> set[str]:
    """Every workspace a user root was read in on this pass, by realpath.

    Read rather than held: these are the threads that reached `classified`, and
    a workspace missing from this set is one no user root came back from — not
    one the daemon is known to hold none in (`NOTHING_TO_VOUCH_FOR`).
    """
    return {
        workspace
        for thread, child in classified
        if child.is_main and (workspace := _workspace_of(thread)) is not None
    }


def _split_on_roots(
    unaccounted: Sequence[ProcessObservation],
    roots: set[str],
) -> tuple[list[ProcessObservation], list[ProcessObservation]]:
    """The terminals behind no row, split on whether a user root was read where they run.

    `unaccounted` is every terminal that ended up behind no row, by either
    route, so a terminal that composed a process-only row is in neither half.
    Each workspace is resolved once here, which is the other reason this is one
    pass: `realpath` is the only filesystem call this module makes.
    """
    beside_a_root: list[ProcessObservation] = []
    alone: list[ProcessObservation] = []
    for terminal in unaccounted:
        where = os.path.realpath(terminal.candidate.workspace)
        (beside_a_root if where in roots else alone).append(terminal)
    return beside_a_root, alone


def _under_reporting(beside_a_root: Sequence[ProcessObservation]) -> str | None:
    """Why this roster may be thinner than the machine, when it may be.

    `codex --last` and the interactive picker resume a thread whose id appears
    nowhere this lane can read, so their terminal vouches for nothing while the
    daemon really does hold user roots in that workspace. That is the accepted
    cost of never inventing a row — and the ticket's condition for accepting it
    is that the lane says so rather than passing silently.

    A terminal running where no user root does is not this sentence's business:
    nothing is known to be missing there, and `NOTHING_TO_VOUCH_FOR` is what
    there is to say about it instead.
    """
    reasons = [
        f"a live codex terminal (pid {terminal.candidate.pid}) in "
        f"{terminal.candidate.workspace} vouches for no thread the daemon holds there, "
        "so a Session may be missing from this roster"
        for terminal in beside_a_root
    ]
    return "; ".join(reasons) or None


def _linked_to_their_parents(rows: list[Row]) -> list[Row]:
    """Each child's parent named by the address that parent's own row carries.

    `parentThreadId` names a thread, but a Session's address may also carry the
    pid its terminal supplied, so a parent named from the field alone can be an
    address no row in the roster holds. #79's acceptance `child` step reads this
    link to say a child is listed under its parent, and it failed on exactly
    that difference: the child pointed at `codex:01a040cc-…` while the Session
    that spawned it was `codex:01a040cc-…:36628`.

    **After the loop, not inside it**, because the pid is joined as each thread
    is read and the daemon lists a child before its parent as readily as after.
    Inside the loop the answer would depend on that order; here it cannot.

    A parent the roster does not hold keeps the thread-only address it was read
    with. That is what was observed, and it is the honest answer: inventing a pid
    for a row nobody is holding would be a worse address than one naming less.
    """
    held = {row.inspection.target.session_id: row.inspection.target for row in rows}
    linked = []
    for row in rows:
        parent = row.inspection.child.parent
        address = held.get(parent.session_id) if parent is not None else None
        if address is not None and address != parent:
            child = replace(row.inspection.child, parent=address)
            row = replace(row, inspection=replace(row.inspection, child=child))
        linked.append(row)
    return linked


def errand_of(thread: Mapping[str, Any]) -> str | None:
    """Why this thread is the daemon's own errand, or `None` if it is a Session.

    The reason is carried back rather than a bare `False` because a row that
    stops appearing is a row somebody comes looking for, and "dropped" is not
    an answer to that question. Read as each thread is read, so a daemon errand
    cannot consume or be promoted by a process observation carrying the same
    workspace.
    """
    if thread.get(EPHEMERAL) is True:
        return f"{EPHEMERAL}, so the daemon will not even write it to disk"
    # Only a word identifies an errand. `null`, a missing key, and a shape this
    # build has never seen are all the daemon declining to classify it; row
    # composition separately refuses to promote that silence into a native root.
    source = thread.get(THREAD_SOURCE)
    if isinstance(source, str) and source not in SESSION_THREAD_SOURCES:
        return f"{THREAD_SOURCE} is {source!r}, which is codex's own errand"
    return None


def _child_of(thread: Mapping[str, Any]) -> ChildClassification | None:
    """Whether this is a proven root, a proven child, or unclassified (#79, #144).

    Read off the same two fields `errand_of` reads, on the same cheap
    `thread/read`, and against `CHILD_THREAD_SOURCES` — the half of #112's
    keep-list that exists for this rule. Everything else the daemon runs for
    itself never reaches this function, having been dropped as an errand.

    **A word decides; an absence does not.** `null`, a missing key and a shape
    this build cannot read are all the daemon declining to classify a thread.
    They return `None`: only an exact process identity with an explicit user
    rollout may supply the missing root classification. PID-only evidence never
    enters the roster.
    """
    source = thread.get(THREAD_SOURCE)
    if source == rollouts.USER_THREAD_SOURCE:
        return MAIN_SESSION
    if not isinstance(source, str) or source not in CHILD_THREAD_SOURCES:
        return None
    # The parent is carried where the daemon names it and is `None` where it
    # does not — which is ordinary, not malformed: `parentThreadId` is `null`
    # on every thread the daemon recorded none for. The locked type settles
    # what follows (`seams/agent.py`): a child whose parent could not be
    # established is still a child, because demoting it over a missing link
    # would open the very Relay this classification closes.
    parent = thread.get(PARENT_THREAD_ID)
    named = isinstance(parent, str) and parent.strip()
    return ChildClassification(
        kind=ChildKind.CHILD,
        parent=(
            SessionTarget(agent=AgentKind.CODEX, session_id=str(parent).strip()) if named else None
        ),
    )


def _session_tree_id(thread: Mapping[str, Any]) -> str | None:
    """The native Session tree this daemon thread says it belongs to."""
    tree_id = thread.get(SESSION_TREE_ID)
    return tree_id.strip() if isinstance(tree_id, str) and tree_id.strip() else None


def status_of(thread: Mapping[str, Any]) -> str | None:
    """What this thread says it is doing, in its own word."""
    status = thread.get("status")
    kind = status.get("type") if isinstance(status, Mapping) else None
    return kind if isinstance(kind, str) else None


def _created_at(thread: Mapping[str, Any]) -> float | None:
    """When the daemon says this thread was opened, in epoch seconds."""
    created = thread.get(CREATED_AT)
    if isinstance(created, bool) or not isinstance(created, int | float):
        return None
    return float(created)


def _workspace_of(thread: Mapping[str, Any]) -> str | None:
    """The thread's own workspace, resolved the way a terminal's cwd is."""
    cwd = thread.get("cwd")
    return os.path.realpath(cwd) if isinstance(cwd, str) and cwd.strip() else None


def from_thread(
    thread: Mapping[str, Any],
    pid: int | None,
    progress: ProgressObservation | None = None,
    child: ChildClassification = MAIN_SESSION,
) -> SessionInspection:
    """One daemon-held thread as the seam holds it.

    `child` is passed in rather than read here because the caller has already
    asked — the answer decides whether this row may take a pid at all, and
    asking twice would be two readings of one field.
    """
    kind = status_of(thread)
    state = STATUS_TYPES.get(str(kind), SessionState.RUNNING)
    cwd = thread.get("cwd")
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
        progress=progress or ProgressObservation(),
        # Free on the cheap read, and honest for a thread mid-turn too: it is
        # the thread's own account of when it last moved, which is exactly the
        # case `last_activity` exists to answer when nothing was said (#76).
        last_activity=thread_tail.last_activity(thread),
        child=child,
    )


def _from_process(candidate: Candidate, session_id: str, pid: int | None) -> SessionInspection:
    """One running TUI whose native root identity was independently proven.

    **The state is `RUNNING` because nothing here can see one.** A process is
    not evidence of a Reply Window, and `RUNNING` is the reading that holds a
    Relay rather than delivering it into a Session that may be mid-turn. That
    matters more here than anywhere: this Session's Relay would fail at the wire
    anyway (#82), and a held Relay is one the user gets back.
    """
    return SessionInspection(
        target=SessionTarget(agent=AgentKind.CODEX, session_id=session_id, pid=pid),
        workspace=candidate.workspace,
        lifecycle=SessionLifecycle.LIVE,
        state=SessionState.RUNNING,
        waiting_for=WaitingFor(),
    )
