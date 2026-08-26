"""The installation boundary — ADR 0012, and ADR 0011's block inside it.

The load-bearing property is the round trip: an uninstall reproduces the file
that was there before the install, byte for byte, including the foreign hooks
beside ours and the ones *inside our own matcher group*. That is what makes it
safe to write into a file the user owns, and it is asserted here rather than
described, because a merge that eats somebody else's hook does not look wrong
until the day they need it.

Every `main` call below supplies a `home` and a `launchd`. Without the first, the
Codex item resolves the real `~/Library/LaunchAgents`; without the second, it
loads what it finds there into the real login session. Both happened while #83
was being written, which is why `conftest.py` now refuses the real `launchctl`
outright — these arguments are what a test says instead.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import FakeLaunchd, codex_home
from gpt_voicecoding.core.policy import CorePolicy
from gpt_voicecoding.installation import Outcome, State, read_intent, replace_text, write_intent
from gpt_voicecoding.installation import claude_hooks as hooks
from gpt_voicecoding.installation import codex_launch_agent as codex
from gpt_voicecoding.installation.__main__ import EXIT_FAILED, EXIT_OK, main

INTERPRETER = Path("/Applications/GPT-VoiceCoding.app/Contents/Resources/engine/bin/python3")
OTHER_INTERPRETER = Path("/opt/homebrew/bin/python3.12")

#: Two hooks that are not ours, in the shape a real settings file carries them:
#: one in an event we never touch, and one *inside the group we write into*.
FOREIGN = {
    "hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/local/bin/audit"}]}
        ],
        "PermissionRequest": [
            {"hooks": [{"type": "command", "command": "/usr/local/bin/watch-permissions"}]}
        ],
    },
    "model": "opus",
}


def config_directory(root: Path, document: dict | None = None) -> Path:
    """A Claude config directory, with a settings file when one is given."""
    directory = root / ".claude"
    directory.mkdir()
    if document is not None:
        (directory / "settings.json").write_text(
            json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return directory


def settings(directory: Path) -> str:
    return (directory / "settings.json").read_text(encoding="utf-8")


def _run(verb: str, environ: dict, base: Path, launchd: FakeLaunchd, home: Path) -> int:
    """One `bridge-install` verb, against nothing the machine running this owns."""
    return main(
        [verb],
        environ=environ,
        base_dir=base,
        interpreter=INTERPRETER,
        home=home,
        launchd=launchd.launchd,
    )


# -- the round trip ------------------------------------------------------


@pytest.mark.parametrize(
    "before",
    [
        pytest.param(None, id="no settings file at all"),
        pytest.param({}, id="an empty object"),
        pytest.param({"model": "opus"}, id="settings with no hooks key"),
        pytest.param(FOREIGN, id="two foreign hooks, one in our own event"),
    ],
)
def test_uninstall_reproduces_the_file_install_found(tmp_path: Path, before: dict | None) -> None:
    directory = config_directory(tmp_path, before)
    original = settings(directory) if before is not None else None

    assert hooks.install(directory, INTERPRETER).changed is True
    assert hooks.uninstall(directory).state is State.ABSENT

    if original is None:
        # Nothing was there to reproduce; what install created is an empty object.
        assert json.loads(settings(directory)) == {}
    else:
        assert settings(directory) == original


def test_a_foreign_handler_in_our_own_group_survives(tmp_path: Path) -> None:
    directory = config_directory(tmp_path, FOREIGN)
    hooks.install(directory, INTERPRETER)

    groups = json.loads(settings(directory))["hooks"]["PermissionRequest"]
    commands = [handler["command"] for group in groups for handler in group["hooks"]]
    assert "/usr/local/bin/watch-permissions" in commands
    assert any(hooks.APPROVAL_MODULE in command for command in commands)


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            "/usr/bin/logger 'gpt_voicecoding.hook ran'",
            id="a quoted phrase shlex hands back as one token",
        ),
        pytest.param(
            "/usr/bin/logger gpt_voicecoding.audit",
            id="a command that names one of our modules without running it",
        ),
        pytest.param(
            "/usr/bin/python3 -c 'import gpt_voicecoding.audit'",
            id="a command that imports one of our modules its own way",
        ),
    ],
)
def test_a_command_that_does_not_run_our_module_is_not_ours(tmp_path: Path, command: str) -> None:
    """Naming one of our modules is not running it.

    The identity is the program the command runs; the program here is an
    interpreter, so the identity is the module it is *told to run* — and `-m` is
    what "told to run" looks like. A rule that recognised any of these would take
    somebody else's hook back out with ours.
    """
    mentions = {
        "hooks": {"PermissionRequest": [{"hooks": [{"type": "command", "command": command}]}]}
    }
    directory = config_directory(tmp_path, mentions)
    original = settings(directory)

    hooks.install(directory, INTERPRETER)
    assert command in settings(directory), "install took out a hook that was not ours"
    hooks.uninstall(directory)
    assert settings(directory) == original


# -- idempotence and staleness -------------------------------------------


def test_a_second_install_writes_nothing(tmp_path: Path) -> None:
    directory = config_directory(tmp_path, FOREIGN)
    hooks.install(directory, INTERPRETER)
    written = settings(directory)

    again = hooks.install(directory, INTERPRETER)
    assert (again.state, again.changed, again.ok) == (State.CURRENT, False, True)
    assert settings(directory) == written


def test_a_moved_bundle_is_stale_and_is_rewritten(tmp_path: Path) -> None:
    directory = config_directory(tmp_path, FOREIGN)
    hooks.install(directory, INTERPRETER)

    assert hooks.inspect(directory, OTHER_INTERPRETER).state is State.STALE
    rewritten = hooks.install(directory, OTHER_INTERPRETER)
    assert (rewritten.state, rewritten.changed) == (State.CURRENT, True)
    assert str(OTHER_INTERPRETER) in settings(directory)
    assert str(INTERPRETER) not in settings(directory)


def test_the_hook_command_names_the_interpreter_it_was_given(tmp_path: Path) -> None:
    """#38 is a hook command naming a developer checkout's interpreter. The only
    defence is that no interpreter is ever written down — it is passed in."""
    directory = config_directory(tmp_path, {})
    hooks.install(directory, INTERPRETER)

    group = json.loads(settings(directory))["hooks"]["PermissionRequest"][0]
    assert group["hooks"][0]["command"] == f"{INTERPRETER} -m {hooks.APPROVAL_MODULE}"


def test_the_hook_timeout_is_not_below_the_approval_budget() -> None:
    """Claude Code's ceiling must outlast Bridge Core's budget, or Claude Code
    gives up on a dialog the engine is still holding open for the user."""
    assert hooks.APPROVAL_TIMEOUT_SECONDS >= CorePolicy().approval_budget_seconds


# -- refusals ------------------------------------------------------------


def test_no_config_directory_is_not_a_failure(tmp_path: Path) -> None:
    """A user who does not run Claude Code is not a failed install."""
    outcome = hooks.install(tmp_path / "absent", INTERPRETER)
    assert (outcome.ok, outcome.state, outcome.changed) == (True, State.ABSENT, False)


def test_a_settings_file_that_is_not_json_is_refused_untouched(tmp_path: Path) -> None:
    directory = config_directory(tmp_path)
    (directory / "settings.json").write_text("{not json", encoding="utf-8")

    outcome = hooks.install(directory, INTERPRETER)
    assert outcome.ok is False
    assert settings(directory) == "{not json"


def test_a_concurrent_writer_is_reported_and_not_retried(tmp_path: Path, monkeypatch) -> None:
    """ADR 0011's untested exposure, made testable.

    Atomicity cannot stop a lost update. What this asserts is the honest half:
    when what landed is not what we wrote, the run says so and stops.
    """
    target = tmp_path / "settings.json"
    real_replace = os.replace

    def replace_then_clobber(source, destination):  # noqa: ANN001 - patching os.replace
        real_replace(source, destination)
        Path(destination).write_text("somebody else got here", encoding="utf-8")

    monkeypatch.setattr("gpt_voicecoding.installation.os.replace", replace_then_clobber)
    failure = replace_text(target, "what this run wrote")

    assert "another process wrote this file at the same time" in failure
    assert str(target) in failure


# -- the recorded intent -------------------------------------------------


def test_reconcile_installs_on_a_first_run_and_records_it(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    directory = config_directory(tmp_path, FOREIGN)
    base = tmp_path / "support"

    assert read_intent(base).first_run is True
    code = main(
        ["reconcile"],
        environ={"CLAUDE_CONFIG_DIR": str(directory)},
        base_dir=base,
        interpreter=INTERPRETER,
        home=tmp_path,
        launchd=launchd.launchd,
    )

    assert code == EXIT_OK
    assert read_intent(base).wanted is True
    assert hooks.APPROVAL_MODULE in settings(directory)


def test_reconcile_leaves_an_uninstalled_machine_alone(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """Without this, the next launch puts back what the user just took away."""
    directory = config_directory(tmp_path, FOREIGN)
    base = tmp_path / "support"
    environ = {"CLAUDE_CONFIG_DIR": str(directory)}

    _run("install", environ, base, launchd, tmp_path)
    _run("uninstall", environ, base, launchd, tmp_path)
    untouched = settings(directory)

    assert read_intent(base).wanted is False
    assert _run("reconcile", environ, base, launchd, tmp_path) == EXIT_OK
    assert settings(directory) == untouched


def test_install_overrides_a_recorded_uninstall(tmp_path: Path, launchd: FakeLaunchd) -> None:
    directory = config_directory(tmp_path, FOREIGN)
    base = tmp_path / "support"
    environ = {"CLAUDE_CONFIG_DIR": str(directory)}

    write_intent(False, base)
    assert _run("install", environ, base, launchd, tmp_path) == EXIT_OK
    assert read_intent(base).wanted is True
    assert hooks.APPROVAL_MODULE in settings(directory)


def test_a_failed_uninstall_does_not_record_that_it_worked(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """`wanted: false` is what stops every later reconcile from touching this
    machine. Recorded over a failed uninstall, it would leave our hooks in the
    user's file with nothing left that would ever repair or remove them."""
    directory = config_directory(tmp_path, FOREIGN)
    base = tmp_path / "support"
    environ = {"CLAUDE_CONFIG_DIR": str(directory)}

    _run("install", environ, base, launchd, tmp_path)
    (directory / "settings.json").write_text("{not json any more", encoding="utf-8")

    assert _run("uninstall", environ, base, launchd, tmp_path) == (EXIT_FAILED)
    assert read_intent(base).wanted is True, "an uninstall that failed recorded that it worked"


