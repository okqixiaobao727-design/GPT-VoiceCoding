"""The Codex login `LaunchAgent` — #83, on ADR 0012's boundary.

The load-bearing properties are different from the Claude item's, and that is why
they are asserted separately. This file is **wholly ours**: no foreign content
ever shares it, so the round trip is not a merge but a creation and a removal,
and "byte for byte" means the directory is back to not having the file at all.
What replaces the merge as the thing that can go wrong is *identity*: a file
already sitting at our path that we did not write must be refused, not deleted.

The second property is that **nothing here is hard-coded** — not the user, not
their home, not `CODEX_HOME`, and not the Codex version. #83's scope says so in
those words, and #38 is what it looks like when a rendered artifact names a path
that was true only on the machine that rendered it.

The third is the asymmetry, which is this item's whole reason for having its own
rule: it asks launchd to **load** the job, and never to unload it. The product
starts a daemon the user's TUIs will join; it never stops one they are attached
to. Every `bootout` assertion below is an assertion that a command was *not* run.

**No test here may reach the real launchd.** `Launchd` is passed in everywhere and
has no default, because the first draft of this module defaulted it and a test run
loaded a real job into the author's own login session, naming a plist that pytest
deleted a second later.
"""

from __future__ import annotations

import plistlib
import re
from collections.abc import Sequence
from pathlib import Path

import pytest

from gpt_voicecoding.installation import (
    BootstrappedRender,
    State,
    read_bootstrapped_render,
    write_bootstrapped_render,
)
from gpt_voicecoding.installation import codex_launch_agent as agent
from launchd_fake import DOMAIN, FakeLaunchd, codex_home


def launch_agents(root: Path) -> Path:
    directory = root / "Library" / "LaunchAgents"
    directory.mkdir(parents=True)
    return directory


def log_in(root: Path) -> Path:
    return root / "Application Support" / "GPT-VoiceCoding" / "codex-daemon.log"


def record_in(root: Path) -> Path:
    return root / "Application Support" / "GPT-VoiceCoding" / "installation.json"


def written(directory: Path) -> dict:
    return plistlib.loads(agent.plist_path(directory).read_bytes())


# -- what the job says ---------------------------------------------------


