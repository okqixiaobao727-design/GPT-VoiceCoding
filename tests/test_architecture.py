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
import sys
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

#: Where the call cues are synthesised, and the reason it is a second file next
#: to the one above rather than a section inside it (#186).
CUE_MODULE = ADAPTERS / "call" / "realtime" / "cues.py"

#: Where the Companion Channel's HTTP client is allowed to be, and nowhere else.
#: The same confinement as the audio stack, for the opposite reason: this
#: repository takes no Telegram dependency and hand-rolls the little it needs, so
#: no dependency list can catch the wire spreading. One file can be read.
TELEGRAM_WIRE_MODULE = ADAPTERS / "companion_channel" / "telegram" / "api.py"

#: The Companion Channel subpackage, whose other modules must stay wireless.
COMPANION_CHANNEL = ADAPTERS / "companion_channel"

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
SEAMS_PREFIX = "gpt_voicecoding.seams"

#: Installation (ADR 0012) is not a seam and not policy: it has to run when no
#: engine exists, so it reaches nothing the engine is made of.
INSTALLATION = PACKAGE / "installation"


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


def test_core_never_imports_the_control_plane_mechanism() -> None:
    """The hub answers the control plane; it does not read the surface that speaks it.

    `gpt_voicecoding.control_plane` is framing, sockets and the command parser,
    and its own `commands` module imports `core.relays` to render a receipt. So
    an import the other way is not merely a layering slip — it closes a cycle
    across ADR 0001's boundary, and one that resolves at runtime and therefore
    fails no test that is not this one. It was almost made for a real reason:
    the Call Agent's instructions are generated from the command forms (#193),
    and those forms are vocabulary, so they live in `seams/control_plane.py`
    beside `Action` where the hub may read them.
    """
    offences = _imports_under(CORE, "gpt_voicecoding.control_plane")
    assert not offences, (
        "Bridge Core reaches the control plane through the seam's vocabulary, never "
        "through the package that frames it: " + "; ".join(offences)
    )


def test_seams_package_exists() -> None:
    assert _sources(SEAMS), f"no Python sources found under {SEAMS}"


def test_seams_never_imports_bridge_core() -> None:
    """``seams`` is the shared contract, so the dependency runs one way only."""
    offences = _imports_under(SEAMS, CORE_PREFIX)
    assert not offences, (
        "the seams are what Bridge Core is defined against, so they may not depend on it: "
        + "; ".join(offences)
    )


def test_adapters_never_import_bridge_core() -> None:
    """The other direction of the same one-way rule, and the one that erodes quietly.

    `test_core_never_imports_an_adapter` closes the hub's side. This closes the
    spokes': an adapter reaching into `gpt_voicecoding.core` for a constant, a
    path helper or a dataclass makes the two mutually dependent, and a
    refactor inside the hub then breaks components that are supposed to be
    swappable. It is easy to do by accident — the Call adapter once took its
    default workspace from Bridge Core's persistence layout — and nothing but a
    read catches it. Adapters depend on `seams`, and on nothing else here.
    """
    offences = _imports_under(ADAPTERS, CORE_PREFIX)
    named = "; ".join(offences)
    assert not offences, f"an adapter depends on the seams, never on the hub: {named}"


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


def test_the_cues_are_synthesised_with_the_standard_library_alone() -> None:
    """Why the cue waveforms are not in the audio module, asserted rather than said.

    #174 said the real adapter would synthesise these shapes "from named module
    constants inside `webrtc.py`". They are next door instead, because `webrtc.py`
    is the file that imports the voice extra, CI never installs the voice extra,
    and #186 asks for the durations and the peaks to be *graded* — which can only
    happen in a module CI can import. So the deviation buys a test, and this is
    the check that keeps it paid for: the moment a numpy or a sounddevice import
    appears here, the synthesis has stopped being gradeable and the second file
    has stopped earning its keep.

    Stated as "the standard library and this package", not as "not an audio
    library": the existing confinement already forbids the three named ones, and
    what would actually happen is somebody reaching for numpy.
    """
    assert CUE_MODULE.is_file(), f"{CUE_MODULE} is not there, so nothing is confined"
    offences = [
        f"{CUE_MODULE.name} imports {module}"
        for module in _imported_modules(CUE_MODULE.read_text(encoding="utf-8"))
        if module.split(".")[0] not in sys.stdlib_module_names
        and not module.startswith("gpt_voicecoding.")
    ]
    named = "; ".join(offences)
    assert not offences, f"the cues are synthesised with the standard library alone: {named}"


def test_the_telegram_wire_lives_in_exactly_one_file() -> None:
    """One module speaks HTTP; the rest of the channel is ordinary Python.

    The Telegram *libraries* are already forbidden everywhere by
    `PROTOCOL_LIBRARIES`, and this repository would not use one anyway. What
    that leaves is the wire arriving hand-rolled, from the standard library —
    exactly how the Codex adapter's WebSocket client is built — so the rule that
    matters here is where `urllib` and `http` may appear.
    """
    offences = [
        f"{path.relative_to(PACKAGE)} imports {module}"
        for path in _sources(COMPANION_CHANNEL)
        if path != TELEGRAM_WIRE_MODULE
        for module in _imported_modules(path.read_text(encoding="utf-8"))
        if module.split(".")[0] in WIRE_MODULES | {"urllib"}
    ]
    named = "; ".join(offences)
    assert not offences, f"the Telegram wire belongs in {TELEGRAM_WIRE_MODULE.name} alone: {named}"


