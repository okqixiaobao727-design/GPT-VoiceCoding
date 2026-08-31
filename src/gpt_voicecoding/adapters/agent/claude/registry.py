"""Where a Claude Session says it can be reached. Read-only, and fail-closed.

Claude Code writes one JSON file per live process into `~/.claude/sessions`,
naming its pid, its session id, its cwd, the peer socket it listens on, and what
it is currently doing. This engine **only ever reads** it: the file belongs to
another program, and a bridge that wrote there would be inventing a Session that
Claude Code does not believe in.

**The pin is `peerProtocol`, not the version string.** The peer socket is
undocumented, so this adapter is proven against exactly one wire — but the field
that governs that wire is the protocol number the record itself carries. A
version string moves every week for reasons that have nothing to do with this
socket; the protocol number moves when the socket changes. Refusing on the
number is the check that means something, and refusing on the string would
refuse every live Session on the machine the day after an upgrade.

Static re-probe against 2.1.238 confirmed the registry fields this module
validates: `pid`, `sessionId`, `cwd`, `version`, `peerProtocol`,
`messagingSocketPath` and `status`; the live re-probe against 2.1.251 recorded
below found the same seven, and read a fourth word into `status`. The validated shape is
retained so retiring its former consumer does not widen the records accepted by
the remaining Reply Window and Session Launcher paths.

**`status` has four words, and the fourth is `idle` under another name**
(#154). Claude Code rewrites `idle` to `shell` for the pid-file write when a
`local_bash` background task is still running, so the word says "the turn has
ended and something is still running in the background" rather than a state of
its own. It reaches this reader alone: `claude agents --json` maps it back to
`busy`, so `discovery.py` never sees it. The measurement beside
`PROVEN_AGAINST_VERSION` is what says a Session at `shell` takes the next turn.

**`waitingFor` is carried, not judged.** A `waiting` record names which of that
status's several causes it is, in the same write (#150, measured on 2.1.251).
This reader stops at carrying the word; `waiting_labels.py` holds the table that
says what each one means, and the builds it was measured on.

**Liveness is a separate question and is asked separately.** A record parses
whether or not its process still exists, because "the registry says this" and
"that process is still there" are two different facts and a reader that conflates
them cannot tell a stale record from a malformed one.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The registry's default home. A location, so it defaults; see `settings.py`.
DEFAULT_REGISTRY_DIRECTORY = Path.home() / ".claude" / "sessions"

#: The one peer-socket wire this adapter has been proven against. The record
#: carries this number, and a record carrying another one is refused by it.
PEER_PROTOCOL = 1

#: The Claude Code build the pin was last re-probed against. Documentation, not
#: a gate — `PEER_PROTOCOL` is the gate. Written down so the next re-probe knows
#: what its baseline was.
PROVEN_AGAINST_VERSION = "2.1.251"

# **What `shell` was measured to mean** (#154), on 2.1.251, on Simon's machine
# on 2026-08-31. The word is not documented anywhere, so it is written down
# here rather than inferred, and `window.py` reads its Reply Window off this.
#
# *What the build does.* The reader's accepted enum is
# `["busy","shell","idle","waiting"]`, and the write-side rewrite is
# `pB = rb === "idle" && <background tasks running> ? "shell" : rb` — so the
# underlying status is `idle` and the rewrite only records that a `local_bash`
# task outlived the turn.
#
# *What the roster says about the same Session.* Nothing: `claude agents
# --json` reported pid 10075 as `busy` at the same moment
# `~/.claude/sessions/10075.json` read `status: "shell"`.
#
# *Whether a Relay is acted on during it.* Measured, not assumed, because a
# Reply Window declared OPEN on assumption is exactly what this seam forbids.
# Stimulus: one peer-inbox message — the same route `inbox.py` uses for an
# Answer Relay — sent to pid 10075 while its record read `shell`. Registry
# samples taken every 0.2s across the whole window: `shell` unbroken from
# t=1788151990.1, `busy` at t=1788152012.5 (~0.5s after the send), and the
# Session's reply came back as that turn, returning to `shell` at
# t=1788152033.5. So a Relay delivered during `shell` is acted on as the next
# turn, and `shell` reads OPEN.
# A Stop at `shell` carries the same transcript-derived progress observation as
# one at `idle`; the background task is status/activity, never synthetic speech.

# **What an empty `status` was measured to be** (#157), on 2.1.251, on Simon's
# machine on 2026-08-31. It is not a fifth status word. It is a record that has
# not said yet.
#
# *Two writes, not one.* Claude Code creates the pid file first and writes
# `status` in a second write about a tenth of a second later. The creating
# write's object carries `pid`, `sessionId`, `cwd`, `startedAt`, `procStart`,
# `version`, `peerProtocol`, `peerFeatures`, `kind`, `entrypoint`, `pidDomain`,
# `tmux`, `messagingSocketPath`, `name`, `nameSource` and `nameSince` — and no
# `status` key at all. Three launches, each a fresh pty with the inherited
# `CLAUDE_CODE_*` markers scrubbed, the directory sampled every 1ms: the file
# appeared without `status`, then gained `status: "idle"` and `statusUpdatedAt`
# 117.3ms, 106.4ms and 128.9ms later. `entry_without_a_status` in
# `tests/test_claude_registry.py` is one of those three, transcribed.
#
# *The word is never written empty.* The build has no site that writes
# `status:""` into the record, and its own reader of the field is
# `["busy","shell","idle","waiting"].includes(s) ? s : undefined` — an empty
# string is outside that enum on the vendor's side too. So the `""` this reader
# reports is `_text`'s default standing in for an absent key, never a word the
# registry chose. Absent and blank are one fact here for the same reason they
# are one fact for `waitingFor`: neither is a broken record.
#
# *Not a torn read.* The creating write is already complete, well-formed JSON,
# so a half-written file is not where this comes from. 60s at 1ms across five
# live Sessions — eight writes, including one `busy -> shell` — produced no
# unparseable read, and no write ever dropped a `status` key it had already
# carried. Once written, the word stays.
#
# *Why CLOSED is right, and structurally so.* `window.py`'s finished-turn edge
# needs `was_active`: some earlier sweep that saw `busy` or `waiting`. A
# statusless record exists only between a Session's creation and its first
# status write, so no sweep can have seen that pid in a turn at all, and this
# cannot arrive late the way `shell` did (#154). CLOSED costs one sweep of a
# Session that was not reachable yet anyway.
#
# *Legacy has nothing to port here, and that is the citation* (ADR 0010).
# Legacy never read `~/.claude/sessions` at all — no registry, no polled status
# word, and so no vocabulary for a fifth value to be missing from
# (`legacy@1d32845`: `grep -rn 'claude/sessions'` finds nothing). *Dropped,
# because* the behaviour did not exist there to be ported.
#
# *Not measured:* whether a `bg`, `daemon` or `daemon-worker` Session carries
# this state for longer than the creating write. Every record measured here was
# `kind: "interactive"`.


class RegistryError(Exception):
    """The registry does not say what this adapter needs, or does not say it clearly."""


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """One live Claude process, as its own registry entry describes it."""

    pid: int
    session_id: str
    cwd: Path
    version: str
    #: What Claude Code says this Session is doing right now: `idle`, `busy`,
    #: `shell` or `waiting`. Left as the registry's own word — including
    #: `shell`, which is not normalised to `idle` here even though that is what
    #: it was measured to mean, because flattening it would throw away the fact
    #: that a background task is still running. Translating it into a Reply
    #: Window is `window.py`'s job, not this reader's.
    #:
    #: Empty means *this record has not said yet* (#157) — the pid file exists
    #: but its creating write carried no `status` key, which lasts about a tenth
    #: of a second from a Session's creation. It is not a fifth word, and the
    #: build writes no empty one; the measurement is beside
    #: `PROVEN_AGAINST_VERSION`.
    status: str
    name: str = ""
    #: Which of `waiting`'s several causes this one is, in Claude Code's own
    #: word — `permission prompt`, `dialog open`, `input needed` and the rest
    #: (#150). Written into this record in the same write as `status`, and
    #: empty on every record that is not `waiting` and on builds that do not
    #: write it at all. Left as the vendor's own string for the reason `status`
    #: is: reading it is `waiting_labels.py`'s job, not this reader's.
    waiting_for_label: str = ""


def read_record(directory: Path, pid: int) -> SessionRecord:
    """Read one Session's registry entry, refusing every shape that is not exactly one."""
    path = directory / f"{pid}.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as unreadable:
        raise RegistryError(
            f"no Claude Session registry record for pid {pid} at {path}: {unreadable}"
        ) from None
    return _record(path, raw, expected_pid=pid)


