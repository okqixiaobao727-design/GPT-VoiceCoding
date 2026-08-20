"""The Codex Agent adapter, and the factory a configuration file names.

`config.toml` points at `gpt_voicecoding.adapters.agent.codex:codex_agent`, and
the composition root calls it with the event sink and this seam's settings table.
Nothing else imports an adapter (ADR 0001).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gpt_voicecoding.adapters.agent.codex.adapter import (
    NOTICE_FRAME,
    CodexAgentAdapter,
    notice_text,
)
from gpt_voicecoding.adapters.codex_app_server.settings import CodexSettings, SettingsError

__all__ = [
    "NOTICE_FRAME",
    "CodexAgentAdapter",
    "CodexSettings",
    "SettingsError",
    "codex_agent",
    "notice_text",
]


def codex_agent(
    *,
    sink: Any = None,
    settings: dict[str, Any] | None = None,
    own_socket_path: Path | None = None,
    own_log_path: Path | None = None,
) -> CodexAgentAdapter:
    """Build the adapter from an opaque settings table, refusing keys it lacks."""
    return CodexAgentAdapter(
        sink=sink,
        settings=CodexSettings.of(settings),
        own_socket_path=own_socket_path,
        own_log_path=own_log_path,
    )
