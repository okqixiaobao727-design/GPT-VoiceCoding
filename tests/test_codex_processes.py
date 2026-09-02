"""Which `codex` processes are Sessions, and which are jobs wearing the same name.

Every argument vector here was taken off this machine on 2026-08-26, and the
reason this file exists is what that reading showed: `pgrep -x codex` returned
five processes and **none** of them was a Session. A roster built on the process
name would have invented five.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from gpt_voicecoding.adapters.agent.codex.processes import (
    START_TIME_RESOLUTION_SECONDS,
    Candidate,
    elapsed_seconds,
    enumerate_sessions,
    is_interactive,
    session_id_from_argv,
)

#: What `self.found` below pretends the clock says, so a start time computed
#: from an elapsed reading is an exact number in an assertion.
NOW = 1_787_700_000.0

#: How long a `ps` takes in the test below. Longer than the one second the
#: comparison allows, because what it stands for is a machine under an
#: acceptance run's own load — the case the allowance does not cover.
SLOW_PS_SECONDS = 1.5

CODEX = "/Users/simon/.nvm/versions/node/v24.13.0/lib/node_modules/@openai/codex/bin/codex"

THREAD = "01a03b06-f995-7b60-bc9f-e2152ee4ed32"

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
        return asyncio.run(
            enumerate_sessions(run=self.build(listing, cwds), now=lambda: NOW)  # type: ignore[arg-type]
        )

    def test_a_session_is_reported_by_pid_workspace_and_start_time(self) -> None:
        rows = self.found(f"  101 10 ttys001 00:05 {CODEX}\n", {101: "/tmp/workspace"})
        assert rows == (
            Candidate(
                pid=101,
                workspace=Path("/tmp/workspace"),
                started_at=NOW - 5,
            ),
        )

    def test_a_bare_codex_carries_no_thread_id_and_is_still_a_candidate(self) -> None:
        """#201: the ordinary hand-started TUI. It used to be read and thrown away."""
        rows = self.found(f"  101 10 ttys001 00:05 {CODEX} fix the bug\n", {101: "/tmp/w"})
        assert [(row.pid, row.session_id) for row in rows] == [(101, None)]

    def test_the_clock_is_read_once_for_the_whole_pass(self) -> None:
        """Every `etime` came out of one `ps`, so one moment has to date them all.

        The `lsof` between two candidates is a subprocess and can take seconds.
        A clock read per candidate would date the second one's elapsed reading
        against a later moment and push its start forward by the whole lookup,
        past the one second `START_TIME_RESOLUTION_SECONDS` allows — and a real
        hand-started root would read as predating its own terminal and be
        dropped, which is the bug this whole change exists to fix.

        **The clock ticks here, and that is the whole point of the fixture.**
        Every other test in this class injects a constant `now`, under which a
        clock read once and a clock read per candidate answer identically — so
        none of them can tell the two implementations apart, and the regression
        would come back unseen. This one hands out a later moment on every call,
        so the assertion is that the second candidate was dated by the first
        call rather than by its own.
        """
        ticking = iter([NOW, NOW + 10, NOW + 20, NOW + 30])
        listing = "\n".join(
            (
                f"  101 10 ttys001 00:05 {CODEX}",
                f"  102 10 ttys002 00:05 {CODEX}",
            )
        )

        rows = asyncio.run(
            enumerate_sessions(
                run=self.build(listing, {101: "/tmp/one", 102: "/tmp/two"}),  # type: ignore[arg-type]
                now=lambda: next(ticking),
            )
        )

        assert [row.started_at for row in rows] == [NOW - 5, NOW - 5]

    def test_a_slow_ps_cannot_make_a_terminal_younger_than_its_own_thread(self) -> None:
        """The drift that dropped a live Session on run `20260902T071547Z`.

        `etime` is sampled by `ps` when `ps` reads the process table; the clock
        this module subtracts it from is read in this process. Every moment
        between those two readings is added to the computed start, and awaiting
        a subprocess on a loaded machine is exactly such a moment: two lanes,
        two engines and two TUIs at once measured well past the one second
        `START_TIME_RESOLUTION_SECONDS` allows. The cost is the bug #201 exists
        to prevent, met from the other side — a terminal read as *younger* than
        the thread it started, so `roster.py`'s start-time filter rules it out
        as that thread's owner and a live Session drops off the roster. It did:
        the TUI started at 19:15:56.366 and its rollout is stamped
        `2026-09-02T19-15-56`, the same second.

        So the clock is read **before** `ps` is launched, and the assertion is
        the roster's own predicate rather than a number: a thread created just
        after this terminal must not read as predating it.
        """
        created = NOW - 5 + 0.4  # the thread this terminal opened, the same second
        # The clock moves *because* `ps` was awaited, which is what makes the
        # order of the two readings the thing under test: a clock read before
        # the launch answers NOW, one read after it answers NOW + 1.5.
        reading = [NOW]

        async def run(argv: list[str]) -> str:
            if argv[0].endswith("ps"):
                reading[0] += SLOW_PS_SECONDS
                return f"  101 10 ttys001 00:05 {CODEX}\n"
            return "p101\nfcwd\nn/tmp/workspace\n"

        rows = asyncio.run(
            enumerate_sessions(run=run, now=lambda: reading[0])  # type: ignore[arg-type]
        )

        started = rows[0].started_at
        assert started == NOW - 5
        assert not created < started - START_TIME_RESOLUTION_SECONDS

    def test_a_line_whose_elapsed_time_cannot_be_read_is_skipped(self) -> None:
        assert self.found(f"  101 10 ttys001 later {CODEX}\n", {101: "/tmp/w"}) == ()

    def test_a_resumed_uuid_is_the_processes_exact_native_identity(self) -> None:
        rows = self.found(
            f"  101 10 ttys001 00:05 {CODEX} resume {THREAD}\n",
            {101: "/tmp/workspace"},
        )
        assert rows[0].session_id == THREAD

    def test_a_resume_name_is_not_an_exact_native_identity(self) -> None:
        assert session_id_from_argv([CODEX, "resume", "named-session"]) is None

    def test_a_fork_uuid_names_the_source_not_the_new_session(self) -> None:
        assert session_id_from_argv([CODEX, "fork", THREAD]) is None

    def test_only_a_process_with_a_controlling_terminal_is_a_session(self) -> None:
        """The detached shape captured by #144 is not a live interactive run."""
        listing = "\n".join(
            (
                f"  101 10 ttys001 00:05 {CODEX}",
                f"  102 1 ?? 00:05 {CODEX}",
            )
        )

        rows = self.found(listing, {101: "/tmp/live", 102: "/tmp/detached"})

        assert [row.pid for row in rows] == [101]

    def test_the_jobs_beside_it_are_left_out(self) -> None:
        listing = "\n".join(
            (
                f"  101 10 ttys001 00:05 {CODEX}",
                f"  102 10 ttys002 00:05 {CODEX} mcp-server",
                f"  103 10 ttys003 00:05 {' '.join(CHATGPT_APP_SERVER)}",
                "  104 10 ttys004 00:05 /usr/bin/python3 codex",
            )
        )
        rows = self.found(listing, {n: "/tmp" for n in (101, 102, 103, 104)})
        assert [row.pid for row in rows] == [101]

    def test_a_process_whose_cwd_cannot_be_read_is_left_out(self) -> None:
        """It ended between the listing and the lookup, or it is not ours."""
        assert self.found(f"  101 10 ttys001 00:05 {CODEX}\n", {}) == ()

    def test_no_codex_at_all_is_an_empty_answer_not_a_failure(self) -> None:
        assert self.found("  1 0 ?? 00:05 /sbin/launchd\n", {}) == ()

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


class TestElapsedTime:
    """`etime`'s POSIX form, `[[dd-]hh:]mm:ss`, and nothing else."""

    def test_minutes_and_seconds(self) -> None:
        assert elapsed_seconds("07:12") == 432.0

    def test_hours_minutes_and_seconds(self) -> None:
        assert elapsed_seconds("06:47:42") == 24462.0

    def test_days_hours_minutes_and_seconds(self) -> None:
        assert elapsed_seconds("2-06:47:42") == 197262.0

    def test_a_shape_this_build_cannot_read_is_no_answer(self) -> None:
        assert elapsed_seconds("Wed  2 Sep 08:52:06 2026") is None
        assert elapsed_seconds("") is None
