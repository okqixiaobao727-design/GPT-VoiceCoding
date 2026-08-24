"""The Claude Agent adapter, and the factory a configuration file names.

`config.toml` points `[adapters.agents] claude` at
`gpt_voicecoding.adapters.agent.claude:claude_agent`, and the composition root
calls it with the event sink and this seam's settings table. Nothing else
imports an adapter (ADR 0001).

This package is the shared Claude adapter for the Answer Relay over the MCP
Session Channel and the Approval Relay over the `PermissionRequest` hook — two
routes, two proofs, one settings table, one bootstrap contract with the launcher,
and one set of socket privacy rules.

The approval socket is bound at `connect` in a directory of this engine's own,
because its address has to exist before any Session launches — the launch is what
carries it to the hook.

**The Approval Relay needs two things from a launch that no other verb does**: a
`--plugin-dir` naming the rendered hook plugin, and the approval socket's address
in the bootstrap variable. `write_hook_plugin` renders the first and
`approval_socket_path` names the second; `remove_hook_plugin` is the uninstall
path, and it is a directory removal rather than a settings edit.

**The Answer Relay likewise needs two launch arguments**: a second
`--plugin-dir` naming the rendered Session Channel plugin and `--channels`
naming its selector. `write_plugin` renders the directory and
`channel_selector` names the channel; the Session Launcher removes both inline
plugins with their per-launch directory.
"""

from __future__ import annotations

from typing import Any

from gpt_voicecoding.adapters.agent.claude.adapter import ClaudeAgentAdapter
from gpt_voicecoding.adapters.agent.claude.approval import (
    ApprovalError,
    approval_socket_path,
    hook_decision,
)
from gpt_voicecoding.adapters.agent.claude.bootstrap import (
    CHANNEL_CONFIG_VARIABLE,
    BootstrapError,
    bootstrap_value,
)
from gpt_voicecoding.adapters.agent.claude.hook_plugin import (
    HOOK_PLUGIN_NAME,
    remove_hook_plugin,
    write_hook_plugin,
)
from gpt_voicecoding.adapters.agent.claude.plugin import channel_selector, write_plugin
from gpt_voicecoding.adapters.agent.claude.registry import (
    PEER_PROTOCOL,
    PROVEN_AGAINST_VERSION,
    RegistryError,
)
from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings, SettingsError

__all__ = [
    "CHANNEL_CONFIG_VARIABLE",
    "HOOK_PLUGIN_NAME",
    "PEER_PROTOCOL",
    "PROVEN_AGAINST_VERSION",
    "ApprovalError",
    "BootstrapError",
    "ClaudeAgentAdapter",
    "ClaudeSettings",
    "RegistryError",
    "SettingsError",
    "approval_socket_path",
    "bootstrap_value",
    "channel_selector",
    "claude_agent",
    "hook_decision",
    "remove_hook_plugin",
    "write_hook_plugin",
    "write_plugin",
]


def claude_agent(*, sink: Any = None, settings: dict[str, Any] | None = None) -> ClaudeAgentAdapter:
    """Build the adapter from an opaque settings table, refusing keys it lacks."""
    return ClaudeAgentAdapter(sink=sink, settings=ClaudeSettings.of(settings))
