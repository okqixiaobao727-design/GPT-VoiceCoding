"""Preflight's refusal to grade a walk another Codex TUI is already sitting in (#228).

Why a foreign TUI makes a walk ungradeable is `support.foreign_codex_refusal`'s
own docstring and is not restated here. What is graded is the refusal, and
because it is a reading of the process table it is pinned against a faked one:
no real `codex` is started by this file, and none is needed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
import support

#: What the injected clock says, so the elapsed time in a refusal is an exact
#: string rather than whatever the test machine took to get here.
NOW = 1_787_700_000.0

#: A `codex` as this machine installs it, the same reading
#: `tests/test_codex_processes.py` was built on.
CODEX = "/Users/simon/.nvm/versions/node/v24.13.0/lib/node_modules/@openai/codex/bin/codex"

#: A workspace no acceptance run owns: the maintainer's own checkout, which is
#: where run `20260904T091550Z`'s foreign TUI (`GPT-VoiceCoding · 实现 #223`) sat.
FOREIGN_WORKSPACE = "/Users/simon/Documents/coding/GPT-VoiceCoding"


def process_table(listing: str, cwds: dict[int, str]) -> Callable[[list[str]], Awaitable[str]]:
    """A `ps` and an `lsof` that answer from these rows and nothing else.

    Shaped exactly like `tests/test_codex_processes.py`'s, because what the
    preflight injects is the adapter's own `Runner` and the two commands it runs.
    """

    async def run(argv: list[str]) -> str:
        if argv[0].endswith("ps"):
            return listing
        pid = int(argv[argv.index("-p") + 1])
        return f"p{pid}\nfcwd\nn{cwds[pid]}\n" if pid in cwds else ""

    return run


@pytest.fixture(autouse=True)
def _no_inherited_acceptance_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default acceptance root, whatever the shell running the tests set.

    `foreign_codex_refusal` decides what this run owns from `acceptance_root()`,
    which reads `GPTVOICECODING_ACCEPTANCE_ROOT`. That override exists to point a
    run at another tree — a developer with it set to `~/Documents/coding` would
    otherwise see `FOREIGN_WORKSPACE` classified as owned, and the refusal cases
    below would go green by inheriting an environment rather than by passing.
    """
    monkeypatch.delenv(support.ACCEPTANCE_ROOT_VARIABLE, raising=False)


def refusal(listing: str, cwds: dict[int, str]) -> str | None:
    return support.foreign_codex_refusal(
        run=process_table(listing, cwds),  # type: ignore[arg-type]
        now=lambda: NOW,
    )


class TestAForeignSessionStopsTheRun:
    def test_a_live_tui_is_named_by_pid_workspace_and_how_long_it_has_been_up(self) -> None:
        """The operator needs to find the window, so all three are in the text."""
        reason = refusal(f"  101 10 ttys001 02:30:00 {CODEX}\n", {101: FOREIGN_WORKSPACE})

        assert reason is not None
        assert "101" in reason
        assert FOREIGN_WORKSPACE in reason
        assert "2h30m" in reason

    def test_the_refusal_says_the_one_action_that_unblocks_it(self) -> None:
        """The wording #228 asked for, pinned as text rather than as the constant.

        A refusal that only says the run is blocked leaves the operator to work
        out what a Codex Session has to do with it.
        """
        reason = refusal(f"  101 10 ttys001 02:30:00 {CODEX}\n", {101: FOREIGN_WORKSPACE})

        assert reason is not None
        assert "quit these codex sessions and re-run" in reason.lower()

    def test_every_live_tui_is_named_rather_than_the_first(self) -> None:
        """Two windows open is one round trip, not two runs refused in turn."""
        reason = refusal(
            f"  101 10 ttys001 00:30 {CODEX}\n  102 10 ttys002 00:30 {CODEX} resume --last\n",
            {101: FOREIGN_WORKSPACE, 102: "/Users/simon/Documents/coding/other"},
        )

        assert reason is not None
        assert "101" in reason
        assert "102" in reason


class TestAMachineThisRunCanIsolate:
    def test_an_empty_process_table_does_not_refuse(self) -> None:
        assert refusal("", {}) is None

    def test_a_job_wearing_the_name_is_not_a_session(self) -> None:
        """`codex mcp-server` is what Claude Code starts, one per session.

        The judgement is the adapter's and is graded in
        `tests/test_codex_processes.py`; what this pins is that preflight asks it
        rather than counting processes called `codex`.
        """
        assert refusal(f"  101 10 ttys001 00:30 {CODEX} mcp-server\n", {101: "/tmp/x"}) is None

    def test_a_session_in_this_runs_own_tree_is_not_foreign(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The walk hand-starts its TUIs in a workspace under the acceptance root.

        This check runs before the first hand-start, so in a real run there is
        nothing of the walk's to see yet. The rule is here anyway because the
        acceptance criterion is that a Session the walk hand-started is
        *never* counted, and an ordering nobody can see is not a rule.
        """
        monkeypatch.setenv(support.ACCEPTANCE_ROOT_VARIABLE, str(tmp_path))
        workspace = support.workspace_path(tmp_path / "20260905T000000Z", "claude")

        assert refusal(f"  101 10 ttys001 00:30 {CODEX}\n", {101: str(workspace)}) is None


class TestNotBeingAbleToLook:
    @pytest.mark.parametrize("unreadable", [OSError("no /bin/ps"), TimeoutError()])
    def test_a_process_table_that_cannot_be_read_refuses(self, unreadable: Exception) -> None:
        """Not being able to look is not the same as there being nothing to see.

        A verdict that cannot be attributed to this engine is worse than no
        verdict (`tests/acceptance/conftest.py`'s docstring), and a run that
        could not check whether it had the machine to itself cannot attribute one.
        """

        async def run(argv: list[str]) -> str:
            raise unreadable

        reason = support.foreign_codex_refusal(run=run, now=lambda: NOW)  # type: ignore[arg-type]

        assert reason is not None
        assert "process table" in reason
