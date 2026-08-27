"""The Session registry — Bridge Core state, and deliberately not a module.

Conversing with a Session is the Agent seam; what Sessions *exist* is held here,
in the hub, and every surface queries it rather than keeping a copy (ADR 0001).

**Rows arrive by observation, not by registration.** A Session is one the *user*
started (#68), so nothing tells this registry a Session exists — a lane goes and
looks, on a cadence, and hands back everything it can see as a `LaneDiscovery`.
`observe` makes the roster agree with that answer. The reference implementation
had it the other way round: a Session existed because a hook had announced it,
so a Session started before the engine, or one whose hook failed, was invisible
forever while its process ran.

Four refusals are the point of this file, and each one is a defect the reference
implementation carried:

- **An unknown identity fails closed.** Nothing resolves to "probably that one".
- **A stale identity is not an unknown one.** A wrong pid under a known session
  id means the Session forked — `--resume` starts a second process under the
  same session id — so the refusal names the pids that *are* live instead of
  pretending the session id was never seen.
- **A Child Process is seen, not spoken to.** It is listed like any other row and
  refused as a Relay target, by the registry rather than by a caller's memory —
  and it is never named, whatever a lane composed for it (#78, #79). A name is
  what the user says to reach a Session, and there is nothing here to reach.
- **A Session Name disambiguates or asks.** Names are for matching and for
  speech; two candidates are answered by refusing and naming both, never by
  picking.

There is one registry and one Reply Window per Session — and the Reply Window is
**derived**, so there is no second copy to disagree with the first. The reference
implementation ran two live ledgers and rendered both; nothing here may grow a
second.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from gpt_voicecoding.core.errors import (
    AmbiguousNameError,
    ChildSessionError,
    DuplicateSessionError,
    NoNameMatchError,
    StaleSessionError,
    UnknownSessionError,
)
from gpt_voicecoding.seams.agent import (
    MAIN_SESSION,
    ChildClassification,
    LaneDiscovery,
    Progress,
    ReplyWindow,
    SessionInspection,
    SessionLifecycle,
    SessionState,
    WaitingFor,
    derive_reply_window,
)
from gpt_voicecoding.seams.identity import AgentKind, SessionName, SessionTarget

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Session:
    """One coding-agent run the user started, as the roster holds it.

    Every field but `first_seen` is a fact one lane observed, carried from
    `SessionInspection` unchanged. `first_seen` is this registry's own — when
    *we* first saw it, which no agent knows.

    **There is one name, and it is `name`.** It used to be two — a `label` the
    user's side composed and a `name` the agent reported — which is two fields
    meaning almost the same thing and two answers to "what is this Session
    called". #78 collapsed them into the glossary's single *Session Name*
    (`CONTEXT.md`): `<project> · <task>`, composed by the lane that saw the
    Session and frozen here.
    """

    target: SessionTarget
    workspace: Path
    first_seen: float
    #: What this Session is called. `None` is ordinary: an unnamed row is listed
    #: like any other, and a Codex thread has neither a name nor an id to make
    #: one from until it takes its first turn (#73).
    name: SessionName | None = None
    lifecycle: SessionLifecycle = SessionLifecycle.LIVE
    state: SessionState = SessionState.RUNNING
    waiting_for: WaitingFor = field(default_factory=WaitingFor)
    progress: Progress | None = None
    last_activity: datetime | None = None
    child: ChildClassification = MAIN_SESSION

    @property
    def reply_window(self) -> ReplyWindow:
        """Derived, never stored — see `seams.agent.derive_reply_window`.

        An ended Session accepts nothing, whatever it was doing when it went.
        """
        if self.lifecycle is not SessionLifecycle.LIVE:
            return ReplyWindow.CLOSED
        return derive_reply_window(self.state, self.child)

    @property
    def is_live(self) -> bool:
        return self.lifecycle is SessionLifecycle.LIVE

    def observed(self, row: SessionInspection, *, target: SessionTarget) -> Session:
        """This same Session, as a lane has just seen it again.

        Keeps what the registry knows and the lane does not — `first_seen` and
        the Session Name it has already accepted — and takes everything else
        from the reading, because the reading is the newer truth.

        `target` is the identity the registry settled on (`_better_known`),
        which is not always the one the reading carried: a tick with only
        process evidence names a Session it cannot see the id of. It is passed
        in rather than read off the row because the naming rule turns on it.
        """
        return replace(
            self,
            target=target,
            workspace=row.workspace,
            name=self._named_as(row, target),
            lifecycle=row.lifecycle,
            state=row.state,
            waiting_for=row.waiting_for,
            progress=row.progress,
            last_activity=row.last_activity,
            child=row.child,
        )

    def _named_as(self, row: SessionInspection, target: SessionTarget) -> SessionName | None:
        """The Session Name this row keeps — **the one its official source states**.

        A Child Process keeps none. It is listed and it is never a target, so a
        name for it would be a name the user could say and nothing could answer
        — the risk #78's own table names, held here rather than in each lane so
        it holds however #79 comes to find children.

        A name is composed once per exact `SessionTarget` and **changes only
        when the source it was composed from renames the Session** (#78 as
        amended by Simon on #113, 2026-08-27). Stability is still the point — the
        user says the name to address it — and what makes a rename safe is where
        it can come from: `SessionInspection.name` is composed by a lane from its
        agent's *official* name for the Session and from nothing else (Claude's
        roster `name`, Codex's daemon `Thread.name` — `adapters/agent/_naming.py`),
        so a change here is the agent renaming its own Session and never this
        product changing its mind. The routes that could have made a name drift
        on their own were dropped from the #67 port table before #78 was written:
        no self-report (`legacy@1d32845:bridge/hook.py:215-253`), no
        transcript-derived `ai-title` (`bridge/labels.py:73-84`).

        **Why the freeze could not simply stay.** codex 0.150.0 names a thread
        the moment its first user message lands, with the first 36 characters of
        that message, and then replaces it with a generated title (#113,
        measured — the delay before the replacement is not measured and cannot
        be read back from the daemon; it is observed on #80's run of record).
        Frozen, the product kept the fragment for the
        Session's whole life; the Codex lane now refuses that provisional name
        (`adapters/agent/codex/discovery.py::_thread_name`) and this rule is what
        lets the real title reach the roster when it arrives. On the Claude lane
        the same rule is a no-op in practice: its roster names are `derived` and
        steady, so they move only when somebody deliberately renames a Session,
        which is the one case this is meant to follow.

        Against legacy: `bridge/store.py:1875-1902` froze on first write and
        refused a different one, **adapted** — the source is now a live official
        name rather than a one-shot report, so a rename by that source is
        followed instead of refused.

        A **target change also re-composes**, and it is not a rename: it is a
        different Session under the same row. Two ways it happens, both measured
        — a Codex row takes its first turn and gains the thread id it had no name
        to be built from (#73), and the user types `/new` in that TUI so the pid
        stays and the thread does not (#77). The second is a new thread; naming
        it after the old one is the failure this rule exists to prevent.
        """
        if not row.child.is_main:
            return None
        if target != self.target:
            return row.name
        if self.name is None:
            return row.name
        if row.name is None or row.name.task == self.name.task:
            # A lane that has stopped stating a name states nothing about the
            # name it already gave: `None` is "not read this tick", which is the
            # reading a degraded Codex pass produces on every row it holds.
            #
            # **The task half alone decides**, because it is the only half the
            # agent states. The project half is resolved on this side, by running
            # `git` against the workspace (`adapters/agent/_project.py`), and it
            # moves for reasons that are not renames: a `git` that answered once
            # and failed the next tick, a workspace that becomes a repository
            # under a Session already running in it. Following those would be
            # this product changing its mind about a Session's name, which is
            # the one thing CONTEXT.md says may not move it.
            return self.name
        # Info, and it is one line per rename rather than one per tick: the held
        # name becomes this one, so the next pass compares equal and says nothing.
        _log.info(
            "%s is now called %s by its lane; it was %s",
            target,
            row.name,
            self.name,
        )
        return row.name


def _better_known(held: SessionTarget, seen: SessionTarget) -> SessionTarget:
    """The identity to keep when one process has been named two ways.

    Three cases, and the rule is one sentence: **`None` never overwrites a known
    session id; a different known id does.**

    - The reading names a Session we held anonymously — it has taken its first
      turn and written the rollout that names it (#73). Take the new name.
    - The reading is anonymous and we hold a name — this tick had only process
      evidence, because the shared daemon is not answering. Keep the name; a
      Session cannot un-know its own id, and dropping it would make the row
      look like a different Session to everything that addresses it.
    - The reading names a *different* Session on the same process — the user
      typed `/new` in that TUI, so the process is the same one and the thread
      is not. Take the new name: whoever could read an id is the authority on
      which one it is.
    """
    if seen.session_id is None:
        if held.session_id is not None:
            _log.info(
                "%s was read with process evidence only this tick; keeping its known session id",
                held,
            )
        return held
    if held.session_id is not None and held.session_id != seen.session_id:
        _log.info("pid %s moved from thread %s to %s", held.pid, held.session_id, seen.session_id)
    return seen


def _normalise(text: str) -> str:
    """Case- and whitespace-insensitive form used for name matching only."""
    return " ".join(text.split()).casefold()


def session_from(row: SessionInspection, *, first_seen: float) -> Session:
    """A row a lane just saw, as a roster entry seen for the first time.

    A Child Process arrives unnamed for the reason `Session._named_as` gives:
    the roster lists it and nothing can be said to it.
    """
    return Session(
        target=row.target,
        workspace=row.workspace,
        first_seen=first_seen,
        name=row.name if row.child.is_main else None,
        lifecycle=row.lifecycle,
        state=row.state,
        waiting_for=row.waiting_for,
        progress=row.progress,
        last_activity=row.last_activity,
        child=row.child,
    )


class SessionRegistry:
    """What Sessions exist. Holds state; decides no policy about them."""

    def __init__(self) -> None:
        self._sessions: dict[SessionTarget, Session] = {}
        #: Why a lane could not enumerate, per agent. `status` shows it; nothing
        #: else reads it, because it is news about the lane and not about a row.
        self._lane_errors: dict[AgentKind, str] = {}
        #: Why a lane's rows came from a weaker source than usual, per agent.
        #: Kept apart from `_lane_errors` rather than folded in, because the two
        #: are different news: one lane has rows that are true and thin, the
        #: other has no news at all. Collapsing them would repeat the two-field
        #: encoding `LaneDiscovery` exists to avoid.
        self._lane_degradations: dict[AgentKind, str] = {}

    # -- observation ----------------------------------------------------

    def observe(
        self, agent: AgentKind, lane: LaneDiscovery, *, now: float
    ) -> tuple[SessionTarget, ...]:
        """Make this lane's rows the roster's truth about this lane, and say what went.

        Returns the Sessions that ended on *this* observation, as they were last
        addressed. **Only this class can answer that**, and the answer is not
        recoverable by comparing the roster before and after: a Codex row gains
        its thread id at its first turn (#73) and so changes `SessionTarget`
        without anything having ended, and a Codex `/new` does the same. A
        caller diffing targets would read both as a death — and the news of a
        death is not free, because it terminates every Relay queued for that
        target.

        **Both of those are Codex, and the qualifier is load-bearing** (#79).
        They are cases of a reading that could not name itself being matched by
        its process, which is what `_same_row` does for a lane that is not
        `always_named`. Claude names every row, so a Claude row never moves
        between targets: a `/clear` is a new session id under the same process,
        and it ends the old row and starts a new one rather than re-keying the
        held one. That conversation really is over, and its queued Relays are
        answered as such.

        **A lane that could not look changes nothing.** `LaneDiscovery.error`
        means the roster has no newer information, not that the machine emptied:
        reading a failed enumeration as an empty one would end every Session on
        that lane every time a daemon restarted. The error is recorded for
        `status` and the rows are left exactly as they were.

        **A lane that looked found what it found — even if it found nothing, and
        even if it looked with something weaker than usual.** An empty
        enumeration ends rows, because a Session that is gone must stop being
        offered as a target. `LaneDiscovery.degraded` says the rows came from a
        weaker source; it does not make them less true.

        **A Session that stopped being seen ends once, then is forgotten.** It
        goes `LIVE → ENDED` on the first discovery that does not contain it, so
        a surface can still say what happened to it, and is dropped on the next
        one. Forgetting immediately would make a Session that ended between two
        ticks indistinguishable from one that never existed.

        The other lane's rows are untouched in every case: two agents fail
        independently, and one of them being down is not news about the other.
        """
        if not lane.enumerated:
            assert lane.error is not None  # `enumerated` is exactly this test
            self._lane_errors[agent] = lane.error
            _log.info("the %s lane could not enumerate: %s", agent, lane.error)
            return ()
        self._lane_errors.pop(agent, None)
        if lane.degraded is None:
            self._lane_degradations.pop(agent, None)
        else:
            self._lane_degradations[agent] = lane.degraded
            _log.info("the %s lane is reading from a weaker source: %s", agent, lane.degraded)

        seen: set[SessionTarget] = set()
        for row in lane.rows:
            if row.target.agent is not agent:
                _log.warning(
                    "the %s lane returned a %s row (%s); ignored, because a lane speaks "
                    "only for its own agent",
                    agent,
                    row.target.agent,
                    row.target,
                )
                continue
            seen.add(self.observed_one(row, now=now).target)

        ended: list[SessionTarget] = []
        for held in [row for row in self._of(agent) if row.target not in seen]:
            if held.is_live:
                self._sessions[held.target] = replace(held, lifecycle=SessionLifecycle.ENDED)
                ended.append(held.target)
            else:
                del self._sessions[held.target]
        return tuple(ended)

    def lane_errors(self) -> dict[AgentKind, str]:
        """Which lanes could not be enumerated at their last attempt, and why."""
        return dict(self._lane_errors)

    def lane_degradations(self) -> dict[AgentKind, str]:
        """Which lanes have rows read by something weaker than usual, and which.

        The rows are true; this says what read them. Separate from
        `lane_errors` because a user reading `status` needs to tell "Codex is
        running on the process table because the shared daemon is not up" from
        "nobody could look at Codex at all" — the first still lists Sessions.

        A lane that could not look leaves this alone, for the same reason it
        leaves its rows alone: the note describes rows that did not change.
        """
        return dict(self._lane_degradations)

    def observed_one(self, row: SessionInspection, *, now: float) -> Session:
        """Fold one freshly-read row into the roster, in place where it belongs.

        Public because the per-target read is a caller too (#76's `progress`
        verb): it looked at one Session and at no others, so it may correct that
        Session's entry and may conclude nothing about the rest. **`observe` is
        the whole-lane verb and stays the only one that ends a row** — a verb
        that ended rows because it was asked about one Session would end the
        whole lane every time somebody asked about one of its members.
        """
        held = self._same_row(row)
        if held is None:
            fresh = session_from(row, first_seen=now)
            self._sessions[fresh.target] = fresh
            return fresh

        target = _better_known(held.target, row.target)
        updated = held.observed(row, target=target)
        if target != held.target:
            del self._sessions[held.target]
        self._sessions[target] = updated
        return updated

    def _same_row(self, row: SessionInspection) -> Session | None:
        """The roster entry this reading is *about*, whatever it happens to name it.

        **The process is the identity; the session id is a field on it** — for
        an agent that does not always say its id. A Codex TUI exists before it
        has one (#73) and keeps its pid across `/new`, so keying on the id would
        make one process come and go from the roster every time it was read by a
        different source. Where there is no pid — a daemon thread nobody could
        tie to a TUI — the id is all there is, and it is the key.

        **An agent that always names itself is matched on its name, never on
        its process** (`AgentKind.always_named`). Claude's official roster
        carries a session id on every row from the moment the Session exists, so
        there is no Claude reading that *has* to be matched by pid — and one
        that is matched by pid is matched wrongly, because a Claude pid is not
        one Session. Two things share it, and #79 is how that stopped being
        theoretical:

        - **A Child Process runs inside its parent's process.** A Task subagent
          is not a process of its own (`claude/children.py`, measured), so its
          row carries its parent's pid. Joined on that, the child's reading
          replaces the parent's row — one tick later the user's own Session has
          become an unrelayable child, logged as the process having moved
          threads.
        - **Two children of one Session share it with each other**, so the
          second would swallow the first however the classification was
          compared.

        What is left for Claude is the exact match above, which is what the
        `--resume` fork already relies on: two processes under one session id
        are two rows, and a `/clear` under one process is a new Session that
        starts a new row while the old one ends for having left the roster.
        """
        exact = self._sessions.get(row.target)
        if exact is not None:
            return exact
        if row.target.pid is None or row.target.agent.always_named:
            return None
        return next(
            (held for held in self._of(row.target.agent) if held.target.pid == row.target.pid),
            None,
        )

    # -- addressing -----------------------------------------------------

    def register(self, session: Session) -> Session:
        """Record a Session that was found to exist. Refuses to register truth twice."""
        target = session.target
        if target in self._sessions:
            raise DuplicateSessionError(target)
        if target.named and not target.agent.addressed_by_pid and self._by_session_id(target):
            raise DuplicateSessionError(target)
        self._sessions[target] = session
        return session

    def resolve(self, target: SessionTarget) -> Session:
        """The Session that exact identity names, or a refusal saying why not."""
        candidates = self._by_session_id(target)
        if not candidates:
            raise UnknownSessionError(target)

        if target.agent.addressed_by_pid or not target.named:
            matched = [held for held in candidates if held.target.pid == target.pid]
            if not matched:
                raise StaleSessionError(
                    target,
                    reason="that session id runs under a different process",
                    live_pids=tuple(
                        held.target.pid
                        for held in candidates
                        if held.is_live and held.target.pid is not None
                    ),
                )
            session = matched[0]
        else:
            session = candidates[0]

        if not session.is_live:
            raise StaleSessionError(target, reason=f"that Session is {session.lifecycle}")
        if not session.child.is_main:
            raise ChildSessionError(target, session.child.parent)
        return session

    def match_name(self, query: str) -> Session:
        """Find the one live Session a spoken name names, or refuse.

        The query is matched as a fragment against the Session Name, and **more
        than one match refuses**, with every candidate named. An exact name is
        deliberately *not* given precedence, for three reasons:

        - The costs are asymmetric. A refusal costs one spoken round trip; a
          wrong pick delivers the user's own words into the wrong Session,
          silently, carrying the user's authority.
        - Exactness is only evidence when the text is trustworthy, and the
          primary source here is a realtime voice transcript. "ship it" may be
          the user meaning the short name, or the transcriber clipping "ship it
          later". Exactness of lossy text says nothing about intent.
        - "A Session Name is not a target" is locked. Letting an exact name win
          promotes it to a target by right, which is the first step back toward
          addressing by name.

        The collision only exists while two live names stand in a fragment
        relation. That is worth fixing where names are minted — by keeping a
        fresh title word-level distinct from the live ones — rather than by
        making matching cleverer here.
        """
        wanted = _normalise(query)
        if not wanted:
            # Every name contains the empty fragment, so an empty query would
            # match the whole roster — and match a single Session *exactly*,
            # which is a silent delivery into a Session the user never named.
            # The reference implementation has no matching behaviour at all to
            # cite here: gen-1 addressed a session by its id inside a tool call
            # and its labels were only ever spoken (`legacy@1d32845` composes a
            # label in `bridge/labels.py` and never looks one up), so spoken
            # matching and every refusal in it are this generation's.
            raise NoNameMatchError(query)
        candidates = [
            held
            for held in self.live()
            if held.child.is_main and held.name is not None and wanted in _normalise(str(held.name))
        ]

        if not candidates:
            raise NoNameMatchError(query)
        if len(candidates) > 1:
            raise AmbiguousNameError(query, tuple(candidates))
        return candidates[0]

    def set_state(self, target: SessionTarget, state: SessionState) -> Session:
        """Record what a lane observed this Session to be doing.

        The Reply Window follows from it and is never set directly — there is
        one field, so there is nothing for a second writer to disagree with.
        """
        held = self._live_row(target)
        updated = replace(held, state=state)
        self._sessions[held.target] = updated
        return updated

    def mark_ended(self, target: SessionTarget) -> Session:
        """A Session is gone. Its Reply Window closes with it, by derivation.

        Deliberately not routed through `resolve`: a Child Process ends like
        anything else, and refusing to record that because it may not be
        Relayed into would leave the roster claiming a dead process is running.
        `resolve` guards *addressing*; this records *what happened*.
        """
        held = self._live_row(target)
        ended = replace(held, lifecycle=SessionLifecycle.ENDED)
        self._sessions[held.target] = ended
        return ended

    def _live_row(self, target: SessionTarget) -> Session:
        """The held row for that identity, whatever it is — or why there is none."""
        candidates = self._by_session_id(target)
        if not candidates:
            raise UnknownSessionError(target)
        if target.agent.addressed_by_pid or not target.named:
            matched = [held for held in candidates if held.target.pid == target.pid]
            if not matched:
                raise StaleSessionError(
                    target, reason="that session id runs under a different process"
                )
            return matched[0]
        return candidates[0]

    def forget(self, target: SessionTarget) -> None:
        """Drop a Session entirely. Resolving it afterwards is unknown, not stale."""
        session = self._sessions.pop(target, None)
        if session is None:
            raise UnknownSessionError(target)

    def live(self) -> tuple[Session, ...]:
        """The roster, in the order the Sessions were first seen."""
        return tuple(held for held in self._sessions.values() if held.is_live)

    def all(self) -> tuple[Session, ...]:
        """Every Session held, ended ones included, in the order first seen."""
        return tuple(self._sessions.values())

    def _of(self, agent: AgentKind) -> tuple[Session, ...]:
        return tuple(held for held in self._sessions.values() if held.target.agent is agent)

    def _by_session_id(self, target: SessionTarget) -> list[Session]:
        return [
            held
            for held in self._sessions.values()
            if held.target.agent is target.agent and held.target.session_id == target.session_id
        ]


def spoken_name(session: Session) -> str:
    """What to call one Session out loud: its Session Name, else its address.

    **The one answer to "what is this called", so no two surfaces give two.**
    Matching is deliberately not done through here — `match_name` matches names
    and nothing else, because an address the user never heard is not something
    they can have meant. This is for saying and for showing: a Session with no
    name is still a Session the user has to be told about, and naming it by the
    thing that does address it is the honest floor.
    """
    if session.name is not None:
        return str(session.name)
    return spoken_target(session.target)


def spoken_target(target: SessionTarget) -> str:
    """One identity, said out loud. The floor under every name."""
    return f"{target.agent} {target.session_id or f'pid {target.pid}'}"
