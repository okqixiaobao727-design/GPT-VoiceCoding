"""ADR 0001's splitting principles, enforced.

Principle 1 says Bridge Core speaks only seam verbs: no protocol library may ever
be imported by ``gpt_voicecoding.core``, and Bridge Core never reaches into an
adapter. Both are cheap to state and easy to erode, so they are asserted here
rather than left to review.
"""

from __future__ import annotations

import ast
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "src" / "gpt_voicecoding" / "core"

# Top-level distribution names that carry a wire, terminal or transport protocol.
# Adding to this list is cheap; removing from it needs a new ADR.
PROTOCOL_LIBRARIES = frozenset(
    {
        "aiohttp",
        "aiortc",
        "av",
        "grpc",
        "httpx",
        "jsonrpc",
        "jsonrpcserver",
        "libtmux",
        "pyaudio",
        "pydub",
        "requests",
        "sounddevice",
        "telebot",
        "telegram",
        "telethon",
        "websocket",
        "websockets",
    }
)

FORBIDDEN_INTERNAL_PREFIX = "gpt_voicecoding.adapters"


def _imported_modules(source: str) -> set[str]:
    """Every module name a source file imports, dotted paths included."""
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def _core_sources() -> list[Path]:
    return sorted(CORE.rglob("*.py"))


def test_core_package_exists() -> None:
    assert _core_sources(), f"no Python sources found under {CORE}"


def test_core_imports_no_protocol_library() -> None:
    """ADR 0001, principle 1: policy in the hub, mechanism in the spokes."""
    offences: list[str] = []
    for path in _core_sources():
        for module in _imported_modules(path.read_text(encoding="utf-8")):
            if module.split(".")[0] in PROTOCOL_LIBRARIES:
                offences.append(f"{path.name} imports {module}")
    assert not offences, "Bridge Core must speak only seam verbs, never a protocol: " + "; ".join(
        offences
    )


def test_core_never_imports_an_adapter() -> None:
    """Bridge Core depends on seams; adapters depend on Bridge Core's seams."""
    offences: list[str] = []
    for path in _core_sources():
        for module in _imported_modules(path.read_text(encoding="utf-8")):
            if module == FORBIDDEN_INTERNAL_PREFIX or module.startswith(
                FORBIDDEN_INTERNAL_PREFIX + "."
            ):
                offences.append(f"{path.name} imports {module}")
    assert not offences, "Bridge Core must not reach into an adapter: " + "; ".join(offences)
