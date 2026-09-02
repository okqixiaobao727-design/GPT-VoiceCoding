"""What the run config has to say for a mic-free Live Call to be possible (#183).

Three wirings, none of which can be read off the acceptance run itself — that
costs a real call and a real bot — and all of which are ordinary code:

* the derived config points `[adapters] call` at the harness's own adapter and
  gives it its per-lane settings;
* it points `[delegate] cli` at a wrapper that logs every `bridgectl` the Call
  Agent runs, by **absolute** path, so a PATH shadow cannot intercept it;
* the engine is given a `PYTHONPATH` on which `live_call` resolves, because the
  engine is the bundle's interpreter and the module is the harness's.

A wiring that is wrong here fails the acceptance run twenty minutes and one call
in. That is what these are for.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tomllib
from pathlib import Path

import journey
import live_call
import support

A_CONFIG = """
[engine]
socket_path = "/tmp/never.sock"
state_path = "/tmp/never.json"

[log]
path = "/tmp/never.log"
max_bytes = 8388608
retained_files = 3
level = "INFO"
stripped_environment_prefixes = ["CLAUDE", "CODEX"]

[delegate]
model = "a-model"
cli = "/usr/bin/true"

[adapters]
call = "gpt_voicecoding.adapters.call.realtime:realtime_call"
companion_channel = "gpt_voicecoding.adapters.companion_channel.telegram:telegram_channel"

[adapters.agents]
claude = "gpt_voicecoding.adapters.agent.claude:claude_agent"
codex = "gpt_voicecoding.adapters.agent.codex:codex_agent"

[adapters.settings.companion_channel]
token_env = "GPTVOICECODING_TELEGRAM_TOKEN"
chat_id = "8675309"
"""


def _nowhere(event: str, **fields: object) -> None:  # noqa: ARG001
    """A journal that writes nowhere: these tests grade the config, not the log."""


def _derived(tmp_path: Path, name: str = "claude", **extra: object) -> support.DerivedConfig:
    source = tmp_path / "config.toml"
    source.write_text(A_CONFIG)
    return support.derive_config(
        source=source,
        run_directory=tmp_path / f"engine-{name}",
        workspace=tmp_path / f"workspace-{name}",
        socket_path=tmp_path / f"{name}.sock",
        project_name=f"acceptance-{name}",
        **extra,  # type: ignore[arg-type]
    )


def _written(derived: support.DerivedConfig) -> dict:
    return tomllib.loads(derived.path.read_text())


# --- the transport seam -----------------------------------------------------


def test_the_call_seam_is_left_alone_when_no_lane_asks_for_a_spoken_call(tmp_path: Path) -> None:
    """The default is still the user's real config: this run accepts their engine."""
    written = _written(_derived(tmp_path))
    assert written["adapters"]["call"] == "gpt_voicecoding.adapters.call.realtime:realtime_call"
    assert "call" not in written["adapters"]["settings"]


def test_a_spoken_call_names_the_harness_adapter_and_its_own_paths(tmp_path: Path) -> None:
    derived = _derived(tmp_path, harness_live_call=True)
    written = _written(derived)
    assert written["adapters"]["call"] == live_call.REFERENCE
    table = written["adapters"]["settings"]["call"]
    assert table["observations"] == str(derived.call_observations)
    assert Path(table["wav_directory"]).is_relative_to(tmp_path)


def test_the_harness_table_is_one_the_harness_adapter_can_actually_read(tmp_path: Path) -> None:
    """The config's own keys and the module's own reader, checked against each other.

    A table this file wrote and no adapter ever read is exactly the failure that
    only shows up on a real call.
    """
    derived = _derived(tmp_path, harness_live_call=True)
    table = _written(derived)["adapters"]["settings"]["call"]
    harness, remainder = live_call.HarnessSettings.split(table)
    assert harness.observations == derived.call_observations
    assert harness.request == live_call.REQUEST
    # Whatever is left has to be readable by the shipped adapter's own settings.
    from gpt_voicecoding.adapters.call.realtime.settings import RealtimeCallSettings

    RealtimeCallSettings.of(remainder)