def records(directory: Path) -> tuple[SessionRecord, ...]:
    """Every record in the registry that can be read, skipping the ones that cannot.

    One unreadable file is not a broken registry. Claude Code writes these
    entries live, so a half-written file is an ordinary momentary state — and
    refusing the whole directory over one would make every reader flaky. A
    caller that needs *one* Session asks for it by pid and gets a refusal.
    """
    try:
        entries = sorted(directory.glob("*.json"))
    except OSError:
        return ()
    found: list[SessionRecord] = []
    for path in entries:
        try:
            found.append(_record(path, path.read_text(encoding="utf-8"), expected_pid=None))
        except (OSError, RegistryError):
            continue
    return tuple(found)


def pid_is_live(pid: int) -> bool:
    """Whether that process still exists, asked the only way that does not disturb it.

    Signal 0 performs the permission and existence checks and delivers nothing.
    `EPERM` means the process exists and belongs to somebody else — which is
    still "alive", and is not this function's business to judge.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _record(path: Path, raw: str, *, expected_pid: int | None) -> SessionRecord:
    try:
        document: Any = json.loads(raw)
    except json.JSONDecodeError as unreadable:
        raise RegistryError(f"{path} is not JSON: {unreadable}") from None
    if not isinstance(document, dict):
        raise RegistryError(f"{path} must hold a JSON object")

    pid = _int(document, "pid", path)
    if expected_pid is not None and pid != expected_pid:
        raise RegistryError(
            f"{path} names pid {pid}; a registry record whose pid disagrees with its own "
            "filename is not a record this adapter will address"
        )

    protocol = _int(document, "peerProtocol", path)
    if protocol != PEER_PROTOCOL:
        raise RegistryError(
            f"{path} speaks peerProtocol {protocol}; this adapter is proven against "
            f"{PEER_PROTOCOL} only (last re-probed against Claude Code "
            f"{PROVEN_AGAINST_VERSION}) and will not guess at another wire"
        )
    # Keep the existing record-acceptance contract even though no surviving
    # consumer needs to store this address.
    _text(document, "messagingSocketPath", path)

    return SessionRecord(
        pid=pid,
        session_id=_text(document, "sessionId", path),
        cwd=Path(_text(document, "cwd", path, default=str(path.parent))),
        version=_text(document, "version", path, default=""),
        status=_text(document, "status", path, default=""),
        name=_text(document, "name", path, default=""),
        # Absent, blank or not a string all read as "this record said nothing",
        # because they are the same fact and none of them is a broken record.
        waiting_for_label=_text(document, "waitingFor", path, default=""),
    )


def _int(document: dict[str, Any], field: str, path: Path) -> int:
    value = document.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RegistryError(f"{path} does not carry a whole number {field}, got {value!r}")
    return value


def _text(document: dict[str, Any], field: str, path: Path, *, default: str | None = None) -> str:
    value = document.get(field)
    if isinstance(value, str) and value.strip():
        return value
    if default is not None:
        return default
    raise RegistryError(f"{path} does not carry a non-empty {field}, got {value!r}")
