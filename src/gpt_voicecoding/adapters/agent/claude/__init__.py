"""The Claude Agent adapter, and the factory a configuration file names.

`config.toml` points `[adapters.agents] claude` at
`gpt_voicecoding.adapters.agent.claude:claude_agent`, and the composition root
calls it with the event sink and this seam's settings table. Nothing else
imports an adapter (ADR 0001).

This package is the shared Claude adapter all three Relay verbs live in. It
carries the Answer Relay — the MCP Session Channel — and the Notice Relay — the
peer socket — along with the pieces the third extends: the settings table, the
bootstrap contract with the launcher, the socket privacy rules and the plugin
packaging.

**One long-lived resource lives outside this process's own directories**: the
receipt listener socket, bound inside Claude Code's shared `cc-socks` directory
because the receiver refuses a reply address from anywhere else. It is bound on
first use and removed on `aclose`. `remove_stale_listeners` is the uninstall
path for anything an unclean exit left behind; it removes only sockets this
engine could have named, only this user's, and only ones positively proven to
have nobody accepting on them.
"""

from __future__ import annotations

from typing import Any

from gpt_voicecoding.adapters.agent.claude.adapter import ClaudeAgentAdapter
from gpt_voicecoding.adapters.agent.claude.bootstrap import (
    CHANNEL_CONFIG_VARIABLE,
    BootstrapError,
    bootstrap_value,
    socket_path_in,
)
from gpt_voicecoding.adapters.agent.claude.peer import PeerError, remove_stale_listeners
from gpt_voicecoding.adapters.agent.claude.plugin import channel_selector, write_plugin
from gpt_voicecoding.adapters.agent.claude.registry import (
    PEER_PROTOCOL,
    PROVEN_AGAINST_VERSION,
    RegistryError,
)
from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings, SettingsError

__all__ = [
    "CHANNEL_CONFIG_VARIABLE",
    "PEER_PROTOCOL",
    "PROVEN_AGAINST_VERSION",
    "BootstrapError",
    "ClaudeAgentAdapter",
    "ClaudeSettings",
    "PeerError",
    "RegistryError",
    "SettingsError",
    "bootstrap_value",
    "channel_selector",
    "claude_agent",
    "remove_stale_listeners",
    "socket_path_in",
    "write_plugin",
]


def claude_agent(*, sink: Any = None, settings: dict[str, Any] | None = None) -> ClaudeAgentAdapter:
    """Build the adapter from an opaque settings table, refusing keys it lacks."""
    return ClaudeAgentAdapter(sink=sink, settings=ClaudeSettings.of(settings))
