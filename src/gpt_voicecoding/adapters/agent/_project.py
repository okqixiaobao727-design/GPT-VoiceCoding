"""What project a Session is working on, read from its workspace.

The project half of every Session Name (`_naming.py`), and the **one place**
either lane runs a command to get it — a rule worth a module of its own, because
this is asked once per row per discovery tick and a second copy of it in the
other lane would be a second subprocess per row per tick.

**Git first, the directory second.** `git rev-parse --git-common-dir` is what the
reference implementation asked (`legacy@1d32845:bridge/labels.py:28-70`), and it
is the right question: it answers with the *repository's* directory from any
worktree or subdirectory, so two Sessions in `repo/` and `repo/src/` are working
on one project and are named for it. Legacy then raised when the answer was not
a repository at all, and legacy's label never existed. That is **adapted** here: v1.0
bridges every Session the user starts, so one in `~/scratch` is named
`scratch · <task>` from the directory's own basename rather than left unnamed
and unspeakable. The basename is a fact about the Session, not a guess.

**Cached per resolved workspace, for the life of the adapter.** A workspace does
not change which repository it belongs to while a Session runs in it, and the
cadence would otherwise pay a subprocess per Session every five seconds. The
cache is keyed on the realpath, so the same repository reached by two paths is
one entry. **The basename fallback is remembered too**, deliberately: a `git`
that was busy the first time a workspace was seen would otherwise be asked again
on every tick for a Session whose name the registry has already frozen, and the
second answer could never reach the roster anyway.

**A failure is a directory name, never an exception.** Discovery is answering
"what is running"; `git` being absent, slow or angry is not a reason to lose a
row, and the basename is standing by.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final

_log = logging.getLogger(__name__)

#: The command, and the flags that make its answer one absolute path. Asked of
#: the Session's own workspace, so the answer is about the Session's repository
#: and not about wherever this engine happens to be running.
GIT_COMMAND: Final = ("git", "rev-parse", "--path-format=absolute", "--git-common-dir")

#: How long `git` is given before the directory's basename is the answer. Short
#: on purpose: this runs inside a discovery tick, and a name is worth less than
#: the roster it would hold up.
COMMAND_TIMEOUT_SECONDS: Final = 5.0

#: What a Git common directory is called. A `rev-parse` answer that is not one is
#: an answer this build does not understand, and it falls back rather than
#: cutting a project name out of a shape it is guessing at.
GIT_DIRECTORY_NAME: Final = ".git"


#: How `git` is asked, and what it answered on stdout — `None` when it could not
#: be asked or refused. Injected for the reason the Claude lane injects its
#: roster command: a test that shelled out here would be measuring whichever
#: repositories the machine running it happens to have.
GitAnswer = Callable[[Path], Awaitable[str | None]]


class ProjectNames:
    """The project name for each workspace this lane has seen, read once each."""

    def __init__(
        self, *, ask: GitAnswer | None = None, timeout_seconds: float = COMMAND_TIMEOUT_SECONDS
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._ask = ask or self._run_git
        #: realpath → the project name it resolved to.
        self._known: dict[str, str] = {}

    async def of(self, workspace: Path) -> str | None:
        """This workspace's project name, or `None` if there is no workspace.

        `None` means the row carried no `cwd` at all — the lanes spell that
        `Path()` — and it is the only case that yields no name. Everything else
        resolves: a repository to its own directory name, anything else to the
        workspace's basename.
        """
        if not str(workspace).strip() or str(workspace) == ".":
            return None
        resolved = os.path.realpath(workspace)
        remembered = self._known.get(resolved)
        if remembered is not None:
            return remembered

        found = _project_in(await self._ask(Path(resolved)) or "") or Path(resolved).name.strip()
        if not found:
            return None
        self._known[resolved] = found
        return found

    async def _run_git(self, workspace: Path) -> str | None:
        """Ask `git` about this workspace, and hand back what it said on stdout."""
        try:
            process = await asyncio.create_subprocess_exec(
                *GIT_COMMAND,
                cwd=workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (OSError, ValueError) as unavailable:
            _log.info("could not ask git about %s: %s", workspace, unavailable)
            return None
        try:
            out, _ = await asyncio.wait_for(process.communicate(), self._timeout_seconds)
        except TimeoutError:
            process.kill()
            await process.wait()
            _log.info("git did not answer about %s within %ss", workspace, self._timeout_seconds)
            return None
        if process.returncode != 0:
            return None
        return out.decode("utf-8", errors="replace")


def _project_in(answer: str) -> str | None:
    """The project name inside one `rev-parse` answer, or `None` if it is not one.

    Three shapes are refused, all of them legacy's (`labels.py:56-70`): more than
    one line, a path that is not absolute, and a directory that is not `.git`.
    Each would mean the answer is not the one this asked for, and a name cut out
    of it would be this build guessing.
    """
    text = answer.strip()
    if not text or len(text.splitlines()) > 1:
        return None
    common = Path(text)
    if not common.is_absolute() or common.name != GIT_DIRECTORY_NAME:
        return None
    return common.parent.name.strip() or None