def test_a_failed_install_still_records_the_want(tmp_path: Path, launchd: FakeLaunchd) -> None:
    """The other direction, and the asymmetry is deliberate: this file holds what
    the user wants, and a failed install is a want the next reconcile retries."""
    directory = config_directory(tmp_path)
    (directory / "settings.json").write_text("{not json", encoding="utf-8")
    base = tmp_path / "support"

    main(
        ["install"],
        environ={"CLAUDE_CONFIG_DIR": str(directory)},
        base_dir=base,
        interpreter=INTERPRETER,
        home=tmp_path,
        launchd=launchd.launchd,
    )
    assert read_intent(base).wanted is True


def test_status_writes_nothing(tmp_path: Path, launchd: FakeLaunchd) -> None:
    directory = config_directory(tmp_path, FOREIGN)
    base = tmp_path / "support"
    original = settings(directory)

    code = main(
        ["status"],
        environ={"CLAUDE_CONFIG_DIR": str(directory)},
        base_dir=base,
        interpreter=INTERPRETER,
        home=tmp_path,
        launchd=launchd.launchd,
    )

    assert code == EXIT_OK
    assert settings(directory) == original
    assert read_intent(base).first_run is True


def test_a_failed_item_makes_the_run_fail(tmp_path: Path, launchd: FakeLaunchd) -> None:
    directory = config_directory(tmp_path)
    (directory / "settings.json").write_text("{not json", encoding="utf-8")

    code = main(
        ["install"],
        environ={"CLAUDE_CONFIG_DIR": str(directory)},
        base_dir=tmp_path / "support",
        interpreter=INTERPRETER,
        home=tmp_path,
        launchd=launchd.launchd,
    )
    assert code == EXIT_FAILED


