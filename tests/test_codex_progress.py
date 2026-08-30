"""Where the Codex lane's `Progress` and `last_activity` come from (#76).

The reading is pure and tested in `test_codex_thread_tail.py`. This is the
wiring, and its whole subject is **cost**: a `thread/read` with turns answered
558,875 bytes against the real daemon for a thread of two turns, where the same
read without them answered 3,600 (measured on codex 0.149.1, 2026-08-26, and
`numTurns` is not a parameter of that method — passing it changed nothing). So
the roster row is a cached, gated projection and the per-target verb is the live
read, and the tests below are mostly about *which reads happen*.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from gpt_voicecoding.adapters.agent.codex import CodexAgentAdapter
from gpt_voicecoding.adapters.agent.codex.discovery import ProcessEvidence, TurnCache, discover
from gpt_voicecoding.seams.agent import (
    ProgressRole,
    SessionLifecycle,
    SessionState,
)
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget
from test_codex_discovery import (
    THREAD,
    FakeDaemon,
    running,
    thread,
    write_live_user_rollout,
)
from test_codex_thread_tail import MEASURED, MEASURED_SECONDS, spoke, told, turn

WORKSPACE = "/tmp/workspace"
_HOME_GUARD = tempfile.TemporaryDirectory(prefix="gvc-codex-progress-")
TEST_HOME = Path(_HOME_GUARD.name)
write_live_user_rollout(TEST_HOME, THREAD, WORKSPACE)


class TurnedDaemon(FakeDaemon):
    """A daemon that answers turns only when they were asked for, and counts it."""

    def __init__(self, threads: dict[str, dict], turns: dict[str, list[dict]]) -> None:
        super().__init__(threads)
        self._turns = turns
        #: Every thread id a turn list was asked for, in order.
        self.deep: list[str] = []

    async def request(self, method: str, params: dict | None = None) -> dict:
        answer = await super().request(method, params)
        if method != "thread/read":
            return answer
        thread_id = str((params or {}).get("threadId"))
        if (params or {}).get("includeTurns"):
            self.deep.append(thread_id)
            return {"thread": {**answer["thread"], "turns": self._turns.get(thread_id, [])}}
        return answer


def stopped(**extra: object) -> dict:
    return {**thread(THREAD, cwd=WORKSPACE, status="idle"), "updatedAt": MEASURED_SECONDS, **extra}


def working(**extra: object) -> dict:
    return {
        **thread(THREAD, cwd=WORKSPACE, status="active"),
        "updatedAt": MEASURED_SECONDS,
        **extra,
    }


def once() -> list[dict]:
    return [turn(told("do the thing"), spoke("done"))]


async def processes() -> tuple:
    return (running(6548, WORKSPACE, session_id=THREAD),)


def found(daemon: FakeDaemon, cache: TurnCache | None = None):
    return asyncio.run(
        discover(
            daemon,
            evidence=ProcessEvidence(list_sessions=processes, home=TEST_HOME),
            turns=cache,
        )
    )


class TestTheRosterRow:
    """The cheap projection: gated on the turn, cached on the thread's own clock."""

    def test_a_stopped_thread_says_what_it_has_been_saying(self) -> None:
        daemon = TurnedDaemon({THREAD: stopped()}, {THREAD: once()})

        row = found(daemon, TurnCache()).rows[0]

        assert row.progress is not None
        assert [(entry.role, entry.text) for entry in row.progress.recent] == [
            (ProgressRole.USER, "do the thing"),
            (ProgressRole.ASSISTANT, "done"),
        ]

    def test_a_thread_mid_turn_is_not_read_deeply_at_all(self) -> None:
        """559 KB per thread per tick is what this gate is worth."""
        daemon = TurnedDaemon({THREAD: working()}, {THREAD: once()})

        row = found(daemon, TurnCache()).rows[0]

        assert daemon.deep == []
        assert row.state is SessionState.RUNNING
        assert row.progress is None

    def test_a_thread_that_has_not_moved_is_not_read_twice(self) -> None:
        """Keyed on `updatedAt`: an untouched thread cannot have a changed tail."""
        daemon = TurnedDaemon({THREAD: stopped()}, {THREAD: once()})
        cache = TurnCache()

        first = found(daemon, cache).rows[0]
        second = found(daemon, cache).rows[0]

        assert daemon.deep == [THREAD]
        assert second.progress == first.progress

    def test_a_thread_that_moved_is_read_again(self) -> None:
        daemon = TurnedDaemon({THREAD: stopped()}, {THREAD: once()})
        cache = TurnCache()

        found(daemon, cache)
        daemon.threads[THREAD] = stopped(updatedAt=MEASURED_SECONDS + 60)
        daemon._turns[THREAD] = [turn(told("do the thing"), spoke("and again"))]  # noqa: SLF001
        row = found(daemon, cache).rows[0]

        assert daemon.deep == [THREAD, THREAD]
        assert row.progress is not None
        assert [entry.text for entry in row.progress.recent][-1] == "and again"

    def test_a_thread_that_names_no_time_is_never_cached(self) -> None:
        """Without a clock there is nothing to say it has not moved."""
        daemon = TurnedDaemon({THREAD: thread(THREAD, cwd=WORKSPACE)}, {THREAD: once()})
        cache = TurnCache()

        found(daemon, cache)
        found(daemon, cache)

        assert daemon.deep == [THREAD, THREAD]

    def test_a_thread_the_daemon_let_go_is_forgotten(self) -> None:
        """The cache is roster-sized, not machine-lifetime-sized."""
        daemon = TurnedDaemon({THREAD: stopped()}, {THREAD: once()})
        cache = TurnCache()

        found(daemon, cache)
        cache.retain(set())
        found(daemon, cache)

        assert daemon.deep == [THREAD, THREAD]

    def test_a_thread_the_daemon_could_not_describe_is_unread_rather_than_empty(self) -> None:
        """One bad thread is not a bad roster, and it is certainly not an idle one."""
        daemon = TurnedDaemon({THREAD: stopped()}, {THREAD: once()})
        daemon._turns.clear()  # noqa: SLF001

        async def refuse(method: str, params: dict | None = None) -> dict:
            if method == "thread/read" and (params or {}).get("includeTurns"):
                raise RuntimeError("the daemon dropped the read")
            return await FakeDaemon.request(daemon, method, params)

        daemon.request = refuse  # type: ignore[method-assign]
        row = found(daemon, TurnCache()).rows[0]

        assert row.progress is None
        assert row.last_activity == MEASURED

    def test_last_activity_is_free_and_is_there_even_mid_turn(self) -> None:
        """It comes off the cheap read, so a working Session still has one."""
        daemon = TurnedDaemon({THREAD: working()}, {THREAD: once()})

        row = found(daemon, TurnCache()).rows[0]

        assert row.last_activity == MEASURED
        assert row.progress is None

    def test_a_lane_with_no_turn_cache_reads_no_turns(self) -> None:
        """`discover` without one is still the roster it always was (#74)."""
        daemon = TurnedDaemon({THREAD: stopped()}, {THREAD: once()})

        row = found(daemon).rows[0]

        assert daemon.deep == []
        assert row.progress is None


