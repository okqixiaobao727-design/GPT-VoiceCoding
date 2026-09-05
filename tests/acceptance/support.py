"""What the acceptance run is made of: a run directory, a journal, and one engine.

`docs/acceptance-design.md` said this module would be `tests/e2e/support.py`,
lifted. **The E2E suite was never built**, and its standing is #62's decision, not
this ticket's — so the support code lives here, owned by the acceptance, and #62
lifts from it if it decides the fake-far-side suite is worth building. What the
two would genuinely have shared is small: a `bridgectl` runner and derived
deadlines. Everything else here — the real bundle, the real credentials, the run
directory, the verdict — has no counterpart in a hermetic suite.

Three rules this module exists to hold:

* **Every product action goes through the bundle's own `bridgectl`.** Not the
  editable install's, not an in-process call. The thing being accepted is the
  `.app` on disk.
* **Nothing is asserted that was not observed.** A control-plane reply says what
  the engine believes; the workspace, the chat and `engine.log` say what happened.
  Both go in the journal, marked for which they are.
* **Deadlines are derived, never picked.** Reply deadlines come from
  `control_plane.client.timeout_for`, the same function `bridgectl` itself uses.
  The far-side waits are the only numbers chosen here, and each one carries what
  it was measured against.
"""

from __future__ import annotations

import asyncio
import filecmp
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import tomllib
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import live_call

from gpt_voicecoding import __version__
from gpt_voicecoding.adapters.agent.claude.settings import DEFAULT_ACK_TIMEOUT_SECONDS
from gpt_voicecoding.adapters.agent.codex import discovery as codex_discovery
from gpt_voicecoding.adapters.agent.codex import processes as codex_processes
from gpt_voicecoding.adapters.agent.codex import shared_daemon as codex_shared_daemon
from gpt_voicecoding.adapters.codex_app_server import process as codex_app_server
from gpt_voicecoding.adapters.codex_app_server.settings import CodexSettings
from gpt_voicecoding.adapters.companion_channel.telegram.api import TelegramError, Transport
from gpt_voicecoding.adapters.companion_channel.telegram.settings import (
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
)
from gpt_voicecoding.control_plane.client import DEFAULT_TIMEOUT_SECONDS, ask
from gpt_voicecoding.installation import claude_hooks
from gpt_voicecoding.seams.control_plane import Action, Request
from gpt_voicecoding.seams.identity import AgentKind

# --- where a run lives ------------------------------------------------------

ACCEPTANCE_ROOT = Path.home() / "Library" / "Application Support" / "GPT-VoiceCoding" / "acceptance"
ACCEPTANCE_ROOT_VARIABLE = "GPTVOICECODING_ACCEPTANCE_ROOT"

#: The bundle under test. A location, overridable, because a run against a
#: side-by-side install is a legitimate thing to want and hard-coding
#: `/Applications` would make it impossible.
BUNDLE_VARIABLE = "GPTVOICECODING_ACCEPTANCE_BUNDLE"
DEFAULT_BUNDLE = Path("/Applications/GPT-VoiceCoding.app")

#: The engine's real configuration, the one the run derives its own from.
SOURCE_CONFIG_VARIABLE = "GPTVOICECODING_ACCEPTANCE_SOURCE_CONFIG"
REALTIME_PROBE_VARIABLE = "GPTVOICECODING_ACCEPTANCE_REALTIME_PROBE"

#: The maintained probe stays in the sibling legacy checkout; this relative
#: location is the repository convention, never a maintainer-specific path.
LEGACY_REALTIME_PROBE = Path("GPT-VoiceCoding-legacy/scripts/rt_prototype.py")


class RealtimeProbeUnavailable(Exception):
    """The required external probe cannot be used by this acceptance run."""


#: **Every paid turn this run drives is pinned here, and nowhere else.** A run
#: spends tokens in three places — each lane's hand-started Session and the three
#: extra Sessions started the same way (`live_call_step` reuses the lane's own
#: `arguments`), and the Delegated Turn the Call Agent hands to the Codex
#: app-server — and until #198 none of them named a model. All three therefore
#: read whichever config the person happened to be carrying: measured on run
#: `20260904T124243Z`, that was `opus[1m]` on the Claude side (859,329 cache-read
#: tokens at the 1M-context tier, for 26 assistant turns of five short
#: instructions) and `gpt-5.6-sol` at `xhigh` on the Codex side. Neither was ever
#: chosen for this run; both were the top tier of a personal setting leaking in.
#:
#: **Pinned rather than defaulted, and cheap rather than strong, because nothing
#: here grades a model.** Every step reads the *product's* rows, transcripts,
#: prompts and ledger; the only thing an agent has to be good enough at is
#: following a short instruction. The three steps where that shows first are
#: `child`, `question` and `stop notice` — a red on one of those, and nowhere
#: else, is what "the pin was cut too fine" looks like.
#:
#: The realtime Voice is deliberately absent: its model is mechanism identity
#: rather than a dial (`realtime/settings.py`), so it is not a cost lever this
#: run may turn.
CLAUDE_LANE_MODEL = "sonnet"
CLAUDE_LANE_EFFORT = "medium"

#: One Codex-side pin, spent twice: the Codex lane's own Session and — as
#: `DELEGATED_TURN_MODEL` — the work the Call Agent hands off during a Live Call.
#: They are one name because they are one bill, against one app-server.
#:
#: **There is no reasoning-effort pin here, and it may not be re-added as a `-c`
#: override** (#232). It was one, for one run: `-c model_reasoning_effort="high"`
#: alongside the model pin. A `-c` override makes codex-tui run its own core
#: instead of joining the shared daemon, and the Codex roster composes a row from
#: a daemon-held user thread plus a live terminal in its workspace — so the pin
#: made this lane's own Session invisible to the product it was there to grade.
#: Run `20260904T202319Z` failed at `roster` and SKIPPED the nine steps behind
#: it, and the engine's codex discovery never mentioned the TUI's thread at all.
#:
#: **The mechanism was already written down in this repository**, which is the
#: part worth carrying forward: `can_reuse_implicit_local_daemon` requires
#: `cli_kv_overrides.is_empty()` (`tui/src/lib.rs:919-921`, cited in
#: `hand_started`'s module docstring since #110). It does not test *which* key
#: was overridden, so there is no such thing as a harmless `-c` here — and the
#: rule beside that citation said "never reach for `-c` to solve a boot gate",
#: which is how a `-c` reached for *cost* got past it. The rule now names the
#: flag rather than the motive, and the table below is the black-box confirmation
#: of the source read.
#:
#: Measured 2026-09-05 with the harness's own pty launch in a trusted workspace,
#: asking the daemon `thread/loaded/list` for the TUI's own thread id — codex-cli
#: 0.153.0 against a shared app-server 0.149.1:
#:
#: | launch flags                                | daemon holds the thread |
#: |---------------------------------------------|-------------------------|
#: | `--sandbox workspace-write`                 | yes                     |
#: | `… -m gpt-5.6-luna`                         | yes                     |
#: | `… -c model_reasoning_effort="high"`        | **no**                  |
#: | `… -m gpt-5.6-luna -c model_reasoning_effort="high"` | **no**         |
#:
#: So the model pin stays — it was measured to join — and the effort pin is gone.
#: `codex --help` on 0.153.0 has no dedicated effort flag, and `-p/--profile` is
#: another config layer whose effect on daemon membership is **unverified**: it
#: is not a way around this until somebody measures it the same way. Billing
#: reasoning effort at all needs a codex feature rather than a change here (#232,
#: out of scope). What guards this comment is `codex_daemon_membership`, which
#: reads the fact in the boot wait and refuses the lane rather than grading nine
#: SKIPPED steps behind a `roster` red, and the fast test that pins the tuple.
CODEX_LANE_MODEL = "gpt-5.6-luna"
DELEGATED_TURN_MODEL = CODEX_LANE_MODEL

#: Darwin caps an AF_UNIX path at 103 bytes, so the run's socket cannot live in
#: the run directory — that path is already 70 characters before the run id. The
#: same reasoning `config.RUNTIME_ROOT` applies, applied again.
SOCKET_ROOT = Path("/tmp")


def acceptance_root() -> Path:
    override = os.environ.get(ACCEPTANCE_ROOT_VARIABLE)
    return Path(override).expanduser() if override else ACCEPTANCE_ROOT


def harness_root() -> Path:
    """Where the harness's own modules live — `tests/acceptance`, not the runs.

    Distinct from `acceptance_root`, which is where a run's artifacts go. This is
    an *import* path: the engine is handed it so `[adapters] call` can name
    `live_call`, and it is derived from this file rather than from the checkout
    so a worktree gets its own copy rather than another tree's (#183).
    """
    return Path(__file__).resolve().parent


def bundle_path() -> Path:
    override = os.environ.get(BUNDLE_VARIABLE)
    return Path(override).expanduser() if override else DEFAULT_BUNDLE


def bundled_python(bundle: Path | None = None) -> Path:
    return (bundle or bundle_path()) / "Contents/Resources/engine/bin/python3"


def bundled_bridgectl(bundle: Path | None = None) -> Path:
    return (bundle or bundle_path()) / "Contents/Resources/engine/bin/bridgectl"


def source_config_path() -> Path:
    override = os.environ.get(SOURCE_CONFIG_VARIABLE)
    if override:
        return Path(override).expanduser()
    engine = Path.home() / "Library" / "Application Support" / "GPT-VoiceCoding" / "engine"
    return engine / "config.toml"