def test_the_job_runs_the_managed_binary_and_nothing_else(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """#82 locked the command: the standalone managed binary's `daemon start`.

    Not `codex` off the user's `PATH` — that is whatever their shell resolves,
    which on this product's own author's machine was a gen-1 wrapper function.
    """
    directory, home = launch_agents(tmp_path), codex_home(tmp_path)
    agent.install(directory, home, log_in(tmp_path), record_in(tmp_path), launchd.launchd)

    assert written(directory)["ProgramArguments"] == [
        str(agent.managed_binary(home)),
        "app-server",
        "daemon",
        "start",
    ]


def test_the_job_starts_at_login_and_is_never_kept_alive(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """#83's scope: a one-shot idempotent daemon start, and no supervisor.

    `KeepAlive` would make this a polling supervisor by another name — launchd
    restarting `daemon start` forever the moment it exits, which it does at once
    because starting the daemon is all it is for. Legacy's job had it
    (`legacy@1d32845:scripts/launch-agent.py:64`) and legacy's job was a
    supervised daemon; this one is dropped on the way across.
    """
    directory, home = launch_agents(tmp_path), codex_home(tmp_path)
    agent.install(directory, home, log_in(tmp_path), record_in(tmp_path), launchd.launchd)
    job = written(directory)

    assert job["RunAtLoad"] is True
    assert "KeepAlive" not in job


def test_the_job_raises_both_open_file_limits_to_the_ruled_value(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """#129: launchd's soft default of 256 failed after two days at 271 fds.

    The daemon leaks descriptors in the managed Codex binary, outside this
    repository.  A high launch limit keeps Sessions usable while that defect is
    reported upstream; both limits deliberately carry the same ruled value.
    """
    directory, home = launch_agents(tmp_path), codex_home(tmp_path)
    agent.install(directory, home, log_in(tmp_path), record_in(tmp_path), launchd.launchd)
    job = written(directory)

    assert job["SoftResourceLimits"] == {"NumberOfFiles": 65_536}
    assert job["HardResourceLimits"] == {"NumberOfFiles": 65_536}


def test_the_job_carries_the_codex_home_it_was_resolved_from(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """launchd hands a job none of the user's shell environment.

    Without this, a user whose `CODEX_HOME` is not the default gets a daemon on
    one home and TUIs on another, and an empty roster nothing explains.
    """
    directory, home = launch_agents(tmp_path), codex_home(tmp_path)
    agent.install(directory, home, log_in(tmp_path), record_in(tmp_path), launchd.launchd)

    assert written(directory)["EnvironmentVariables"]["CODEX_HOME"] == str(home)


def test_the_job_names_no_version(tmp_path: Path, launchd: FakeLaunchd) -> None:
    """`current` is a symlink Codex's own updater moves. Resolving it here would
    pin this product to whichever Codex was installed on the day it was."""
    directory, home = launch_agents(tmp_path), codex_home(tmp_path)
    agent.install(directory, home, log_in(tmp_path), record_in(tmp_path), launchd.launchd)

    assert "current" in str(agent.managed_binary(home))
    assert "0.149" not in agent.plist_path(directory).read_text(encoding="utf-8")


def test_the_job_logs_where_the_engine_does_not(tmp_path: Path, launchd: FakeLaunchd) -> None:
    """ADR 0004: rotation is rename-and-reopen, and launchd cannot be told to
    reopen anything — so this descriptor must never be on the engine's log."""
    directory, home = launch_agents(tmp_path), codex_home(tmp_path)
    log = log_in(tmp_path)
    agent.install(directory, home, log, record_in(tmp_path), launchd.launchd)
    job = written(directory)

    assert job["StandardOutPath"] == job["StandardErrorPath"] == str(log)
    assert log.parent.is_dir(), "launchd will not spawn a job whose log directory is missing"


# -- resolving from the environment --------------------------------------


def test_codex_home_comes_from_the_environment_when_it_is_set(tmp_path: Path) -> None:
    assert agent.default_codex_home({"CODEX_HOME": str(tmp_path)}, tmp_path) == tmp_path


@pytest.mark.parametrize("stated", ["", "   "])
def test_a_blank_codex_home_is_no_codex_home(tmp_path: Path, stated: str) -> None:
    assert agent.default_codex_home({"CODEX_HOME": stated}, tmp_path) == tmp_path / ".codex"


def test_the_launch_agents_directory_is_the_users_own(tmp_path: Path) -> None:
    assert agent.default_launch_agents_directory(tmp_path) == tmp_path / "Library" / "LaunchAgents"


def test_the_label_is_not_the_gen_one_job(tmp_path: Path, launchd: FakeLaunchd) -> None:
    """`com.gpt-voicecoding.bridge` is still in real `~/Library/LaunchAgents`
    directories and is #54's to dispose of. A collision would have this install
    silently replace a supervised job it never wrote."""
    directory = launch_agents(tmp_path)
    (directory / "com.gpt-voicecoding.bridge.plist").write_bytes(
        plistlib.dumps({"Label": "com.gpt-voicecoding.bridge", "KeepAlive": True})
    )
    agent.install(
        directory, codex_home(tmp_path), log_in(tmp_path), record_in(tmp_path), launchd.launchd
    )

    assert agent.LABEL != "com.gpt-voicecoding.bridge"
    assert plistlib.loads((directory / "com.gpt-voicecoding.bridge.plist").read_bytes()) == {
        "Label": "com.gpt-voicecoding.bridge",
        "KeepAlive": True,
    }


# -- asking launchd, and the one thing it is never asked -----------------


def test_install_loads_the_job_now_rather_than_at_the_next_login(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """The whole reason this item carries a process action at all.

    A `.app` dragged in has no install step (ADR 0012), so without this a user
    who opens `codex` on install day gets no shared daemon and no bridged Codex
    Session until they next log out.
    """
    directory = launch_agents(tmp_path)
    plist = agent.plist_path(directory)

    outcome = agent.install(
        directory, codex_home(tmp_path), log_in(tmp_path), record_in(tmp_path), launchd.launchd
    )

    assert (outcome.ok, outcome.state, outcome.changed) == (True, State.CURRENT, True)
    assert ["print", "bootstrap", "print"] == launchd.verbs
    assert launchd.commands[1] == ["/bin/launchctl", "bootstrap", DOMAIN, str(plist)]


def test_an_already_loaded_job_is_not_bootstrapped_again(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """`bootstrap` on a loaded job fails, and that failure is not one: the state
    it was reaching for is the state that is already there."""
    directory, home = launch_agents(tmp_path), codex_home(tmp_path)
    log = log_in(tmp_path)
    agent.install(directory, home, log, record_in(tmp_path), launchd.launchd)
    launchd.commands.clear()

    again = agent.install(directory, home, log, record_in(tmp_path), launchd.launchd)

    assert (again.state, again.changed, again.ok) == (State.CURRENT, False, True)
    assert "bootstrap" not in launchd.verbs


def test_a_job_that_died_is_loaded_again_by_the_next_reconcile(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """Which is repair at an event, not a supervisor on a timer (#83's scope)."""
    directory, home = launch_agents(tmp_path), codex_home(tmp_path)
    log = log_in(tmp_path)
    agent.install(directory, home, log, record_in(tmp_path), launchd.launchd)
    launchd.program = None  # the job died
    launchd.commands.clear()

    repaired = agent.install(directory, home, log, record_in(tmp_path), launchd.launchd)

    assert (repaired.ok, repaired.state, repaired.changed) == (True, State.CURRENT, True)
    assert "bootstrap" in launchd.verbs


def test_launchd_refusing_to_load_the_job_is_reported_in_its_own_words(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    launchd.refuses = True
    outcome = agent.install(
        launch_agents(tmp_path),
        codex_home(tmp_path),
        log_in(tmp_path),
        record_in(tmp_path),
        launchd.launchd,
    )

    assert outcome.ok is False
    assert agent.LABEL in outcome.note
    assert "Input/output error" in outcome.note


def test_a_changed_render_is_written_and_the_running_job_is_left_alone(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """The asymmetry, at its sharpest.

    Reloading means `bootout`, and by now the user's own `codex` TUIs are thin
    clients of the daemon this job started. The new render is for the next login;
    what the user has running is the job that was right for them.
    """
    directory, home = launch_agents(tmp_path), codex_home(tmp_path)
    log = log_in(tmp_path)
    agent.install(directory, home, log, record_in(tmp_path), launchd.launchd)
    moved = codex_home(tmp_path / "elsewhere")
    launchd.commands.clear()

    rewritten = agent.install(directory, moved, log, record_in(tmp_path), launchd.launchd)

    assert (rewritten.ok, rewritten.changed) == (True, True)
    assert "next login" in rewritten.note
    assert "bootstrap" not in launchd.verbs
    assert written(directory)["ProgramArguments"][0] == str(agent.managed_binary(moved))


def test_uninstall_never_asks_launchd_for_anything(tmp_path: Path, launchd: FakeLaunchd) -> None:
    """No `bootout`, ever. It would stop the daemon live Sessions are attached
    to, which is what #83 forbids in the words "without stopping user Sessions"."""
    directory, home = launch_agents(tmp_path), codex_home(tmp_path)
    agent.install(directory, home, log_in(tmp_path), record_in(tmp_path), launchd.launchd)
    launchd.commands.clear()

    removed = agent.uninstall(directory)

    assert (removed.ok, removed.state, removed.changed) == (True, State.ABSENT, True)
    assert launchd.commands == []
    assert launchd.held is True, "the daemon the user's Sessions are on kept running"


# -- the round trip ------------------------------------------------------


def test_uninstall_takes_the_file_back_out(tmp_path: Path, launchd: FakeLaunchd) -> None:
    directory, home = launch_agents(tmp_path), codex_home(tmp_path)

    assert (
        agent.install(
            directory, home, log_in(tmp_path), record_in(tmp_path), launchd.launchd
        ).changed
        is True
    )
    assert agent.plist_path(directory).exists()

    agent.uninstall(directory)
    assert list(directory.iterdir()) == []


def test_reinstalling_the_same_loaded_render_is_current(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """Recreating a removed plist is not a render change when its SHA is loaded."""
    directory, home = launch_agents(tmp_path), codex_home(tmp_path)
    log = log_in(tmp_path)
    record = record_in(tmp_path)
    agent.install(directory, home, log, record, launchd.launchd)
    agent.uninstall(directory)

    restored = agent.install(directory, home, log, record, launchd.launchd)

    assert (restored.state, restored.changed, restored.ok) == (State.CURRENT, True, True)


def test_uninstall_with_nothing_there_is_not_a_failure(tmp_path: Path) -> None:
    outcome = agent.uninstall(launch_agents(tmp_path))
    assert (outcome.ok, outcome.state, outcome.changed) == (True, State.ABSENT, False)


def test_a_moved_codex_home_is_stale(tmp_path: Path, launchd: FakeLaunchd) -> None:
    directory, home = launch_agents(tmp_path), codex_home(tmp_path)
    log = log_in(tmp_path)
    agent.install(directory, home, log, record_in(tmp_path), launchd.launchd)

    moved = codex_home(tmp_path / "elsewhere")
    assert (
        agent.inspect(directory, moved, log, record_in(tmp_path), launchd.launchd).state
        is State.STALE
    )


def test_a_current_plist_with_no_job_loaded_is_stale(tmp_path: Path, launchd: FakeLaunchd) -> None:
    """Both facts are reported, and only one of them is `state`. A plist that is
    right with no job holding it is a machine that will be right at next login
    and is not right now — which is the thing a status run has to be able to say.
    """
    directory, home = launch_agents(tmp_path), codex_home(tmp_path)
    log = log_in(tmp_path)
    agent.install(directory, home, log, record_in(tmp_path), launchd.launchd)
    launchd.program = None  # the job died

    standing = agent.inspect(directory, home, log, record_in(tmp_path), launchd.launchd)
    assert standing.state is State.STALE
    assert "is current" in standing.note and "not loaded" in standing.note


def test_inspect_writes_nothing(tmp_path: Path, launchd: FakeLaunchd) -> None:
    directory = launch_agents(tmp_path)
    standing = agent.inspect(
        directory, codex_home(tmp_path), log_in(tmp_path), record_in(tmp_path), launchd.launchd
    )

    assert (standing.ok, standing.state) == (True, State.ABSENT)
    assert list(directory.iterdir()) == []
    assert "bootstrap" not in launchd.verbs


# -- refusals ------------------------------------------------------------


def test_no_managed_binary_is_not_a_failure(tmp_path: Path, launchd: FakeLaunchd) -> None:
    """A user whose Codex came from Homebrew or npm has no standalone package.

    Nothing went wrong; there is simply nothing this job could start. Same answer
    the Claude item gives a user with no Claude config directory. #83's own words:
    the resolved path is in the reason, so a status run can say "binary missing"
    without launchd ever having had to fail first.
    """
    directory, home = launch_agents(tmp_path), codex_home(tmp_path, managed=False)
    outcome = agent.install(directory, home, log_in(tmp_path), record_in(tmp_path), launchd.launchd)

    assert (outcome.ok, outcome.state, outcome.changed) == (True, State.ABSENT, False)
    assert not agent.plist_path(directory).exists()
    assert str(agent.managed_binary(home)) in outcome.note
    assert launchd.commands == []


def test_a_file_at_our_path_that_is_not_ours_is_refused_untouched(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """The one way this file can carry somebody else's content.

    It cannot be merged — a launchd job is not a document with room for two — so
    the only honest answers are refuse and say so. Deleting it on an uninstall
    would take away a job this product never wrote.
    """
    directory, home = launch_agents(tmp_path), codex_home(tmp_path)
    theirs = plistlib.dumps({"Label": "com.somebody.else", "ProgramArguments": ["/bin/true"]})
    agent.plist_path(directory).write_bytes(theirs)

    refused = agent.install(directory, home, log_in(tmp_path), record_in(tmp_path), launchd.launchd)
    assert refused.ok is False
    assert "com.somebody.else" in refused.note
    assert agent.plist_path(directory).read_bytes() == theirs

    assert agent.uninstall(directory).ok is False
    assert agent.plist_path(directory).read_bytes() == theirs


def test_a_plist_that_will_not_parse_is_refused_untouched(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    directory, home = launch_agents(tmp_path), codex_home(tmp_path)
    agent.plist_path(directory).write_text("<plist> and then nothing", encoding="utf-8")

    assert (
        agent.install(directory, home, log_in(tmp_path), record_in(tmp_path), launchd.launchd).ok
        is False
    )
    assert agent.plist_path(directory).read_text(encoding="utf-8") == "<plist> and then nothing"
    assert agent.uninstall(directory).ok is False


def test_a_launch_agents_directory_that_is_not_there_is_created(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """Unlike Claude's config directory, this one is macOS's and is ours to make:
    a user who has never installed a login item simply has no such directory."""
    directory = tmp_path / "Library" / "LaunchAgents"
    outcome = agent.install(
        directory, codex_home(tmp_path), log_in(tmp_path), record_in(tmp_path), launchd.launchd
    )

    assert (outcome.ok, outcome.state, outcome.changed) == (True, State.CURRENT, True)
    assert agent.plist_path(directory).exists()


def test_a_launch_agents_directory_that_cannot_be_written_is_reported(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """#83's installation-side failure mode, in the boundary's own vocabulary."""
    directory = launch_agents(tmp_path)
    directory.chmod(0o500)
    try:
        outcome = agent.install(
            directory, codex_home(tmp_path), log_in(tmp_path), record_in(tmp_path), launchd.launchd
        )
    finally:
        directory.chmod(0o700)

    assert outcome.ok is False
    assert str(directory) in outcome.note
    assert "bootstrap" not in launchd.verbs


# -- what a status run says about the daemon itself ----------------------


def test_matching_versions_are_reported_as_answering(tmp_path: Path) -> None:
    def answer(arguments: Sequence[str]) -> tuple[int, str]:
        assert list(arguments[1:]) == ["app-server", "daemon", "version"]
        return (0, '{"cliVersion": "0.149.1", "appServerVersion": "0.149.1"}')

    said = agent.daemon_versions(codex_home(tmp_path), answer)
    assert "answering" in said and "0.149.1" in said


def test_a_version_mismatch_says_which_side_is_which(tmp_path: Path) -> None:
    """#82's proven failure: CLI 0.148.0 against daemon 0.149.1. A Session the
    user starts with that CLI will not speak to the daemon this job started."""
    said = agent.daemon_versions(
        codex_home(tmp_path),
        lambda _: (0, '{"cliVersion": "0.148.0", "appServerVersion": "0.149.1"}'),
    )
    assert "0.148.0" in said and "0.149.1" in said


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        pytest.param(
            (1, "failed to connect to app-server-control.sock"), "not answering", id="no daemon"
        ),
        pytest.param((0, "not json at all"), "not with JSON", id="not JSON"),
        pytest.param((0, "[1, 2]"), "not a version document", id="JSON of the wrong shape"),
        pytest.param((-1, "did not answer within 10 seconds"), "not answering", id="a hang"),
    ],
)
def test_a_daemon_that_cannot_be_asked_says_so_rather_than_raising(
    tmp_path: Path, answer: tuple[int, str], expected: str
) -> None:
    """A status run must survive every one of these: the daemon is normally
    absent, and this is the verb a person typed to find out why."""
    assert expected in agent.daemon_versions(codex_home(tmp_path), lambda _: answer)


def test_a_daemon_that_says_no_versions_is_not_a_daemon_whose_versions_agree(
    tmp_path: Path,
) -> None:
    """`None == None` is true, and it must not be the thing that decides this.

    A document with neither field would otherwise be reported as "CLI and
    app-server both None" — a version proof that passes by saying nothing.
    """
    said = agent.daemon_versions(codex_home(tmp_path), lambda _: (0, "{}"))
    assert "without saying its versions" in said


def test_the_command_timeout_fits_inside_the_shell_ceiling_it_is_derived_from() -> None:
    """The number is derived, and this is what holds the derivation together.

    `Installation.deadline` is the only measured ceiling in this picture: the
    shell kills a reconcile that outlives it, and this item's subprocesses run
    inside that. The value lives in Swift and is used in Python, which is exactly
    the shape #47 records — a constant spelled in two languages with no test
    between them. So this reads the Swift one rather than trusting a comment.
    """
    swift = (
        Path(__file__).resolve().parents[1] / "shell/Sources/ShellCore/Installation.swift"
    ).read_text(encoding="utf-8")
    stated = re.search(r"deadline:\s*TimeInterval\s*=\s*([0-9.]+)", swift)
    assert stated, "the shell no longer states a reconcile deadline this can be derived from"

    assert float(stated.group(1)) == agent.SHELL_RECONCILE_DEADLINE_SECONDS
    assert agent.COMMAND_TIMEOUT_SECONDS * agent.COMMANDS_PER_RUN <= float(stated.group(1))


def test_a_loaded_job_still_running_the_previous_render_is_stale(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """The thing `state` would otherwise lie about.

    Nothing here reloads a job, so after a render changes there is a window —
    until the next login — in which the file on disk is right and what launchd is
    actually running is the render before it. A status run that read our own file
    back would call that `current`. Asking launchd what it holds is what makes
    the difference visible.
    """
    directory, home = launch_agents(tmp_path), codex_home(tmp_path)
    log = log_in(tmp_path)
    agent.install(directory, home, log, record_in(tmp_path), launchd.launchd)
    moved = codex_home(tmp_path / "elsewhere")
    agent.install(directory, moved, log, record_in(tmp_path), launchd.launchd)

    standing = agent.inspect(directory, moved, log, record_in(tmp_path), launchd.launchd)

    assert standing.state is State.STALE
    assert str(agent.managed_binary(home)) in standing.note, "it does not say what is running"
    assert "next login" in standing.note
    assert launchd.program == str(agent.managed_binary(home)), "the job was reloaded"


def test_a_loaded_job_with_the_same_program_and_previous_render_is_stale(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """#132: the program path cannot identify the whole loaded render.

    #129 changed only the resource limits, so launchd kept the same program while
    holding the old definition.  A status run must not call that loaded job current.
    """
    directory, home = launch_agents(tmp_path), codex_home(tmp_path)
    previous_log = log_in(tmp_path / "previous")
    current_log = log_in(tmp_path)
    agent.install(directory, home, previous_log, record_in(tmp_path), launchd.launchd)
    agent.install(directory, home, current_log, record_in(tmp_path), launchd.launchd)

    standing = agent.inspect(directory, home, current_log, record_in(tmp_path), launchd.launchd)

    assert standing.state is State.STALE
    assert "loaded job is a previous render; applies at the next login" in standing.note
    assert launchd.program == str(agent.managed_binary(home)), "the job was reloaded"


def test_a_second_reconcile_in_the_same_login_keeps_the_previous_render_stale(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """A byte-identical reconcile is not evidence that launchd reloaded the file."""
    directory, home = launch_agents(tmp_path), codex_home(tmp_path)
    record = record_in(tmp_path)
    previous_log = log_in(tmp_path / "previous")
    current_log = log_in(tmp_path)
    agent.install(directory, home, previous_log, record, launchd.launchd)
    agent.install(directory, home, current_log, record, launchd.launchd)
    recorded_stale = record.read_bytes()
    launchd.commands.clear()

    again = agent.install(directory, home, current_log, record, launchd.launchd)

    assert again.state is State.STALE
    assert "previous render" in again.note
    assert record.read_bytes() == recorded_stale
    assert "bootstrap" not in launchd.verbs


def test_a_new_login_makes_the_render_it_loaded_current(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """ASID change is the fake equivalent of logout/login in #132's acceptance."""
    directory, home = launch_agents(tmp_path), codex_home(tmp_path)
    record = record_in(tmp_path)
    previous_log = log_in(tmp_path / "previous")
    current_log = log_in(tmp_path)
    agent.install(directory, home, previous_log, record, launchd.launchd)
    agent.install(directory, home, current_log, record, launchd.launchd)
    launchd.begin_login(agent.plist_path(directory))

    reconciled = agent.install(directory, home, current_log, record, launchd.launchd)
    standing = agent.inspect(directory, home, current_log, record, launchd.launchd)

    assert reconciled.state is State.CURRENT
    assert standing.state is State.CURRENT


def test_a_new_login_records_the_disk_render_before_reconcile_changes_it(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """The login loaded the old disk bytes, not the new build's later render."""
    directory, home = launch_agents(tmp_path), codex_home(tmp_path)
    record = record_in(tmp_path)
    previous_log = log_in(tmp_path / "previous")
    current_log = log_in(tmp_path)
    agent.install(directory, home, previous_log, record, launchd.launchd)
    launchd.begin_login(agent.plist_path(directory))

    reconciled = agent.install(directory, home, current_log, record, launchd.launchd)
    standing = agent.inspect(directory, home, current_log, record, launchd.launchd)

    assert reconciled.state is State.STALE
    assert standing.state is State.STALE
    assert "previous render" in standing.note


def test_a_missing_loaded_render_record_fails_closed_without_status_writing(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """An upgraded install cannot guess which same-program render launchd holds."""
    directory, home = launch_agents(tmp_path), codex_home(tmp_path)
    log = log_in(tmp_path)
    record = record_in(tmp_path)
    agent.install(directory, home, log, record, launchd.launchd)
    record.unlink()

    standing = agent.inspect(directory, home, log, record, launchd.launchd)

    assert standing.state is State.STALE
    assert "unknown render" in standing.note
    assert not record.exists(), "status wrote the missing installation record"


def test_install_records_an_unknown_render_when_the_loaded_record_is_missing(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    directory, home = launch_agents(tmp_path), codex_home(tmp_path)
    log = log_in(tmp_path)
    record = record_in(tmp_path)
    agent.install(directory, home, log, record, launchd.launchd)
    record.unlink()

    reconciled = agent.install(directory, home, log, record, launchd.launchd)
    loaded = read_bootstrapped_render(record)

    assert reconciled.state is State.STALE
    assert "unknown render" in reconciled.note
    assert loaded is not None
    assert loaded.render_sha256 is None
    assert loaded.login_asid == launchd.login_asid


def test_a_new_login_with_no_plist_records_an_unknown_loaded_render(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """A loaded job plus an absent file never supplies bytes the login loaded."""
    directory, home = launch_agents(tmp_path), codex_home(tmp_path)
    log = log_in(tmp_path)
    record = record_in(tmp_path)
    agent.install(directory, home, log, record, launchd.launchd)
    agent.plist_path(directory).unlink()
    launchd.login_asid += 1

    reconciled = agent.install(directory, home, log, record, launchd.launchd)
    loaded = read_bootstrapped_render(record)
    standing = agent.inspect(directory, home, log, record, launchd.launchd)

    assert reconciled.state is State.STALE
    assert "unknown render" in reconciled.note
    assert loaded is not None and loaded.render_sha256 is None
    assert loaded.login_asid == launchd.login_asid
    assert standing.state is State.STALE
    assert "unknown render" in standing.note


def test_a_launchd_that_does_not_say_what_it_loaded_fails_closed(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """A `print` whose shape this does not recognise is reported as unknown.

    The alternative is a status run that calls a job current on the strength of a
    line it could not find, which is the failure this whole reading exists to
    avoid.
    """
    directory, home = launch_agents(tmp_path), codex_home(tmp_path)
    log = log_in(tmp_path)
    agent.install(directory, home, log, record_in(tmp_path), launchd.launchd)
    launchd.commands.clear()

    silent = agent.Launchd(domain=DOMAIN, run=lambda _: (0, "a shape this does not recognise"))
    standing = agent.inspect(directory, home, log, record_in(tmp_path), silent)

    assert standing.state is State.STALE
    assert "did not say what it runs" in standing.note


def test_a_loaded_render_record_without_an_asid_fails_closed(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    directory, home = launch_agents(tmp_path), codex_home(tmp_path)
    log = log_in(tmp_path)
    record = record_in(tmp_path)
    agent.install(directory, home, log, record, launchd.launchd)
    loaded = read_bootstrapped_render(record)
    assert loaded is not None
    write_bootstrapped_render(
        record, BootstrappedRender(render_sha256=loaded.render_sha256, login_asid=None)
    )

    standing = agent.inspect(directory, home, log, record, launchd.launchd)

    assert standing.state is State.STALE
    assert "which login" in standing.note


def test_reconcile_keeps_a_foreign_loaded_program_stale(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    directory, home = launch_agents(tmp_path), codex_home(tmp_path)
    log = log_in(tmp_path)
    record = record_in(tmp_path)
    agent.install(directory, home, log, record, launchd.launchd)
    launchd.program = "/foreign/codex"

    reconciled = agent.install(directory, home, log, record, launchd.launchd)

    assert reconciled.state is State.STALE
    assert "/foreign/codex" in reconciled.note


def test_reconcile_keeps_a_foreign_loaded_program_stale_when_plist_is_missing(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """The wanted program is still authoritative when reconcile recreates the plist."""
    directory, home = launch_agents(tmp_path), codex_home(tmp_path)
    log = log_in(tmp_path)
    record = record_in(tmp_path)
    agent.install(directory, home, log, record, launchd.launchd)
    agent.plist_path(directory).unlink()
    launchd.program = "/foreign/codex"

    reconciled = agent.install(directory, home, log, record, launchd.launchd)

    assert reconciled.state is State.STALE
    assert "/foreign/codex" in reconciled.note


def test_bootstrap_without_a_login_asid_records_an_unknown_render(tmp_path: Path) -> None:
    directory, home = launch_agents(tmp_path), codex_home(tmp_path)
    log = log_in(tmp_path)
    record = record_in(tmp_path)
    binary = str(agent.managed_binary(home))
    print_count = 0

    def answer(arguments: Sequence[str]) -> tuple[int, str]:
        nonlocal print_count
        if arguments[1] == "bootstrap":
            return (0, "")
        print_count += 1
        if print_count == 1:
            return (113, "Could not find service")
        return (0, f"program = {binary}")

    launchd = agent.Launchd(domain=DOMAIN, run=answer)

    outcome = agent.install(directory, home, log, record, launchd)
    loaded = read_bootstrapped_render(record)

    assert outcome.state is State.STALE
    assert loaded is not None and loaded.render_sha256 is None
    assert loaded.login_asid is None


#: One real `launchctl print` answer, captured on 2026-08-26 from the job this
#: item installed. The parse below is the one thing in this module that depends
#: on launchd's output shape, so it is held against a real one rather than only
#: against the fake that was written from it.
REAL_PRINT = """gui/501/com.gpt-voicecoding.codex-daemon = {
\tactive count = 0
\tpath = /Users/simon/Library/LaunchAgents/com.gpt-voicecoding.codex-daemon.plist
\ttype = LaunchAgent
\tstate = not running

\tprogram = /Users/simon/.codex/packages/standalone/current/codex
\targuments = {
\t\t/Users/simon/.codex/packages/standalone/current/codex
\t\tapp-server
\t\tdaemon
\t\tstart

\tasid = 100016
"""


def test_the_program_is_read_out_of_a_real_launchctl_answer() -> None:
    held = agent.Launchd(domain=DOMAIN, run=lambda _: (0, REAL_PRINT)).held_job()
    assert held is not None
    assert held.program == "/Users/simon/.codex/packages/standalone/current/codex"
    assert held.login_asid == 100_016
