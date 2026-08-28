"""A launchd that answers what a test told it to, and the Codex home beside it.

**Why this is not in `conftest.py`, which is where it started.** `conftest.py` is
pytest's own file: pytest loads it *by path*, but `from conftest import …` is an
ordinary import and goes through `sys.path` — where the name `conftest` is not
unique, because `tests/acceptance/` has one too. With only `[dev]` installed the
acceptance conftest is `importorskip`ped, its directory never reaches `sys.path`,
and the bare name resolves here. Install `[acceptance]` and it resolves *there*
instead, and the whole unit suite dies at collection ([#93](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/93)).

So the rule this file exists to keep: **a test module never imports a
`conftest`.** Anything two test files share lives in a module with a name of its
own, the way `fakes.py` and `hub.py` already do. `conftest.py` keeps only what
pytest asks it for — the fixtures — and reaches *here* for the pieces they need.
"""

from __future__ import annotations

import plistlib
from collections.abc import Sequence
from pathlib import Path

from gpt_voicecoding.installation import codex_launch_agent

#: The domain a fake launchd answers in. A real one is `gui/<uid>`; this is that
#: shape and no user's, so a command built from it could not work by accident.
DOMAIN = "gui/501"


class FakeLaunchd:
    """A launchd that records what it was asked and answers what it was told to.

    It holds a *program* and not just a flag, because that is the thing real
    launchd holds: a job loaded from one render keeps running that render's
    program after the file on disk has been rewritten, and a fake that answered a
    bare yes could not tell the two apart. `bootstrap` reads the program out of
    the plist it is handed, exactly as launchd does, and never re-reads it after.

    Nothing here ever *unloads* a job, and that is the point: this product has no
    verb that does, and any test that made one pass would be testing a product
    that stops daemons its user's Sessions are attached to.
    """

    def __init__(self) -> None:
        #: Set by a test that wants launchd to turn the job down.
        self.refuses = False
        self.commands: list[list[str]] = []
        #: `None` is "launchd holds nothing". A test sets it to stand a job up or
        #: to kill one; `bootstrap` sets it the way launchd would.
        self.program: str | None = None
        #: A GUI login's audit session identifier. A kickstart leaves it alone;
        #: a new login changes it, which is #132's reload evidence.
        self.login_asid = 100_016

    @property
    def held(self) -> bool:
        return self.program is not None

    def begin_login(self, path: Path) -> None:
        """Start a new fake GUI login and load the plist then on disk."""
        self.login_asid += 1
        if not path.exists():
            self.program = None
            return
        self.program = plistlib.loads(path.read_bytes())["ProgramArguments"][0]

    def __call__(self, arguments: Sequence[str]) -> tuple[int, str]:
        self.commands.append(list(arguments))
        verb = arguments[1]
        if verb == "print":
            if self.program is None:
                return (113, "Could not find service")
            # The shape real launchd answers in, kept to the two lines this reads.
            return (
                0,
                f"{arguments[2]} = {{\n\tstate = running\n"
                f"\tprogram = {self.program}\n\tasid = {self.login_asid}\n}}",
            )
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
