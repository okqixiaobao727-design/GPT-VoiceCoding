"""`bridgectl`, driven the way a person drives it: one line in, one answer out.

The surface is thin on purpose, so these tests are about the three things a thin
surface can still get wrong — asking the wrong engine, hiding a refusal, and
failing to tell "the engine said no" apart from "there is no engine".

It is exercised against a real assembled engine over a real socket, because a
surface tested against a mock of its own protocol proves only that the mock
agrees with it.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from fakes import FakeCall
from gpt_voicecoding.cli import main
from gpt_voicecoding.config import load
from gpt_voicecoding.control_plane.client import DEFAULT_TIMEOUT_SECONDS, EngineUnreachable
from gpt_voicecoding.control_plane.commands import render
from gpt_voicecoding.engine.composition import Engine
from gpt_voicecoding.seams.call import CallSnapshot
from gpt_voicecoding.seams.control_plane import Action, Reply

#: Longer than any deadline these tests hand the surface, and short enough that
#: the engine's own shutdown does not wait on it. It stands in for the real
#: thing #28 was found on: an action that outruns the client's patience.
SLOWER_THAN_ANY_DEADLINE_SECONDS = 3.0


class SlowCall(FakeCall):
    """A Call adapter that is slow, so an action can time out against it."""

    async def ensure_call(self, instructions: str) -> CallSnapshot:
        await asyncio.sleep(SLOWER_THAN_ANY_DEADLINE_SECONDS)
        return await super().ensure_call(instructions)


def slow_call(*, sink: object = None, **rest: object) -> SlowCall:
    return SlowCall(sink=sink)  # type: ignore[arg-type]


CONFIG = """
[engine]
socket_path = "{socket}"
state_path = "{state}"

[adapters]
call = "fakes:FakeCall"
companion_channel = "fakes:FakeCompanionChannel"

[adapters.agents]
codex = "fakes:FakeAgent"

[delegate]
model = "the-model-the-user-chose"

