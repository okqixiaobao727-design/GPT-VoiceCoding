"""``bridgectl`` — a control-plane surface, and nothing more.

It speaks the control-plane interface (``seams.control_plane``) over the engine's
Unix domain socket. It holds no policy and no state of its own.
"""

__all__ = ["main"]


def main() -> int:
    """Entry point for the ``bridgectl`` console script.

    Not built yet — this repository is the locked architecture's layout, not its
    implementation. See ``docs/adr/README.md`` for what has been decided so far.
    """
    raise SystemExit("bridgectl is not implemented yet; see docs/adr/README.md")
