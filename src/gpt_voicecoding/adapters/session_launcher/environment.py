"""The child's environment, built by allowlist — the launcher owns it completely.

The locked decision is that the launcher owns the child environment. This module
is what "owns" means here, and the shape it takes is **default-deny**: a launched
Session's environment is exactly what is named below, plus what the launch
request asked for, and nothing else. Nothing is inherited by accident, from
either direction.

**Why not "strip the known-bad names off what we already have".** ADR 0004's
outage was `MallocStackLogging`, a variable nothing in that repository ever set:
it was inherited from whichever shell happened to run the installer, and it then
rode into every spawned child, where libmalloc answered it with one line of
stderr per process — 681,929 lines, 98.1% of the log. A subtractive rule could
only have caught it if somebody had known to list it in advance, and the whole
point is that nobody did. An allowlist has no such failure mode: a variable
nobody named is a variable nobody gets.

**And the leak the subtractive rule would have opened.** The obvious baseline —
"the engine's own environment" — is not ownership at all, only a change of
parent. The engine's environment is where this system keeps its *own* secrets:
the Companion Channel reads the Telegram bot token out of it, by a variable the
operator names in `token_env`. Forwarding that wholesale would hand every
launched coding agent the credentials of the bridge that launched it. So the
engine's environment is a *source of specific values*, never a baseline.

**What is on the list, and why each one.** These are what a terminal coding agent
needs to be itself, and the list is short enough to read:

- `PATH` — the agent runs tools;
- `HOME` — the agent's own configuration and its own credentials live there
  (`~/.claude`, `~/.codex`), so this is what lets it authenticate *as the user*;
- `SHELL`, `TMPDIR` — the agent spawns shells and writes scratch files;
- `USER`, `LOGNAME` — what tools print when they name the operator;
- `LANG`, `LC_ALL`, `LC_CTYPE` — a TUI drawing box characters needs a UTF-8
  locale, and a missing one shows up as mojibake rather than as an error.

`TERM` is not read from the engine at all: it is stated, because the direct-child
adapter allocates the pseudo-terminal itself and there is no terminal anywhere to
inherit one from. See `settings.DEFAULT_TERMINAL_TYPE`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

#: Read from the engine's environment when present, and simply absent when not.
#: See the module note for why each one is here — and for why the list is a list
#: rather than "everything we happen to have".
INHERITED_NAMES: tuple[str, ...] = (
    "PATH",
    "HOME",
    "SHELL",
    "TMPDIR",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
)

#: Set by the launcher rather than inherited, because nothing upstream has one.
TERM_VARIABLE = "TERM"


def child_environment(
    requested: Mapping[str, str],
    *,
    terminal_type: str,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Exactly what one launched Session's environment is, and nothing more.

    `requested` is `LaunchRequest.env` — the variables Bridge Core asked for, and
    the ones the Relay routes are bootstrapped through. They are applied last and
    win, because a launch that asked for a value and did not get it is a launch
    that lied about what it started.

    `source` is where the inherited names are read from, defaulting to this
    process's environment. It is a parameter so a test can state a whole
    environment rather than mutate the one it is running in.
    """
    environ = os.environ if source is None else source
    built = {name: environ[name] for name in INHERITED_NAMES if name in environ}
    built[TERM_VARIABLE] = terminal_type
    built.update(requested)
    return built
