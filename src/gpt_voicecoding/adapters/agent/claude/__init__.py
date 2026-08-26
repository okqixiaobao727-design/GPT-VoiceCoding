"""The Claude Agent adapter, and the factory a configuration file names.

`config.toml` points `[adapters.agents] claude` at
`gpt_voicecoding.adapters.agent.claude:claude_agent`, and the composition root
calls it with the event sink and this seam's settings table. Nothing else
imports an adapter (ADR 0001).

This package is the shared Claude adapter for the Answer Relay over the Session's
own **inbox socket** and the Approval Relay over the `PermissionRequest` hook —
two routes, two proofs, one settings table, and one set of socket privacy rules.
The inbox carries the user's words; only the hook can carry their authority, and
#71 proved that boundary is upstream's own and enforced (`inbox.py`).

The approval socket is bound at `connect` in a directory of this engine's own,
because its address has to exist before any Session's hook dials it.

**The Approval Relay needs two things this package does not launch**: the hook
installed in the Session's config directory, which is `installation`'s job
(ADR 0011, ADR 0012), and this engine's approval address published where that
hook can read it, which `connect` does through `bootstrap.publish_address`.
Neither is a launch argument any more: v1.0 bridges Sessions the user starts, so
there is no launch to carry anything (#67).
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
    publish_address,
    withdraw_address,
)
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
    "ApprovalError",
    "BootstrapError",
    "ClaudeAgentAdapter",
    "ClaudeSettings",
    "RegistryError",
    "SettingsError",
    "approval_socket_path",
    "claude_agent",
    "hook_decision",
    "publish_address",
    "withdraw_address",
]


def claude_agent(*, sink: Any = None, settings: dict[str, Any] | None = None) -> ClaudeAgentAdapter:
    """Build the adapter from an opaque settings table, refusing keys it lacks."""
    return ClaudeAgentAdapter(sink=sink, settings=ClaudeSettings.of(settings))
