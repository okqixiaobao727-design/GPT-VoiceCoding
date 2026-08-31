"""The Codex Agent adapter, and the factory a configuration file names.

`config.toml` points at `gpt_voicecoding.adapters.agent.codex:codex_agent`, and
the composition root calls it with the event sink and this seam's settings table.
Nothing else imports an adapter (ADR 0001).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from gpt_voicecoding.adapters.agent.codex.adapter import CodexAgentAdapter
from gpt_voicecoding.adapters.codex_app_server.settings import CodexSettings, SettingsError
from gpt_voicecoding.seams.agent import ProgressCapture

__all__ = [
    "CodexAgentAdapter",
    "CodexSettings",
    "SettingsError",
    "codex_agent",
]


def codex_agent(
    *,
    progress_capture: ProgressCapture,
    sink: Any = None,
    settings: dict[str, Any] | None = None,
    own_socket_path: Path | None = None,
    own_log_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> CodexAgentAdapter:
    """Build the adapter from an opaque settings table, refusing keys it lacks."""
    return CodexAgentAdapter(
        progress_capture=progress_capture,
        sink=sink,
        settings=CodexSettings.of(
            settings,
            environ=os.environ if environ is None else environ,
        ),
        own_socket_path=own_socket_path,
        own_log_path=own_log_path,
    )
