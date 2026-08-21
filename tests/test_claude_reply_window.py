"""Reporting a Claude Session's Reply Window, so Bridge Core can flush against it.

This discharges the obligation the Answer Relay left behind: nothing reported a
Claude Session's window, so `Session.reply_window` stayed at its fail-closed
default and every Relay queued forever. The tests are about the two ways that
could be got wrong — claiming a window is open when it has not been observed, and
reporting so much that a transition means nothing.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from gpt_voicecoding.adapters.agent.claude.registry import PEER_PROTOCOL
from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings
from gpt_voicecoding.adapters.agent.claude.window import ReplyWindowWatcher, window_for
from gpt_voicecoding.seams.agent import ReplyWindow, ReplyWindowChanged
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget

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


def watching(tmp_path: Path, sink: Sink) -> ReplyWindowWatcher:
    """A watcher over that stand-in registry, polling fast enough for a test to see it."""
    return ReplyWindowWatcher(
        settings=ClaudeSettings(
            registry_directory=registry(tmp_path), reply_window_poll_seconds=0.02
        ),
        emit=sink.emit,
    )


class TestWhatOneStatusMeans:
    def test_only_idle_is_an_open_window(self, tmp_path: Path) -> None:
        sink = Sink()
        watcher = watching(tmp_path, sink)
        say(tmp_path, "idle")

        watcher.watch(TARGET)

        assert sink.windows == [ReplyWindow.OPEN]

    @pytest.mark.parametrize("status", ["busy", "waiting"])
    def test_busy_and_waiting_are_both_closed(self, tmp_path: Path, status: str) -> None:
        """`waiting` is a permission dialog, and a dialog blocks every Relay there is."""
        sink = Sink()
        watcher = watching(tmp_path, sink)
        say(tmp_path, status)

        watcher.watch(TARGET)

        assert sink.windows == [ReplyWindow.CLOSED]

    def test_a_status_this_build_has_never_seen_is_closed(self, tmp_path: Path) -> None:
        """A whitelist, so a new state cannot arrive claiming readiness by default."""
        sink = Sink()
        watcher = watching(tmp_path, sink)
        say(tmp_path, "meditating")

        watcher.watch(TARGET)

        assert sink.windows == [ReplyWindow.CLOSED]

    def test_no_record_at_all_is_closed(self, tmp_path: Path) -> None:
        sink = Sink()

        watching(tmp_path, sink).watch(TARGET)

        assert sink.windows == [ReplyWindow.CLOSED]

    def test_a_record_for_another_session_on_that_pid_is_closed(self, tmp_path: Path) -> None:
        """A recycled pid says nothing about the Session that used to hold it."""
        sink = Sink()
        say(tmp_path, "idle", session_id="somebody-else")

        watching(tmp_path, sink).watch(TARGET)

        assert sink.windows == [ReplyWindow.CLOSED]

    def test_a_missing_record_is_closed_without_a_record_object(self) -> None:
        assert window_for(None) is ReplyWindow.CLOSED


class TestWhatGetsReported:
    def test_registering_reports_the_level_immediately(self, tmp_path: Path) -> None:
        """Otherwise an already-idle Session waits for a transition that may never come."""
        sink = Sink()
        say(tmp_path, "idle")

        watching(tmp_path, sink).watch(TARGET)

        assert sink.events == [ReplyWindowChanged(target=TARGET, window=ReplyWindow.OPEN)]

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

        assert sink.windows == [ReplyWindow.CLOSED, ReplyWindow.OPEN, ReplyWindow.CLOSED]

    def test_watching_the_same_session_twice_reports_it_once(self, tmp_path: Path) -> None:
        sink = Sink()
        watcher = watching(tmp_path, sink)
        say(tmp_path, "idle")

        watcher.watch(TARGET)
        watcher.watch(TARGET)

        assert len(sink.events) == 1

    def test_a_forgotten_session_stops_being_reported(self, tmp_path: Path) -> None:
        sink = Sink()
        watcher = watching(tmp_path, sink)
        say(tmp_path, "busy")
        watcher.watch(TARGET)

        watcher.forget(TARGET)
        say(tmp_path, "idle")
        watcher.poll_once()

        assert watcher.watching == ()
        assert sink.windows == [ReplyWindow.CLOSED]

    def test_a_session_that_vanishes_is_reported_closed(self, tmp_path: Path) -> None:
        """A Session that has gone cannot take a user turn, and must not look like it can."""
        sink = Sink()
        watcher = watching(tmp_path, sink)
        say(tmp_path, "idle")
        watcher.watch(TARGET)

        (registry(tmp_path) / f"{LIVE_PID}.json").unlink()
        watcher.poll_once()

        assert sink.windows == [ReplyWindow.OPEN, ReplyWindow.CLOSED]


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
        assert sink.windows == [ReplyWindow.CLOSED, ReplyWindow.OPEN]

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
        assert sink.windows == [ReplyWindow.CLOSED]

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
        assert sink.windows == [ReplyWindow.CLOSED, ReplyWindow.OPEN]
