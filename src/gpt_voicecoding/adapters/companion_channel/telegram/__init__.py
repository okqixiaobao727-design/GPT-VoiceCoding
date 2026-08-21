"""The generic Telegram Companion Channel, and the factory a configuration file names.

    [adapters]
    companion_channel = "gpt_voicecoding.adapters.companion_channel.telegram:telegram_channel"

    [adapters.settings.companion_channel]
    token_env = "GPT_VOICECODING_TELEGRAM_TOKEN"
    chat_id = "123456789"

Generic and public: a bot token, a chat id, and nothing about any particular
deployment. The credentials themselves are not here and never will be — the
token is read from the environment variable `token_env` names, at assembly time,
so an engine with a channel it could never authenticate to refuses to start
instead of discovering it at the moment the user needed to be reached.

A private deployment that wants a different channel writes its own adapter out of
tree and names it here. That is the whole of what the seam asks of it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from gpt_voicecoding.adapters.companion_channel.telegram.adapter import (
    TelegramCompanionChannel,
    split_message,
    utf16_length,
)
from gpt_voicecoding.adapters.companion_channel.telegram.api import (
    FailureLayer,
    TelegramError,
    Transport,
    http_transport,
)
from gpt_voicecoding.adapters.companion_channel.telegram.settings import (
    MESSAGE_LIMIT_UTF16_UNITS,
    SettingsError,
    TelegramSettings,
)

__all__ = [
    "MESSAGE_LIMIT_UTF16_UNITS",
    "FailureLayer",
    "SettingsError",
    "TelegramCompanionChannel",
    "TelegramError",
    "TelegramSettings",
    "Transport",
    "http_transport",
    "split_message",
    "telegram_channel",
    "utf16_length",
]


def telegram_channel(
    *,
    sink: Any = None,
    settings: dict[str, Any] | None = None,
    transport: Transport | None = None,
    environ: Mapping[str, str] | None = None,
) -> TelegramCompanionChannel:
    """Build the adapter from an opaque settings table, refusing keys it lacks.

    The token is read here, while the engine is still being assembled, for the
    same reason the Call adapter's factory proves its audio extra here: a
    configuration fault that will never heal on its own should stop the start,
    not wait to become an outage.
    """
    read = TelegramSettings.of(settings)
    token = read.token_in(os.environ if environ is None else environ)
    return TelegramCompanionChannel(
        sink=sink,
        settings=read,
        transport=transport or http_transport(token=token, api_root=read.api_root),
    )
