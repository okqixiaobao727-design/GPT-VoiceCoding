"""The Claude Agent adapter, and the factory a configuration file names.

`config.toml` points `[adapters.agents] claude` at
`gpt_voicecoding.adapters.agent.claude:claude_agent`, and the composition root
calls it with the event sink and this seam's settings table. Nothing else
imports an adapter (ADR 0001).

This package is the shared Claude adapter all three Relay verbs live in. It
carries the Answer Relay — the MCP Session Channel — and the pieces the other
two extend: the settings table, the bootstrap contract with the launcher, the
socket privacy rules and the plugin packaging.
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
from gpt_voicecoding.adapters.agent.claude.plugin import channel_selector, write_plugin
from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings, SettingsError

__all__ = [
    "CHANNEL_CONFIG_VARIABLE",
    "BootstrapError",
    "ClaudeAgentAdapter",
    "ClaudeSettings",
    "SettingsError",
    "bootstrap_value",
    "channel_selector",
    "claude_agent",
    "socket_path_in",
    "write_plugin",
]


def claude_agent(*, sink: Any = None, settings: dict[str, Any] | None = None) -> ClaudeAgentAdapter:
    """Build the adapter from an opaque settings table, refusing keys it lacks."""
    return ClaudeAgentAdapter(sink=sink, settings=ClaudeSettings.of(settings))
