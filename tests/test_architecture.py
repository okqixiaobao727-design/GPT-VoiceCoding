"""ADR 0001's splitting principles, enforced.

Principle 1 says Bridge Core speaks only seam verbs: no protocol library may ever
be imported by ``gpt_voicecoding.core``, and Bridge Core never reaches into an
adapter. Both are cheap to state and easy to erode, so they are asserted here
rather than left to review.

The dependency direction is asserted too. ``seams`` is the contract package both
sides share, so it must depend on neither of them: Bridge Core imports ``seams``
and adapters import ``seams``, and nothing imports back. Letting ``seams`` reach
into ``core`` would make the two mutually dependent, which stays legal only for
as long as nobody adds an import to ``core/__init__.py`` — exactly the kind of
invariant that erodes silently.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "gpt_voicecoding"
CORE = PACKAGE / "core"
SEAMS = PACKAGE / "seams"
ADAPTERS = PACKAGE / "adapters"

#: Where a real-time audio stack is allowed to be imported, and nowhere else.
#: One file, so the Call adapter's signalling and classification — the part CI
#: actually runs — stays free of anything that wants a microphone or a network.
AUDIO_MODULE = ADAPTERS / "call" / "realtime" / "webrtc.py"

#: The distributions that file exists to confine.
AUDIO_LIBRARIES = frozenset({"aiortc", "av", "sounddevice"})

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

# Standard-library modules whose only purpose is speaking a wire. They matter
# because this repository hand-rolls what it needs rather than taking a
# dependency — the Codex adapter frames its own WebSocket — and a rule that only
# named third-party distributions would let exactly that hand-rolling smuggle a
# protocol into the hub while every listed library stayed absent.
WIRE_MODULES = frozenset({"http", "socket", "socketserver", "ssl", "struct"})

FORBIDDEN_INTERNAL_PREFIX = "gpt_voicecoding.adapters"
CORE_PREFIX = "gpt_voicecoding.core"


def _imported_modules(source: str) -> set[str]:
    """Every module name a source file imports, dotted paths included."""
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def _sources(package: Path) -> list[Path]:
    return sorted(package.rglob("*.py"))


def _core_sources() -> list[Path]:
    return _sources(CORE)


def _protocol_imports(package: Path) -> list[str]:
    """Every import in ``package`` that pulls in a wire, terminal or transport."""
    offences: list[str] = []
    for path in _sources(package):
        for module in _imported_modules(path.read_text(encoding="utf-8")):
            if module.split(".")[0] in PROTOCOL_LIBRARIES:
                offences.append(f"{path.name} imports {module}")
    return offences


def _wire_imports(package: Path) -> list[str]:
    """Every import in ``package`` that speaks a wire without naming a library."""
    offences: list[str] = []
    for path in _sources(package):
        for module in _imported_modules(path.read_text(encoding="utf-8")):
            if module.split(".")[0] in WIRE_MODULES:
                offences.append(f"{path.name} imports {module}")
    return offences


def _imports_under(package: Path, prefix: str) -> list[str]:
    """Every import in ``package`` that reaches into ``prefix``."""
    offences: list[str] = []
    for path in _sources(package):
        for module in _imported_modules(path.read_text(encoding="utf-8")):
            if module == prefix or module.startswith(prefix + "."):
                offences.append(f"{path.name} imports {module}")
    return offences


def test_core_package_exists() -> None:
    assert _core_sources(), f"no Python sources found under {CORE}"


def test_core_imports_no_protocol_library() -> None:
    """ADR 0001, principle 1: policy in the hub, mechanism in the spokes."""
    offences = _protocol_imports(CORE)
    assert not offences, "Bridge Core must speak only seam verbs, never a protocol: " + "; ".join(
        offences
    )


def test_core_never_imports_an_adapter() -> None:
    """Bridge Core depends on seams; adapters depend on Bridge Core's seams."""
    offences = _imports_under(CORE, FORBIDDEN_INTERNAL_PREFIX)
    assert not offences, "Bridge Core must not reach into an adapter: " + "; ".join(offences)


def test_seams_package_exists() -> None:
    assert _sources(SEAMS), f"no Python sources found under {SEAMS}"


def test_seams_never_imports_bridge_core() -> None:
    """``seams`` is the shared contract, so the dependency runs one way only."""
    offences = _imports_under(SEAMS, CORE_PREFIX)
    assert not offences, (
        "the seams are what Bridge Core is defined against, so they may not depend on it: "
        + "; ".join(offences)
    )


def test_seams_never_imports_an_adapter() -> None:
    offences = _imports_under(SEAMS, FORBIDDEN_INTERNAL_PREFIX)
    assert not offences, "a contract may not depend on one of its implementations: " + "; ".join(
        offences
    )


def test_seams_imports_no_protocol_library() -> None:
    """A seam names what varies; the protocol that varies lives behind it."""
    offences = _protocol_imports(SEAMS)
    assert not offences, "the seams describe verbs, never wires: " + "; ".join(offences)


def test_core_frames_no_wire_of_its_own() -> None:
    """Hand-rolling a protocol may not be the way one gets into Bridge Core.

    The library list above catches a dependency being added; this catches the
    same protocol arriving with no dependency at all, which is exactly how the
    Codex adapter's WebSocket client is built.
    """
    offences = _wire_imports(CORE)
    assert not offences, "Bridge Core speaks seam verbs, not frames: " + "; ".join(offences)


def test_seams_frame_no_wire_of_their_own() -> None:
    offences = _wire_imports(SEAMS)
    assert not offences, "a seam names a verb, never a frame: " + "; ".join(offences)


def test_the_audio_stack_lives_in_exactly_one_file() -> None:
    """`aiortc`, `av` and `sounddevice` are confined, and it is asserted here.

    They are an optional extra CI does not install, so an import that escaped
    this file would not fail in CI — it would fail on a user's machine, at the
    moment they tried to speak. The rule has to be checked by reading, which is
    what this does.
    """
    offences = [
        f"{path.relative_to(PACKAGE)} imports {module}"
        for path in _sources(PACKAGE)
        if path != AUDIO_MODULE
        for module in _imported_modules(path.read_text(encoding="utf-8"))
        if module.split(".")[0] in AUDIO_LIBRARIES
    ]
    named = "; ".join(offences)
    assert not offences, f"the audio stack belongs in {AUDIO_MODULE.name} alone: {named}"


def test_the_audio_module_is_where_it_says_it_is() -> None:
    """A guard that silently stops guarding is worse than no guard."""
    assert AUDIO_MODULE.is_file(), f"{AUDIO_MODULE} is not there, so nothing is confined"
