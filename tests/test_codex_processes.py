"""Which `codex` processes are Sessions, and which are jobs wearing the same name.

Every argument vector here was taken off this machine on 2026-08-26, and the
reason this file exists is what that reading showed: `pgrep -x codex` returned
five processes and **none** of them was a Session. A roster built on the process
name would have invented five.
"""

from __future__ import annotations

import asyncio

from gpt_voicecoding.adapters.agent.codex.processes import (
    Candidate,
    enumerate_sessions,
    is_interactive,
)

CODEX = "/Users/simon/.nvm/versions/node/v24.13.0/lib/node_modules/@openai/codex/bin/codex"

#: ChatGPT.app's own app-server, verbatim and shortened. The `-c` value is the
#: whole point: it sits where the subcommand would be.
CHATGPT_APP_SERVER = [
    "/Applications/ChatGPT.app/Contents/Resources/codex",
    "-c",
    "features.code_mode_host=true",
    "app-server",
    "--analytics-default-enabled",
]


class TestJobsThatAreNotSessions:
    def test_an_mcp_server_is_not_a_session(self) -> None:
        """Claude Code starts one of these per session; there were four."""
        assert not is_interactive([CODEX, "mcp-server"])

    def test_the_chatgpt_app_server_is_not_a_session(self) -> None:
        """The measured false positive: `-c`'s value landed in the subcommand slot."""
        assert not is_interactive(CHATGPT_APP_SERVER)

    def test_a_headless_exec_run_is_not_a_session(self) -> None:
        """Headless runs started by other tools are out of scope for v1.0."""
        assert not is_interactive([CODEX, "exec", "do the thing"])

    def test_an_alias_counts_as_its_subcommand(self) -> None:
        assert not is_interactive([CODEX, "e", "do the thing"])

    def test_a_subcommand_after_a_value_taking_option_is_still_found(self) -> None:
        assert not is_interactive([CODEX, "-C", "/tmp", "review"])

    def test_a_subcommand_after_a_self_contained_option_is_still_found(self) -> None:
        assert not is_interactive([CODEX, "--config=a.b=c", "review"])


class TestSessions:
    def test_a_bare_codex_is_a_session(self) -> None:
        assert is_interactive([CODEX])

    def test_codex_with_only_flags_is_a_session(self) -> None:
        assert is_interactive([CODEX, "--search", "-m", "gpt-5"])

    def test_a_prompt_opens_a_session(self) -> None:
        """`codex --help` lists `[PROMPT]` as an optional positional."""
        assert is_interactive([CODEX, "fix", "the", "bug"])

    def test_resume_opens_a_session(self) -> None:
        assert is_interactive([CODEX, "resume", "--last"])

    def test_fork_opens_a_session(self) -> None:
        assert is_interactive([CODEX, "fork", "--last"])


class TestReadingTheProcessTable:
    """The two commands are injected: what is under test is the reading."""

    def build(self, listing: str, cwds: dict[int, str]) -> object:
        async def run(argv: list[str]) -> str:
            if argv[0].endswith("ps"):
                return listing
            pid = int(argv[argv.index("-p") + 1])
            return f"p{pid}\nfcwd\nn{cwds[pid]}\n" if pid in cwds else ""

        return run

    def found(self, listing: str, cwds: dict[int, str]) -> tuple[Candidate, ...]:
        return asyncio.run(enumerate_sessions(run=self.build(listing, cwds)))  # type: ignore[arg-type]

    def test_a_session_is_reported_by_pid_and_workspace(self) -> None:
        rows = self.found(f"  101 {CODEX}\n", {101: "/tmp/workspace"})
        assert rows == (Candidate(pid=101, workspace=__import__("pathlib").Path("/tmp/workspace")),)

    def test_the_jobs_beside_it_are_left_out(self) -> None:
        listing = "\n".join(
            (
                f"  101 {CODEX}",
                f"  102 {CODEX} mcp-server",
                f"  103 {' '.join(CHATGPT_APP_SERVER)}",
                "  104 /usr/bin/python3 codex",
            )
        )
        rows = self.found(listing, {n: "/tmp" for n in (101, 102, 103, 104)})
        assert [row.pid for row in rows] == [101]

    def test_a_process_whose_cwd_cannot_be_read_is_left_out(self) -> None:
        """It ended between the listing and the lookup, or it is not ours."""
        assert self.found(f"  101 {CODEX}\n", {}) == ()

    def test_no_codex_at_all_is_an_empty_answer_not_a_failure(self) -> None:
        assert self.found("  1 /sbin/launchd\n", {}) == ()

    def test_a_line_the_table_wrapped_or_mangled_is_skipped(self) -> None:
        assert self.found("not a process line\n\n", {}) == ()

    def test_a_process_table_that_cannot_be_read_raises_to_the_caller(self) -> None:
        """Not being able to look is a lane error, and the lane decides that."""

        async def run(argv: list[str]) -> str:
            del argv
            raise OSError("no ps on this machine")

        try:
            asyncio.run(enumerate_sessions(run=run))  # type: ignore[arg-type]
        except OSError:
            return
        raise AssertionError("a process table that cannot be read must not read as empty")