def realtime_probe_path(
    repository: Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> Path:
    override = (environment if environment is not None else os.environ).get(REALTIME_PROBE_VARIABLE)
    if override:
        probe = Path(override).expanduser()
    else:
        common_directory = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        primary_checkout = Path(common_directory).resolve().parent
        probe = primary_checkout.parent / LEGACY_REALTIME_PROBE

    if not probe.is_file() or not os.access(probe, os.R_OK):
        raise RealtimeProbeUnavailable(
            f"no usable realtime probe at {probe}; set {REALTIME_PROBE_VARIABLE} "
            "to the legacy checkout's scripts/rt_prototype.py"
        )
    return probe


# --- the journal ------------------------------------------------------------


class Journal:
    """One JSON line per event, in the order the run produced them.

    Locked because the engine's reader thread and the test's own thread both
    write to it, and a half-written line is worse than a missing one: the file
    is the evidence every verdict points at.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    def __call__(self, event: str, **fields: Any) -> dict[str, Any]:
        line = {"at": datetime.now(UTC).isoformat(), "event": event, **fields}
        with self._lock, self.path.open("a") as sink:
            sink.write(json.dumps(line, default=str) + "\n")
        return line

    def read(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]


# --- the login shell's PATH, in one place -----------------------------------

#: Mirrors `shell/Sources/ShellCore/LoginShellPath.swift`, which is the method the
#: menu-bar shell uses and therefore the PATH the engine really runs on. `-lic`,
#: not `-lc`: zsh sources `~/.zshrc` only when interactive, and `~/.zshrc` is
#: where `nvm` and `brew shellenv` actually write. The sentinels separate the
#: answer from an interactive profile's chatter, and **exactly two or nothing** —
#: a third means something other than the `printf` wrote the marker, and then no
#: part of the output is the answer.
#:
#: The budget follows the shell's, and is not a second opinion about it: this
#: harness refuses a run when it cannot read a PATH, so a mirror that gave up
#: sooner than the product would refuse runs the product would have served —
#: which is how #118 was found. `TestTheThingsThatMustAgree` in
#: `tests/test_app_bundle.py` reads the Swift and fails if the two drift.
PATH_SENTINEL = "<<<GVC-PATH>>>"
PATH_SCRIPT = f"printf '{PATH_SENTINEL}%s{PATH_SENTINEL}' \"$PATH\""
PATH_TIMEOUT_SECONDS = 10.0


def _read(command: list[str], *, timeout_seconds: float) -> str | None:
    """One command's stdout, or `None` for every way running it can fail.

    Its callers want the same thing — what the command printed, if it could be
    run at all — and what they do about `None` is what differs, so that stays
    with them: a PATH this harness cannot read is a refusal, a `vm_stat` it
    cannot read is a `null` on the verdict.

    **The budget is the caller's and is never defaulted here.** One of the two
    is a login shell mirroring the product's own budget, the other is a kernel
    read that answers in milliseconds; a shared default would tie the second to
    the first, and the first is pinned to a Swift literal by
    `tests/test_app_bundle.py`. Moving the product's shell budget must not
    silently move how long a `vm_stat` is waited on.

    The return code is deliberately not consulted. Every caller parses what it
    got and answers `None` when it cannot make sense of it, so a command that
    printed a usable answer and exited non-zero would be thrown away twice for
    one reason.
    """
    try:
        finished = subprocess.run(
            command,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout_seconds,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return finished.stdout


def login_shell_path() -> str | None:
    """The user's own PATH, or None — never a guess, never a partial answer."""
    shell = os.environ.get("SHELL")
    if not shell or not os.access(shell, os.X_OK):
        return None
    printed = _read([shell, "-lic", PATH_SCRIPT], timeout_seconds=PATH_TIMEOUT_SECONDS)
    if printed is None:
        return None
    parts = printed.split(PATH_SENTINEL)
    if len(parts) != 3:  # exactly two sentinels bound exactly one answer
        return None
    answer = parts[1].strip(" \t")
    if not answer or "\n" in answer or "\0" in answer:
        return None
    if not any(entry.startswith("/") for entry in answer.split(":")):
        return None
    return answer


# --- the machine a run has to have to itself --------------------------------


def foreign_codex_refusal(
    *,
    run: codex_processes.Runner = codex_processes.run_command,
    now: codex_processes.Clock = time.time,
) -> str | None:
    """Why this run cannot be isolated from the Codex Sessions already open, or None.

    **Codex discovery is machine-wide by construction, and that is the product
    behaving as written.** The adapter lists every live interactive `codex` TUI
    on the machine from one `ps` (`adapters/agent/codex/processes.py`), and the
    per-lane `socket_directory` this run derives isolates the app-server socket,
    not that scan. Both lanes' engines load both agent adapters. So a Codex
    window the operator left open registers with **both** acceptance engines and
    sits on the roster for the whole walk: on run `20260904T091550Z` one did, in
    586 of the run's 687 `bridgectl` readings, and `roster`, `stable name` and
    `switches` each read a row — and a Stop source — that the walk had not
    created. The run graded anyway (#228).

    What is missing is not a narrower scan; it is the harness refusing to grade
    a walk it cannot isolate. So this asks **the adapter's own enumeration**
    rather than reading the process table a second way: a second scanner would
    be a second answer to "is this a Session", and the whole reason a foreign
    TUI reaches the roster is the answer the adapter gives.

    **Unconditional on `--lane`.** The polluted roster is not the Codex lane's
    problem alone — both lanes' engines load both adapters, so a `--lane claude`
    run reads the same foreign row.

    **What the run owns is decided by place, not by a launch record.** This runs
    before the first hand-start, so in an ordinary run nothing of the walk's
    exists yet to be miscounted. The acceptance criterion is stronger than that
    ordering — a Session the walk hand-started is *never* foreign — and an ordering
    nobody can see is not a rule, so a candidate whose workspace is inside
    `acceptance_root()` is the walk's own: every run directory and every lane
    workspace is made there (`new_run_directory`, `workspace_path`). Both sides
    are resolved before comparison, because `lsof` reports a real path and an
    overridden root may be reached through a symlink (`/tmp` is one).

    What place costs, said out loud: a TUI opened by hand inside an *earlier*
    run's workspace — to read what that run left there — reads as this run's own
    and is waved through. That is the ordering rule's behaviour exactly, so the
    containment rule is never worse than the ordering it strengthens; it is the
    one case where it is not better.

    **A candidate the adapter cannot name a workspace for is not reported**, and
    that is inherited rather than decided here: `enumerate_sessions` drops a row
    whose cwd `lsof` will not give up. Reporting it would need the second scanner
    this deliberately does not have.

    **The clock is read once, here, and handed down.** `enumerate_sessions`
    dates every `etime` against one reading taken before its `ps`; passing that
    same moment in means the elapsed times in the refusal are computed against
    the moment the starts were, and keeps the read on the safe side of the `ps`
    (`processes.START_TIME_RESOLUTION_SECONDS`).

    Harness only, so #228 says the legacy-citation rule does not apply — and the
    citation is here anyway because it is short and it was checked. Legacy never
    enumerated the process table for Codex at all: its one `pgrep` matches its
    own bundle executable (`legacy@1d32845:bridge/host.py:795`) and its one `ps`
    reads a pid it already holds (`legacy@1d32845:bridge/codex.py:205`). A
    Session it had not started did not exist for it, so there is no preflight
    and no foreign-Session refusal to port. **Dropped, because** legacy has no
    such behaviour.
    """
    sampled_at = now()
    try:
        live = asyncio.run(codex_processes.enumerate_sessions(run=run, now=lambda: sampled_at))
    except (OSError, TimeoutError) as unreadable:
        return (
            "the process table could not be read, so this run cannot tell whether a Codex "
            f"Session it did not hand-start is live and would join both lanes' rosters: "
            f"{unreadable!r}"
        )
    owned = acceptance_root().expanduser().resolve(strict=False)
    foreign = [
        candidate
        for candidate in live
        if not candidate.workspace.expanduser().resolve(strict=False).is_relative_to(owned)
    ]
    if not foreign:
        return None
    named = "; ".join(
        f"pid {candidate.pid} in {candidate.workspace}"
        f"{_uptime_text(sampled_at, candidate.started_at)}"
        for candidate in foreign
    )
    return (
        f"a Codex Session this run did not hand-start is live on this machine, and both lanes' "
        f"engines bridge every Codex TUI on it — so it would sit on the roster the walk is "
        f"graded on: {named}. Quit these Codex sessions and re-run; this run will not "
        "stop them for you."
    )


def _uptime_text(sampled_at: float, started_at: float | None) -> str:
    """How long a Session has been up, in the compact form the refusal reads in.

    **The `None` arm is unreachable today and kept anyway.** `enumerate_sessions`
    drops a row whose `etime` it cannot parse, so nothing it returns is missing a
    start — but `Candidate.started_at` is typed `float | None`, no CI gate type-
    checks (`.github/workflows/ci.yml`), and this is called from a session-scoped
    autouse fixture whose whole promise is `REFUSED` or pass, never a traceback
    (`conftest.py`'s docstring). A dead branch that keeps a typed contract from
    becoming an unrefused error is worth its one line; omitting the duration
    invents nothing, which is what makes it the safe thing to omit.
    """
    if started_at is None:
        return ""
    whole = max(int(sampled_at - started_at), 0)
    hours, rest = divmod(whole, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f", up {hours}h{minutes:02d}m"
    return f", up {minutes}m{seconds:02d}s" if minutes else f", up {seconds}s"


# --- is this Session one the product can see at all? ------------------------

#: What the daemon is asked. Taken from the adapter's own constant rather than
#: spelled again, because two spellings of one method name is how a harness comes
#: to ask a question the product does not ask.
DAEMON_ROSTER_METHOD = codex_discovery.ROSTER_METHOD

#: How the daemon is found, and how it is dialled. Both are the engine's own
#: functions, defaulted here and injectable only so a test can pin the reading
#: without a socket — there is deliberately no second route to the daemon under
#: `tests/acceptance` (advisor ruling on #232).
DaemonLocator = Callable[[str], Awaitable[tuple[codex_shared_daemon.DaemonAddress | None, str]]]
DaemonDial = Callable[..., Awaitable[Any]]

#: Every spelling of a config override `codex --help` carries on 0.153.0, in both
#: the separated and the `=`-joined form. One list, because the tuple test and the
#: refusal's own reading of the flags have to agree about what a `-c` looks like.
CONFIG_OVERRIDE_FLAGS = ("-c", "--config")


def is_config_override(flag: str) -> bool:
    """Whether one launch argument is a `-c` override, in either spelling.

    The `=`-joined form matters as much as the separated one: `clap` accepts
    `-c=key=value` and `--config=key=value`, and `cli_kv_overrides` fills the same
    way from both — so a check that only knew `("-c", "--config")` would wave
    through the exact flag #232 exists to keep out.
    """
    return flag in CONFIG_OVERRIDE_FLAGS or flag.startswith(
        tuple(f"{name}=" for name in CONFIG_OVERRIDE_FLAGS)
    )


@dataclass(frozen=True)
class DaemonMembership:
    """Whether the shared Codex daemon holds one thread, and how that was learned.

    **`held` is a tri-state and that is the whole design.** `True` and `False`
    are both observations — the daemon answered and this thread was, or was not,
    among the ids it named. `None` is *no observation*, and the module this
    borrows its rule from says why it may not collapse into `False`: never claim
    anything about the daemon this build did not observe
    (`adapters/agent/codex/shared_daemon.py`, #96). A daemon that is down, moved
    or answering a shape this build cannot read is not evidence that a thread is
    absent from it, and a lane refused on one of those would blame #232's own
    cause for somebody else's outage.
    """

    thread_id: str
    held: bool | None
    held_threads: tuple[str, ...] = ()
    daemon: str = ""
    reason: str = ""

    def refusal(self, flags: Sequence[str]) -> str | None:
        """Why this lane cannot be walked, or `None` — and it is only ever the observed absence.

        **Why an absence is a refusal rather than a red.** ADR 0020 defines a
        Codex Session as a daemon thread a terminal vouches for, so a TUI outside
        the daemon is a Session the product is *right* not to list: `roster`
        failing on it grades the harness's own ground as a product defect, and
        the nine steps behind it as SKIPPED with `blocked by roster` — which is
        the reading run `20260904T202319Z` produced and #232 was opened to
        replace. Making such a terminal a row is refused by the same ADR and is
        out of #233's scope too; there is nothing for the product to do here.

        **The flags are quoted rather than diagnosed.** A `-c` override is what
        was measured to keep a TUI out (2026-09-05), and it is named as that
        measurement when it is present. When it is not, the sentence says what
        was launched and stops: a stock explanation printed under a lane that
        cannot have this cause is how the next person loses an afternoon.
        """
        if self.held is not False:
            return None
        override = [flag for flag in flags if is_config_override(flag)]
        measured = (
            " This lane was launched with a `-c` override, and a `-c` override was measured "
            "on 2026-09-05 to make codex-tui run its own core instead of joining the shared "
            "daemon (#232) — which is exactly this."
            if override
            else " No `-c` override is in those flags, so this is not the cause #232 measured "
            "and the daemon-side reason is unknown to this run."
        )
        return (
            f"the Codex Session the harness hand-started writes thread {self.thread_id} into "
            f"its own rollout, and the shared daemon at {self.daemon} does not hold it — it "
            f"holds {list(self.held_threads) or 'no threads'}. A Codex Session is a daemon "
            f"thread a terminal vouches for (ADR 0020), so nothing the product does can give "
            f"this TUI a roster row, and every step of this lane reads one. Launched with "
            f"{list(flags)}.{measured}"
        )


def codex_daemon_membership(
    thread_id: str,
    *,
    executable: str | None = None,
    settings: CodexSettings | None = None,
    locate: DaemonLocator = codex_shared_daemon.locate,
    attach: DaemonDial = codex_app_server.attach,
) -> DaemonMembership:
    """Ask the shared Codex daemon whether it holds the thread this lane started.

    **The one fact that decides whether this lane can be walked at all.** The
    Codex roster composes a row from a daemon-held user thread plus a live
    terminal in its workspace (`adapters/agent/codex/roster.py`), so a TUI whose
    thread the daemon does not hold is invisible to the product by construction —
    not under-reported, not degraded, absent. Run `20260904T202319Z` walked such
    a lane: the engine's codex discovery never mentioned the TUI's thread at all,
    `roster` failed, and nine steps were SKIPPED behind it (#232).

    **Asked the engine's own way, through the engine's own functions.**
    `shared_daemon.locate` finds the socket by asking `codex app-server daemon
    version`, which is the one address that cannot go stale, and
    `codex_app_server.attach` becomes one more client of a daemon somebody else
    owns. This is `foreign_codex_refusal`'s rule applied a second time: a harness
    that implemented the wire again would be a second answer to a question the
    product already answers, and the product's answer is the one that decides
    whether a row appears.

    **The connection is let go of on every path.** Join-only is the daemon
    module's rule and it is this call's too: it opens a client, asks one method,
    and closes its own end — the user's TUIs are thin clients of that daemon and
    nothing here may outlive one read of it.

    Harness only, so #232 says the legacy-citation rule does not apply. Checked
    anyway and it is short: gen 1 had no shared daemon to be outside of — it drove
    a per-Session app-server it spawned itself
    (`legacy@1d32845:bridge/codex.py:1319-1347`), so a Session it started was
    reachable by construction and there was no membership to read. **Dropped,
    because** legacy has no such behaviour.
    """
    if not thread_id.strip():
        return DaemonMembership(
            thread_id=thread_id,
            held=None,
            reason=(
                "the agent's own record names no thread yet, so there is nothing to look for "
                "in the daemon"
            ),
        )
    codex_settings = settings if settings is not None else CodexSettings()
    return asyncio.run(
        _codex_daemon_membership(
            thread_id.strip(),
            executable=executable or codex_settings.executable,
            settings=codex_settings,
            locate=locate,
            attach=attach,
        )
    )


async def _codex_daemon_membership(
    thread_id: str,
    *,
    executable: str,
    settings: CodexSettings,
    locate: DaemonLocator,
    attach: DaemonDial,
) -> DaemonMembership:
    address, why_not = await locate(executable)
    if address is None:
        return DaemonMembership(thread_id=thread_id, held=None, reason=why_not)
    where = (
        f"{address.socket_path} (CLI {address.cli_version!r}, "
        f"app-server {address.app_server_version!r})"
    )
    try:
        connection = await attach(
            address.socket_path, version=__version__, settings=settings, experimental=False
        )
    except Exception as undialled:  # noqa: BLE001 - every way this fails is one fact
        return DaemonMembership(
            thread_id=thread_id,
            held=None,
            daemon=where,
            reason=f"the shared Codex daemon at {where} could not be dialled: {undialled!r}",
        )
    try:
        answer = await connection.request(
            DAEMON_ROSTER_METHOD, {}, timeout_seconds=settings.request_timeout_seconds
        )
    except Exception as unanswered:  # noqa: BLE001 - same fact again
        return DaemonMembership(
            thread_id=thread_id,
            held=None,
            daemon=where,
            reason=(
                f"the shared Codex daemon at {where} did not answer "
                f"{DAEMON_ROSTER_METHOD}: {unanswered!r}"
            ),
        )
    finally:
        await connection.aclose()

    listed = answer.get("data") if isinstance(answer, dict) else None
    if not isinstance(listed, list):
        return DaemonMembership(
            thread_id=thread_id,
            held=None,
            daemon=where,
            reason=(
                f"the shared Codex daemon at {where} answered {DAEMON_ROSTER_METHOD} in a "
                f"shape this run cannot read: {answer!r}"
            ),
        )
    held_threads = tuple(row.strip() for row in listed if isinstance(row, str) and row.strip())
    return DaemonMembership(
        thread_id=thread_id,
        held=thread_id in held_threads,
        held_threads=held_threads,
        daemon=where,
        reason=f"read from {DAEMON_ROSTER_METHOD} at {where}",
    )


# --- provenance -------------------------------------------------------------


@dataclass(frozen=True)
class Provenance:
    """Whether the installed bundle is the tree this run is being asked to accept."""

    bundle: Path
    commit: str
    matches: bool
    differences: tuple[str, ...]

    @property
    def reason(self) -> str:
        if self.matches:
            return f"the bundle's engine is byte-identical to {self.commit}"
        listed = ", ".join(self.differences[:5])
        return (
            f"the bundle's engine differs from the working tree at {self.commit}: {listed}"
            f"{' …' if len(self.differences) > 5 else ''}"
        )


#: Where gen-1 put itself on this machine. Named here so the environment block
#: below can say whether it is still there.
GEN1_RUNTIME = Path.home() / "Library" / "Application Support" / "GPT-VoiceCoding" / "runtime"
GEN1_HOOK = GEN1_RUNTIME / "bridge-hook"
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"


#: The `vm_stat` lines a new allocation could actually be served from. "Pages
#: free" alone is near zero on any warm macOS, so a block built on it would
#: record what looks like pressure on every host and distinguish nothing —
#: which is the one thing these numbers exist to do.
AVAILABLE_PAGES = ("Pages free", "Pages inactive", "Pages speculative")
PAGE_SIZE = re.compile(r"page size of (\d+) bytes")
SWAP_USED = re.compile(r"\bused\s*=\s*([\d.]+)M")
MEGABYTE = 1024 * 1024

#: What a host reading is waited on. `vm_stat` and `sysctl` are local kernel
#: reads that answer in milliseconds, so this is three orders of magnitude of
#: headroom and still turns a wedged one into a `null` on the verdict rather
#: than a run that hangs. Its own constant, and not `PATH_TIMEOUT_SECONDS`:
#: that one mirrors the product's login-shell budget and is pinned to a Swift
#: literal, and these two have nothing to do with either.
HOST_READ_TIMEOUT_SECONDS = 5.0


def free_memory_from(vm_stat: str) -> int | None:
    """`vm_stat`'s available pages, at the page size it names — or `None`.

    The page size is read rather than assumed because Apple silicon pages at
    16K and Intel at 4K, and this harness runs on both. A count it cannot find
    is no answer at all: a recorded number nobody can trust is worse than a
    recorded `null`, because only one of the two ever gets checked.
    """
    sized = PAGE_SIZE.search(vm_stat)
    if sized is None:
        return None
    pages = 0
    for name in AVAILABLE_PAGES:
        # The counts carry a trailing period, which is what a bare `int()` on
        # the last field chokes on.
        found = re.search(rf"^{name}:\s+(\d+)\.?\s*$", vm_stat, re.MULTILINE)
        if found is None:
            return None
        pages += int(found.group(1))
    return pages * int(sized.group(1))


def swap_used_from(swapusage: str) -> int | None:
    """How deep the swap file is, out of `sysctl -n vm.swapusage` — or `None`."""
    found = SWAP_USED.search(swapusage)
    if found is None:
        return None
    return int(float(found.group(1)) * MEGABYTE)


@dataclass(frozen=True)
class HostPressure:
    """What the machine itself was doing, as one reading (#230).

    **Why a verdict carries this at all.** Three runs of byte-identical
    `adapters/call/realtime/` gave zero, two and four undrained playouts, and
    the candidate cause for that spread is not in the code: an event loop
    starved by paging delivers inbound frames in bursts, which keeps the
    speaker's last-frame stamp fresh and holds `drained` False without the
    remote peer changing anything. It fits the run ordering — the clean run on
    a fresher machine, the stalled ones after many more hours of uptime — and
    it is unconfirmed, because no artefact recorded the host's state during any
    of them. Recording it costs one `vm_stat`; reconstructing it afterwards is
    impossible, which is exactly why the hypothesis is still a hypothesis.

    **Unknown is `None` and never a zero.** A machine that would not answer and
    a machine with no swap in use are opposite readings, and a block that spelt
    both as `0` would retire the hypothesis on the strength of a missing tool.

    One value rather than three loose keys, for the reason `webrtc.Playout` is
    one: the three are read within a few milliseconds of each other and mean
    something only together — free memory beside a swap depth from a different
    minute describes no machine that ever existed.
    """

    free_memory_bytes: int | None
    swap_used_bytes: int | None
    load_average: tuple[float, ...] | None

    @classmethod
    def read(cls) -> HostPressure:
        """Take the reading now, answering `None` for whatever will not answer.

        These are notes on the side of a verdict: the acceptance grades the
        product, a missing `vm_stat` is not a finding about the product, and
        raising here would turn one into a refused run.
        """
        vm_stat = _read(["vm_stat"], timeout_seconds=HOST_READ_TIMEOUT_SECONDS)
        swapusage = _read(
            ["sysctl", "-n", "vm.swapusage"], timeout_seconds=HOST_READ_TIMEOUT_SECONDS
        )
        try:
            load: tuple[float, ...] | None = tuple(os.getloadavg())
        except OSError:
            load = None
        return cls(
            free_memory_bytes=None if vm_stat is None else free_memory_from(vm_stat),
            swap_used_bytes=None if swapusage is None else swap_used_from(swapusage),
            load_average=load,
        )

    def as_facts(self) -> dict[str, Any]:
        """The reading as the verdict's `environment` block carries it.

        Prefixed `host_`, because everything else in that block is about what is
        *installed* on this machine and this is about what it was *doing*.
        """
        return {
            "host_free_memory_bytes": self.free_memory_bytes,
            "host_swap_used_bytes": self.swap_used_bytes,
            "host_load_average": None if self.load_average is None else list(self.load_average),
        }


def environment_facts() -> dict[str, Any]:
    """What else is installed over the Sessions this run starts — recorded, not refused.

    Three of gen-1's parts are still on this machine while the map's disposal
    ticket (#54) waits behind every build ticket, and each one touches what this
    run means:

    * **The shell functions.** `~/.zshrc` redefines `claude` and `codex` to route
      into `claude-hosted` / `codex-hosted`. The harness executes the resolved
      binary and never a shell, so they cannot apply to a Session it starts — but
      while they are installed, the Sessions *Simon* starts are wrapped and this
      run's green does not describe them. That gap closes on #54, not here.
    * **The user-scope hooks.** `~/.claude/settings.json` carries gen-1's
      `bridge-hook` on `SessionStart`, `Stop`, `Notification` and `SessionEnd` —
      the same file and two of the same slots ADR 0011 installs into. They are
      the foreign hooks the installer's merge has to keep, and they are real
      rather than hypothetical.
    * **Its daemon.** Measured 2026-08-26: not running, its socket absent, and
      the hook logs `skipped (bridge-unavailable)`. A *running* gen-1 daemon
      would be a second bridge over the same Sessions, so the run says whether
      one is there.

    Refusing on any of this would deadlock the map — the disposal is last and
    this run is first — so it is written down instead, on the verdict where a
    reader of a green run can see what the green was measured beside.

    `HostPressure` rides in the same block for the same reason, one step
    further out: not what is installed over this run, but what the machine was
    doing underneath it. Same question — what was this green or this red
    measured beside — so the same block is where a reader will look.
    """
    hooks: list[str] = []
    if CLAUDE_SETTINGS.exists():
        try:
            settings = json.loads(CLAUDE_SETTINGS.read_text())
        except (OSError, json.JSONDecodeError):
            settings = {}
        for event, entries in (settings.get("hooks") or {}).items():
            if str(GEN1_HOOK) in json.dumps(entries):
                hooks.append(event)
    shell = os.environ.get("SHELL", "")
    functions: list[str] = []
    if shell and os.access(shell, os.X_OK):
        for name in ("claude", "codex"):
            # Deliberately not routed through `_read`: that would answer `None`
            # on a probe that timed out, and an empty `shell_wrappers` would
            # then mean both "there are none" and "this run could not tell".
            # They are different facts about what a green was measured beside,
            # so a probe that cannot run still stops the run (#230's review).
            probe = subprocess.run(
                [shell, "-lic", f"type {name} 2>/dev/null"],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=PATH_TIMEOUT_SECONDS,
            )
            if "hosted" in probe.stdout or "function" in probe.stdout:
                functions.append(f"{name}: {probe.stdout.strip().splitlines()[:1]}")
    socket = GEN1_RUNTIME.parent / "bridge.sock"
    return {
        "gen1_runtime_present": GEN1_RUNTIME.exists(),
        "gen1_hooks_in_user_settings": sorted(hooks),
        "gen1_daemon_listening": socket.exists(),
        "shell_wrappers": functions,
        "scrubbed_agent_markers": sorted(
            name
            for name in os.environ
            if name.startswith("CLAUDE_CODE_")
            or name in ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT")
        ),
        **HostPressure.read().as_facts(),
    }


def compare_engine_to_tree(bundle: Path, repository: Path) -> Provenance:
    """`diff -r` the bundle's installed package against `src/`, as `docs/app-bundle.md` does.

    Only the project's own package is compared. The interpreter and the locked
    wheels beneath it are what the signature and the lock cover; what a run has to
    know is that the *product* inside the `.app` is the product in this checkout.
    """
    commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    installed = next(
        (bundle / "Contents/Resources/engine/lib").glob("python*/site-packages/gpt_voicecoding"),
        None,
    )
    if installed is None:
        return Provenance(bundle, commit, False, ("no gpt_voicecoding package inside the bundle",))
    tree = repository / "src" / "gpt_voicecoding"
    differences = tuple(_differing(tree, installed))
    return Provenance(bundle, commit, not differences, differences)


def _differing(tree: Path, installed: Path) -> Iterator[str]:
    """Every `.py` the two sides do not agree on — **in both directions**.

    Walking the tree alone answers "is everything in this checkout in the
    bundle", which is not the question. The question is whether the engine
    inside the `.app` **is** this checkout, and a bundle carrying a module the
    checkout has since deleted is not — it is an installation that still has the
    old file, still imports it, and would still run it. A one-way compare called
    that byte-identical and let the run attribute its verdict to a tree that
    never produced it.
    """
    for source in sorted(tree.rglob("*.py")):
        relative = source.relative_to(tree)
        candidate = installed / relative
        if not candidate.exists():
            yield f"{relative} missing from the bundle"
        elif not filecmp.cmp(source, candidate, shallow=False):
            yield f"{relative} differs"
    for extra in sorted(installed.rglob("*.py")):
        relative = extra.relative_to(installed)
        if not (tree / relative).exists():
            yield f"{relative} is in the bundle and not in the tree"


# --- the run's configuration ------------------------------------------------


#: How `[adapters.settings]` names the Codex agent's own table. Built the way the
#: engine builds it (`config.py:132`), so the two cannot drift into a table the
#: engine will refuse.
CODEX_SETTINGS_KEY = f"agent.{AgentKind.CODEX}"

#: How `[adapters.settings]` names the Call seam's table — flat, by the seam name
#: the engine itself builds (`config.py:132`), for `CODEX_SETTINGS_KEY`'s reason.
CALL_SETTINGS_KEY = "call"

#: The Call adapter a Live Call run points `[adapters] call` at. Taken from the
#: module that defines it rather than spelled again here: two copies of a
#: `module:attribute` are two things to keep in step, and the engine only ever
#: resolves one of them. The module is `tests/acceptance/live_call.py`, which the
#: engine reaches because `Engine.environment` puts this directory on its
#: `PYTHONPATH`.
HARNESS_CALL_REFERENCE = live_call.REFERENCE

#: The workspace names a run that names none gets — the harness's own defaults,
#: so a caller that does not care about the folded `live call`'s pinning still writes a
#: complete table. A lane says its own (`journey.Lane`).
DEFAULT_CALL_WORKSPACES = live_call.CallWorkspaces(
    focus=live_call.FOCUS_WORKSPACE_NAME,
    ringing=live_call.RINGING_WORKSPACE_NAME,
    waiting=live_call.WAITING_WORKSPACE_NAME,
)

#: The Relay ceiling every run is given, in seconds, in place of the one the user
#: configured. The shipped default is ten minutes (`core/policy.py`), and #197
#: asks a step to observe what happens *past* the ceiling — so a run on the real
#: number would spend ten minutes per lane waiting for a clock rather than
#: proving a path. The value is short enough to sit inside one step and longer
#: than the round trip a `bridgectl relay` takes, so a Relay that is held is held
#: because the Session's window is shut and not because the harness was slow.
#: The steps read it back out of the run's config (`journey.Walk`), so this
#: number appears nowhere else.
ACCEPTANCE_RELAY_CEILING_SECONDS = 20


@dataclass(frozen=True)
class DerivedConfig:
    path: Path
    socket_path: Path
    state_path: Path
    log_path: Path
    project_name: str
    workspace: Path
    token_variable: str
    chat_id: str
    #: Where the harness's Call adapter writes what it saw, when this run has
    #: one. `None` on a run that left the user's own Call adapter in place.
    call_observations: Path | None = None
    #: Where the WAVs it synthesised are kept, for a person to listen to after.
    call_wav_directory: Path | None = None
    #: The `bridgectl` wrapper `[delegate] cli` names, when this run wrote one.
    cli_wrapper: Path | None = None
    #: Where that wrapper logs the runs the Call Agent made.
    cli_wrapper_log: Path | None = None
    #: What this lane's three extra Sessions' workspaces are called (#196,
    #: #198). The step creates the directories and the harness's Call adapter
    #: says the first of them out loud, so both halves read this one value.
    call_focus_workspace: str | None = None
    call_ringing_workspace: str | None = None
    #: The one that stops inside the Cool-down after the hang-up (#198). Nothing
    #: says it out loud; it is here so one value names it on both sides.
    call_waiting_workspace: str | None = None


def derive_config(
    *,
    source: Path,
    run_directory: Path,
    workspace: Path,
    socket_path: Path,
    project_name: str,
    token_variable: str | None = None,
    codex_socket_directory: Path | None = None,
    dropped_agents: tuple[AgentKind, ...] = (),
    harness_live_call: bool = False,
    call_workspaces: live_call.CallWorkspaces | None = None,
    control_plane_cli: Path | None = None,
) -> DerivedConfig:
    """The user's real config, with only what a run must not share redirected.

    Every value is copied, because the point of the run is to accept the engine
    the user actually configured. Three are **replaced** — the socket, the state
    and the log — because they are what a second engine would otherwise fight the
    first one over. The launcher's tables are **dropped**, which is a change of
    kind and so is said out loud:

    `[launch]`, `[[launch.projects]]` and the `session_launcher` seam are what
    #72 parked. `config.of` no longer reads any of them — `REQUIRED_SEAMS` is
    `("call", "companion_channel")` (`config.py:70`) and there is no `launch`
    section — so carrying them would be inert. Inert is not harmless here: the
    user's real `[[launch.projects]]` names twelve of his actual project
    directories, and a run config that names them is a run config that could
    point an agent at one. `[adapters] session_launcher` goes with them because
    it names a module the parked tree no longer has. What is left is a config the
    engine under test could have been given.

    **A fourth value is replaced, and it is a policy dial rather than a path:**
    `[policy] relay_ceiling_seconds` becomes `ACCEPTANCE_RELAY_CEILING_SECONDS`.
    #197 asks a step to observe what a Relay does *past* its ceiling, and the
    shipped ceiling is ten minutes — so a run on the user's own number would
    spend ten minutes per lane waiting for a clock. What acceptance proves here
    is the path a relay that finally failed takes to the user, not the number it
    waits out; the number is policy, and the fast suite holds the shipped
    default. Said out loud because it is a deviation of a different kind from
    the three above: those keep two engines out of each other's way, and this
    one changes what the engine under test does.

    One value is **rewritten** rather than copied, and only when the caller asks:
    `[adapters.settings.companion_channel] token_env`. Two lanes run at once
    (#182) and one bot serves one engine, so each lane's engine has to read its
    own bot's token — which is the shipped mechanism doing exactly what it is
    for: `token_env` is how the engine is told which variable holds the token.
    `None` keeps the user's own name, which is what the first lane wants.

    **A second value is rewritten for the same reason**, and it is the one two
    lanes cannot discover for themselves:
    `[adapters.settings.agent.codex] socket_directory`. The engine's own Codex
    app-server listens at `<socket_directory>/gpt-voicecoding-<uid>/
    codex-app-server.sock` (`adapters/agent/codex/adapter.py:143`), which is
    per **machine**, not per engine — and the product refuses rather than
    shadows it: "leaving … in place: something is still listening on it, and the
    file is what makes the next engine refuse to start". Measured on run
    `20260902T012313Z`, where the second lane's engine died at start for exactly
    that reason. Pointing each lane at its own directory is the setting doing
    what it is for; the engines then have an app-server each.

    **And one entry is dropped that the user's real config has**, which is the
    third deviation from "accept the user's real config" and the one with no
    setting behind it: `[adapters.agents]` loses every kind in `dropped_agents`,
    and the caller passes the Claude kind for the Codex lane (#202). The Claude
    approval address is a fixed file per user per machine (`locations.py:56`),
    and only an engine that loads the Claude adapter ever claims it — so an
    engine that will never walk a Claude journey has no business holding the
    machine's one Claude approval route. The product now refuses the second
    claimant rather than displacing the first; this is the harness's half, and
    with one claimant there is no contention to refuse. The Claude lane's table
    is untouched, because it is the lane that needs the route.

    **Two more are replaced when the run holds a Live Call** (`harness_live_call`,
    #183), and both are the shipped mechanism doing what it is for rather than a
    reach past it:

    `[adapters] call` is pointed at `live_call:harness_call`, which builds the
    production `RealtimeCallAdapter` with the production WebRTC transport at
    `silent=True` and feeds its track from synthesised speech. The seam is
    already a `module:attribute` composition resolves (`config.py:70`), so no
    `src/` change is needed for a call with nobody at the microphone. Its
    `[adapters.settings.call]` table carries the two paths that module cannot
    default — where it writes what it saw, and where it keeps the WAVs — and
    they are **per lane**, because both lanes hold this call at once.

    `[delegate] cli` is pointed at a wrapper that logs each invocation and execs
    the real `bridgectl`. The engine puts this value into the generated
    instructions verbatim (`composition.py:_instruction_context`) and it is
    **absolute**, so what the Call Agent runs is this run's wrapper rather than
    whatever its own PATH resolves — a PATH shadow cannot intercept an absolute
    path. The wrapper is transparent: the real CLI's output and its exit code go
    straight back, because the Call Agent branches on both.
    """
    run_directory.mkdir(parents=True, exist_ok=True)
    document = tomllib.loads(source.read_text())

    # The Delegated Turn's cost pin, and it is set here rather than left to the
    # source config for the reason the lane flags exist: the source config is the
    # person's, `[delegate] model` is the one key in it the engine calls "the cost
    # lever" outright (`config.py:199`), and a run that copied it would bill
    # whatever they were last using. Unconditional, so no path through this
    # function can leave a run spending on a model it did not name.
    delegate = dict(document["delegate"])
    delegate["model"] = DELEGATED_TURN_MODEL
    document["delegate"] = delegate

    engine = dict(document.get("engine", {}))
    engine["socket_path"] = str(socket_path)
    engine["state_path"] = str(run_directory / "state.json")
    document["engine"] = engine

    log = dict(document["log"])
    log["path"] = str(run_directory / "engine.log")
    document["log"] = log

    policy = dict(document.get("policy", {}))
    policy["relay_ceiling_seconds"] = ACCEPTANCE_RELAY_CEILING_SECONDS
    document["policy"] = policy

    dropped = [name for name in ("launch",) if document.pop(name, None) is not None]
    adapters = dict(document["adapters"])
    if adapters.pop("session_launcher", None) is not None:
        dropped.append("adapters.session_launcher")
    settings = dict(adapters.get("settings", {}))
    if settings.pop("session_launcher", None) is not None:
        dropped.append("adapters.settings.session_launcher")
    adapters["settings"] = settings
    document["adapters"] = adapters

    agents = dict(adapters["agents"])
    for kind in dropped_agents:
        # Spelled from `AgentKind` for the reason `CODEX_SETTINGS_KEY` is: a name
        # this file invented is a table the engine refuses outright.
        if agents.pop(str(kind), None) is not None:
            dropped.append(f"adapters.agents.{kind}")
        # And its settings table goes with it. `[adapters.settings]` is checked
        # against the seams the engine actually built (`config.py:132`), so a
        # settings table for an adapter that is no longer listed is a key that
        # "names no seam this engine fills" — the refusal that stopped both
        # engines on run `20260902T013222Z`. Dropping the adapter and keeping its
        # settings would hand the Codex lane a config that cannot start.
        if settings.pop(f"agent.{kind}", None) is not None:
            dropped.append(f'adapters.settings."agent.{kind}"')
    adapters["agents"] = agents

    channel = dict(settings["companion_channel"])
    if token_variable is not None:
        channel["token_env"] = token_variable
        settings["companion_channel"] = channel

    if codex_socket_directory is not None:
        # `[adapters.settings]` is keyed **flat**, by the seam names the engine
        # itself builds — `agent.<kind>`, `call`, `companion_channel`
        # (`config.py:132`) — not by nested tables. Spelled from `AgentKind`
        # rather than typed out, because a name this file invented is a table the
        # engine refuses outright: measured on run `20260902T013222Z`, where a
        # nested `[adapters.settings.agent.codex]` stopped **both** engines with
        # "names no seam this engine fills".
        codex = dict(settings.get(CODEX_SETTINGS_KEY, {}))
        codex["socket_directory"] = str(codex_socket_directory)
        settings[CODEX_SETTINGS_KEY] = codex

    observations = wav_directory = wrapper = wrapper_log = None
    focus_workspace = ringing_workspace = waiting_workspace = None
    if harness_live_call:
        observations = run_directory / "live-call.jsonl"
        wav_directory = run_directory / "live-call-wav"
        # **The three extra Sessions' workspace names are per lane, and they
        # come from here** (#196, #198). The project half of a Session Name is
        # the workspace directory's basename, and the `live call` walk says the
        # first of them out loud to pin the Session a relay must land in, and the
        # second to grade that nothing spoken ever names it — so a name shared by two
        # lanes stops pinning anything. It has to: the Codex daemon is
        # machine-wide, so the Claude lane's engine holds the Codex lane's
        # Sessions too, and run `20260903T093813Z` had it looking at two rows
        # called `二号工位 · Reply READY` and answering with `brief`.
        named = call_workspaces or DEFAULT_CALL_WORKSPACES
        focus_workspace, ringing_workspace, waiting_workspace = (
            named.focus,
            named.ringing,
            named.waiting,
        )
        adapters["call"] = HARNESS_CALL_REFERENCE
        settings[CALL_SETTINGS_KEY] = {
            **settings.get(CALL_SETTINGS_KEY, {}),
            "observations": str(observations),
            "wav_directory": str(wav_directory),
            "focus_workspace": focus_workspace,
            "ringing_workspace": ringing_workspace,
            "waiting_workspace": waiting_workspace,
        }
        wrapper_log = run_directory / "bridgectl-runs.log"
        wrapper = write_cli_wrapper(
            run_directory / "bridgectl-wrapper",
            real=control_plane_cli if control_plane_cli is not None else bundled_bridgectl(),
            log=wrapper_log,
        )
        delegate = dict(document["delegate"])
        delegate["cli"] = str(wrapper)
        document["delegate"] = delegate

    path = run_directory / "config.toml"
    path.write_text(_as_toml(document))
    (run_directory / "config-dropped.json").write_text(json.dumps(dropped, indent=2) + "\n")
    return DerivedConfig(
        path=path,
        socket_path=socket_path,
        state_path=Path(engine["state_path"]),
        log_path=Path(log["path"]),
        project_name=project_name,
        workspace=workspace,
        token_variable=str(channel["token_env"]),
        chat_id=str(channel["chat_id"]),
        call_observations=observations,
        call_wav_directory=wav_directory,
        cli_wrapper=wrapper,
        cli_wrapper_log=wrapper_log,
        call_focus_workspace=focus_workspace,
        call_ringing_workspace=ringing_workspace,
        call_waiting_workspace=waiting_workspace,
    )


#: How the wrapper stamps each run: UTC, to the second, so two lanes' logs and
#: the engine's own log can be read on one timeline.
CLI_WRAPPER_STAMP = "%Y-%m-%dT%H:%M:%SZ"


def write_cli_wrapper(path: Path, *, real: Path, log: Path) -> Path:
    """A `bridgectl` that records what it was asked and then is the real one.

    Ported from the probe's stand-in (`realtime_text_entry_probe.py:776-791`),
    with the one difference that matters: the probe's stand-in *replaced*
    `bridgectl` and printed `call ended` without ending anything, because it was
    measuring whether the Call Agent would run the verb at all. This one runs
    the real verb, because the step is measuring the route through the product —
    a `bridgectl live` that ended no call would leave `CallEnded` unobserved.

    `exec` rather than a subshell: the real CLI inherits the process, so its
    stdout, stderr and exit code reach the Call Agent unchanged and there is no
    wrapper left holding a descriptor while a call is up.

    The log line is written **before** the exec, which is the only order that
    works — after it there is no wrapper left to write anything.

    **Both paths are quoted**, and that is the whole difference between a
    wrapper that records and one that silently does not. The acceptance's own
    run directory lives under `~/Library/Application Support/…`, which has a
    space in it: unquoted, `>> $log` is an ambiguous redirect and `exec $real`
    is a command that does not exist, so every `bridgectl` the Call Agent ran
    would fail and leave no trace of having been run. Written out because a
    fixture in a `tmp_path` has no spaces and would never have shown it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        f'printf \'%s %s\\n\' "$(date -u +{CLI_WRAPPER_STAMP})" "$*" >> {shlex.quote(str(log))}\n'
        f'exec {shlex.quote(str(real))} "$@"\n',
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def cli_wrapper_runs(log: Path | None) -> list[str]:
    """Every `bridgectl` the wrapper recorded. No log is no runs, never an error.

    A lower bound by construction, which is what the step asserts on: #181
    finding 1 is that a hand-off may happen more than once per request, so the
    number of runs is not something this run gets to predict.
    """
    if log is None or not log.exists():
        return []
    return [line for line in log.read_text(errors="replace").splitlines() if line.strip()]


def _as_toml(document: dict[str, Any]) -> str:
    """The smallest TOML writer that covers this document, and it says so.

    Python ships a TOML *reader* and no writer, and the acceptance will not take a
    dependency to emit four tables. What it covers is exactly the shape
    `docs/control-plane.md` documents: tables, arrays of tables, strings, numbers,
    booleans and string arrays. A key it cannot render raises rather than being
    silently dropped — a config missing a key the user set would make the run
    accept an engine the user is not running.
    """
    lines: list[str] = []
    _emit_table(document, (), lines)
    return "\n".join(lines) + "\n"


def _emit_table(table: dict[str, Any], prefix: tuple[str, ...], lines: list[str]) -> None:
    scalars = {key: value for key, value in table.items() if not _is_table_like(value)}
    if prefix and scalars:
        lines.append(f"[{'.'.join(_quoted(part) for part in prefix)}]")
    elif prefix:
        lines.append(f"[{'.'.join(_quoted(part) for part in prefix)}]")
    for key, value in scalars.items():
        lines.append(f"{_quoted(key)} = {_as_value(value)}")
    if scalars or prefix:
        lines.append("")
    for key, value in table.items():
        if isinstance(value, dict):
            _emit_table(value, (*prefix, key), lines)
        elif _is_array_of_tables(value):
            for entry in value:
                lines.append(f"[[{'.'.join(_quoted(part) for part in (*prefix, key))}]]")
                for inner_key, inner_value in entry.items():
                    lines.append(f"{_quoted(inner_key)} = {_as_value(inner_value)}")
                lines.append("")


def _is_table_like(value: Any) -> bool:
    return isinstance(value, dict) or _is_array_of_tables(value)


def _is_array_of_tables(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(item, dict) for item in value)


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _quoted(key: str) -> str:
    return key if _BARE_KEY.match(key) else json.dumps(key)


def _as_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_as_value(item) for item in value) + "]"
    raise TypeError(
        f"the acceptance's TOML writer cannot render {value!r} ({type(value).__name__})"
    )


# --- the engine under test --------------------------------------------------

#: How long the engine gets to bind its socket before the run gives up on it.
#: Generous rather than derived; a slow one is a finding, and the log says why.
#:
#: **Deliberately not raised for a loaded machine.** On 2026-09-04 three runs
#: refused here at 22–31s while another session held this Mac at load 20–49; a
#: `sample` of a starting engine sat in `waitpid`, which is fork/exec starvation
#: rather than a child slow by design. A deadline long enough to pass under that
#: load would also have to stretch the Cool-down window, the Silence Ceiling and
#: the Voice watch — every one of which is a *measurement*, not a wait. A walk
#: that passed by widening them would be grading the machine. So the refusal
#: says what the load was (`load_now`) and the run is made again when it drops.
ENGINE_START_SECONDS = 30.0


def load_now() -> str:
    """The machine's load averages, so a refusal can say whether it was the machine.

    Read at the moment of the complaint rather than at the start of the run: what
    a reader needs to know is what this engine was competing with while it failed
    to bind.
    """
    try:
        one, five, fifteen = os.getloadavg()
    except OSError:  # pragma: no cover - POSIX always has it; a refusal still reads
        return "load unavailable"
    return f"load {one:.1f} / {five:.1f} / {fifteen:.1f} (1/5/15 min)"


#: The grace a stopped engine gets to unlink its socket and let its Sessions go.
ENGINE_STOP_SECONDS = 20.0


class Engine:
    """The bundle's own interpreter, running the bundle's own engine.

    Spawned by the harness rather than by the menu-bar shell: repeatability over
    coverage, and the shell is out of scope (`docs/acceptance-design.md` § Ruled).
    What the shell *does* contribute — the login shell's PATH — is reproduced
    here, because without it the launcher cannot find `claude` or `codex`.
    """

    def __init__(
        self,
        *,
        config: DerivedConfig,
        bundle: Path,
        journal: Journal,
        token: str,
        path_value: str,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config
        self._bundle = bundle
        self._journal = journal
        self._token = token
        self._path_value = path_value
        #: What the engine's environment is built on. The process's own, unless
        #: a caller states one — which only a test does, so that "an existing
        #: `PYTHONPATH` is extended rather than replaced" can be checked without
        #: writing into the environment of whoever is running the suite.
        self._base = dict(os.environ if environment is None else environment)
        self._process: subprocess.Popen[bytes] | None = None

    @property
    def environment(self) -> dict[str, str]:
        """The real HOME, the login shell's PATH, and the token by its own name.

        `HOME` is the user's own because the real `claude` keeps its login and its
        session registry there, and an engine given a temporary one would launch
        an agent that has never been logged in. That is the one thing the run
        cannot isolate; the workspaces and every path the engine writes are under
        the run directory instead.

        **`PYTHONPATH` gains this directory, and only on a run that needs it**
        (#183). `[adapters] call` is a `module:attribute` the *engine* imports,
        and on a Live Call run that module is the harness's own `live_call` —
        which the bundle's interpreter, carrying the product and not the tests,
        otherwise cannot see.

        Conditional rather than always, because this directory is a flat pile of
        modules with ordinary names (`support`, `journey`, `live_call`) and
        putting it **ahead** of the engine's own import path changes what every
        `import` in that process resolves to. A run that never dials has no
        reason to pay that, and the run this harness exists to accept is the one
        with nothing of the harness in it.

        Extended, never replaced: the user's own environment is what reaches the
        engine, and dropping an entry of it would be the harness quietly
        changing the thing it is accepting.
        """
        environment = dict(self._base)
        environment["PATH"] = self._path_value
        environment[self._config.token_variable] = self._token
        if self._config.call_observations is not None:
            roots = [str(harness_root())]
            existing = environment.get("PYTHONPATH", "")
            roots.extend(part for part in existing.split(os.pathsep) if part)
            environment["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(roots))
        return environment

    def start(self) -> None:
        command = [
            str(bundled_python(self._bundle)),
            "-m",
            "gpt_voicecoding.engine",
            "--config",
            str(self._config.path),
        ]
        self._journal("engine.start", command=command, socket=str(self._config.socket_path))
        # **Not redirected**, and that is ADR 0004 rather than an oversight: "the
        # engine owns its log … nothing that starts the engine redirects output".
        # The legacy log grew ~1 GB/month and could not be rotated precisely
        # because a shell redirect owned the descriptor, and a harness that took
        # the descriptor here would be that shell. The engine `dup2`s the
        # configured log — `engine-<lane>/engine.log`, under the run directory —
        # onto its own stdout and stderr before it exists, so a redirect here
        # would only ever have caught the moments before that, which the ADR
        # accounts for in as many words: "Output before adoption is discarded —
        # an engine dying that early is surfaced by silence on the socket."
        # `start` below is that silence, with the reason on the exception.
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            env=self.environment,
        )
        expiry = time.monotonic() + ENGINE_START_SECONDS
        while time.monotonic() < expiry:
            if self._config.socket_path.exists():
                self._journal("engine.listening", socket=str(self._config.socket_path))
                return
            if self._process.poll() is not None:
                raise EngineRefused(
                    f"the engine exited {self._process.returncode} before binding its socket; "
                    f"log at {self._config.log_path} (ADR 0004: output before the engine adopts "
                    f"its own log is discarded, and this silence is how that surfaces)"
                )
            time.sleep(0.2)
        # **A refusal takes the engine with it.** Without this the process goes
        # on starting after the walk has given up on it, and the next run meets
        # a second bridge over every Session on this machine: run
        # `20260904T002514Z` refused and left two engines holding the published
        # approval address, and `20260904T002637Z` refused behind them. The
        # start that failed is this object's to clean up, because nothing else
        # has a handle on it.
        self.stop()
        raise EngineRefused(
            f"the engine did not bind {self._config.socket_path} within "
            f"{ENGINE_START_SECONDS:.0f}s at {load_now()}; log at {self._config.log_path}"
        )

    def stop(self) -> None:
        if self._process is None or self._process.poll() is not None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=ENGINE_STOP_SECONDS)
        except subprocess.TimeoutExpired:
            self._journal("engine.killed", reason="did not exit on SIGTERM")
            self._process.kill()
            self._process.wait(timeout=ENGINE_STOP_SECONDS)
        self._journal("engine.stopped", returncode=self._process.returncode)

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def log_lines(self) -> list[str]:
        if not self._config.log_path.exists():
            return []
        return self._config.log_path.read_text(errors="replace").splitlines()


class EngineRefused(RuntimeError):
    """The engine under test never came up."""


# --- the surface ------------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    """One `bridgectl` call and what came back."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """Exit 0 — the engine answered and did not refuse. `bridgectl` § Three exits."""
        return self.returncode == 0

    @property
    def text(self) -> str:
        return self.stdout.strip() if self.ok else self.stderr.strip()


class Bridgectl:
    """Every product action the run performs, through the bundle's own CLI."""

    def __init__(self, *, bundle: Path, socket_path: Path, journal: Journal) -> None:
        self._executable = bundled_bridgectl(bundle)
        self._socket = socket_path
        self._journal = journal

    def __call__(self, *arguments: str, timeout: float | None = None) -> Answer:
        deadline = timeout if timeout is not None else _default_deadline()
        # `--timeout` is passed only when the caller states one. Left off, the CLI
        # picks its own deadline, which is the behaviour the run is accepting;
        # passing it every time would hide exactly the mismatch `relay` records.
        stated = ["--timeout", f"{deadline:.1f}"] if timeout is not None else []
        argv = [str(self._executable), "--socket", str(self._socket), *stated, *arguments]
        finished = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            # The CLI carries its own deadline; this one only stops a CLI that
            # has stopped carrying it. Generous over it on purpose, so a timeout
            # here always means the *surface* hung and never that the action was
            # merely slow — the distinction #28 was about.
            timeout=deadline + 30.0,
        )
        answer = Answer(tuple(argv), finished.returncode, finished.stdout, finished.stderr)
        self._journal(
            "bridgectl",
            command=list(arguments),
            returncode=answer.returncode,
            stdout=answer.stdout.strip(),
            stderr=answer.stderr.strip(),
            deadline_seconds=deadline,
            deadline_stated=timeout is not None,
        )
        return answer


def _default_deadline() -> float:
    """The deadline `bridgectl` itself would use — read from the client, not copied.

    This used to call `control_plane.client.timeout_for` and take the action,
    because `launch` had a longer budget than everything else. #72 parked the
    launcher and `timeout_for` went with it: there is one number now, and a
    parameter kept for a per-action future that may never come is a parameter
    that lies about what this function does. When an action needs its own budget
    again, this grows one then. `relay` already needs more than the surface gives
    it and says so in one place — `RELAY_DEADLINE_SECONDS`, passed explicitly.
    """
    return DEFAULT_TIMEOUT_SECONDS


#: What a relay actually needs, derived rather than picked — and *not* what the
#: surface gives it.
#:
#: `bridgectl` hands every action the 10-second default
#: (`control_plane/client.py:44`), while the engine's own proof of delivery on
#: the Claude lane waits `DEFAULT_ACK_TIMEOUT_SECONDS` — 45 seconds
#: (`adapters/agent/claude/settings.py:34`) — for the Session to call
#: `acknowledge_answer`. Measured at build time on this machine: `bridgectl relay`
#: answers "the engine … did not answer within 10s" while `engine.log` goes on to
#: say "relay … not proven delivered … within 45s; it waits". The surface cannot
#: reach the reply. That is #28's shape pointed at `relay`, and the run records it
#: as a finding of its own.
#:
#: The harness passes this instead, so what step 5 observes is the *relay*, not
#: the CLI's deadline. Headroom over the ack wait, in the style of
#: `LAUNCH_TIMEOUT_SECONDS`: the reply is emitted when the wait resolves, so the
#: surface has to outlive it rather than race it.
#:
#: The mismatch itself is no longer a step. #60 raised it as `5b`, and #73's nine
#: names do not include it — it is a defect in `relay`, and `relay` is #77's, so
#: it belongs in that step's evidence rather than as a tenth red line of its own.
#: The two numbers stay named here so the evidence can quote them.
RELAY_DEADLINE_SECONDS = DEFAULT_ACK_TIMEOUT_SECONDS + 30.0
SURFACE_RELAY_DEADLINE_SECONDS = DEFAULT_TIMEOUT_SECONDS
ENGINE_RELAY_PROOF_SECONDS = DEFAULT_ACK_TIMEOUT_SECONDS


# --- the one documented side channel ----------------------------------------


def control_plane_status(socket_path: Path, journal: Journal) -> dict[str, Any]:
    """Read `status`'s **payload**, not `bridgectl`'s rendering of it.

    This exists for exactly one value: `approval_id`. It rides the roster row's
    `waiting_for` in the control-plane reply
    (`control_plane/payloads.py::waiting_for_document`) and it reaches **no human
    surface** — `bridgectl status` prints a row's name and state and not its
    dialog handle, the Stop Notice a permission produces does not carry it
    (`core/briefing.py::_decision_lines`), and the Swift shell counts those rows
    without naming one. So `bridgectl approve <id>` cannot be driven by anyone
    reading a surface.

    Legacy has **no** counterpart behaviour to cite: its permission handling
    pushed "Claude needs your permission to use X" as a *notice*
    (`bridge/daemon.py:143` in the legacy checkout) and the user answered at the
    keyboard. There was never an id-addressed `approve` route, which is why
    nobody found this.

    That is a finding, and the lane records it as one. This side channel is how
    the run gets *past* it, so a single expensive run reports every red rather
    than the first. Every call is journaled as `side-channel`, so no verdict can
    rest on it without the journal saying so.
    """
    return control_plane_payload(
        Action.STATUS,
        socket_path=socket_path,
        journal=journal,
        why="approval_id reaches no surface",
    )


def control_plane_payload(
    action: Action,
    *,
    socket_path: Path,
    journal: Journal,
    payload: dict[str, Any] | None = None,
    why: str,
) -> dict[str, Any]:
    """One control-plane reply, read as the payload `bridgectl` itself receives.

    `bridgectl` is `ask` plus `render`, so this is not a private route into the
    engine — it is the same request with the rendering left off. It exists for
    facts the rendering does not carry, and there are two:

    * `approval_id`, above; and
    * the roster row's own fields. `_roster_lines`
      (`control_plane/commands.py:168`) renders five of them into one line, and
      the steps after `roster` need others — the Session name #78 stabilises,
      the `waiting_for` #75 fills, the roster progress summary #147 publishes, the
      `ChildClassification` #79 sets. None of those tickets locks a *text
      format*, and none should have to: a step asserting on the wording of that
      one line would be asking a build ticket to invent a format and then
      holding it to this harness's guess at one. So the steps read the fields
      and leave the wording to whoever writes it.

    Every call is journaled as `side-channel` with its reason, so no verdict can
    rest on one without the journal saying so.
    """
    reply = asyncio.run(ask(Request(action=action, payload=dict(payload or {})), path=socket_path))
    data = dict(reply.data)
    journal("side-channel", why=why, action=action.value, data=data)
    return data


# --- far-side deadlines -----------------------------------------------------


@dataclass(frozen=True)
class FarSideDeadlines:
    """Every wait on something the engine does not answer for.

    Each is a *measured* number with headroom, not a guess, and each says what it
    was measured against. `docs/acceptance-design.md` § Deadlines requires that;
    `conftest.py` fills them in from the measurement recorded on ticket #60.
    """

    agent_turn_seconds: float
    telegram_round_trip_seconds: float
    workspace_effect_seconds: float
    absence_window_seconds: float


def wait_for(
    predicate,  # noqa: ANN001 - any zero-argument question
    *,
    deadline_seconds: float,
    poll_seconds: float = 0.5,
):  # noqa: ANN201 - whatever the predicate returns
    """Poll a question until it answers truthily, or return its last falsey answer."""
    expiry = time.monotonic() + deadline_seconds
    answer = predicate()
    while not answer and time.monotonic() < expiry:
        time.sleep(poll_seconds)
        answer = predicate()
    return answer


# --- the verdict ------------------------------------------------------------


class Result(StrEnum):
    """The closed set a step's verdict comes from, and it is closed for a reason.

    `docs/acceptance-design.md` § Artifacts names exactly these four. They were
    four bare module-level strings, which is the shape that lets a typo become a
    fifth state nobody notices until a reader is told a run `PASSSED`. A StrEnum
    keeps them comparable to and serialisable as the same strings — `verdict.json`
    is byte-identical either way — while making the set the type rather than a
    convention. The same choice, for the same reason, as `seams/delivery.Delivery`
    and `core/lifecycle.Lifecycle`.
    """

    PASS = "PASS"
    FAIL = "FAIL"
    REFUSED = "REFUSED"
    SKIPPED = "SKIPPED"


#: Named at module level as well, because every call site reads better as
#: `support.PASS` than `support.Result.PASS`, and the enum is what they are.
PASS = Result.PASS
FAIL = Result.FAIL
REFUSED = Result.REFUSED
SKIPPED = Result.SKIPPED


@dataclass
class StepVerdict:
    step: str
    result: str
    evidence: str
    #: Whether this row is one of the steps the run **promised**. A run that
    #: selected one step still walks that step's prerequisites, and those rows are
    #: evidence about the arrangement rather than claims about the product — see
    #: `Verdict.result`, which is decided by the graded rows alone.
    graded: bool = True


@dataclass
class Verdict:
    """`verdict.json` — the one artifact a reader needs.

    A run is `PASS` only when every step on every lane is `PASS`
    (`docs/acceptance-design.md` § Artifacts and the verdict).
    """

    run_id: str
    bundle: str
    commit: str
    provenance: str
    versions: dict[str, str] = field(default_factory=dict)
    #: What else was installed over the Sessions this run started. See
    #: `environment_facts`: recorded rather than refused, because a green run
    #: beside a live gen-1 install means something different from a green run
    #: without one.
    environment: dict[str, Any] = field(default_factory=dict)
    #: Things the run *arranged* or *saw* but does not grade — the trust gate,
    #: #44's approval directory. Kept out of `lanes` so the graded step set stays
    #: exactly the ones the build tickets cite, and kept out of nothing else.
    observations: list[dict[str, Any]] = field(default_factory=list)
    lanes: dict[str, list[StepVerdict]] = field(default_factory=dict)
    #: What the run promised to observe: the **selected** steps, which is all nine
    #: on a full run and one ticket's step on a single-step run. `missing` is the
    #: difference between this and what it recorded, and `result` will not say
    #: PASS while any is outstanding.
    expected_lanes: tuple[str, ...] = ()
    expected_steps: tuple[str, ...] = ()
    #: The prerequisites those selected steps were walked on, ungraded. Written
    #: beside them so `verdict.json` says which steps a green run actually judged.
    setup_steps: tuple[str, ...] = ()
    #: Two lanes write here at once (#182: one thread each), and every mutation
    #: below is a read-modify-write of a list or a dict. One lock, rather than a
    #: promise that two threads never interleave. `write` is not under it: it runs
    #: once, from the fixture's teardown, after both lanes have been joined.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def observe(self, lane: str, what: str, detail: str) -> None:
        with self._lock:
            self.observations.append({"lane": lane, "what": what, "detail": detail})

    def record(
        self, lane: str, step: str, result: Result, evidence: str, *, graded: bool = True
    ) -> StepVerdict:
        recorded = StepVerdict(step=step, result=Result(result), evidence=evidence, graded=graded)
        with self._lock:
            self.lanes.setdefault(lane, []).append(recorded)
        return recorded

    def refuse(self, lane: str, reason: str) -> None:
        """Write a refusal down before raising it.

        Preflight refuses rather than runs, and the design says the refusal
        carries "verdict `REFUSED` with the reason". A refusal that only raised
        left `verdict.json` with no trace of why the run stopped: the reason
        reached the terminal, and nothing that outlives it.
        """
        self.record(lane, "preflight", REFUSED, reason)

    @property
    def missing(self) -> tuple[str, ...]:
        """Every (lane, step) the run promised and did not record.

        A run is judged on what it **set out** to observe, not on what it managed
        to. Without this, a lane that never ran — a collection error, a fixture
        raising, a lane deselected by hand — contributes no rows at all, and a
        verdict made only of the surviving lane's greens says `PASS` for a run
        that observed half the product. That is the one failure mode a verdict
        file must not have, because it is the one nobody re-checks.
        """
        absent: list[str] = []
        for lane in self.expected_lanes:
            recorded = {step.step for step in self.lanes.get(lane, []) if step.graded}
            absent.extend(f"{lane}/{step}" for step in self.expected_steps if step not in recorded)
        return tuple(absent)

    @property
    def result(self) -> Result:
        """Decided by the graded rows alone.

        A setup row is how the run *reached* the step it promised, not a claim
        about the product, so it never decides what the run says. It cannot
        quietly hide a red either: `Journey` blocks the lane on a failed setup
        step, and the graded steps behind it are then `SKIPPED`, which is not PASS.
        """
        recorded = [step for steps in self.lanes.values() for step in steps]
        if not recorded:
            # Nothing at all was written down: the run refused before it could
            # observe anything. A run that recorded only *setup* rows did start,
            # and owes the step it promised — that is `missing`, and a FAIL.
            return REFUSED
        results = [step.result for step in recorded if step.graded]
        if any(result == REFUSED for result in results):
            return REFUSED
        if self.missing:
            return FAIL
        return PASS if all(result == PASS for result in results) else FAIL

    def write(self, path: Path) -> Path:
        path.write_text(
            json.dumps(
                {
                    "run_id": self.run_id,
                    "result": str(self.result),
                    "missing": list(self.missing),
                    "selection": {
                        "selected": list(self.expected_steps),
                        "setup": list(self.setup_steps),
                    },
                    "bundle": self.bundle,
                    "commit": self.commit,
                    "provenance": self.provenance,
                    "versions": self.versions,
                    "environment": self.environment,
                    "observations": self.observations,
                    "lanes": {
                        lane: [
                            {
                                "step": step.step,
                                "result": str(step.result),
                                "graded": step.graded,
                                "evidence": step.evidence,
                            }
                            for step in steps
                        ]
                        for lane, steps in self.lanes.items()
                    },
                },
                indent=2,
            )
            + "\n"
        )
        return path


def write_refusal(run_directory: Path, reason: str) -> Path:
    """The smallest honest `verdict.json` — for a run that refused before it had one.

    A refusal that reached only the terminal left an artifact directory a reader
    could not interpret: a run id, maybe a journal, and no statement of why
    nothing else is there. This is that statement. It carries the same `result`
    vocabulary as a full verdict and says plainly that the refusal preceded the
    facts a full one would have named — the bundle, the commit, the versions —
    rather than inventing them.
    """
    path = run_directory / "verdict.json"
    path.write_text(
        json.dumps(
            {
                "run_id": run_directory.name,
                "result": str(REFUSED),
                "reason": reason,
                "note": (
                    "step 0 refused before the run had a verdict to write on, so the bundle, "
                    "commit and versions a full verdict names were never established"
                ),
            },
            indent=2,
        )
        + "\n"
    )
    return path


# --- versions the verdict names ---------------------------------------------


def binary_version(binary: str, path_value: str) -> str:
    """`<binary> --version` on the PATH the engine will be handed, or why not."""
    resolved = shutil.which(binary, path=path_value)
    if resolved is None:
        return "not on the engine's PATH"
    try:
        finished = subprocess.run(
            [resolved, "--version"], capture_output=True, text=True, timeout=30.0
        )
    except (subprocess.TimeoutExpired, OSError) as failure:
        return f"{resolved}: {failure}"
    return f"{resolved}: {(finished.stdout or finished.stderr).strip().splitlines()[0]}"


def run_id(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")


def new_run_directory(identifier: str | None = None) -> Path:
    directory = acceptance_root() / (identifier or run_id())
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def workspace_path(run_directory: Path, lane: str) -> Path:
    """The one disposable-workspace location used by run construction and preflight."""
    return run_directory / f"workspace-{lane}-{run_directory.name}"


def fresh_workspace(run_directory: Path, lane: str, path_value: str) -> Path:
    """A disposable `git init` directory, one per lane, kept with the run."""
    return workspace_at(workspace_path(run_directory, lane), path_value)


def workspace_at(workspace: Path, path_value: str) -> Path:
    """One disposable `git init` directory, at a path the caller chose.

    Split out of `fresh_workspace` because one caller needs to choose the
    *basename*: the project half of a Session Name is the workspace directory's
    name (`adapters/agent/_project.py`), so a step that has to say a Session's
    name out loud has to pick the directory it is named for (#196).
    """
    workspace.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--quiet", str(workspace)],
        check=True,
        capture_output=True,
        env={**os.environ, "PATH": path_value},
    )
    return workspace


def token_from_environment(variable: str) -> str:
    """The bot token, by the name the run's own config gives it, or a refusal."""
    token = os.environ.get(variable)
    if not token:
        raise LookupError(
            f"{variable} is not set. The acceptance drives the real bot, and the token has no "
            f"durable home yet (#55). Export it into the shell that runs pytest."
        )
    return token


def chat_open_refusal(
    transport: Transport,
    *,
    chat_id: str,
    bot_username: str,
    timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
) -> str | None:
    """Why this bot cannot reach the chat it is configured for, or None when it can.

    **A bot cannot open a chat with a person.** Telegram gives the first move to
    the human: until the account has sent `/start`, every `sendMessage` from the
    bot is refused, and a run that discovered that mid-journey would report it as
    a product red on whichever step read the chat first. #182 adds a second bot,
    which makes "the user never opened this one" an ordinary state of a fresh
    machine rather than a hypothetical — so it is a preflight refusal naming the
    human step, in the shape `docs/acceptance-design.md` § Preflight fixes.

    Asked with `getChat`, which is a **read**. The obvious alternative — send
    something and see whether it lands — would write a probe into the very chat
    the run then reads for evidence, and every step's attribution rule would have
    to know about it.
    """
    try:
        transport("getChat", {"chat_id": chat_id}, timeout_seconds=timeout_seconds)
    except TelegramError as refused:
        return (
            f"@{bot_username} cannot reach chat {chat_id}: {refused.detail}. A bot cannot open "
            f"a chat with a person — send `/start` to @{bot_username} from the Telegram user "
            f"account this acceptance drives, then run again."
        )
    return None


def duplicate_bot_refusal(
    bots: Mapping[str, Mapping[str, Any]], *, variables: Mapping[str, str]
) -> str | None:
    """Why two lanes are pointed at the same bot, or None when each has its own.

    **Two lanes at once is two bots, and nothing but this check says so.** One bot
    serves one engine (`docs/app-bundle.md` § Cutover), and the reason is
    mechanical: the engine reaches Telegram by long-polling `getUpdates`, and two
    engines polling one bot take each other's updates — an inbound message reaches
    whichever asked first, at random. The lanes would then be neither isolated nor
    reproducible, and every red would be somebody else's.

    The variables are two names in one `.env`, so "both hold the same token" is a
    copy-paste away and answers `getMe` perfectly on both. Only the identity the
    two calls come back with can tell them apart.
    """
    together: dict[Any, list[str]] = {}
    for lane, identity in bots.items():
        together.setdefault(identity["id"], []).append(lane)
    shared = {bot: lanes for bot, lanes in together.items() if len(lanes) > 1}
    if not shared:
        return None
    return "; ".join(
        f"the {' and '.join(sorted(lanes))} lanes resolve to the same bot "
        f"@{bots[lanes[0]]['username']} (id {bot}) — "
        f"{', '.join(variables[lane] for lane in sorted(lanes))} hold the same token, and two "
        f"engines long-polling one bot take each other's updates"
        for bot, lanes in shared.items()
    )


def matching_lines(lines: Sequence[str], pattern: str) -> list[str]:
    compiled = re.compile(pattern)
    return [line for line in lines if compiled.search(line)]


def files_under(directory: Path) -> set[Path]:
    """Every file an agent could have left, ignoring the `git init` skeleton."""
    return {
        path.relative_to(directory)
        for path in directory.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }


def read_if_exists(path: Path) -> str | None:
    return path.read_text(errors="replace") if path.exists() else None


def flatten(values: Iterable[Any]) -> str:
    return ", ".join(str(value) for value in values)


# --- walking a lane's journey ----------------------------------------------


class StepFailed(Exception):
    """This step did not observe what it must. The lane goes on."""


class LaneBlocked(Exception):
    """This step did not observe what it must, and nothing after it can run."""


class Journey:
    """One lane's walk through the steps, recording a verdict for every one.

    A step that fails does **not** end the run. An acceptance whose job is to
    find bugs is worth far more when one expensive walk reports every red than
    when it reports the first — so an ordinary failure is recorded and the walk
    continues, and only a step that leaves nothing to observe (no Session, no
    engine) blocks the rest, which are then `SKIPPED` rather than silently absent.

    **A step walked as setup is the one other thing that blocks.** `steps` is what
    this walk runs and `setup` is the part of it the run did not promise — the
    prerequisites a `--step` selection brought with it (#182). A setup step that
    fails has not found a bug in the step the run is grading; it has failed to
    arrange the ground that step needs. Grading the selection on that ground would
    report the arrangement as the product's red, so the lane blocks instead and
    what the run promised is `SKIPPED`.
    """

    def __init__(
        self,
        *,
        lane: str,
        verdict: Verdict,
        journal: Journal,
        steps: Sequence[str],
        setup: Sequence[str] = (),
    ):
        self.lane = lane
        self._verdict = verdict
        self._journal = journal
        self._remaining = list(steps)
        self._setup = set(setup)
        self._blocked: str | None = None

    def graded(self, step: str) -> bool:
        return step not in self._setup

    def run(self, step: str, action) -> Any:  # noqa: ANN001 - a zero-argument step
        """Run one step, record its verdict, and return whatever it observed."""
        if step in self._remaining:
            self._remaining.remove(step)
        if self._blocked is not None:
            self._record(step, SKIPPED, f"blocked by {self._blocked}")
            return None
        self._journal("step.start", lane=self.lane, step=step, graded=self.graded(step))
        try:
            evidence = action()
        except LaneBlocked as blocking:
            self._record(step, FAIL, str(blocking))
            self._blocked = step
            return None
        except StepFailed as failure:
            self._record(step, FAIL, str(failure))
            if not self.graded(step):
                self._blocked = f"{step} (setup)"
            return None
        self._record(step, PASS, str(evidence))
        return evidence

    def observe(self, what: str, detail: str) -> None:
        """Write down something the run arranged, or saw and does not grade.

        Kept out of the graded step set on purpose. The steps are cited
        **verbatim** by the "Red first" line of every build ticket (#74–#80), so
        a tenth row in `lanes` would read as a tenth red line for someone to
        clear. These land in `verdict.observations` instead, where a reader sees
        them beside the run they belong to and nobody mistakes them for exits.
        """
        self._verdict.observe(self.lane, what, detail)
        self._journal("observation", lane=self.lane, what=what, detail=detail)

    def skip_rest(self, why: str) -> None:
        for step in list(self._remaining):
            self._record(step, SKIPPED, why)
            self._remaining.remove(step)

    def _record(self, step: str, result: Result, evidence: str) -> None:
        graded = self.graded(step)
        self._verdict.record(self.lane, step, result, evidence, graded=graded)
        self._journal(
            "step.verdict",
            lane=self.lane,
            step=step,
            result=result,
            graded=graded,
            evidence=evidence,
        )


# --- the trust gate ---------------------------------------------------------

#: Where each agent records that a directory has been trusted. Both were read off
#: this machine rather than remembered: Claude Code's state file keeps a
#: `projects` map whose entries carry `hasTrustDialogAccepted`, and
#: `~/.codex/config.toml` keeps `[projects."<path>"] trust_level = "trusted"`.
CLAUDE_STATE_NAME = ".claude.json"
CODEX_CONFIG = Path.home() / ".codex" / "config.toml"
CLAUDE_TRUST_KEY = "hasTrustDialogAccepted"


def claude_state_path(environment: Mapping[str, str], home: Path | None = None) -> Path:
    """Which Claude state file a Session launched with this environment will read.

    **Not a constant, because it was one and that cost a lane** (#217). Run
    `20260903T050619Z` granted trust in `~/.claude.json`, journalled
    `trust.granted`, and still met the full-screen dialog: the shell that started
    it exported `CLAUDE_CONFIG_DIR=~/.claude-b`, `hand_started.terminal_environment`
    scrubs only the agent markers, so the Session read
    `~/.claude-b/.claude.json` — a file the gate had never opened. `roster` failed
    ungraded and the two graded steps behind it were SKIPPED, and nothing in the
    journal said the arrangement had missed.

    Measured on 2026-09-03 against `claude` 2.1.259, in a pty over a `git init`
    workspace under the acceptance root: with the variable set, only an entry in
    `$CLAUDE_CONFIG_DIR/.claude.json` clears the dialog; with it unset, only an
    entry in `~/.claude.json`.

    **The unset default is the home directory, not the default config
    directory**, which is the whole reason this is its own rule rather than
    `claude_hooks.default_config_directory(environment) / CLAUDE_STATE_NAME`. The
    *named* case defers to that function anyway, so the harness and the engine
    read one rule about what `CLAUDE_CONFIG_DIR` means — `bridgectl verify`
    reports which directory it checked hooks under, and a run where those two
    disagreed is what this whole function exists to stop.

    **Legacy (ADR 0010).** *Dropped, because gen-1 never met this gate.* Its
    acceptance is a person at a keyboard answering `pass`/`fail`
    (`acceptance.sh:16-33` — `read -r -p "${title} [pass/fail]: "` at :20), so nothing
    in it ever launched an agent into a directory the agent had not seen, and
    there is no trust gate there to port. What gen-1 *does* have is the nearest
    relative of the writing half, and it is ported rather than reinvented: its
    preflight edits the user's own `~/.codex/config.toml` and is held to keeping
    every unrelated line, `[projects."…"] trust_level = "trusted"` included
    (`test_preflight.py:235-255`). That is the same promise `_trust_claude` and
    `_untrust_claude` keep about the entries this run did not write. Gen-1 also
    knew `CLAUDE_CONFIG_DIR` is the knob that isolates a Claude install
    (`bridge/channelplugin.py:117-124`, measured against 2.1.222) — it had no
    reader that could disagree with itself about which directory it meant, which
    is the gap this function closes for the harness.
    """
    home = home or Path.home()
    stated = environment.get(claude_hooks.CONFIG_DIRECTORY_VARIABLE)
    if stated and stated.strip():
        return claude_hooks.default_config_directory(environment, home) / CLAUDE_STATE_NAME
    return home / CLAUDE_STATE_NAME


#: Both lanes grant trust in the same two files, and both grants are a
#: read-modify-write of a file **the user owns** — `~/.claude.json` is their whole
#: Claude state. Two lanes running at once (#182) would interleave read, read,
#: write, write, and the second write would be a copy of the file without the
#: first lane's entry: the lane that lost the race then runs untrusted, and worse,
#: the gate that wrote last removes an entry the other gate is still relying on.
#: One process-wide lock, held across a whole grant and a whole revoke.
_TRUST_LOCK = threading.Lock()


class TrustGate:
    """Trust one disposable workspace for both agents, and put the files back after.

    **Why this is a precondition and not a step.** A launch into a directory the
    agent has never seen stops at a full-screen "Is this a project you created or
    one you trust?" and the Session never registers — measured on this machine at
    build time, and it is the same gate #18 reports for Codex. A fresh workspace
    is what `docs/acceptance-design.md` requires, so every run would end at step
    1a for a reason that is real but singular. Arranging trust is how the other
    seven steps stay observable; that the product cannot cross this gate on its
    own is recorded as its own finding rather than hidden by the arrangement.

    Both files are the user's, so both are backed up into the run directory before
    a byte is written and both are restored on the way out — the entry this adds
    is removed, and nothing else in either file is touched.

    **Trusted under both spellings.** Measured on 2026-08-26: `claude agents
    --json` reports a Session's `cwd` resolved, and `codex`'s `session_meta.cwd`
    the same. A run directory under Application Support is not behind a symlink
    today, so the two spellings coincide — but a gate that only holds while that
    stays true is a gate that fails once and confusingly. Both are granted when
    they differ, and both are revoked.

    **The gate is told the environment its Session will be launched with**, and
    resolves the Claude state file from it (`claude_state_path`). It used to
    write a module constant built from `Path.home()`, which is #217: a run under
    `CLAUDE_CONFIG_DIR` arranged trust in a file nobody read, reported
    `trust.granted`, and lost its whole lane to the dialog it had just paid to
    avoid. The journal now names the file, so the next such run says so.
    """

    def __init__(
        self,
        workspace: Path,
        *,
        run_directory: Path,
        journal: Journal,
        label: str,
        environment: Mapping[str, str],
    ) -> None:
        self._workspace = workspace
        self._paths = sorted({str(workspace), os.path.realpath(workspace)})
        self._run_directory = run_directory
        self._journal = journal
        #: Which lane this gate belongs to. It names the backup, because two gates
        #: writing `.claude.json.before-trust` would have the second one copy a
        #: file the first had already changed — and the pristine copy, which is
        #: the whole point of a backup, would be gone.
        self._label = label
        self._claude_state = claude_state_path(environment)
        #: What each granted spelling looked like before the grant: an entry to
        #: put back, or `None` where there was none. A revoke that only deleted
        #: would take an entry the user already had — the whole point of writing
        #: into their file is that some of what is in it is theirs.
        self._claude_restore: dict[str, dict[str, Any] | None] = {}
        self._codex_block: str | None = None

    def __enter__(self) -> TrustGate:
        with _TRUST_LOCK:
            self._trust_claude()
            self._trust_codex()
        return self

    def __exit__(self, *_: object) -> None:
        with _TRUST_LOCK:
            self._untrust_claude()
            self._untrust_codex()

    def _backup(self, path: Path) -> None:
        if path.exists():
            shutil.copy2(path, self._run_directory / f"{path.name}.before-trust-{self._label}")

    def _trust_claude(self) -> None:
        state_path = self._claude_state
        if not state_path.exists():
            self._journal(
                "trust.absent", agent="claude", path=str(state_path), state=str(state_path)
            )
            return
        self._backup(state_path)
        state = json.loads(state_path.read_text())
        projects = state.setdefault("projects", {})
        #: **Untrusted is not absent.** The question is whether this path is
        #: trusted, and an entry that says `false` — or one that carries the
        #: user's `allowedTools` and no verdict at all — answers no while being
        #: present. Asking `path not in projects` reported `trust.already` and
        #: granted nothing for both, which is the #217 failure by a second route
        #: on any machine that has opened this directory once before.
        granted = [
            path for path in self._paths if not (projects.get(path) or {}).get(CLAUDE_TRUST_KEY)
        ]
        if not granted:
            self._journal(
                "trust.already", agent="claude", workspaces=self._paths, state=str(state_path)
            )
            return
        for path in granted:
            existing = projects.get(path)
            self._claude_restore[path] = deepcopy(existing) if existing is not None else None
            projects[path] = {**(existing or {}), CLAUDE_TRUST_KEY: True}
        state_path.write_text(json.dumps(state, indent=2))
        self._journal("trust.granted", agent="claude", workspaces=granted, state=str(state_path))

    def _untrust_claude(self) -> None:
        state_path = self._claude_state
        if not self._claude_restore or not state_path.exists():
            return
        state = json.loads(state_path.read_text())
        projects = state.get("projects", {})
        restored = []
        for path, before in self._claude_restore.items():
            if before is None:
                if projects.pop(path, None) is None:
                    continue
            else:
                projects[path] = before
            restored.append(path)
        if restored:
            state_path.write_text(json.dumps(state, indent=2))
        self._journal("trust.revoked", agent="claude", workspaces=restored, state=str(state_path))

    def _trust_codex(self) -> None:
        if not CODEX_CONFIG.exists():
            self._journal("trust.absent", agent="codex", path=str(CODEX_CONFIG))
            return
        self._backup(CODEX_CONFIG)
        existing = CODEX_CONFIG.read_text()
        wanted = [path for path in self._paths if f'[projects."{path}"]' not in existing]
        if not wanted:
            self._journal("trust.already", agent="codex", workspaces=self._paths)
            return
        # Appended as one block and removed as the same block, so the user's own
        # file keeps its order, its comments and its formatting.
        self._codex_block = "".join(
            f'\n[projects."{path}"]\ntrust_level = "trusted"\n' for path in wanted
        )
        CODEX_CONFIG.write_text(existing + self._codex_block)
        self._journal("trust.granted", agent="codex", workspaces=wanted)

    def _untrust_codex(self) -> None:
        if self._codex_block is None or not CODEX_CONFIG.exists():
            return
        existing = CODEX_CONFIG.read_text()
        if self._codex_block in existing:
            CODEX_CONFIG.write_text(existing.replace(self._codex_block, "", 1))
        self._journal("trust.revoked", agent="codex", workspace=str(self._workspace))
