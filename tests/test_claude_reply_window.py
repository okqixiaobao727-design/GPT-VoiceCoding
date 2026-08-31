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
from gpt_voicecoding.adapters.agent.claude.waiting_labels import SANDBOX_TOOL_NAME
from gpt_voicecoding.adapters.agent.claude.window import (
    PID_RECYCLED,
    PROCESS_GONE,
    ReplyWindowWatcher,
    StopReading,
    death_for,
    window_for,
)
from gpt_voicecoding.seams.agent import (
    AgentEvent,
    Option,
    ProgressObservation,
    ReplyWindow,
    ReplyWindowChanged,
    SessionEnded,
    SessionLifecycle,
    SessionStopped,
    WaitingFor,
    WaitingKind,
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
    """Everything the watcher raises, for the cases that intentionally mix events.

    Deliberately not a widening of `Sink`. `Sink` collects windows and nothing
    else, so an unrelated event leaking into a window-only case breaks it loudly.
    Cases where a stop or death is part of the specified behavior opt into this
    broader sink instead.
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

    @property
    def stops(self) -> list[SessionStopped]:
        return [event for event in self.events if isinstance(event, SessionStopped)]


def registry(tmp_path: Path) -> Path:
    """A stand-in for `~/.claude/sessions`, the directory Claude Code writes records to."""
    directory = tmp_path / "sessions"
    directory.mkdir(exist_ok=True)
    return directory


def say(
    tmp_path: Path,
    status: str,
    *,
    pid: int = LIVE_PID,
    session_id: str = SESSION,
    waiting_for: str | None = None,
) -> None:
    """Write what Claude Code would have written about a Session in that state.

    `waiting_for` is the label it writes beside `waiting`, in the same write
    (#150). A `waiting` record with none is an older build, or one of this
    build's waits that this reader has not measured.
    """
    document: dict[str, object] = {
        "pid": pid,
        "sessionId": session_id,
        "cwd": "/a/workspace",
        "version": "2.1.238",
        "peerProtocol": PEER_PROTOCOL,
        "messagingSocketPath": f"/tmp/cc-socks/{pid}.sock",
        "status": status,
    }
    if waiting_for is not None:
        document["waitingFor"] = waiting_for
    (registry(tmp_path) / f"{pid}.json").write_text(json.dumps(document), encoding="utf-8")


class Clock:
    """A monotonic clock a test can spend, so a budget in seconds can be tested.

    The sweep is driven by `poll_once` here rather than by the passage of time,
    so the budget has to be spendable independently of it — otherwise a test
    either sleeps for the real budget or proves nothing about it.
    """

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def spend(self, seconds: float) -> None:
        self.now += seconds


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

    @pytest.mark.parametrize("status", ["idle", "shell"])
    def test_idle_and_shell_are_both_an_open_window(self, tmp_path: Path, status: str) -> None:
        """`shell` is `idle` with a background task still running (#154, measured)."""
        say(tmp_path, status)

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
        sink = AllEvents()
        watcher = watching(tmp_path, sink)
        say(tmp_path, "busy")
        watcher.watch(TARGET)

        watcher.poll_once()
        watcher.poll_once()
        say(tmp_path, "idle")
        watcher.poll_once()
        watcher.poll_once()
        say(tmp_path, "waiting", waiting_for="permission prompt")
        watcher.poll_once()

        assert sink.windows == [ReplyWindow.OPEN, ReplyWindow.CLOSED]
        # Two Stops, and they are two facts: the turn reached `idle`, and a
        # dialog then went up (#77). The second is the one a question rides.
        assert [event.target for event in sink.stops] == [TARGET, TARGET]
        assert sink.deaths == []

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


class TestReportingStops:
    def test_a_session_that_finishes_a_turn_is_reported_stopped(self, tmp_path: Path) -> None:
        sink = AllEvents()
        watcher = watching(tmp_path, sink)
        say(tmp_path, "busy")
        watcher.watch(TARGET)

        say(tmp_path, "idle")
        watcher.poll_once()

        assert [event.target for event in sink.stops] == [TARGET]

    def test_a_first_idle_record_is_not_a_turn_that_stopped(self, tmp_path: Path) -> None:
        sink = AllEvents()
        watcher = watching(tmp_path, sink)
        watcher.watch(TARGET)

        say(tmp_path, "idle")
        watcher.poll_once()

        assert sink.stops == []

    def test_a_permission_wait_that_finishes_is_reported_stopped(self, tmp_path: Path) -> None:
        sink = AllEvents()
        watcher = watching(tmp_path, sink)
        say(tmp_path, "waiting", waiting_for="permission prompt")
        watcher.watch(TARGET)

        say(tmp_path, "idle")
        watcher.poll_once()

        assert [event.target for event in sink.stops] == [TARGET]

    def test_a_turn_that_ends_into_a_background_shell_is_reported_stopped(
        self, tmp_path: Path
    ) -> None:
        """`busy -> shell` is the turn ending, and is not made to wait for `idle` (#154)."""
        sink = AllEvents()
        watcher = watching(tmp_path, sink)
        say(tmp_path, "busy")
        watcher.watch(TARGET)

        say(tmp_path, "shell")
        watcher.poll_once()

        assert [event.target for event in sink.stops] == [TARGET]

    def test_the_background_task_finishing_is_not_a_second_stop(self, tmp_path: Path) -> None:
        """`shell -> idle` is the same ended turn losing its background task (#154)."""
        sink = AllEvents()
        watcher = watching(tmp_path, sink)
        say(tmp_path, "busy")
        watcher.watch(TARGET)

        say(tmp_path, "shell")
        watcher.poll_once()
        say(tmp_path, "idle")
        watcher.poll_once()

        assert [event.target for event in sink.stops] == [TARGET]

    def test_a_first_shell_record_is_not_a_turn_that_stopped(self, tmp_path: Path) -> None:
        """Registration seeds the baseline, so an already-shell Session announces nothing."""
        sink = AllEvents()
        watcher = watching(tmp_path, sink)
        watcher.watch(TARGET)

        say(tmp_path, "shell")
        watcher.poll_once()

        assert sink.stops == []

    def test_a_mid_turn_missing_record_does_not_erase_the_stop(self, tmp_path: Path) -> None:
        sink = AllEvents()
        watcher = watching(tmp_path, sink)
        say(tmp_path, "busy")
        watcher.watch(TARGET)

        (registry(tmp_path) / f"{LIVE_PID}.json").unlink()
        watcher.poll_once()
        say(tmp_path, "idle")
        watcher.poll_once()
        watcher.poll_once()

        assert [event.target for event in sink.stops] == [TARGET]
        assert sink.deaths == []


class TestReportingAStopTheMomentADialogGoesUp:
    """A Session entering `waiting` has stopped on the user, and says so (#77).

    Found by #75's worker and fenced into this ticket. `STATUSES_MEANING_TURN_
    ACTIVE` counts `waiting` as part of the turn, which is right for a permission
    dialog's Reply Window and wrong for the Stop Notice: a Session that raised a
    question or a permission is stopped on the *user*, which is the whole thing
    a Stop Notice exists to say. #128 later opened the window only for a question
    whose exact hook is still held; that adapter fact does not change this roster
    transition. Before this, the only Stop arrived after the user had answered,
    when the transcript carried `tool_result` and `analyse` correctly said NONE.

    The permission half already reached the user by the hook route
    (`approval.py` → `AwaitingApproval`). **The question half had no
    announcement route at all**, and `CONTEXT.md` names "the question with its
    options" as Stop Notice content. Which of the two announces when both fire
    is Bridge Core's policy, not this watcher's (`core/bridge.py`).
    """

    def test_a_dialog_going_up_is_reported_stopped(self, tmp_path: Path) -> None:
        sink = AllEvents()
        watcher = watching(tmp_path, sink)
        say(tmp_path, "busy")
        watcher.watch(TARGET)

        say(tmp_path, "waiting", waiting_for="permission prompt")
        watcher.poll_once()

        assert [event.target for event in sink.stops] == [TARGET]

    def test_it_is_reported_once_however_long_the_dialog_stands(self, tmp_path: Path) -> None:
        """A dialog waits for a human. Every sweep must not be another notice."""
        sink = AllEvents()
        watcher = watching(tmp_path, sink)
        say(tmp_path, "busy")
        watcher.watch(TARGET)

        say(tmp_path, "waiting", waiting_for="permission prompt")
        watcher.poll_once()
        watcher.poll_once()
        watcher.poll_once()

        assert len(sink.stops) == 1

    def test_a_record_that_could_not_be_read_is_not_a_second_dialog(self, tmp_path: Path) -> None:
        """Absence is not evidence, here as everywhere else in this sweep."""
        sink = AllEvents()
        watcher = watching(tmp_path, sink)
        say(tmp_path, "busy")
        watcher.watch(TARGET)
        say(tmp_path, "waiting", waiting_for="permission prompt")
        watcher.poll_once()

        (registry(tmp_path) / f"{LIVE_PID}.json").unlink()
        watcher.poll_once()
        say(tmp_path, "waiting", waiting_for="permission prompt")
        watcher.poll_once()

        assert len(sink.stops) == 1

    def test_a_second_dialog_in_the_same_turn_is_reported_again(self, tmp_path: Path) -> None:
        sink = AllEvents()
        watcher = watching(tmp_path, sink)
        say(tmp_path, "busy")
        watcher.watch(TARGET)

        say(tmp_path, "waiting", waiting_for="permission prompt")
        watcher.poll_once()
        say(tmp_path, "busy")
        watcher.poll_once()
        say(tmp_path, "waiting", waiting_for="permission prompt")
        watcher.poll_once()

        assert len(sink.stops) == 2

    def test_answering_the_dialog_still_reports_the_turn_stopping(self, tmp_path: Path) -> None:
        """Two different facts: the dialog went up, and the turn later ended."""
        sink = AllEvents()
        watcher = watching(tmp_path, sink)
        say(tmp_path, "busy")
        watcher.watch(TARGET)

        say(tmp_path, "waiting", waiting_for="permission prompt")
        watcher.poll_once()
        say(tmp_path, "idle")
        watcher.poll_once()

        assert len(sink.stops) == 2

    def test_a_dialog_already_up_when_the_engine_arrives_is_reported(self, tmp_path: Path) -> None:
        """Registration announces nothing (#27); the first sweep is where it lands."""
        sink = AllEvents()
        watcher = watching(tmp_path, sink)
        say(tmp_path, "waiting", waiting_for="permission prompt")
        watcher.watch(TARGET)

        watcher.poll_once()

        assert [event.target for event in sink.stops] == [TARGET]

    def test_the_dialog_stop_asks_what_it_stopped_on_with_the_records_own_word(
        self, tmp_path: Path
    ) -> None:
        """The label is what the reader hands over, not the bare fact of `waiting`.

        Before #150 this handed over `UNKNOWN` for every wait there is, which is
        what made a `/model` picker indistinguishable from a permission dialog.
        """
        asked: list[WaitingFor | None] = []

        def stopped_on(target: SessionTarget, roster: WaitingFor | None = None) -> StopReading:
            asked.append(roster)
            return StopReading(
                waiting_for=roster if roster is not None else WaitingFor(),
                progress=ProgressObservation(),
            )

        sink = AllEvents()
        watcher = ReplyWindowWatcher(
            settings=ClaudeSettings(
                registry_directory=registry(tmp_path), reply_window_poll_seconds=0.02
            ),
            emit=sink.emit,
            stopped_on=stopped_on,
        )
        say(tmp_path, "busy")
        watcher.watch(TARGET)

        say(tmp_path, "waiting", waiting_for="permission prompt")
        watcher.poll_once()

        assert asked == [WaitingFor(kind=WaitingKind.PERMISSION)]
        assert sink.stops[0].waiting_for.kind is WaitingKind.PERMISSION

    def test_the_turn_stop_still_asks_with_nothing_assumed(self, tmp_path: Path) -> None:
        """An idle Session is not waiting on anything the roster knows about."""
        asked: list[WaitingFor | None] = []

        def stopped_on(target: SessionTarget, roster: WaitingFor | None = None) -> StopReading:
            asked.append(roster)
            return StopReading(waiting_for=WaitingFor(), progress=ProgressObservation())

        sink = AllEvents()
        watcher = ReplyWindowWatcher(
            settings=ClaudeSettings(
                registry_directory=registry(tmp_path), reply_window_poll_seconds=0.02
            ),
            emit=sink.emit,
            stopped_on=stopped_on,
        )
        say(tmp_path, "busy")
        watcher.watch(TARGET)

        say(tmp_path, "idle")
        watcher.poll_once()

        # `None`, not an empty `WaitingFor`: the registry record for an idle
        # Session says nothing about what it is waiting for, and "nothing to
        # add" is the honest thing to hand a reader that can look properly.
        assert asked == [None]

    def test_a_dead_session_reports_its_death_and_no_dialog(self, tmp_path: Path) -> None:
        """Death is terminal and reported first; nothing else is said in that sweep."""
        sink = AllEvents()
        watcher = watching(tmp_path, sink)
        say(tmp_path, "busy")
        watcher.watch(TARGET)

        say(
            tmp_path,
            "waiting",
            pid=LIVE_PID,
            session_id="somebody-else",
            waiting_for="permission prompt",
        )
        watcher.poll_once()

        assert sink.stops == []
        assert len(sink.deaths) == 1


class Scene:
    """One watched Session, its registry, its clock and everything it raised.

    The cases below are all the same shape — put a record on disk, turn the
    sweep, spend some of the budget — and each of them is about *which* of those
    produces a notice. Arranging that in every test buried the one line that
    differed, so the arrangement lives here and the tests keep their assertions.
    """

    def __init__(
        self,
        tmp_path: Path,
        *,
        status: str = "busy",
        readings: list[WaitingFor] | None = None,
        budget: float = 5.0,
    ) -> None:
        self.tmp_path = tmp_path
        self.clock = Clock()
        self.sink = AllEvents()
        #: What the transcript reader answers, in the order it is asked. `None`
        #: leaves the watcher without one, which is how the window-only cases
        #: watch windows.
        self.readings = readings
        self.asked: list[WaitingFor | None] = []
        say(tmp_path, status)
        self.watcher = ReplyWindowWatcher(
            settings=ClaudeSettings(
                registry_directory=registry(tmp_path),
                reply_window_poll_seconds=0.02,
                stop_catch_up_budget_seconds=budget,
            ),
            emit=self.sink.emit,
            stopped_on=None if readings is None else self._stopped_on,
            clock=self.clock,
        )
        self.watcher.watch(TARGET)

    def _stopped_on(self, target: SessionTarget, roster: WaitingFor | None = None) -> StopReading:
        self.asked.append(roster)
        if roster is None:
            # A finished turn asks with nothing assumed, and the real reader
            # answers it from a transcript that is no longer held up.
            waiting_for = WaitingFor()
        else:
            assert self.readings is not None
            waiting_for = self.readings[min(len(self.asked) - 1, len(self.readings) - 1)]
        return StopReading(waiting_for=waiting_for, progress=ProgressObservation())

    def at(self, status: str, label: str | None = None) -> Scene:
        """Claude Code rewrites the record, and the sweep reads it."""
        say(self.tmp_path, status, waiting_for=label)
        self.watcher.poll_once()
        return self

    def again(self, *, after: float = 0.0) -> Scene:
        """Another sweep, with that much of the budget spent since the last one."""
        self.clock.spend(after)
        self.watcher.poll_once()
        return self

    @property
    def stops(self) -> list[SessionStopped]:
        return self.sink.stops

    @property
    def kinds(self) -> list[WaitingKind]:
        return [event.waiting_for.kind for event in self.sink.stops]


class TestWhichWaitsAreAStopAtAll:
    """`status: "waiting"` has five causes and only some of them need the user (#150).

    The reported defect: starting a Session and running `/model` produced
    "stopped and may need you — it has not said what it is waiting for yet",
    twice, about a picker its user was looking at. Claude Code writes which
    cause it is in the same record write, and these are the three answers.
    """

    @pytest.mark.parametrize("label", ["dialog open", "goal proposal"])
    def test_a_dialog_the_user_is_driving_is_never_a_stop(self, tmp_path: Path, label: str) -> None:
        scene = Scene(tmp_path).at("waiting", label).again(after=60.0)

        assert scene.stops == []

    def test_the_reported_sequence_produces_no_notice_at_all(self, tmp_path: Path) -> None:
        """Registration, a `/model` picker, then back to idle — the reported case.

        The Session took no turn, so the `idle` at the end is not a turn that
        stopped either, and the whole sequence is silent.
        """
        scene = Scene(tmp_path, status="idle").at("waiting", "dialog open")
        scene.clock.spend(10.0)

        assert scene.at("idle").stops == []

    def test_a_named_wait_is_announced_at_once(self, tmp_path: Path) -> None:
        """A wait only the user can end is not held for anybody's transcript."""
        scene = Scene(tmp_path).at("waiting", "permission prompt")

        assert scene.kinds == [WaitingKind.PERMISSION]

    def test_a_sandbox_request_names_the_sandbox(self, tmp_path: Path) -> None:
        scene = Scene(tmp_path).at("waiting", "sandbox request")

        assert scene.kinds == [WaitingKind.PERMISSION]
        assert scene.stops[0].waiting_for.tool_name == SANDBOX_TOOL_NAME


#: A transcript reader that has nothing to add yet — the state the catch-up
#: budget exists to give time to.
NOT_FLUSHED_YET = [WaitingFor(kind=WaitingKind.UNKNOWN, caught_up=False)]


class TestAWaitNothingCanYetName:
    """`caught_up=False` means *ask again, never guess*, and this is what asks (#150).

    Ported from the reference implementation, which "waits semantically rather
    than by clock: re-reads until an unanswered question is visible, then gives
    up on a configured budget" (`legacy@1d32845:bridge/daemon.py:1933-1936,
    2116-2160`). Here the re-read rides the sweep that was already turning
    rather than a dedicated poll inside a blocked hook.
    """

    def test_a_wait_the_reader_has_not_caught_up_with_is_not_announced(
        self, tmp_path: Path
    ) -> None:
        scene = Scene(tmp_path, readings=NOT_FLUSHED_YET).at("waiting", "input needed")

        assert scene.stops == []
        assert scene.asked == [WaitingFor(kind=WaitingKind.UNKNOWN, caught_up=False)]

    def test_it_is_re_read_on_the_ordinary_cadence_until_it_resolves(self, tmp_path: Path) -> None:
        """The question arrives in the transcript a moment after the dialog does."""
        question = WaitingFor(
            kind=WaitingKind.QUESTION, prompt="Which one?", options=(Option(text="this"),)
        )
        scene = Scene(tmp_path, readings=[*NOT_FLUSHED_YET, question])

        scene.at("waiting", "input needed").again(after=1.0)

        assert len(scene.asked) == 2
        assert [event.waiting_for for event in scene.stops] == [question]

    def test_the_question_it_finally_announces_carries_what_the_session_asked(
        self, tmp_path: Path
    ) -> None:
        question = WaitingFor(
            kind=WaitingKind.QUESTION, prompt="Which one?", options=(Option(text="this"),)
        )
        scene = Scene(tmp_path, readings=[question]).at("waiting", "input needed")

        assert scene.stops[0].waiting_for.prompt == "Which one?"

    def test_the_budget_being_spent_produces_exactly_one_honest_unknown(
        self, tmp_path: Path
    ) -> None:
        scene = Scene(tmp_path, readings=NOT_FLUSHED_YET, budget=5.0)

        scene.at("waiting", "worker request").again(after=5.0)

        assert scene.kinds == [WaitingKind.UNKNOWN]
        assert scene.stops[0].waiting_for.caught_up is False

    def test_the_same_persisting_wait_is_never_announced_twice(self, tmp_path: Path) -> None:
        """A dialog stands for as long as the person takes. One decision, one notice."""
        scene = Scene(tmp_path, readings=NOT_FLUSHED_YET).at("waiting", "worker request")

        for _tick in range(6):
            scene.again(after=5.0)

        assert len(scene.stops) == 1

    def test_an_unlabelled_wait_is_held_the_same_way(self, tmp_path: Path) -> None:
        """An older build writes no label at all, and that is not evidence either."""
        scene = Scene(tmp_path, readings=NOT_FLUSHED_YET).at("waiting")

        assert scene.stops == []
        assert len(scene.again(after=5.0).stops) == 1

    def test_a_wait_that_ends_inside_the_budget_is_never_announced(self, tmp_path: Path) -> None:
        """The user answered it at the keyboard. Nothing was ever theirs to be told."""
        scene = Scene(tmp_path, readings=NOT_FLUSHED_YET).at("waiting", "worker request")
        scene.clock.spend(1.0)

        assert scene.at("busy").again(after=60.0).stops == []

    def test_a_held_wait_that_ends_is_not_a_turn_that_stopped_either(self, tmp_path: Path) -> None:
        """A wait held inside its budget was never announced, so nothing ended.

        The other half of the picker defect, found by #150's review. Marking an
        undecidable wait as a turn in progress made the `idle` after it a
        finished-turn Stop — so a Session that opened something this reader
        could not name and closed it again still called the user, with the
        emptiest notice there is.
        """
        scene = Scene(tmp_path, status="idle", readings=NOT_FLUSHED_YET).at("waiting")
        scene.clock.spend(1.0)

        assert scene.at("idle").stops == []

    def test_a_wait_that_was_announced_still_ends_its_turn(self, tmp_path: Path) -> None:
        """Once the budget is spent the user has been told, and the turn is theirs.

        So the `idle` that follows is the finished turn it always was — the
        distinction is whether anything was ever announced, not whether the
        Session passed through `waiting`.
        """
        scene = Scene(tmp_path, status="idle", readings=NOT_FLUSHED_YET)

        scene.at("waiting").again(after=5.0).at("idle")

        assert scene.kinds == [WaitingKind.UNKNOWN, WaitingKind.NONE]

    def test_a_named_wait_from_an_idle_session_still_ends_its_turn(self, tmp_path: Path) -> None:
        """A permission announced out of an idle Session behaves exactly as it did."""
        scene = Scene(tmp_path, status="idle", readings=[WaitingFor()])

        scene.at("waiting", "permission prompt").at("idle")

        assert len(scene.stops) == 2

    def test_a_turn_that_simply_ends_still_stops_without_waiting_for_anything(
        self, tmp_path: Path
    ) -> None:
        """The finished-turn Stop is a different fact and no budget touches it."""
        scene = Scene(tmp_path, readings=[WaitingFor()]).at("idle")

        assert len(scene.stops) == 1
        assert scene.asked == [None]

    def test_a_second_dialog_after_one_was_announced_is_held_on_its_own_merits(
        self, tmp_path: Path
    ) -> None:
        """The budget is per dialog, not per Session: a new wait starts a new one."""
        scene = Scene(tmp_path, readings=NOT_FLUSHED_YET)

        scene.at("waiting", "worker request").again(after=5.0)
        scene.at("busy").at("waiting", "worker request")

        assert len(scene.stops) == 1

        assert len(scene.again(after=5.0).stops) == 2

    def test_an_unreadable_record_does_not_restart_the_budget(self, tmp_path: Path) -> None:
        """Absence is not evidence, here as everywhere else in this sweep."""
        scene = Scene(tmp_path, readings=NOT_FLUSHED_YET).at("waiting", "worker request")

        scene.clock.spend(3.0)
        (registry(tmp_path) / f"{LIVE_PID}.json").unlink()
        scene.watcher.poll_once()
        scene.clock.spend(2.0)

        assert len(scene.at("waiting", "worker request").stops) == 1

    def test_a_forgotten_session_leaves_no_budget_behind(self, tmp_path: Path) -> None:
        scene = Scene(tmp_path, readings=NOT_FLUSHED_YET).at("waiting", "worker request")

        scene.watcher.forget(TARGET)
        scene.watcher.watch(TARGET)

        assert scene.again(after=5.0).stops == []


class TestPolling:
    def test_polling_sees_a_change_nobody_asked_about(self, tmp_path: Path) -> None:
        sink = AllEvents()

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
        assert [event.target for event in sink.stops] == [TARGET]
        assert sink.deaths == []

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
        sink = AllEvents()

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
        assert [event.target for event in sink.stops] == [TARGET]
        assert sink.deaths == []


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

        assert hub.state.sessions.all()[0].lifecycle is SessionLifecycle.ENDED
        assert hub.state.relays.pending() == ()
        assert hub.agent.calls == []
        assert any("never reached the session" in spoken for spoken in hub.call.spoken)
