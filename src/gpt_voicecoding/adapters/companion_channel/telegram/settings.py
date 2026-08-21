"""What the Telegram spoke is told, read out of one opaque table.

The same rule the Call spoke's settings follow, applied here: the composition
root forwards `[adapters.settings.companion_channel]` without looking inside it,
an unrecognised key refuses to start rather than falling back silently, and
**locations and mechanism identity default while decisions do not**.

**The bot token is never in this table.** It is named *by variable*: `token_env`
says which environment variable holds it, and the adapter reads that variable.
A `token` key does not exist, so writing one is an unknown-key refusal that
points at `token_env` — a configuration file is a file that gets committed by
accident, and this is the one shape that cannot be.

**The chat id is in this table, and deliberately has no `chat_id_env` twin.** The
ticket's locked line classes it with the token as something that "comes from
environment/config outside the repo" — and `config.toml` *is* config outside the
repo, so a plain chat id there satisfies it. The token gets the stricter
treatment because a token is a credential and a chat id is an address. A second
way to state one fact, with no deployment asking for it, is a dormant parameter.

The message cap is not configurable. 4096 UTF-16 code units is the API's own
limit, so a key for it would be a "decision" with one workable value, which is
mechanism identity rather than a choice.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

#: Where the API lives. A location, so it defaults — and so a proof script or a
#: test can point the same adapter at something else without a second code path.
DEFAULT_API_ROOT = "https://api.telegram.org"

#: How long one `getUpdates` is allowed to hang open waiting for a message.
#: This is what makes the channel a long poll rather than a busy loop, and it
#: runs on a worker thread, so it never occupies the engine's event loop.
DEFAULT_POLL_TIMEOUT_SECONDS = 25.0

#: How long any single HTTP request may take beyond whatever it is waiting for.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0

#: How long the reader waits before trying again after the network refused it.
#: Bounded backoff, not a tight retry: an unreachable Telegram must not turn
#: into a spin that heats the machine for as long as the outage lasts.
DEFAULT_RETRY_SECONDS = 5.0

#: The API's own per-message cap, counted in UTF-16 code units — which is why
#: `len()` is the wrong ruler and an emoji costs two.
MESSAGE_LIMIT_UTF16_UNITS = 4096


#: The two keys with no default, because neither is a location or a mechanism
#: identity: which variable holds the token, and which chat this channel reaches.
REQUIRED = ("token_env", "chat_id")


class SettingsError(Exception):
    """The settings table names something this adapter does not have, or omits what it needs."""


@dataclass(frozen=True, slots=True)
class TelegramSettings:
    """Everything this spoke may be told. Nothing policy-shaped appears here."""

    #: The name of the environment variable holding the bot token. Never the token.
    token_env: str
    #: The one destination this channel reaches, and the one it accepts from.
    chat_id: str
    api_root: str = DEFAULT_API_ROOT
    poll_timeout_seconds: float = DEFAULT_POLL_TIMEOUT_SECONDS
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    retry_seconds: float = DEFAULT_RETRY_SECONDS

    def __post_init__(self) -> None:
        if not self.token_env.strip():
            raise SettingsError("token_env must name the environment variable holding the token")
        if not self.chat_id.strip():
            raise SettingsError("chat_id must name the chat this channel reaches")
        if not _is_whole_number(self.chat_id):
            # The API takes an `@name` for *sending*, and an inbound update
            # carries only the numeric id — so a channel configured that way
            # would push perfectly and be permanently deaf, which is exactly the
            # healthy-looking outage this seam is shaped against.
            raise SettingsError(
                f"chat_id must be the numeric chat id, and {self.chat_id!r} is not. A "
                "group's is negative. An @name would send and then never hear: an "
                "inbound message carries the numeric id, and this channel accepts text "
                "only from the chat it is configured for"
            )
        if not self.api_root.strip():
            raise SettingsError("api_root must be the base URL of a Telegram Bot API")
        for name in ("poll_timeout_seconds", "request_timeout_seconds", "retry_seconds"):
            if getattr(self, name) <= 0:
                raise SettingsError(f"{name} must be a positive number of seconds")
        if self.poll_timeout_seconds < 1:
            # The API takes this as a whole number of seconds, so anything under
            # one second arrives as zero — which is not a short poll, it is a
            # busy loop hammering Telegram for as long as the engine runs.
            raise SettingsError(
                "poll_timeout_seconds must be at least 1: the API counts whole seconds, "
                "and anything less becomes a busy loop rather than a long poll"
            )

    @classmethod
    def of(cls, table: dict[str, Any] | None) -> TelegramSettings:
        """Read one settings table, refusing every key it does not recognise."""
        known = {field.name for field in fields(cls)}
        if not table:
            raise SettingsError(
                "[adapters.settings.companion_channel] is empty, and this adapter cannot "
                f"reach anyone without it. It needs: {', '.join(sorted(known))}"
            )
        unknown = sorted(set(table) - known)
        if unknown:
            raise SettingsError(
                f"[adapters.settings.companion_channel] does not have {', '.join(unknown)}"
                + _instead_of(unknown)
                + f". It has: {', '.join(sorted(known))}"
            )
        # Refused here rather than left to a TypeError on the constructor: the
        # composition root would report that as "could not be constructed with
        # the event sink", which names the wrong thing entirely.
        missing = sorted(name for name in REQUIRED if name not in table)
        if missing:
            raise SettingsError(
                f"[adapters.settings.companion_channel] is missing {', '.join(missing)}, "
                "and this adapter cannot reach anyone without them"
            )
        return cls(**{key: _typed(key, value) for key, value in table.items()})

    def token_in(self, environ: Mapping[str, str]) -> str:
        """The bot token, read from the variable this table named. Refuses an empty one.

        Read at construction rather than at the first push: a variable that is
        not set is a configuration fault that will never heal on its own, and an
        engine that starts with a channel it can never authenticate to is the
        healthy-looking outage this whole seam is shaped against.
        """
        token = environ.get(self.token_env, "")
        if not token.strip():
            raise SettingsError(
                f"the bot token is read from ${self.token_env}, which is not set in this "
                "engine's environment. Export it there — it is a credential and never "
                "belongs in a configuration file"
            )
        return token.strip()


def _is_whole_number(value: str) -> bool:
    """Whether this is a chat id and not a name. Negative is ordinary: groups are."""
    return value.strip().lstrip("-").isdigit()


def _instead_of(unknown: list[str]) -> str:
    """Point the one mistake worth guiding at the key that replaces it."""
    return " — the token is named by variable, in token_env" if "token" in unknown else ""


def _typed(key: str, value: Any) -> Any:
    """Turn one TOML value into what the field holds, or refuse in the operator's words."""
    if key == "chat_id":
        # Telegram chat ids are integers, and a group's is negative. TOML can
        # say either, and both mean the same address, so both are accepted and
        # one is stored.
        if isinstance(value, bool) or not isinstance(value, int | str):
            raise SettingsError("chat_id must be a chat id, as a number or a string")
        return str(value).strip()
    if key in ("token_env", "api_root"):
        if not isinstance(value, str) or not value.strip():
            raise SettingsError(f"{key} must be a non-empty string")
        return value.strip()
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SettingsError(f"{key} must be a number of seconds")
    return float(value)
