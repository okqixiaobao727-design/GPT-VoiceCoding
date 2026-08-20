"""The engine: the process that is Bridge Core plus everything around it.

`composition` assembles one from configuration; this module is how a person or a
menu-bar shell starts that process. There is deliberately nothing else here —
the engine has no behaviour of its own beyond assembling, serving and stopping.

Run it as ``python -m gpt_voicecoding.engine``. It stays in the foreground: the
shell spawns it as a direct child and expects it to remain one (ADR 0005), so it
never daemonises, and a clean exit is still an exit the shell will restart.
"""

from __future__ import annotations

__all__ = ["main"]

from gpt_voicecoding.engine.runner import main
