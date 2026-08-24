"""Reporting a Claude Session's Reply Window, so Bridge Core can flush against it.

This discharges the obligation the Answer Relay left behind: nothing reported a
Claude Session's window, so `Session.reply_window` stayed at its fail-closed
default and every Relay queued forever. The tests are about the two ways that
could be got wrong — claiming a window is open when it has not been observed, and
reporting so much that a transition means nothing.

The same sweep is also the only observer of a Claude Session's *death* (#20), and
that half is tested for the way it could be got worst-wrong: reporting a death
that has not happened. So most of those cases assert an absence.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from gpt_voicecoding.adapters.agent.claude import window as window_module
from gpt_voicecoding.adapters.agent.claude.registry import PEER_PROTOCOL, SessionRecord
from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings
from gpt_voicecoding.adapters.agent.claude.window import (
    PID_RECYCLED,
    PROCESS_GONE,
    ReplyWindowWatcher,
    death_for,
    window_for,
)
from gpt_voicecoding.core.sessions import SessionState
from gpt_voicecoding.seams.agent import (
    AgentEvent,
    ReplyWindow,
    ReplyWindowChanged,
    SessionEnded,
)
from gpt_voicecoding.seams.companion_channel import InboundText
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget
from hub import Hub

SESSION = "430b0def-38ef-4783-8d57-d800710d83bd"
LIVE_PID = os.getpid()
TARGET = SessionTarget(agent=AgentKind.CLAUDE, session_id=SESSION, pid=LIVE_PID)


@dataclass
class Sink:
    events: list[ReplyWindowChanged] = field(default_factory=list)

    def emit(self, event: ReplyWindowChanged) -> None:
        self.events.append(event)

    @property
    def windows(self) -> list[ReplyWindow]:
        return [event.window for event in self.events]


@dataclass
class AllEvents:
    """Everything the watcher raises, for the cases that are about death.

    Deliberately not a widening of `Sink`. `Sink` collects windows and nothing
    else, so a `SessionEnded` leaking into a window case breaks it loudly — and
    that is exactly the regression the death rule could introduce, so the sentinel
    the older cases already are has to keep its edge.
    """

    events: list[AgentEvent] = field(default_factory=list)

    def emit(self, event: AgentEvent) -> None:
        self.events.append(event)

    @property
    def windows(self) -> list[ReplyWindow]:
        return [event.window for event in self.events if isinstance(event, ReplyWindowChanged)]

    @property
    def deaths(self) -> list[SessionEnded]:
        return [event for event in self.events if isinstance(event, SessionEnded)]


def registry(tmp_path: Path) -> Path:
    """A stand-in for `~/.claude/sessions`, the directory Claude Code writes records to."""
    directory = tmp_path / "sessions"
    directory.mkdir(exist_ok=True)
    return directory


def say(tmp_path: Path, status: str, *, pid: int = LIVE_PID, session_id: str = SESSION) -> None:
    """Write what Claude Code would have written about a Session in that state."""
    (registry(tmp_path) / f"{pid}.json").write_text(
        json.dumps(
            {
                "pid": pid,
                "sessionId": session_id,
                "cwd": "/a/workspace",
                "version": "2.1.238",
                "peerProtocol": PEER_PROTOCOL,
                "messagingSocketPath": f"/tmp/cc-socks/{pid}.sock",
                "status": status,
            }
        ),
        encoding="utf-8",
    )


def watching(tmp_path: Path, sink: Sink | AllEvents) -> ReplyWindowWatcher:
    """A watcher over that stand-in registry, polling fast enough for a test to see it."""
    return ReplyWindowWatcher(
        settings=ClaudeSettings(
            registry_directory=registry(tmp_path), reply_window_poll_seconds=0.02
        ),
        emit=sink.emit,
    )


class TestWhatOneStatusMeans:
    """What one registry status means, asked of the level query that answers the seam.

    These read through `level` rather than through an emitted event. The level is
    what a status *is*, and `level` is the verb the Agent seam's `reply_window`
    answers with, so asking it directly tests the meaning at the place the meaning
    is now published. They asked `watch` before, and read the report it used to
    emit — a report #27 removed, because registration runs before Bridge Core
    holds the Session and every one of those reports was dropped.
    """

    def test_only_idle_is_an_open_window(self, tmp_path: Path) -> None:
        say(tmp_path, "idle")

        assert watching(tmp_path, Sink()).level(TARGET) is ReplyWindow.OPEN

    @pytest.mark.parametrize("status", ["busy", "waiting"])
    def test_busy_and_waiting_are_both_closed(self, tmp_path: Path, status: str) -> None:
        """`waiting` is a permission dialog, and a dialog blocks every Relay there is."""
        say(tmp_path, status)

        assert watching(tmp_path, Sink()).level(TARGET) is ReplyWindow.CLOSED

    def test_a_status_this_build_has_never_seen_is_closed(self, tmp_path: Path) -> None:
        """A whitelist, so a new state cannot arrive claiming readiness by default."""
        say(tmp_path, "meditating")

        assert watching(tmp_path, Sink()).level(TARGET) is ReplyWindow.CLOSED

    def test_no_record_at_all_is_closed(self, tmp_path: Path) -> None:
        assert watching(tmp_path, Sink()).level(TARGET) is ReplyWindow.CLOSED

    def test_a_record_for_another_session_on_that_pid_is_closed(self, tmp_path: Path) -> None:
        """A recycled pid says nothing about the Session that used to hold it."""
        say(tmp_path, "idle", session_id="somebody-else")

        assert watching(tmp_path, Sink()).level(TARGET) is ReplyWindow.CLOSED

    def test_asking_for_a_level_neither_reports_nor_starts_watching(self, tmp_path: Path) -> None:
        """A pure query: Bridge Core asks it of Sessions the sweep already owns."""
        sink = Sink()
        say(tmp_path, "idle")
        watcher = watching(tmp_path, sink)

        watcher.level(TARGET)

        assert sink.events == []
        assert watcher.watching == ()

    def test_a_missing_record_is_closed_without_a_record_object(self) -> None:
        assert window_for(None) is ReplyWindow.CLOSED


class TestWhatGetsReported:
    def test_registering_seeds_the_level_and_reports_nothing(self, tmp_path: Path) -> None:
        """Registration is silent, and #27 is why it has to be.

        This asserted the opposite until #27: registration emitted the current
        level, on the argument that an already-idle Session would otherwise wait
        for a transition that might never come. The need was real and the
        mechanism never once met it — registration runs before Bridge Core holds
        the Session, so **every report this test ever asserted was dropped by the
        hub as belonging to a Session nobody knew**, and because the watcher had
        recorded it as sent the sweep could never repeat it.

        The level is now pulled by Bridge Core through the seam's `reply_window`
        the instant its roster holds the Session, so registration only has to
        leave the sweep a baseline to compare against. Staying silent is also
        what keeps "a Reply Window changed on an unknown Session" out of the log
        of every healthy launch, where it would cost that line the evidential
        weight it earned in #21.
        """
        sink = Sink()
        say(tmp_path, "idle")

        watcher = watching(tmp_path, sink)
        watcher.watch(TARGET)

        assert sink.events == []
        assert watcher.watching == (TARGET,)
        # Seeded at the level it actually observed, not at a default: a sweep
        # that found the same `idle` again must have nothing to report.
        watcher.poll_once()
        assert sink.events == []

    def test_only_transitions_are_reported_after_that(self, tmp_path: Path) -> None:
        sink = Sink()
        watcher = watching(tmp_path, sink)
        say(tmp_path, "busy")
        watcher.watch(TARGET)

        watcher.poll_once()
        watcher.poll_once()
        say(tmp_path, "idle")
        watcher.poll_once()
        watcher.poll_once()
        say(tmp_path, "waiting")
        watcher.poll_once()

        assert sink.windows == [ReplyWindow.OPEN, ReplyWindow.CLOSED]

    def test_watching_the_same_session_twice_holds_it_once(self, tmp_path: Path) -> None:
        """Idempotent. Read off the watched set now that registration is silent."""
        sink = Sink()
        watcher = watching(tmp_path, sink)
        say(tmp_path, "idle")

        watcher.watch(TARGET)
        watcher.watch(TARGET)

        assert watcher.watching == (TARGET,)
        assert sink.events == []

    def test_a_forgotten_session_stops_being_reported(self, tmp_path: Path) -> None:
        sink = Sink()
        watcher = watching(tmp_path, sink)
        say(tmp_path, "busy")
        watcher.watch(TARGET)

        watcher.forget(TARGET)
        say(tmp_path, "idle")
        watcher.poll_once()

        assert watcher.watching == ()
        assert sink.windows == []

    def test_a_session_that_vanishes_is_reported_closed(self, tmp_path: Path) -> None:
        """A Session that has gone cannot take a user turn, and must not look like it can."""
        sink = Sink()
        watcher = watching(tmp_path, sink)
        say(tmp_path, "idle")
        watcher.watch(TARGET)

        (registry(tmp_path) / f"{LIVE_PID}.json").unlink()
        watcher.poll_once()

        assert sink.windows == [ReplyWindow.CLOSED]


class TestPolling:
    def test_polling_sees_a_change_nobody_asked_about(self, tmp_path: Path) -> None:
        sink = Sink()

        async def scenario():
            watcher = watching(tmp_path, sink)
            say(tmp_path, "busy")
            watcher.watch(TARGET)
            await watcher.start()
            try:
                say(tmp_path, "idle")
                for _ in range(100):
                    await asyncio.sleep(0.02)
                    if ReplyWindow.OPEN in sink.windows:
                        break
            finally:
                await watcher.aclose()

        asyncio.run(scenario())
        assert sink.windows == [ReplyWindow.OPEN]

    def test_closing_stops_the_polling(self, tmp_path: Path) -> None:
        sink = Sink()

        async def scenario():
            watcher = watching(tmp_path, sink)
            say(tmp_path, "busy")
            watcher.watch(TARGET)
            await watcher.start()
            await watcher.aclose()

            say(tmp_path, "idle")
            await asyncio.sleep(0.1)
            return watcher.watching

        assert asyncio.run(scenario()) == ()
        assert sink.windows == []

    def test_starting_twice_does_not_double_the_reads(self, tmp_path: Path) -> None:
        sink = Sink()

        async def scenario():
            watcher = watching(tmp_path, sink)
            say(tmp_path, "busy")
            watcher.watch(TARGET)
            await watcher.start()
            await watcher.start()
            try:
                say(tmp_path, "idle")
                await asyncio.sleep(0.15)
            finally:
                await watcher.aclose()

        asyncio.run(scenario())
        assert sink.windows == [ReplyWindow.OPEN]


class Child:
    """A real process this test owns, so its death is a fact rather than a stub.

    Nothing about death detection can be proved against `os.getpid()`, which is
    alive by definition — so the tests that need a corpse make one.
    """

    def __init__(self) -> None:
        self._process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.pid = self._process.pid

    def kill(self) -> None:
        """End it and reap it, so the pid is gone rather than left as a zombie."""
        self._process.kill()
        self._process.wait()


@pytest.fixture
def child() -> Iterator[Child]:
    process = Child()
    try:
        yield process
    finally:
        process.kill()


def dead_pid() -> int:
    """A pid whose process has certainly exited and been reaped."""
    process = Child()
    process.kill()
    return process.pid


def target_for(pid: int, *, session_id: str = SESSION) -> SessionTarget:
    return SessionTarget(agent=AgentKind.CLAUDE, session_id=session_id, pid=pid)


def a_record(*, pid: int, session_id: str) -> SessionRecord:
    """One parsed record, built directly — these cases are about the rule, not the file."""
    return SessionRecord(
        pid=pid,
        session_id=session_id,
        cwd=Path("/a/workspace"),
        version="2.1.238",
        peer_protocol=PEER_PROTOCOL,
        socket_path=Path(f"/tmp/cc-socks/{pid}.sock"),
        status="idle",
    )


class TestWhatCountsAsPositiveEvidenceOfDeath:
    """`death_for` alone: the rule, with no filesystem and no watcher around it."""

    def test_a_process_that_is_gone_is_death(self) -> None:
        assert death_for(TARGET, None, alive=False) == PROCESS_GONE

    def test_a_process_that_is_gone_is_death_even_with_a_record_still_on_disk(self) -> None:
        """Liveness is the authority; Claude Code not cleaning up does not resurrect it."""
        record = a_record(pid=LIVE_PID, session_id=SESSION)

        assert death_for(TARGET, record, alive=False) == PROCESS_GONE

    def test_a_record_naming_another_session_on_a_live_pid_is_death(self) -> None:
        record = a_record(pid=LIVE_PID, session_id="somebody-else")

        assert death_for(TARGET, record, alive=True) == PID_RECYCLED.format(pid=LIVE_PID)

    def test_the_two_kinds_of_evidence_are_told_apart(self) -> None:
        """The acceptance criterion: a reader can tell which fact fired."""
        gone = death_for(TARGET, None, alive=False)
        recycled = death_for(TARGET, a_record(pid=LIVE_PID, session_id="else"), alive=True)

        assert gone != recycled

    def test_no_record_at_all_on_a_live_process_is_not_death(self) -> None:
        """The registry is another program's file; a missing one is an ordinary state."""
        assert death_for(TARGET, None, alive=True) is None

    def test_this_sessions_own_record_on_a_live_process_is_not_death(self) -> None:
        record = a_record(pid=LIVE_PID, session_id=SESSION)

        assert death_for(TARGET, record, alive=True) is None


