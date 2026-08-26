"""The layout ADR 0001 describes is the layout that is actually here."""

from __future__ import annotations

import importlib
from pathlib import Path

SEAMS = (
    "agent",
    "call",
    "companion_channel",
    "control_plane",
)

ADAPTER_FAMILIES = (
    "agent",
    "call",
    "companion_channel",
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


#: Where this suite's own modules live, and the directory whose modules share
#: their `sys.path` with them.
TESTS = Path(__file__).resolve().parent
ACCEPTANCE = TESTS / "acceptance"


def _importable(directory: Path) -> set[str]:
    """Every module name that directory contributes to `sys.path`."""
    return {source.stem for source in directory.glob("*.py")}


def test_no_test_module_imports_a_conftest() -> None:
    """`conftest.py` is pytest's file, loaded by path — never an import target.

    `from conftest import …` is an ordinary import and goes through `sys.path`,
    where `conftest` is not a unique name: `tests/` has one and
    `tests/acceptance/` has another. Which one wins depends on whether the
    `[acceptance]` extra is installed, because that is what decides whether the
    acceptance directory is collected and reaches `sys.path` at all. #93: the
    unit suite died at collection for anyone who had run the real-environment
    acceptance, and CI — which never installs the extra — stayed green through
    all of it. So this rule is checked where the bug is *invisible*, which is the
    only place a check would have caught it.
    """
    offences = [
        f"{source.relative_to(TESTS.parent)}:{number}"
        for source in TESTS.rglob("*.py")
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1)
        if line.startswith(("from conftest import", "import conftest"))
    ]
    assert not offences, (
        "a test module imports a conftest by name, which resolves to whichever "
        "conftest reached sys.path first — put the shared piece in a module with "
        "a name of its own instead: " + "; ".join(offences)
    )


def test_no_module_name_is_spelled_in_both_test_directories() -> None:
    """The same trap as above, for every other name these two directories share.

    Both are flat directories on `sys.path` with no package around them, so one
    `support.py` in each would be one `support` for both — and which one a test
    got would again depend on an extra being installed. `conftest` is exempt
    because pytest loads those by path and every directory is meant to have one.
    """
    shared = (_importable(TESTS) & _importable(ACCEPTANCE)) - {"conftest"}
    assert not shared, (
        "these module names exist in both tests/ and tests/acceptance/, so one "
        "shadows the other on sys.path: " + ", ".join(sorted(shared))
    )
