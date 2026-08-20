"""``bridgectl`` — a control-plane surface, and nothing more.

It speaks the control-plane interface (``seams.control_plane``) over the engine's
Unix domain socket. It holds no policy and no state of its own: see
``bridgectl.py`` for the surface itself.
"""

from __future__ import annotations

__all__ = ["main"]

from gpt_voicecoding.cli.bridgectl import main
