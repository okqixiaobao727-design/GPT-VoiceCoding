"""The layout ADR 0001 describes is the layout that is actually here."""

from __future__ import annotations

import importlib

SEAMS = (
    "agent",
    "call",
    "companion_channel",
    "control_plane",
    "session_launcher",
)

ADAPTER_FAMILIES = (
    "agent",
    "call",
    "companion_channel",
    "session_launcher",
)


def test_package_imports() -> None:
    package = importlib.import_module("gpt_voicecoding")
    assert package.__version__


def test_every_seam_has_a_module() -> None:
    for seam in SEAMS:
        importlib.import_module(f"gpt_voicecoding.seams.{seam}")


def test_every_outward_seam_has_an_adapter_package() -> None:
    """Every seam Bridge Core calls out through has somewhere for adapters to live.

    ``control_plane`` is deliberately absent: it is an interface Bridge Core
    exposes, not one it calls out through, so it has surfaces rather than adapters.
    """
    for family in ADAPTER_FAMILIES:
        importlib.import_module(f"gpt_voicecoding.adapters.{family}")