[log]
path = "{log}"
max_bytes = 4096
retained_files = 2
stripped_environment_prefixes = ["GVC_TEST_NOISE_"]
"""


@pytest.fixture
def home() -> Iterator[Path]:
    """A short directory: Darwin caps an AF_UNIX path at 103 bytes."""
    base = Path(tempfile.mkdtemp(prefix="gvc-", dir="/tmp"))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


#: The same engine, wired to adapters that outlast the surface's patience.
SLOW_CONFIG = CONFIG.replace('call = "fakes:FakeCall"', 'call = "test_bridgectl:slow_call"')


@pytest.fixture
def slow_engine_at(home: Path) -> Iterator[Path]:
    """An engine that answers, but not before the surface has stopped listening."""
    yield from _engine_serving(home, SLOW_CONFIG)


@pytest.fixture
def engine_at(home: Path) -> Iterator[Path]:
    """One engine, running in its own loop on another thread, and its config path.

    `bridgectl` runs its own `asyncio.run`, exactly as the console script does,
    so the engine it talks to cannot share this thread's loop.
    """
    yield from _engine_serving(home, CONFIG)


def _engine_serving(home: Path, config: str) -> Iterator[Path]:
    """One assembled engine, served on its own thread, and torn down after."""
    config_path = home / "config.toml"
    config_path.write_text(
        config.format(
            socket=home / "control.sock",
            state=home / "state.json",
            log=home / "engine.log",
        ),
        encoding="utf-8",
    )
    engine = Engine.assemble(load(config_path))
    serving = threading.Event()
    stopping = asyncio.Event()

    async def serve() -> None:
        await engine.start()
        serving.set()
        await stopping.wait()
        await engine.aclose()

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_until_complete, args=(serve(),), daemon=True)
    thread.start()
    serving.wait(timeout=5)
    try:
        yield config_path
    finally:
        loop.call_soon_threadsafe(stopping.set)
        thread.join(timeout=5)
        loop.close()


class TestAskingARunningEngine:
    def test_status_prints_what_the_hub_answered(
        self, engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["--config", str(engine_at), "status"])

        assert code == 0
        assert "duty off" in capsys.readouterr().out

    def test_a_switch_is_flipped_and_the_previous_state_reported(
        self, engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--config", str(engine_at), "switch", "duty", "on"]) == 0

        assert "duty is on (was off)" in capsys.readouterr().out
        assert main(["--config", str(engine_at), "status"]) == 0
        assert "duty on" in capsys.readouterr().out

    def test_the_live_toggle_is_one_command(
        self, engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--config", str(engine_at), "live"]) == 0
        assert "the Live Call is up" in capsys.readouterr().out

        assert main(["--config", str(engine_at), "live"]) == 0
        assert "no Live Call is up" in capsys.readouterr().out

    def test_the_socket_may_be_named_directly(
        self, engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        socket_path = load(engine_at).socket_path

        assert main(["--socket", str(socket_path), "verify"]) == 0
        assert "call: pass" in capsys.readouterr().out


class TestActingOnASessionThatIsNotThere:
    """Addressing is exact, and an address nothing registered is refused.

    The launch-through-close chain this class used to walk went with the
    launcher (#72): nothing registers a Session at runtime until discovery
    lands, so the roster is empty here and the refusals are what remain
    reachable. They are the half that mattered — an address is never guessed at.
    """

    def test_a_session_that_was_never_registered_cannot_be_reached(
        self, engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["--config", str(engine_at), "relay", "codex:never-seen", "carry", "on"])

        assert code == 1
        assert "unknown Session" in capsys.readouterr().err

    def test_a_claude_session_named_without_a_pid_is_refused(
        self, engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`--resume` forks a second process under the same session id."""
        code = main(["--config", str(engine_at), "relay", "claude:abc", "carry", "on"])

        assert code == 1
        assert "pid" in capsys.readouterr().err

    def test_an_empty_roster_says_so(
        self, engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--config", str(engine_at), "brief"]) == 0
        assert "sessions: none" in capsys.readouterr().out


class TestSayingNoOutLoud:
    def test_a_refusal_is_the_hubs_own_words_and_a_different_exit_code(
        self, engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["--config", str(engine_at), "switch", "sound", "on"])

        assert code == 1
        assert "unknown switch: 'sound'" in capsys.readouterr().err

    def test_an_unknown_command_names_the_ones_there_are(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["--socket", "/tmp/nothing.sock", "duty_toggle"])

        assert code == 2
        assert "status" in capsys.readouterr().err

    def test_a_command_written_wrongly_is_shown_how(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["--socket", "/tmp/nothing.sock", "switch", "duty"])

        assert code == 2
        assert "switch <name> on|off" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "pid",
        ["\u00b2", "1" * 4_301],
        ids=["a superscript digit", "past the int-conversion limit"],
    )
    def test_an_address_whose_pid_is_not_one_is_refused_rather_than_raised(
        self, capsys: pytest.CaptureFixture[str], pid: str
    ) -> None:
        """The conversion is the test: a spelling test lets `int` raise behind it (#211)."""
        code = main(["--socket", "/tmp/nothing.sock", "brief", f"codex:abc:{pid}"])

        assert code == 2
        assert "not a process id" in capsys.readouterr().err


class TestRenderingARelayReceipt:
    """The surface prints the receipt's three codes, and composes no sentence.

    A relay receipt is a grade and a reason. The words the user hears are the
    Voice's, re-rendered from these facts (#175); a sentence built here would be
    a second renderer for words the model rewrites anyway.
    """

    def reply(self, **data: object) -> Reply:
        return Reply.answered(
            Action.RELAY,
            {
                "request_id": "r-1",
                "target": {"agent": "codex", "session_id": "abc", "pid": None},
                "state": "delivered",
                "route": "deliver",
                "receipt": {"outcome": "delivered", "reason": ""},
                "reason": "delivered",
                **data,
            },
        )

    def test_a_delivered_relay_prints_its_state_grade_and_reason(self) -> None:
        rendered = render(self.reply())

        assert rendered == "state=delivered grade=delivered reason=delivered"

    def test_words_still_waiting_print_no_grade_rather_than_a_guess(self) -> None:
        """Nothing was attempted, so there is no grade — and it is never `unknown`."""
        rendered = render(
            self.reply(state="retained", receipt=None, reason="awaiting_reply_window")
        )

        assert rendered == "state=retained grade=none reason=awaiting_reply_window"
        assert "unknown" not in rendered

    def test_an_unproven_attempt_prints_the_grade_beside_its_code(self) -> None:
        rendered = render(
            self.reply(
                state="retained",
                receipt={"outcome": "unknown", "reason": "no readback"},
                reason="duplicate_risk",
            )
        )

        assert rendered == "state=retained grade=unknown reason=duplicate_risk"
        # The adapter's evidence travels on the wire and into the log. It is not
        # printed at the user, who is owed a code and not a diagnostic.
        assert "no readback" not in rendered


class TestRenderingAVerdict:
    """`approve` prints the verdict and the same three codes a relay does (#191).

    An Approval Relay is a Relay, so its receipt is the Relay's receipt, and the
    closing sentence this line used to print retired with the loop it closed.
    """

    def reply(self, **data: object) -> Reply:
        return Reply.answered(
            Action.APPROVE,
            {
                "approval_id": "a1",
                "verdict": "allow",
                "request_id": "r-1",
                "target": {"agent": "codex", "session_id": "abc", "pid": None},
                "state": "delivered",
                "route": "deliver",
                "receipt": {"outcome": "delivered", "reason": ""},
                "reason": "delivered",
                **data,
            },
        )

    def test_a_carried_verdict_prints_its_verdict_state_grade_and_reason(self) -> None:
        rendered = render(self.reply())

        assert rendered == "verdict=allow state=delivered grade=delivered reason=delivered"

    def test_an_unproven_verdict_prints_the_grade_it_earned_and_no_sentence(self) -> None:
        rendered = render(
            self.reply(
                state="reported_failed",
                receipt={"outcome": "held", "reason": "the dialog kept it"},
                reason="held_far_side",
            )
        )

        assert rendered == "verdict=allow state=reported_failed grade=held reason=held_far_side"
        assert "the dialog kept it" not in rendered


class TestRenderingLaneDegradation:
    def test_status_says_when_progress_was_unreadable(self) -> None:
        """Where degradation is said, now that the roster verb is a Briefing.

        A degraded lane is news about the lane, not about any Session, and a
        Session Brief is only ever about a Session. `status` is the surface that
        answers "what is this engine holding", so it is the one that says its
        rows were read by something weaker than usual.
        """
        rendered = render(
            Reply.answered(
                Action.STATUS,
                {
                    "switches": {"duty": True},
                    "sessions": [],
                    "call_id": None,
                    "pending_relays": [],
                    "degraded_lanes": {"codex": "the daemon dropped the progress read"},
                },
            )
        )

        assert "codex lane degraded" in rendered
        assert "the daemon dropped the progress read" in rendered

    def test_a_brief_is_printed_in_the_engines_own_words(self) -> None:
        """One renderer (#166 B6): this surface prints, and never composes."""
        rendered = render(
            Reply.answered(
                Action.BRIEF,
                {
                    "kind": "roster",
                    "roster": {"counts": {}, "focus": None, "rows": []},
                    "text": "sessions: none",
                },
            )
        )

        assert rendered == "sessions: none"


class TestNoEngineAtAll:
    def test_an_engine_that_is_not_running_is_not_a_refusal(
        self, home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["--socket", str(home / "absent.sock"), "--timeout", "0.5", "status"])

        assert code == 2
        assert str(home / "absent.sock") in capsys.readouterr().err

    def test_no_configuration_and_no_socket_says_both(
        self, home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["--config", str(home / "absent.toml"), "status"])

        assert code == 2
        error = capsys.readouterr().err
        assert str(home / "absent.toml") in error
        assert "--socket" in error


class TestAnEngineThatTakesTooLong:
    """A silent deadline is this surface's own, and is reported as its own.

    #28 was the opposite: an action still in flight reported as one that failed.
    Nothing is invented about the engine's side — it said nothing, so nothing
    is said on its behalf.
    """

    def test_a_silent_engine_is_not_reported_as_a_refusal(
        self, slow_engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["--config", str(slow_engine_at), "--timeout", "0.3", "live"])

        assert code == 2
        assert "did not answer within 0.3s" in capsys.readouterr().err

    def test_an_explicit_timeout_outranks_the_action_default(
        self, slow_engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Proved by the sentence naming the operator's number, not the default's."""
        code = main(["--config", str(slow_engine_at), "--timeout", "0.3", "live"])

        assert code == 2
        assert "did not answer within 0.3s" in capsys.readouterr().err

    def test_the_action_default_is_what_reaches_the_wire(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deadline is read off the action rather than written at the call site."""
        waited: list[float] = []

        async def record(request: object, *, path: Path, timeout: float) -> Reply:
            waited.append(timeout)
            # Refused rather than answered: this test is about the deadline that
            # reached the wire, and rendering a reply is another test's business.
            raise EngineUnreachable("this engine is a stand-in")

        monkeypatch.setattr("gpt_voicecoding.cli.bridgectl.ask", record)
        socket = ["--socket", str(home / "control.sock")]
        main([*socket, "live"])
        main([*socket, "status"])

        assert waited == [DEFAULT_TIMEOUT_SECONDS, DEFAULT_TIMEOUT_SECONDS]


class TestAskingWhatOneSessionSaid:
    """#171's command. The rendering is here; the reading is the adapters'."""

    def test_a_session_that_was_never_registered_cannot_be_asked(
        self, engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["--config", str(engine_at), "history", "codex:never-seen"])

        assert code == 1
        assert "unknown Session" in capsys.readouterr().err

    def test_it_needs_an_address_and_says_how_to_write_one(
        self, engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--config", str(engine_at), "history"]) == 2
        assert "history <agent>:<session id>" in capsys.readouterr().err

    def test_the_cursor_is_a_flag_with_an_ordinal_beside_it(
        self, engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A bare number after an address would read as a count of entries."""
        assert main(["--config", str(engine_at), "history", "codex:abc", "--before"]) == 2
        assert "--before <ordinal>" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "cursor",
        ["newest", "\u00b2", "1" * 4_301, "12.5"],
        ids=["a word", "a superscript digit", "past the int-conversion limit", "a fraction"],
    )
    def test_a_cursor_that_is_not_an_ordinal_is_refused_before_the_engine_is_asked(
        self, engine_at: Path, capsys: pytest.CaptureFixture[str], cursor: str
    ) -> None:
        """The conversion is the test: a spelling test lets `int` raise behind it."""
        code = main(["--config", str(engine_at), "history", "codex:abc", "--before", cursor])

        assert code == 2
        assert "not an entry's ordinal" in capsys.readouterr().err

    def test_a_cursor_below_the_oldest_ordinal_there_could_be_is_refused(
        self, engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--config", str(engine_at), "history", "codex:abc", "--before", "-1"]) == 2
        assert "counts from the oldest entry" in capsys.readouterr().err


class TestTheHistoryPageOnOneLine:
    """One page, several shapes, and none of them may read as another."""

    def reply(
        self,
        entries: list[dict[str, object]],
        *,
        older: bool = False,
        read_at: str | None = "2026-08-26T02:44:39+00:00",
    ) -> Reply:
        return Reply.answered(
            Action.HISTORY,
            {"entries": entries, "older": older, "read_at": read_at},
        )

    def test_each_entry_says_its_place_and_which_side_spoke_it(self) -> None:
        rendered = render(
            self.reply(
                [
                    {"ordinal": 4, "role": "assistant", "text": "done"},
                    {"ordinal": 3, "role": "user", "text": "do the thing"},
                ]
            )
        )

        assert "4 assistant: done" in rendered
        assert "3 user: do the thing" in rendered

    def test_the_page_says_when_it_was_read(self) -> None:
        """A page's whole meaning is when it was true."""
        rendered = render(self.reply([{"ordinal": 0, "role": "user", "text": "hello"}]))

        assert "read at 2026-08-26T02:44:39+00:00" in rendered

    def test_more_to_come_names_the_cursor_the_next_request_passes(self) -> None:
        rendered = render(
            self.reply(
                [
                    {"ordinal": 6, "role": "assistant", "text": "done"},
                    {"ordinal": 5, "role": "user", "text": "again"},
                ],
                older=True,
            )
        )

        assert "--before 5" in rendered

    def test_the_end_of_the_history_says_so_rather_than_offering_a_cursor(self) -> None:
        rendered = render(self.reply([{"ordinal": 0, "role": "user", "text": "hello"}]))

        assert "that is the whole history" in rendered
        assert "--before" not in rendered

    def test_an_empty_page_is_an_answer_rather_than_a_refusal(self) -> None:
        """And it is said without a cursor in it: the same page answers both reads."""
        rendered = render(self.reply([]))

        assert "no entries on this page" in rendered
        assert "read at 2026-08-26T02:44:39+00:00" in rendered

    def test_an_omitted_entry_keeps_its_slot_and_says_it_was_not_carried(self) -> None:
        """The page always advances: a vanished slot would misname the entry before it."""
        rendered = render(
            self.reply(
                [
                    {"ordinal": 2, "role": "assistant", "omission": "oversize"},
                    {"ordinal": 1, "role": "user", "text": "do the thing"},
                ]
            )
        )

        assert "2 assistant: (too large to carry)" in rendered
        assert "1 user: do the thing" in rendered
