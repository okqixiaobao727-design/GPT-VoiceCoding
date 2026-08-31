"""What this spoke is told, read out of one opaque table.

The composition root forwards `[adapters.settings.agent.claude]` without looking
inside it, and an unknown key refuses to start — the same two rules the Codex
spoke's settings module states, for the same reason: a misspelled timeout that
silently falls back to a default is the configuration-shaped version of the
silent fallback this repository bans.

Locations and protocol mechanics default; policy does not. How long a socket
read waits and how many bytes a line may be are mechanics. How long the user's
words are retained, and whether they are re-sent, are Bridge Core's, and nothing
here restates them.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from gpt_voicecoding.adapters.agent.claude.registry import DEFAULT_REGISTRY_DIRECTORY

#: A short runtime root, for the reason `privacy.py` gives a length limit at
#: all: a channel socket under a long application-support path cannot be bound.
DEFAULT_SOCKET_DIRECTORY = Path("/tmp")

#: How long one dial, write or line read waits.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 10.0

#: How long a Relay waits, holding the caller, for the session to acknowledge it.
#: Bounded on purpose: an unbounded wait turns "we cannot tell" into "we never
#: answer". The session has to notice the notification and call a tool, so this
#: is a model's reaction time rather than a wire's.
DEFAULT_ACK_TIMEOUT_SECONDS = 45.0

#: How long the connection keeps listening *after* that wait is spent, so an
#: acknowledgement that arrives late is still heard and raised upward. This is
#: what stops Bridge Core re-delivering words that provably arrived.
DEFAULT_LATE_ACK_TIMEOUT_SECONDS = 300.0

#: The longest line either end will read. A cap both ends agree on, in UTF-8
#: bytes, because a byte budget measured in anything else is two budgets.
DEFAULT_MAX_MESSAGE_BYTES = 1 << 20

#: The longest the user's words themselves may be, inside that line.
DEFAULT_MAX_TEXT_BYTES = 64 << 10

#: How often the two sources of a Relay's receipt are re-read while one is
#: outstanding: our own reply socket, and the target's transcript. A quarter of a
#: second because neither source announces itself — a status frame lands on a
#: socket this engine is not blocked on, and the transcript is a file the Session
#: appends to — and because the transcript record appears within a moment of the
#: message being injected, so a slower poll would spend the user's wait on
#: nothing. The transcript read behind it is cached on the file's own identity,
#: so a poll that finds it unchanged costs one `stat`.
DEFAULT_RECEIPT_POLL_SECONDS = 0.25

#: How often each registered Session's registry record is re-read to see whether
#: its Reply Window has moved. One second, and the number is a judgement about
#: what is on the other end of it: what waits on this signal is a queued Relay
#: being flushed, against turns that run for minutes, so a second of latency is
#: imperceptible — while the file itself is rewritten sub-second, so polling
#: faster would multiply reads without seeing anything sooner.
DEFAULT_REPLY_WINDOW_POLL_SECONDS = 1.0

#: How long that sweep keeps re-reading a wait it cannot yet name before it
#: announces the honest `UNKNOWN` anyway (#150). Five seconds, on the one-second
#: cadence above, so a wait gets about five re-reads: Claude Code writes
#: `waiting` the moment a dialog opens and its transcript record follows — the
#: reported cases lagged by 133 ms and 3.0 s — so a budget shorter than that
#: would spend the whole thing on the flush and announce "it has not said what
#: it is waiting for yet" about a question the next read would have carried.
#: The reference implementation's equivalent was 1.0 s
#: (`legacy@1d32845:bridge/daemon.py:2148`, `config.plist:443`), but it ran a
#: dedicated 0.1 s poll inside a blocking hook; this one rides a sweep already
#: turning once a second, and nothing is held up while it waits.
DEFAULT_STOP_CATCH_UP_BUDGET_SECONDS = 5.0


class SettingsError(Exception):
    """The settings table names something this adapter does not have."""


@dataclass(frozen=True, slots=True)
class ClaudeSettings:
    """Everything this spoke may be told. Nothing policy-shaped appears here."""

    socket_directory: Path = DEFAULT_SOCKET_DIRECTORY
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    ack_timeout_seconds: float = DEFAULT_ACK_TIMEOUT_SECONDS
    late_ack_timeout_seconds: float = DEFAULT_LATE_ACK_TIMEOUT_SECONDS
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES
    max_text_bytes: int = DEFAULT_MAX_TEXT_BYTES
    #: Where Claude Code keeps the Session records the Reply Window reads.
    registry_directory: Path = DEFAULT_REGISTRY_DIRECTORY
    reply_window_poll_seconds: float = DEFAULT_REPLY_WINDOW_POLL_SECONDS
    #: How long a Stop nothing can yet name is re-read before it is announced
    #: as `UNKNOWN`. Policy-shaped in appearance and mechanical in fact: it is
    #: how long this reader gives another program's file to flush, not how long
    #: the user is made to wait for anything.
    stop_catch_up_budget_seconds: float = DEFAULT_STOP_CATCH_UP_BUDGET_SECONDS
    receipt_poll_seconds: float = DEFAULT_RECEIPT_POLL_SECONDS

    def __post_init__(self) -> None:
        for name in (
            "request_timeout_seconds",
            "ack_timeout_seconds",
            "late_ack_timeout_seconds",
            "reply_window_poll_seconds",
            "receipt_poll_seconds",
            "stop_catch_up_budget_seconds",
        ):
            if getattr(self, name) <= 0:
                raise SettingsError(f"{name} must be a positive number of seconds")
        for name in ("max_message_bytes", "max_text_bytes"):
            if getattr(self, name) <= 0:
                raise SettingsError(f"{name} must be a positive number of bytes")
        if self.max_text_bytes > self.max_message_bytes:
            raise SettingsError(
                "max_text_bytes must fit inside max_message_bytes: the words travel inside "
                "the line, so a text budget larger than the line budget can never be spent"
            )

    @classmethod
    def of(cls, table: dict[str, Any] | None) -> ClaudeSettings:
        """Read one settings table, refusing every key it does not recognise."""
        if not table:
            return cls()
        known = {field.name for field in fields(cls)}
        unknown = sorted(set(table) - known)
        if unknown:
            raise SettingsError(
                f"[adapters.settings.agent.claude] does not have "
                f"{', '.join(unknown)}. It has: {', '.join(sorted(known))}"
            )
        return cls(**{key: _typed(key, value) for key, value in table.items()})


def _typed(key: str, value: Any) -> Any:
    """Turn one TOML value into what the field holds, or refuse in the operator's words."""
    if key.endswith("_directory"):
        if not isinstance(value, str) or not value.strip():
            raise SettingsError(f"{key} must be a directory path")
        return Path(value.strip()).expanduser()
    if key.endswith("_bytes"):
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingsError(f"{key} must be a whole number of bytes")
        return value
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SettingsError(f"{key} must be a number of seconds")
    return float(value)