class TestReportingDeath:
    def test_a_process_that_exited_is_reported_ended_on_the_next_sweep(
        self, tmp_path: Path
    ) -> None:
        sink = AllEvents()
        watcher = watching(tmp_path, sink)
        target = target_for(dead_pid())
        watcher.watch(target)

        watcher.poll_once()

        assert sink.deaths == [SessionEnded(target=target, detail=PROCESS_GONE)]

    def test_a_record_left_behind_by_a_dead_process_does_not_hide_the_death(
        self, tmp_path: Path
    ) -> None:
        """Claude Code does not always clean up; liveness is the authority, not the file."""
        sink = AllEvents()
        watcher = watching(tmp_path, sink)
        pid = dead_pid()
        say(tmp_path, "idle", pid=pid)
        target = target_for(pid)
        watcher.watch(target)

        watcher.poll_once()

        assert [event.detail for event in sink.deaths] == [PROCESS_GONE]

    def test_a_recycled_pid_is_reported_ended(self, tmp_path: Path, child: Child) -> None:
        """A readable record naming a different session id: that pid is somebody else's now."""
        sink = AllEvents()
        watcher = watching(tmp_path, sink)
        say(tmp_path, "idle", pid=child.pid, session_id="somebody-else")
        target = target_for(child.pid)
        watcher.watch(target)

        watcher.poll_once()

        assert sink.deaths == [
            SessionEnded(target=target, detail=PID_RECYCLED.format(pid=child.pid))
        ]

    def test_a_deleted_record_on_a_live_process_is_not_a_death(self, tmp_path: Path) -> None:
        """The one case a false death would be most tempting, and most destructive."""
        sink = AllEvents()
        watcher = watching(tmp_path, sink)
        say(tmp_path, "idle")
        watcher.watch(TARGET)

        (registry(tmp_path) / f"{LIVE_PID}.json").unlink()
        watcher.poll_once()

        assert sink.deaths == []
        assert sink.windows == [ReplyWindow.CLOSED]
        assert watcher.watching == (TARGET,)

    def test_a_torn_record_on_a_live_process_is_not_a_death(self, tmp_path: Path) -> None:
        """Records are rewritten live, so a half-written one is ordinary, not fatal."""
        sink = AllEvents()
        watcher = watching(tmp_path, sink)
        say(tmp_path, "idle")
        watcher.watch(TARGET)

        (registry(tmp_path) / f"{LIVE_PID}.json").write_text('{"pid": 42, "sessi', encoding="utf-8")
        watcher.poll_once()

        assert sink.deaths == []
        assert sink.windows == [ReplyWindow.CLOSED]
        assert watcher.watching == (TARGET,)

    def test_death_is_reported_once_and_drops_the_target(self, tmp_path: Path) -> None:
        sink = AllEvents()
        watcher = watching(tmp_path, sink)
        target = target_for(dead_pid())
        watcher.watch(target)

        watcher.poll_once()
        before = list(sink.events)
        watcher.poll_once()
        watcher.poll_once()

        assert len(sink.deaths) == 1
        assert sink.events == before
        assert watcher.watching == ()

    def test_death_is_never_reported_at_registration(self, tmp_path: Path) -> None:
        """Bridge Core has not registered the Session yet, so a death raised here is lost.

        Since #27 this holds for a stronger reason than it was written for:
        registration now emits *nothing at all*, so the ordering that would have
        lost a death cannot lose one, because none is raised there to lose. The
        assertion is kept and widened to the whole event stream — what has to stay
        true is that no observation escapes registration, not merely that deaths
        do not.
        """
        sink = AllEvents()
        target = target_for(dead_pid())

        watching(tmp_path, sink).watch(target)

        assert sink.deaths == []
        assert sink.events == []

    def test_death_is_not_paired_with_a_window_report(self, tmp_path: Path) -> None:
        """Ending a Session closes its window in core state; saying both says it twice."""
        sink = AllEvents()
        watcher = watching(tmp_path, sink)
        process = Child()
        say(tmp_path, "idle", pid=process.pid)
        target = target_for(process.pid)
        watcher.watch(target)
        assert sink.events == []  # registration is silent since #27

        process.kill()
        watcher.poll_once()

        assert sink.events == [SessionEnded(target=target, detail=PROCESS_GONE)]

    def test_a_target_forgotten_before_it_dies_is_never_reported(self, tmp_path: Path) -> None:
        sink = AllEvents()
        watcher = watching(tmp_path, sink)
        process = Child()
        say(tmp_path, "idle", pid=process.pid)
        target = target_for(process.pid)
        watcher.watch(target)
        watcher.forget(target)

        process.kill()
        watcher.poll_once()

        assert sink.deaths == []
        assert watcher.watching == ()

    def test_a_sweep_that_raises_leaves_the_watch_standing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unchanged from before death detection: one bad sweep must not end the watch."""
        sink = AllEvents()
        raised: list[int] = []

        def probe(pid: int) -> bool:
            if not raised:
                raised.append(pid)
                raise RuntimeError("the liveness probe fell over")
            return True

        async def scenario() -> tuple[SessionTarget, ...]:
            watcher = watching(tmp_path, sink)
            say(tmp_path, "busy")
            watcher.watch(TARGET)
            monkeypatch.setattr(window_module, "pid_is_live", probe)
            await watcher.start()
            try:
                say(tmp_path, "idle")
                for _ in range(100):
                    await asyncio.sleep(0.02)
                    if ReplyWindow.OPEN in sink.windows:
                        break
                return watcher.watching
            finally:
                await watcher.aclose()

        assert asyncio.run(scenario()) == (TARGET,)
        assert raised == [LIVE_PID]  # the sweep really did fall over, once
        assert sink.deaths == []
        assert sink.windows == [ReplyWindow.OPEN]


class TestDeathReachesBridgeCoreEndToEnd:
    """The consumer half was already built and tested; this proves the producer lands."""

    def test_a_dead_session_is_marked_ended_and_its_waiting_relays_are_answered(
        self, tmp_path: Path
    ) -> None:
        sink = AllEvents()
        watcher = watching(tmp_path, sink)
        target = target_for(dead_pid())
        hub = Hub(sessions=((target, "port the log"),))
        watcher.watch(target)
        hub.emit(InboundText(text="ship it"))
        assert len(hub.state.relays.pending()) == 1

        watcher.poll_once()
        hub.emit(*sink.events)

        assert hub.state.sessions.all()[0].state is SessionState.ENDED
        assert hub.state.relays.pending() == ()
        assert hub.agent.calls == []
        assert any("never reached the session" in spoken for spoken in hub.call.spoken)
