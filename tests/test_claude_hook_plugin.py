"""The hook plugin: what is rendered, and what installing and uninstalling mean.

The build issue asks that the hook "installs, uninstalls, and survives a settings
round trip". On the `--plugin-dir` route those read differently from how the
ticket imagined them, and the difference is the point: install is rendering two
files, uninstall is taking exactly those two back, and settings survive a round
trip trivially because no settings file is ever touched. That is asserted here as
a property rather than left as a claim.

Nothing here runs Claude Code. What a real Claude Code does with this directory —
load the hook for one session and no other — is the manual proof script's job.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from gpt_voicecoding.adapters.agent.claude.approval import HOOK_EVENT
from gpt_voicecoding.adapters.agent.claude.hook_plugin import (
    HOOK_MODULE,
    HOOK_PLUGIN_NAME,
    HOOK_TIMEOUT_SECONDS,
    MANIFEST_DIRECTORY,
    hook_plugin_manifest,
    hook_plugin_version,
    hooks_document,
    remove_hook_plugin,
    write_hook_plugin,
)
from gpt_voicecoding.adapters.agent.claude.plugin import PLUGIN_NAME


class TestWhatIsRendered:
    def test_the_hook_runs_this_package_s_own_module(self) -> None:
        """No separate script to install, so nothing can fall out of step."""
        command = hooks_document("/usr/bin/python3")["hooks"][HOOK_EVENT][0]["hooks"][0]["command"]
        assert command == f"/usr/bin/python3 -m {HOOK_MODULE}"

    def test_the_hook_is_offered_every_dialog(self) -> None:
        """No matcher: which prompts the user may answer by voice is not ours to narrow.

        The ceiling on what a spoken word may *grant* lives in `approval.py`.
        Deciding here which tools are askable would be this engine quietly
        editing the user's own permission prompts.
        """
        entry = hooks_document("python3")["hooks"][HOOK_EVENT][0]
        assert "matcher" not in entry

    def test_the_hook_is_given_claude_code_s_own_budget(self) -> None:
        hook = hooks_document("python3")["hooks"][HOOK_EVENT][0]["hooks"][0]
        assert hook["timeout"] == HOOK_TIMEOUT_SECONDS

    def test_the_manifest_declares_no_server_and_no_channel(self) -> None:
        """This plugin is the hook. The channel is a different plugin, deliberately."""
        manifest = hook_plugin_manifest("python3")
        assert "mcpServers" not in manifest
        assert "channels" not in manifest
        assert manifest["name"] == HOOK_PLUGIN_NAME

    def test_it_is_not_the_channel_plugin(self) -> None:
        """`--plugin-dir` has no marketplace and `plugin:name@market` needs one.

        One directory cannot be loaded both ways without being loaded twice, so
        the two names must differ — and they must keep differing.
        """
        assert HOOK_PLUGIN_NAME != PLUGIN_NAME

    def test_the_version_fingerprints_what_the_plugin_says(self) -> None:
        """A changed hook command is a new cache directory, by construction."""
        assert hook_plugin_version("python3") == hook_plugin_version("python3")
        assert hook_plugin_version("python3") != hook_plugin_version("/usr/bin/python3.13")


class TestInstallAndUninstall:
    def test_installing_lays_down_both_files_claude_code_reads(self, tmp_path: Path) -> None:
        write_hook_plugin(tmp_path, "python3")
        manifest = tmp_path / MANIFEST_DIRECTORY / "plugin.json"
        hooks = tmp_path / "hooks" / "hooks.json"
        assert json.loads(manifest.read_text())["name"] == HOOK_PLUGIN_NAME
        assert HOOK_EVENT in json.loads(hooks.read_text())["hooks"]

    def test_installing_twice_is_the_same_installation(self, tmp_path: Path) -> None:
        write_hook_plugin(tmp_path, "python3")
        first = (tmp_path / MANIFEST_DIRECTORY / "plugin.json").read_text()
        write_hook_plugin(tmp_path, "python3")
        assert (tmp_path / MANIFEST_DIRECTORY / "plugin.json").read_text() == first

    def test_uninstalling_takes_back_exactly_what_was_rendered(self, tmp_path: Path) -> None:
        directory = tmp_path / "hook-plugin"
        write_hook_plugin(directory, "python3")
        assert remove_hook_plugin(directory)
        assert not directory.exists()

    def test_uninstalling_refuses_a_directory_that_is_not_ours(self, tmp_path: Path) -> None:
        """An uninstall that took a caller's word for it is a recursive delete."""
        somebody_else = tmp_path / "not-ours"
        (somebody_else / MANIFEST_DIRECTORY).mkdir(parents=True)
        (somebody_else / MANIFEST_DIRECTORY / "plugin.json").write_text('{"name":"theirs"}')
        assert not remove_hook_plugin(somebody_else)
        assert somebody_else.exists()

    def test_uninstalling_something_that_was_never_installed_says_so(self, tmp_path: Path) -> None:
        assert not remove_hook_plugin(tmp_path / "absent")

    def test_nothing_outside_the_rendered_directory_is_ever_written(self, tmp_path: Path) -> None:
        """The whole reason this route was chosen over editing `~/.claude/settings.json`.

        "Survives a settings round trip" is satisfied by construction here: there
        is no settings file in the story at all, so there is nothing a round trip
        could lose.
        """
        directory = tmp_path / "rendered"
        write_hook_plugin(directory, "python3")
        written = {path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_file()}
        assert written == {
            Path("rendered") / MANIFEST_DIRECTORY / "plugin.json",
            Path("rendered") / "hooks" / "hooks.json",
        }


class TestTheTwoWaysThisCouldGoWrongQuietly:
    def test_an_interpreter_path_with_a_space_still_launches(self) -> None:
        """A hook command is a shell line, and real interpreter paths have spaces.

        Unquoted, `/Applications/My App/python3 -m ...` exits 127 and the only
        symptom is a permission dialog nobody ever answers — a failure this
        route reports as silence by design, which is exactly why it must be
        impossible rather than merely unlikely.
        """
        interpreter = "/Applications/My App/python3"
        command = hooks_document(interpreter)["hooks"][HOOK_EVENT][0]["hooks"][0]["command"]
        assert shlex.split(command) == [interpreter, "-m", HOOK_MODULE]

    def test_uninstalling_leaves_a_caller_s_own_files_alone(self, tmp_path: Path) -> None:
        """The plugin directory is a configured path, so it may not be ours alone.

        Removing the tree would take whatever else is in there with it. The
        plugin goes; anything else stays, and so does the directory holding it.
        """
        directory = tmp_path / "shared"
        directory.mkdir()
        theirs = directory / "somebody-elses-notes.md"
        theirs.write_text("not ours")
        write_hook_plugin(directory, "python3")

        assert remove_hook_plugin(directory)
        assert theirs.exists(), "an uninstall may not take a caller's own files"
        assert not (directory / MANIFEST_DIRECTORY).exists()
        assert not (directory / "hooks").exists()