def test_two_lanes_never_share_an_observation_file(tmp_path: Path) -> None:
    """Both lanes hold this call at once, and one file would be two runs' notes."""
    paths = {
        lane.name: _derived(tmp_path, lane.name, harness_live_call=True).call_observations
        for lane in journey.LANES
    }
    assert len(set(paths.values())) == len(journey.LANES), paths


# --- the CLI wrapper --------------------------------------------------------


def test_the_wrapper_is_named_by_absolute_path_so_a_shadow_cannot_intercept(
    tmp_path: Path,
) -> None:
    """The engine puts `[delegate] cli` into the instructions verbatim
    (`composition.py:_instruction_context`), so an absolute path is what makes
    the Call Agent's `bridgectl` this run's wrapper rather than whatever its
    PATH resolves."""
    derived = _derived(
        tmp_path, harness_live_call=True, control_plane_cli=tmp_path / "bin" / "real"
    )
    stated = _written(derived)["delegate"]["cli"]
    assert Path(stated).is_absolute()
    assert stated == str(derived.cli_wrapper)


def test_the_wrapper_logs_every_run_and_still_runs_the_real_cli(tmp_path: Path) -> None:
    """One invocation in, one line out, and the real CLI's own output through."""
    real = tmp_path / "real-bridgectl"
    real.write_text('#!/bin/sh\necho "real saw: $*"\nexit 0\n')
    real.chmod(real.stat().st_mode | stat.S_IXUSR)

    wrapper = support.write_cli_wrapper(tmp_path / "wrapper", real=real, log=tmp_path / "runs.log")
    assert os.access(wrapper, os.X_OK)

    finished = subprocess.run(
        [str(wrapper), "live"], capture_output=True, text=True, timeout=30.0, check=False
    )
    assert finished.returncode == 0
    assert "real saw: live" in finished.stdout

    logged = support.cli_wrapper_runs(tmp_path / "runs.log")
    assert len(logged) == 1
    assert "live" in logged[0]
    # UTC, so two lanes' logs and the engine log can be read on one timeline.
    assert logged[0].endswith("Z live") or " live" in logged[0]
    assert logged[0][:4].isdigit()


def test_the_wrapper_carries_the_real_clis_exit_code_back(tmp_path: Path) -> None:
    """A refusal has to stay a refusal: the Call Agent branches on it."""
    real = tmp_path / "real-bridgectl"
    real.write_text("#!/bin/sh\nexit 3\n")
    real.chmod(real.stat().st_mode | stat.S_IXUSR)
    wrapper = support.write_cli_wrapper(tmp_path / "wrapper", real=real, log=tmp_path / "runs.log")
    finished = subprocess.run([str(wrapper)], capture_output=True, timeout=30.0, check=False)
    assert finished.returncode == 3


def test_every_run_is_logged_rather_than_the_last_one(tmp_path: Path) -> None:
    """The step's assertion is a lower bound (#181 finding 1), so the log appends."""
    real = tmp_path / "real-bridgectl"
    real.write_text("#!/bin/sh\nexit 0\n")
    real.chmod(real.stat().st_mode | stat.S_IXUSR)
    wrapper = support.write_cli_wrapper(tmp_path / "wrapper", real=real, log=tmp_path / "runs.log")
    for arguments in (["status"], ["live"], ["live"]):
        subprocess.run([str(wrapper), *arguments], timeout=30.0, check=True)
    assert len(support.cli_wrapper_runs(tmp_path / "runs.log")) == 3


def test_no_wrapper_log_is_no_runs_rather_than_an_error(tmp_path: Path) -> None:
    assert support.cli_wrapper_runs(tmp_path / "never.log") == []


# --- the engine's import path -----------------------------------------------