class TestAnUnattachedRow:
    """Never inferred: a Session the daemon does not hold has nothing to read."""

    def test_a_process_table_row_carries_no_progress_and_no_time(self) -> None:
        daemon = TurnedDaemon({}, {})

        rows = found(daemon, TurnCache()).rows

        assert [row.progress for row in rows] == [None]
        assert [row.last_activity for row in rows] == [None]


class TestTheDaemonNote:
    """A version disagreement rides out with the rows rather than hiding them."""

    def test_the_note_reaches_the_lane_beside_whatever_else_degraded_it(self) -> None:
        lane = asyncio.run(
            discover(
                None,
                evidence=ProcessEvidence(list_sessions=processes, home=Path("/nonexistent")),
                daemon_note="the Codex CLI is '0.148.0' and the app-server is '0.149.1'",
            )
        )

        assert lane.enumerated
        assert lane.degraded is not None
        assert "0.148.0" in lane.degraded
        assert "process table" in lane.degraded

    def test_a_note_on_its_own_still_degrades_a_lane_that_read_fine(self) -> None:
        daemon = TurnedDaemon({THREAD: stopped()}, {THREAD: once()})

        lane = asyncio.run(
            discover(
                daemon,
                evidence=ProcessEvidence(list_sessions=processes, home=Path("/nonexistent")),
                turns=TurnCache(),
                daemon_note="the shared Codex daemon did not say its versions",
            )
        )

        assert lane.degraded == "the shared Codex daemon did not say its versions"

    def test_silence_is_what_a_healthy_lane_looks_like(self) -> None:
        daemon = TurnedDaemon({THREAD: stopped()}, {THREAD: once()})

        assert found(daemon, TurnCache()).degraded is None


class TestTheReadingSaysWhenItWasTaken:
    def test_read_at_is_the_moment_the_turns_were_read(self) -> None:
        daemon = TurnedDaemon({THREAD: stopped()}, {THREAD: once()})

        before = datetime.now(UTC)
        row = found(daemon, TurnCache()).rows[0]

        assert row.progress is not None
        assert row.progress.read_at is not None
        assert before <= row.progress.read_at <= datetime.now(UTC)


