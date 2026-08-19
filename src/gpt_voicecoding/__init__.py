"""GPT-VoiceCoding: voice-controlling terminal coding agents through a realtime voice call.

The engine is a single asyncio process. Its shape is hub-and-spoke (ADR 0001):
``core`` is the hub — Bridge Core — and every other package is reachable from it
only through a seam.

Layout:

- ``core`` — Bridge Core. All policy, the single source of truth. Speaks seam
  verbs only; never imports a protocol library.
- ``seams`` — the interfaces Bridge Core calls through. One module per seam.
- ``adapters`` — the implementations behind those seams, one subpackage per seam.
  Protocol libraries live here and nowhere else.
- ``cli`` — ``bridgectl``, a control-plane surface.

Read ``CONTEXT.md`` for the vocabulary and ``docs/adr/`` for the decisions before
changing any of it.
"""

__all__ = ["__version__"]

__version__ = "0.0.0"