def test_the_engine_can_import_the_harness_adapter_by_the_name_it_is_configured_under(
    tmp_path: Path,
) -> None:
    """`[adapters] call` is a `module:attribute` the *engine* imports, and the
    engine is the bundle's interpreter, which knows nothing about `tests/`."""
    derived = _derived(tmp_path, harness_live_call=True)
    engine = support.Engine(
        config=derived,
        bundle=tmp_path / "bundle",
        journal=_nowhere,
        token="a-token",
        path_value="/usr/bin:/bin",
    )
    roots = engine.environment["PYTHONPATH"].split(os.pathsep)
    assert str(support.harness_root()) in roots
    assert (support.harness_root() / f"{live_call.REFERENCE.split(':')[0]}.py").exists()


def test_an_existing_pythonpath_is_extended_rather_than_replaced(tmp_path: Path) -> None:
    """The user's own environment reaches the engine (`Engine.environment`), and
    dropping an entry of it would be this harness changing what it accepts."""
    derived = _derived(tmp_path, harness_live_call=True)
    engine = support.Engine(
        config=derived,
        bundle=tmp_path / "bundle",
        journal=_nowhere,
        token="a-token",
        path_value="/usr/bin:/bin",
        environment={"PYTHONPATH": "/somewhere/of/theirs"},
    )
    roots = engine.environment["PYTHONPATH"].split(os.pathsep)
    assert "/somewhere/of/theirs" in roots
    assert str(support.harness_root()) in roots


# --- the tenth step ---------------------------------------------------------


def test_the_live_call_step_runs_alone(tmp_path: Path) -> None:  # noqa: ARG001
    """#183's blocker clause: this step must be runnable by itself.

    It names no Session, so unlike every other step it brings no `roster` with
    it — and a run that walked one would spend an agent turn to reach a claim
    that does not rest on it.
    """
    chosen = journey.select(["live call"])
    assert chosen.selected == ("live call",)
    assert chosen.setup == ()
    assert chosen.steps == ("live call",)
    assert not chosen.whole_lane


def test_the_live_call_step_is_last_so_a_full_run_dials_after_it_has_walked(
    tmp_path: Path,  # noqa: ARG001
) -> None:
    """A call holds the interlock, so on a whole-lane run it comes after the
    steps that drive turns rather than in the middle of them."""
    assert journey.STEPS[-1] == "live call"


def test_the_step_is_bound_to_a_method_like_every_other_name(tmp_path: Path) -> None:  # noqa: ARG001
    assert "live call" in journey.PREREQUISITES
    assert hasattr(journey.Walk, "live_call")


def test_only_a_run_that_walks_the_step_swaps_the_users_call_adapter(tmp_path: Path) -> None:
    """Every other step accepts the Call adapter the user actually configured."""
    without = _written(_derived(tmp_path, "a", harness_live_call=False))
    assert without["adapters"]["call"] != live_call.REFERENCE
    assert without["delegate"]["cli"] == "/usr/bin/true"


# --- what the two lanes heard differently -----------------------------------


def test_a_recogniser_that_splits_a_word_still_counts_as_having_heard_it() -> None:
    """Run `20260902T093755Z`: one lane logged `结束通话` and the other `结束通 话`.

    The same four seconds of synthesised audio, two engines, two transcripts.
    The request carries no spaces, so a space in the transcript is the
    recogniser's — and a substring test that counted one graded which recogniser
    answered rather than whether the words arrived (#181 finding 1).
    """
    heard = (
        "2026-09-02 21:40:04,522 INFO gpt_voicecoding.core.bridge: user speech, "
        "for the voice thread to act on: '那个你把电话挂了吧,我想让你结束通 话'"
    )
    assert journey.LIVE_CALL_HEARD_SUBSTRING not in heard
    assert journey._unspaced(journey.LIVE_CALL_HEARD_SUBSTRING) in journey._unspaced(heard)


def test_the_substring_is_really_part_of_the_request_the_harness_speaks() -> None:
    """A fragment nothing puts on the track would never be heard at all."""
    assert journey.LIVE_CALL_HEARD_SUBSTRING in live_call.REQUEST