def test_an_unreadable_record_reads_as_never_recorded(tmp_path: Path) -> None:
    base = tmp_path / "support"
    base.mkdir()
    (base / "installation.json").write_text("half a fi", encoding="utf-8")

    assert read_intent(base).first_run is True


def test_a_failed_outcome_says_so_on_its_line() -> None:
    line = Outcome("claude-hooks", State.STALE, ok=False, note="a reason").line()
    assert line == "claude-hooks: FAILED — a reason"


# -- both items, on the one boundary -------------------------------------


def test_a_clean_install_lands_both_items(tmp_path: Path, launchd: FakeLaunchd) -> None:
    """ADR 0012's registry is a list of calls, and this is the list being two."""
    directory = config_directory(tmp_path, FOREIGN)
    codex_home(tmp_path)
    base = tmp_path / "support"

    assert (
        _run("install", {"CLAUDE_CONFIG_DIR": str(directory)}, base, launchd, tmp_path) == EXIT_OK
    )

    assert hooks.APPROVAL_MODULE in settings(directory)
    assert codex.plist_path(tmp_path / "Library" / "LaunchAgents").exists()
    assert launchd.held is True


def test_an_uninstall_takes_both_items_back(tmp_path: Path, launchd: FakeLaunchd) -> None:
    directory = config_directory(tmp_path, FOREIGN)
    codex_home(tmp_path)
    base, environ = tmp_path / "support", {"CLAUDE_CONFIG_DIR": str(directory)}
    before = settings(directory)

    _run("install", environ, base, launchd, tmp_path)
    assert _run("uninstall", environ, base, launchd, tmp_path) == EXIT_OK

    assert settings(directory) == before
    assert not codex.plist_path(tmp_path / "Library" / "LaunchAgents").exists()
    assert launchd.held is True, "the uninstall stopped a daemon the user's Sessions are on"