def test_the_telegram_wire_module_is_where_it_says_it_is() -> None:
    """A guard that silently stops guarding is worse than no guard."""
    assert TELEGRAM_WIRE_MODULE.is_file(), f"{TELEGRAM_WIRE_MODULE} is not there"


def test_the_channel_starts_exactly_one_thread_and_only_in_the_adapter() -> None:
    """The reader's thread is the adapter's own, and stays that way.

    It exists for a measured reason — a poll parked on `asyncio.to_thread` holds
    `asyncio.run` open past the engine that let go of it — and machinery kept
    for a measured reason spreads if nothing watches. The wire may not start
    threads, and neither may the settings or the null implementation.
    """
    allowed = COMPANION_CHANNEL / "telegram" / "adapter.py"
    offences = [
        f"{path.relative_to(PACKAGE)} imports {module}"
        for path in _sources(COMPANION_CHANNEL)
        if path != allowed
        for module in _imported_modules(path.read_text(encoding="utf-8"))
        if module.split(".")[0] == "threading"
    ]
    named = "; ".join(offences)
    assert not offences, f"the channel's one thread belongs in {allowed.name} alone: {named}"


def _code_without_documentation(path: Path) -> str:
    """One module's executable code, with its comments and docstrings taken out.

    Prose *about* tmux is exactly what this repository wants: the seam explains
    that pane semantics never cross it, and ADR 0004's log module explains why a
    tmux child is not in the engine's descriptor universe. Those sentences are
    the record, and a guard that forbade them would be pushing the reasoning out
    of the code. What must not spread is tmux *knowledge in code* — a command, an
    environment check, a pane id — so the check reads what actually runs.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body.pop(0)
            if not body:
                body.append(ast.Pass())
    return ast.unparse(ast.fix_missing_locations(tree))


def test_no_module_knows_tmux_at_all() -> None:
    """tmux was one optional *way* to launch a Session, and launching is parked (#72).

    Confinement to one adapter module became absence when that module went, so
    the guard reads the stronger way round: nothing in the composed tree may
    know tmux. The failure mode is the reference implementation's, where a tmux
    pane leaked into everything and the system could not run without one — and
    the coupling arrived as `TMUX` environment checks and pane ids scattered
    through ordinary code, with no import anywhere a dependency rule could have
    caught. So it is checked by reading the source, not the imports.

    Documentation is stripped first: prose may still explain why tmux is not
    here, which is a different thing from code knowing about it.
    """
    offences = [
        str(path.relative_to(PACKAGE))
        for path in _sources(PACKAGE)
        if "tmux" in _code_without_documentation(path).lower()
    ]
    named = "; ".join(offences)
    assert not offences, f"no module may know tmux, and these do: {named}"


def test_no_module_runs_a_tmux_command() -> None:
    """The stronger half of the rule, and the one with no exceptions at all.

    Knowing that `new-window` exists, or that a pane has an id, is the coupling
    itself, and it may not appear anywhere — documentation included, because a
    command spelled out in prose is a command somebody can copy.
    """
    commands = ("new-window", "new-session", "kill-window", "capture-pane", "display-message")
    offences = [
        f"{path.relative_to(PACKAGE)} knows {command!r}"
        for path in _sources(PACKAGE)
        for command in commands
        if command in path.read_text(encoding="utf-8")
    ]
    named = "; ".join(offences)
    assert not offences, f"no module may know a tmux command: {named}"


def test_nothing_allocates_a_pseudo_terminal() -> None:
    """`pty` came with the headless launcher, and it went with it (#72).

    Whoever opens the master end must drain it forever or the Session blocks the
    moment the buffer fills. Nothing carries that obligation now, so nothing may
    open one — a `pty.openpty()` that reappeared outside a launcher would be a
    master with no owner at all.
    """
    offences = [
        f"{path.relative_to(PACKAGE)} imports {module}"
        for path in _sources(PACKAGE)
        for module in _imported_modules(path.read_text(encoding="utf-8"))
        if module.split(".")[0] == "pty"
    ]
    named = "; ".join(offences)
    assert not offences, f"nothing may allocate a pseudo-terminal: {named}"


def test_installation_reaches_nothing_the_engine_is_made_of() -> None:
    """ADR 0012's one-way rule, and the reason installation is a package apart.

    Installation runs before any engine, from a shell that has not spawned one
    and with no `config.toml` to read. An import of `core`, `seams` or an adapter
    would be a claim that some part of the engine has to be constructible before
    the product is installed — which is the loop this arrangement exists to
    break. `locations` is deliberately not on this list: it is the leaf that
    exists so the paths are spelled once.
    """
    offences = [
        offence
        for prefix in (CORE_PREFIX, SEAMS_PREFIX, FORBIDDEN_INTERNAL_PREFIX)
        for offence in _imports_under(INSTALLATION, prefix)
    ]
    assert not offences, (
        "installation must be constructible with no engine, so it imports none of it: "
        + "; ".join(offences)
    )


def test_installation_package_exists() -> None:
    assert _sources(INSTALLATION), f"no Python sources found under {INSTALLATION}"
