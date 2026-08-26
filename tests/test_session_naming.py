"""How a Session Name is made: the pure composition, and the project half.

Two modules, one rule between them (#78). `_naming.compose` is where a project
and a title become a `SessionName` and is pure, so it is tested against strings.
`_project.ProjectNames` is the one place either lane runs `git`, and the command
is injected here for the reason the Claude lane injects its roster command: a
test that really shelled out would be measuring whichever repositories the
machine running it happens to have.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from gpt_voicecoding.adapters.agent._naming import compose
from gpt_voicecoding.adapters.agent._project import ProjectNames, _project_in
from gpt_voicecoding.seams.identity import SessionName


def answering(*answers: str | None) -> ProjectNames:
    """A resolver whose `git` says these things, in this order."""
    remaining = list(answers)

    async def ask(workspace: Path) -> str | None:
        del workspace
        return remaining.pop(0) if remaining else None

    return ProjectNames(ask=ask)


def resolved(names: ProjectNames, workspace: str) -> str | None:
    return asyncio.run(names.of(Path(workspace)))


class TestComposingOne:
    def test_the_two_halves_become_one_name(self) -> None:
        assert compose("GPT-VoiceCoding", "port the log") == SessionName(
            project="GPT-VoiceCoding", task="port the log"
        )

    def test_both_halves_are_stripped_because_both_are_somebody_elses_string(self) -> None:
        assert str(compose("  GPT-VoiceCoding ", " port the log\n")) == (
            "GPT-VoiceCoding · port the log"
        )

    def test_an_empty_title_is_no_name(self) -> None:
        """`legacy@1d32845:bridge/labels.py:100-102`, ported — and answered rather than raised."""
        assert compose("GPT-VoiceCoding", "   ") is None

    def test_a_title_spanning_two_lines_is_no_name(self) -> None:
        """A name the user cannot say back is not a name (`labels.py:103-105`)."""
        assert compose("GPT-VoiceCoding", "port the log\nand the rest") is None

    def test_a_title_with_a_carriage_return_is_no_name(self) -> None:
        assert compose("GPT-VoiceCoding", "port the log\rand the rest") is None

    def test_an_empty_project_is_no_name(self) -> None:
        assert compose("  ", "port the log") is None

    def test_a_half_carrying_the_separator_is_no_name(self) -> None:
        """It would not parse back into two halves, so it never becomes one."""
        assert compose("GPT-VoiceCoding", "port · the log") is None
        assert compose("a · b", "port the log") is None


class TestTheProjectHalf:
    def test_a_workspace_in_a_repository_is_named_for_the_repository(self) -> None:
        names = answering("/src/GPT-VoiceCoding/.git\n")
        assert resolved(names, "/src/GPT-VoiceCoding/tests") == "GPT-VoiceCoding"

    def test_a_worktree_is_named_for_the_repository_it_belongs_to(self) -> None:
        """`--git-common-dir` is the question precisely because it answers this."""
        names = answering("/src/GPT-VoiceCoding/.git\n")
        assert resolved(names, "/tmp/worktrees/78-naming") == "GPT-VoiceCoding"

    def test_a_workspace_outside_a_repository_is_named_for_its_own_directory(self) -> None:
        """*Adapted* from legacy, which raised here and left the Session unnamed."""
        assert resolved(answering(None), "/Users/simon/scratch") == "scratch"

    def test_a_git_that_could_not_be_asked_falls_back_rather_than_failing(self) -> None:
        assert resolved(answering(None), "/Users/simon/scratch") == "scratch"

    def test_a_row_with_no_workspace_at_all_has_no_project(self) -> None:
        """The lanes spell "the roster carried no cwd" as `Path()`."""
        assert asyncio.run(answering("/src/x/.git").of(Path())) is None

    def test_the_answer_is_remembered_so_git_is_asked_once_per_workspace(self) -> None:
        names = answering("/src/GPT-VoiceCoding/.git", "/src/somewhere-else/.git")
        first = resolved(names, "/src/GPT-VoiceCoding")
        second = resolved(names, "/src/GPT-VoiceCoding")
        assert (first, second) == ("GPT-VoiceCoding", "GPT-VoiceCoding")

    def test_two_paths_to_one_directory_are_one_entry(self) -> None:
        names = answering("/src/GPT-VoiceCoding/.git")
        assert resolved(names, "/src/GPT-VoiceCoding") == "GPT-VoiceCoding"
        assert resolved(names, "/src/GPT-VoiceCoding/") == "GPT-VoiceCoding"


class TestReadingWhatGitSaid:
    """Three shapes are refused, all of them legacy's (`labels.py:56-70`)."""

    def test_the_ordinary_answer(self) -> None:
        assert _project_in("/src/GPT-VoiceCoding/.git\n") == "GPT-VoiceCoding"

    def test_more_than_one_line_is_not_one_path(self) -> None:
        assert _project_in("/src/a/.git\n/src/b/.git\n") is None

    def test_a_relative_path_is_refused(self) -> None:
        """`--path-format=absolute` is passed, so a relative answer is not the one asked for."""
        assert _project_in(".git") is None

    def test_a_directory_that_is_not_dot_git_is_refused(self) -> None:
        assert _project_in("/src/GPT-VoiceCoding/somewhere-else") is None

    def test_nothing_is_refused(self) -> None:
        assert _project_in("   ") is None
