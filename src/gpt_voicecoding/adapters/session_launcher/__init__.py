"""Session Launcher seam adapters: a direct child (the default) and tmux (optional).

`config.toml` points `[adapters] session_launcher` at one of the two factories
here, and the composition root calls it with the event sink and this seam's
settings table. Nothing else imports an adapter (ADR 0001).

The two differ in exactly one thing, and everything else about a launch is
shared: **whether a human can see the Session**. The direct-child adapter runs it
on a pseudo-terminal this engine owns, so nobody is looking; the tmux adapter
puts it in a window somebody can attach to. ADR 0008 records that line and the
two consequences that follow it — where a Codex app-server lives, and which
adapter can get through a first-run dialog.

Both adapters need to be introduced to the Agent adapters, because a launch
carries things only those spokes can name: where this engine parks permission
dialogs, and what byte budgets its Session Channel was configured with. The
composition root does the introducing, which is the same shape and the same
reason as the Codex app-server being shared with the Call adapter — only the
root is allowed to know two adapters at once.
"""

from __future__ import annotations

from typing import Any

from gpt_voicecoding.adapters.session_launcher.child import DirectChildLauncher
from gpt_voicecoding.adapters.session_launcher.settings import LauncherSettings, SettingsError
from gpt_voicecoding.adapters.session_launcher.tmux import TmuxLauncher

__all__ = [
    "DirectChildLauncher",
    "LauncherSettings",
    "SettingsError",
    "TmuxLauncher",
    "direct_child_launcher",
    "tmux_launcher",
]


def direct_child_launcher(
    *, sink: Any = None, settings: dict[str, Any] | None = None
) -> DirectChildLauncher:
    """Build the default launcher from an opaque settings table."""
    return DirectChildLauncher(sink=sink, settings=LauncherSettings.of(settings))


def tmux_launcher(*, sink: Any = None, settings: dict[str, Any] | None = None) -> TmuxLauncher:
    """Build the optional tmux launcher from an opaque settings table."""
    return TmuxLauncher(sink=sink, settings=LauncherSettings.of(settings))
