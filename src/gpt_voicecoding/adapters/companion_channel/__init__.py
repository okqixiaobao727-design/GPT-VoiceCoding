"""Companion Channel seam adapters, and the factories a configuration file names.

Two ship here. The Telegram adapter is generic and public — a deployment's own
credentials and wiring are not part of this repo — and the null implementation is
what an engine that deliberately has no text reach loads. Both are named the same
way, as `module:attribute` in `[adapters] companion_channel`, because a seam with
nothing behind it is a state this system refuses to have: an engine that ran
without text reach and one that lost its text reach must not look alike.

    companion_channel = "gpt_voicecoding.adapters.companion_channel:null_channel"
    companion_channel = "gpt_voicecoding.adapters.companion_channel.telegram:telegram_channel"

The Telegram adapter lives in its own subpackage rather than beside this file, so
the wire it speaks stays confined to one module inside it.
"""

from __future__ import annotations

from typing import Any

from gpt_voicecoding.adapters.companion_channel.null import (
    NOT_CONFIGURED,
    NullCompanionChannel,
)

#: What `[adapters] companion_channel` is set to for an engine with no text reach.
NULL_REFERENCE = "gpt_voicecoding.adapters.companion_channel:null_channel"

__all__ = [
    "NOT_CONFIGURED",
    "NULL_REFERENCE",
    "NullCompanionChannel",
    "null_channel",
]


def null_channel(*, sink: Any = None) -> NullCompanionChannel:
    """Build the null implementation. It has nothing to be told, so it takes no settings.

    A settings table addressed to this seam therefore fails the assembly with a
    `TypeError` the composition root turns into a named refusal — which is the
    right outcome: settings written for the Telegram adapter and left behind
    when the seam was pointed at the null one are settings that would otherwise
    silently never be applied.
    """
    return NullCompanionChannel(sink=sink)