def test_a_reconcile_that_agrees_writes_nothing_and_asks_launchd_for_nothing(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    directory = config_directory(tmp_path, FOREIGN)
    codex_home(tmp_path)
    base, environ = tmp_path / "support", {"CLAUDE_CONFIG_DIR": str(directory)}
    _run("install", environ, base, launchd, tmp_path)
    settled = settings(directory)
    plist = codex.plist_path(tmp_path / "Library" / "LaunchAgents").read_bytes()
    launchd.commands.clear()

    assert _run("reconcile", environ, base, launchd, tmp_path) == EXIT_OK

    assert settings(directory) == settled
    assert codex.plist_path(tmp_path / "Library" / "LaunchAgents").read_bytes() == plist
    assert "bootstrap" not in launchd.verbs


def test_one_item_refusing_does_not_take_the_other_back_out(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """ADR 0012: nothing rolls back. A Claude hook block that landed is not made
    wrong by a LaunchAgent that did not, and the run still exits non-zero."""
    directory = config_directory(tmp_path, FOREIGN)
    codex_home(tmp_path)
    launchd.refuses = True

    code = _run(
        "install", {"CLAUDE_CONFIG_DIR": str(directory)}, tmp_path / "support", launchd, tmp_path
    )

    assert code == EXIT_FAILED
    assert hooks.APPROVAL_MODULE in settings(directory), "a refused item took a landed one with it"


def test_a_user_with_no_codex_package_is_not_a_failed_install(
    tmp_path: Path, launchd: FakeLaunchd
) -> None:
    """The mirror of "no Claude config directory": a machine with only one of the
    two agents on it is a machine this product installs onto successfully."""
    directory = config_directory(tmp_path, FOREIGN)
    codex_home(tmp_path, managed=False)

    code = _run(
        "install", {"CLAUDE_CONFIG_DIR": str(directory)}, tmp_path / "support", launchd, tmp_path
    )

    assert code == EXIT_OK
    assert launchd.commands == []
    assert not codex.plist_path(tmp_path / "Library" / "LaunchAgents").exists()


def test_status_reports_both_items_and_the_daemon(
    tmp_path: Path, launchd: FakeLaunchd, capsys
) -> None:
    """#83: start and version errors surface through the existing vocabulary."""
    directory = config_directory(tmp_path, FOREIGN)
    codex_home(tmp_path, managed=False)

    assert _run("status", {"CLAUDE_CONFIG_DIR": str(directory)}, tmp_path, launchd, tmp_path) == (
        EXIT_OK
    )

    said = capsys.readouterr()
    printed = said.out + said.err
    assert hooks.NAME in printed
    assert printed.count(codex.NAME) >= 2, "the item's line, and the daemon's own"
    assert "no managed Codex binary" in printed
