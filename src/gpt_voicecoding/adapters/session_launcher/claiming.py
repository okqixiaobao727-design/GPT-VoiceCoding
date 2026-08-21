"""Which live Claude Session is the one this launch just started.

A launch has to answer with the exact identity Bridge Core will register, and a
Claude target is addressed by pid *and* session id. Neither is knowable in
advance: Claude Code mints the session id itself and writes it, with its cwd,
into one record per live process under its own registry directory.

So the launcher reads that registry and **claims** a record. Claiming is where a
phantom registration would come from, so it is built out of three filters and a
refusal rather than out of a match:

1. **It must be new.** The set of records present is snapshotted *before* the
   spawn, and only a record that was not in it can be a candidate. Deliberately
   a snapshot rather than a timestamp: Claude Code rewrites a record every time
   its status moves, so every live Session's file has a fresh mtime at all times
   and "modified after we spawned" would match all of them.

2. **It must be ours by descent.** The claimed pid has to be the process we
   spawned or one of its descendants. This is the only *positive* evidence of
   ownership in the list — workspace and novelty merely narrow the field — and
   it is what makes the claim survive pid reuse. It matters in practice because
   the pid that registers is often not the pid that was spawned: under tmux the
   pane runs a shell and Claude Code is its child.

3. **It must be in the workspace that was asked for**, compared through
   `realpath` on both sides. On Darwin `/tmp` is a symlink to `/private/tmp`, so
   a launch into `/tmp/x` is registered as being in `/private/tmp/x`; comparing
   the two as strings would fail every correct launch.

And then: **two candidates is a refusal, not a choice.** A workspace can easily
hold a Session this launch did not start — one the user opened, or one an
earlier launch left behind — and picking from among them is how a launch comes
to register somebody else's Session as its own. Refusing names the ambiguity.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path

from gpt_voicecoding.adapters.agent.claude.registry import SessionRecord, records

#: How deep an ancestry walk goes before it gives up. A process is a handful of
#: levels below the launcher at most (a shell, then the agent); a bound exists so
#: that a cycle or a wrong answer from the process table cannot spin forever.
MAX_ANCESTRY_DEPTH = 16

#: How often the registry is re-read while waiting for a Session to appear.
#: Claude Code writes the record during its own startup, so this is a process
#: start rather than a wire, and reading faster would not see it sooner.
CLAIM_POLL_SECONDS = 0.2


class ClaimError(Exception):
    """No record could be claimed, or more than one could."""


#: How the parent of a pid is found. Injectable so a test can state a process
#: tree instead of building one.
ParentOf = Callable[[int], "int | None"]


def snapshot(directory: Path) -> frozenset[int]:
    """Which Sessions were already registered. Taken *before* a spawn, always."""
    try:
        return frozenset(int(path.stem) for path in directory.glob("*.json") if path.stem.isdigit())
    except OSError:
        return frozenset()


def parent_from_process_table(pid: int) -> int | None:
    """The parent of one pid, asked of the operating system's own process table.

    `ps` rather than `/proc`, because this system's platform is Darwin and there
    is no `/proc` there to read.
    """
    try:
        finished = subprocess.run(
            ["/bin/ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    answer = finished.stdout.strip()
    return int(answer) if answer.isdigit() else None


def is_descendant(pid: int, ancestor: int, *, parent_of: ParentOf) -> bool:
    """Whether `pid` is `ancestor` itself or something it started, however deep."""
    seen: set[int] = set()
    walking: int | None = pid
    for _ in range(MAX_ANCESTRY_DEPTH):
        if walking is None or walking in seen:
            return False
        if walking == ancestor:
            return True
        seen.add(walking)
        walking = parent_of(walking)
    return False


def candidates(
    directory: Path,
    *,
    workspace: Path,
    before: Iterable[int],
    ancestor: int,
    parent_of: ParentOf = parent_from_process_table,
) -> tuple[SessionRecord, ...]:
    """Every registered Session that could be this launch's, after all three filters."""
    already = frozenset(before)
    wanted = _real(workspace)
    return tuple(
        record
        for record in records(directory)
        if record.pid not in already
        and _real(record.cwd) == wanted
        and is_descendant(record.pid, ancestor, parent_of=parent_of)
    )


async def claim(
    directory: Path,
    *,
    workspace: Path,
    before: Iterable[int],
    ancestor: int,
    timeout_seconds: float,
    parent_of: ParentOf = parent_from_process_table,
    still_running: Callable[[], bool] | None = None,
) -> SessionRecord:
    """Wait, bounded, for exactly one new Session of ours to register itself.

    `still_running` lets the caller say the child has already exited, which turns
    a wait that could only ever time out into an immediate, truthful refusal —
    the "child exits immediately" case, reported as what it is rather than as a
    launcher that is slow.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while True:
        found = candidates(
            directory,
            workspace=workspace,
            before=before,
            ancestor=ancestor,
            parent_of=parent_of,
        )
        if len(found) > 1:
            named = ", ".join(f"pid {record.pid} ({record.session_id})" for record in found)
            raise ClaimError(
                f"{len(found)} new Claude Sessions in {workspace} descend from pid {ancestor}, "
                f"so which one this launch started cannot be told: {named}"
            )
        if found:
            return found[0]
        if still_running is not None and not still_running():
            raise ClaimError(
                f"the process this launch started is already gone, and it registered no "
                f"Session in {workspace} before it went"
            )
        if loop.time() >= deadline:
            raise ClaimError(
                f"no Claude Session in {workspace} descending from pid {ancestor} registered "
                f"itself in {directory} within {timeout_seconds:.0f}s"
            )
        await asyncio.sleep(CLAIM_POLL_SECONDS)


def _real(path: Path) -> Path:
    """A path with every symlink resolved, so `/tmp` and `/private/tmp` are one.

    `os.path.realpath` rather than `Path.resolve`, because this has to answer for
    a directory that may already be gone — a workspace removed between launch and
    readback should compare as itself, not raise.
    """
    return Path(os.path.realpath(path))