class _HeldDaemon:
    """A `SharedDaemon` that hands back a stand-in, so no real socket is dialled."""

    def __init__(self, client: object | None) -> None:
        self._client = client
        self.note = ""
        self.socket_path = None

    def route_to(self, **_handlers: object) -> None:
        """Where inbound traffic would go. Nothing here sends any."""

    async def client(self) -> object | None:
        return self._client

    async def aclose(self) -> None:
        self._client = None


def adapter_over(daemon: object | None, *, home: Path = TEST_HOME) -> CodexAgentAdapter:
    """A whole adapter over this daemon, enumerating *for real*.

    **Nothing here stubs `discover`, and that is the point.** An earlier version
    of this file did, and it hid a defect the reviewer found by hand: `inspect`
    runs an enumeration first, so a stubbed one cannot see the read that
    enumeration takes — and the verb was paying for two 558,875-byte reads where
    the ticket asks for one. The process table is injected rather than stubbed
    for the ordinary reason: a test that shelled out to `ps` would answer
    differently depending on what the person running it has open.
    """
    return CodexAgentAdapter(
        daemon=_HeldDaemon(daemon),  # type: ignore[arg-type]
        process_evidence=ProcessEvidence(list_sessions=processes, home=home),
    )


class TestThePerTargetRead:
    """The verb beside the roster: one Session, whatever it is doing."""

    def target(self, session_id: str | None = THREAD, pid: int | None = 6548) -> SessionTarget:
        """The exact identity the roster gives this thread: the TUI is joined on cwd."""
        return SessionTarget(agent=AgentKind.CODEX, session_id=session_id, pid=pid)

    def test_a_working_thread_can_still_be_asked_how_far_along_it_is(self) -> None:
        """The cadence skips it; the verb does not. That is the whole difference."""
        daemon = TurnedDaemon({THREAD: working()}, {THREAD: once()})
        adapter = adapter_over(daemon)
        assert asyncio.run(adapter.discover()).rows[0].progress is None  # the cadence skipped it
        daemon.deep.clear()

        row = asyncio.run(adapter.inspect(self.target()))

        assert daemon.deep == [THREAD]
        assert row.progress is not None
        assert [entry.text for entry in row.progress.recent] == ["do the thing", "done"]

    def test_one_ask_is_one_deep_read_however_warm_the_cache_is(self) -> None:
        """The whole verb, counted end to end — enumeration included.

        `inspect` enumerates before it answers, so the roster's own read is part
        of what one ask costs. At 558,875 bytes a read, the difference between
        one and two is the finding this test exists to keep closed.
        """
        daemon = TurnedDaemon({THREAD: stopped()}, {THREAD: once()})
        adapter = adapter_over(daemon)
        asyncio.run(adapter.discover())  # warm the cadence's cache
        daemon.deep.clear()

        row = asyncio.run(adapter.inspect(self.target()))

        assert daemon.deep == [THREAD]
        assert row.progress is not None

    def test_the_one_read_is_live_and_not_the_remembered_one(self) -> None:
        """`updatedAt` is epoch *seconds*, so a warm entry is not proof of freshness.

        Two changes inside one second are one change to that key — tolerable for
        a row nobody asked about, and not for one somebody just did.
        """
        daemon = TurnedDaemon({THREAD: stopped()}, {THREAD: once()})
        adapter = adapter_over(daemon)
        asyncio.run(adapter.discover())
        daemon._turns[THREAD] = [turn(told("do the thing"), spoke("and again"))]  # noqa: SLF001

        row = asyncio.run(adapter.inspect(self.target()))

        assert row.progress is not None
        assert [entry.text for entry in row.progress.recent][-1] == "and again"

    def test_a_session_the_daemon_does_not_hold_is_never_guessed_at(self) -> None:
        """An unattached process with no exact rollout identity is never deep-read."""
        daemon = TurnedDaemon({}, {})
        adapter = adapter_over(daemon, home=Path("/nonexistent"))

        row = asyncio.run(adapter.inspect(self.target(None, 6548)))

        assert daemon.deep == []
        assert row.progress is None

    def test_a_pid_lookup_returns_the_exact_rollout_verified_live_row(self) -> None:
        """The query may name a PID; the row stays live only from its exact native join."""
        adapter = adapter_over(None)

        row = asyncio.run(adapter.inspect(self.target(None, 6548)))

        assert row.progress is None
        assert row.lifecycle is SessionLifecycle.LIVE
        assert row.target.session_id == THREAD
