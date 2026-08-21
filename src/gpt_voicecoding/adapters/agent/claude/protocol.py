"""Every byte shape this spoke shares with something outside it, in one place.

Two wires meet here and neither is negotiable by one side alone:

- **MCP**, which Claude Code speaks to the channel server it spawns. These
  constants are transcribed from the reference implementation's `channel.mjs`
  and from the `@modelcontextprotocol/sdk` 1.30.0 it pins — the version proven
  live against Claude Code 2.1.235 — because a handshake this repository
  invented would be a handshake no real client has ever accepted (ADR 0006).
- **The channel wire**, which the bridge speaks to the channel server over a
  private Unix socket. It is newline-delimited JSON, one message per line.

They are in one module so the server and the client cannot drift apart: the
reference implementation kept the same constants in a `.mjs` file and a `.py`
file and needed a dedicated drift test to notice when they disagreed.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

# -- MCP, as Claude Code expects to find it ------------------------------

#: The newest protocol version this server knows. `channel.mjs`'s SDK negotiates
#: by echoing the client's version when it recognises it and answering with its
#: own newest when it does not, so both halves of that are needed.
LATEST_PROTOCOL_VERSION: Final = "2025-11-25"

#: Every version that negotiation may settle on, newest first. Transcribed from
#: the pinned SDK's `SUPPORTED_PROTOCOL_VERSIONS`.
SUPPORTED_PROTOCOL_VERSIONS: Final = (
    LATEST_PROTOCOL_VERSION,
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
    "2024-10-07",
)

#: The capability that makes this an MCP *channel* rather than a plain server.
#: It is experimental, and its exact key is what Claude Code looks for.
CHANNEL_CAPABILITY: Final = "claude/channel"

#: How a message is pushed into the session. A notification, so nothing waits
#: on it: what proves delivery is the tool call that comes back.
CHANNEL_NOTIFICATION: Final = "notifications/claude/channel"

#: The one tool this server exposes, and the receipt the whole Relay rests on.
ACKNOWLEDGE_TOOL: Final = "acknowledge_answer"

#: What the server calls itself when Claude Code asks.
SERVER_NAME: Final = "gpt-voicecoding-claude-channel"

#: JSON-RPC's own code for "the handler raised", which is what the SDK sends for
#: anything that is not already a coded error.
INTERNAL_ERROR: Final = -32603

#: What the session is told this channel is. It states the receipt obligation,
#: because a message the session acts on without acknowledging is a message the
#: bridge must grade UNKNOWN and send again.
CHANNEL_INSTRUCTIONS: Final = (
    "Messages from this channel are verified user speech for the current Claude Code work "
    "session, delivered by the GPT-VoiceCoding bridge. For every message, call "
    f"{ACKNOWLEDGE_TOOL} with the exact request_id before acting on it. Never apply a "
    "channel message to another request or session."
)

ACKNOWLEDGE_TOOL_DESCRIPTION: Final = (
    "Confirm that this exact Claude Code session received the user's message for a specific "
    "request_id."
)

# -- the channel wire, between the bridge and the channel server ---------

#: The bridge's one outbound message. `text` rather than `answer`, and `kind`
#: carried explicitly rather than assumed, is the defect repair the migration
#: inventory named: the reference implementation stamped every Relay
#: `kind: "user_answer"`, including speech that had answered no question.
TEXT_FIELD: Final = "text"
KIND_FIELD: Final = "kind"
REQUEST_ID_FIELD: Final = "request_id"

#: What the channel answers with. Only `ACKNOWLEDGED` is delivery: `QUEUED` says
#: the notification was pushed, which is not the same as the session reading it.
QUEUED: Final = "queued_for_claude"
ACKNOWLEDGED: Final = "acknowledged_by_claude"
CHANNEL_ERROR: Final = "error"

#: The Relay kind each seam verb puts on the wire.
#:
#: One entry, and that is the honest size of it: the channel is the Answer
#: Relay's route and nothing else's. A Notice Relay rides the peer socket and an
#: Approval Relay rides a hook, so neither has a channel kind to name — when
#: those routes arrive they extend this mapping or they do not touch it at all.
#:
#: `user_message` is deliberately not `user_answer`. Answer Relay carries both
#: the user's answers to a Session's questions *and* their unsolicited
#: instructions, so the only name true of everything it carries is the one that
#: claims nothing about a question.
CHANNEL_KIND_BY_VERB: Final[Mapping[str, str]] = MappingProxyType({"answer_relay": "user_message"})


def channel_kind_for(verb: str) -> str:
    """The wire kind for one seam verb, or a refusal naming the verb.

    A verb with no channel kind is a verb whose Relay does not ride this route.
    Refusing here rather than defaulting is what stops a second Relay kind ever
    being smuggled in under `user_message` the way `user_answer` once was.
    """
    try:
        return CHANNEL_KIND_BY_VERB[verb]
    except KeyError:
        known = ", ".join(sorted(CHANNEL_KIND_BY_VERB))
        raise ValueError(f"{verb} does not ride the MCP channel; {known} does") from None