def test_the_wrapper_records_under_a_path_with_a_space_in_it(tmp_path: Path) -> None:
    """The acceptance's real run directory is under `~/Library/Application Support`.

    Unquoted, `>> $log` is an ambiguous redirect and `exec $real` names nothing,
    so every run the Call Agent made would vanish — which is what run
    `20260902T093755Z` recorded: two lanes, a call each, and two empty wrapper
    logs. A `tmp_path` has no spaces, so only a test that puts one there sees it.
    """
    spaced = tmp_path / "Application Support" / "a run"
    spaced.mkdir(parents=True)
    real = spaced / "real bridgectl"
    real.write_text('#!/bin/sh\necho "real saw: $*"\nexit 0\n')
    real.chmod(real.stat().st_mode | stat.S_IXUSR)

    wrapper = support.write_cli_wrapper(spaced / "wrapper", real=real, log=spaced / "runs.log")
    finished = subprocess.run(
        [str(wrapper), "live"], capture_output=True, text=True, timeout=30.0, check=False
    )
    assert finished.returncode == 0, finished.stderr
    assert "real saw: live" in finished.stdout
    assert len(support.cli_wrapper_runs(spaced / "runs.log")) == 1


# --- the verbs the Call Agent chose -----------------------------------------


def test_the_verbs_are_read_off_the_wrapper_log_including_invented_ones(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    """Run `20260902T095448Z`'s two lanes, verbatim.

    The codex lane ran `--help` and then `live`; the claude lane ran `call end`,
    which is not an action this engine serves. Both are recorded: a verb the
    parser dropped for not matching anything known would hide exactly the guess
    #195 is deferred to fix.
    """
    log = tmp_path / "runs.log"
    log.write_text(
        "2026-09-02T09:57:05Z --socket /tmp/a/control.sock --help\n"
        "2026-09-02T09:57:12Z --socket /tmp/a/control.sock live\n"
        "2026-09-02T09:55:35Z --socket /tmp/a/control.sock call end\n"
    )
    walk = object.__new__(journey.Walk)
    walk.config = support.DerivedConfig(
        path=tmp_path / "config.toml",
        socket_path=tmp_path / "s.sock",
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "engine.log",
        project_name="acceptance",
        workspace=tmp_path,
        token_variable="T",
        chat_id="1",
        cli_wrapper_log=log,
    )
    assert walk._verbs_run() == ["live", "call end"]


def test_a_wrapper_log_of_only_options_names_no_verb(tmp_path: Path) -> None:
    """`--help` alone is a run the lower bound counts and a verb it does not."""
    log = tmp_path / "runs.log"
    log.write_text("2026-09-02T09:57:05Z --socket /tmp/a/control.sock --help\n")
    walk = object.__new__(journey.Walk)
    walk.config = support.DerivedConfig(
        path=tmp_path / "config.toml",
        socket_path=tmp_path / "s.sock",
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "engine.log",
        project_name="acceptance",
        workspace=tmp_path,
        token_variable="T",
        chat_id="1",
        cli_wrapper_log=log,
    )
    assert walk._verbs_run() == []
    assert len(support.cli_wrapper_runs(log)) == 1


# --- who ended the call -----------------------------------------------------


def test_a_call_that_went_down_on_its_own_is_not_credited_to_the_agent() -> None:
    """A dropped connection also leaves `bridgectl status` saying `call: none`.

    Reading only that clock made the verdict carry two contradicting sentences:
    an end reason saying the connection went away by itself, beside a claim that
    the Call Agent ended it. The audio path is the one that knows.
    """
    lost = "the connection went away by itself: ICE failed"
    assert journey._ended_by(True, lost) == "lost"
    assert journey._ended_by(False, lost) == "lost"


def test_an_ending_the_agent_asked_for_is_the_agents() -> None:
    closed = "this side closed the audio path"
    assert journey._ended_by(True, closed) == "agent"


def test_an_ending_the_step_had_to_make_itself_is_the_harnesss() -> None:
    """Green does not depend on the verb guess, and the guess stays visible."""
    closed = "this side closed the audio path"
    assert journey._ended_by(False, closed) == "harness"


def test_no_end_reason_at_all_still_says_who_was_waited_on() -> None:
    assert journey._ended_by(True, None) == "agent"
    assert journey._ended_by(False, None) == "harness"
