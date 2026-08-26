"""What no test in this suite is allowed to reach.

**The real `launchctl`.** The Codex installation item loads a login job into
launchd, and a job loaded from a test is loaded into the launchd of the person
running the test — a real login session, a real `~/Library/LaunchAgents`, and a
real Codex daemon started under it. That is not a thought experiment: two drafts
of `installation/codex_launch_agent.py` did exactly that while it was being
written, once naming a plist pytest deleted a second later.

Passing a fake in is the design (`Launchd` has no default and every entry point
demands one), but a design only holds while everyone remembers it, and a test
that forgets does not fail — it silently changes the machine and passes. So the
real runner is taken away from the whole suite here, and a test that wants a
subprocess has to say so by supplying its own.
"""

from __future__ import annotations

import plistlib
from collections.abc import Sequence
from pathlib import Path

import pytest

from gpt_voicecoding.installation import codex_launch_agent


@pytest.fixture(autouse=True)
def _no_real_launchctl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every subprocess this module would run, refused in the loudest way there is."""

    def refuse(arguments: Sequence[str]) -> tuple[int, str]:
        raise AssertionError(
            "a test reached the real machine through "
            f"gpt_voicecoding.installation.codex_launch_agent: {list(arguments)}. "
            "Pass a Launchd with a `run` of its own, or a `run=` to daemon_versions."
        )

    monkeypatch.setattr(codex_launch_agent, "_run", refuse)


#: The domain a fake launchd answers in. A real one is `gui/<uid>`; this is that
#: shape and no user's, so a command built from it could not work by accident.
DOMAIN = "gui/501"


class FakeLaunchd:
    """A launchd that records what it was asked and answers what it was told to.

    `held` is the job launchd currently holds. `bootstrap` sets it and nothing
    here ever clears it — that is the point: this product has no verb that
    unloads a job, and any test that made one pass would be testing a product
    that stops daemons its user's Sessions are attached to.

    It holds a *program* and not just a flag, because that is the thing real
    launchd holds: a job loaded from one render keeps running that render's
    program after the file on disk has been rewritten, and a fake that answered
    a bare yes could not tell the two apart. `bootstrap` reads the program out of
    the plist it is handed, exactly as launchd does, and never re-reads it after.
    """

    def __init__(self) -> None:
        #: Set by a test that wants launchd to turn the job down.
        self.refuses = False
        self.commands: list[list[str]] = []
        #: `None` is "launchd holds nothing". A test sets it to stand a job up or
        #: to kill one; `bootstrap` sets it the way launchd would.
        self.program: str | None = None

    @property
    def held(self) -> bool:
        return self.program is not None

    def __call__(self, arguments: Sequence[str]) -> tuple[int, str]:
        self.commands.append(list(arguments))
        verb = arguments[1]
        if verb == "print":
            if self.program is None:
                return (113, "Could not find service")
            # The shape real launchd answers in, kept to the two lines this reads.
            return (0, f"{arguments[2]} = {{\n\tstate = running\n\tprogram = {self.program}\n}}")
        if verb == "bootstrap":
            if self.refuses:
                return (5, "Input/output error")
            self.program = plistlib.loads(Path(arguments[3]).read_bytes())["ProgramArguments"][0]
            return (0, "")
        raise AssertionError(f"this item may not run `launchctl {verb}`")

    @property
    def launchd(self) -> codex_launch_agent.Launchd:
        return codex_launch_agent.Launchd(domain=DOMAIN, run=self)

    @property
    def verbs(self) -> list[str]:
        return [command[1] for command in self.commands]


@pytest.fixture
def launchd() -> FakeLaunchd:
    return FakeLaunchd()


def codex_home(root: Path, *, managed: bool = True) -> Path:
    """A `CODEX_HOME` under `root`, with the standalone managed binary when asked.

    Shared, because both the item's own tests and the boundary's build the same
    thing: a `root` of `tmp_path` is what makes the Codex item a real participant
    rather than a permanent `ABSENT`, and two spellings of it would drift the
    moment the managed package's layout does.
    """
    home = root / ".codex"
    if managed:
        binary = codex_launch_agent.managed_binary(home)
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(0o755)
    else:
        home.mkdir()
    return home
