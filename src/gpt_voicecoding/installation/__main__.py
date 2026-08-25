"""``bridge-install`` — the composition root for installation, and its whole CLI.

A ``.app`` dragged into ``/Applications`` has no install step: macOS copies a
directory and runs nothing. So first launch *is* the install, and the menu-bar
shell runs ``bridge-install reconcile`` before it spawns the engine (ADR 0012).
Reconcile is the only verb the shell uses, and it writes nothing when the machine
already agrees with what this build would put there.

**It does not go through the engine, on purpose.** The engine refuses to start
without a ``config.toml`` the user writes by hand, so an installation that waited
for the engine would not have happened by the time the user first opens the app.
Nothing here dials a socket, reads engine state, or asks Bridge Core anything.

**The items are named here, one line each.** That is what stands in for a
registry: with two artifacts and no plugin story, a list of two calls *is* the
boundary, and adding a third is adding a line. The Codex login ``LaunchAgent``
joins these lists with #83.

**The interpreter is ``sys.executable`` and is never written down.** Inside the
bundle that is the bundled python beside this console script, which is the whole
point: a hook command naming a developer checkout's interpreter is #38, and the
only way not to have that bug is never to name an interpreter at all.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from gpt_voicecoding.installation import (
    Intent,
    Outcome,
    State,
    claude_hooks,
    read_intent,
    write_intent,
)

#: Everything went as asked.
EXIT_OK = 0
#: At least one item could not be put where it belongs, and said why.
EXIT_FAILED = 1

VERBS = ("reconcile", "install", "uninstall", "status")

#: How the intent file is named in a report. It is not an installed artifact —
#: it lives in this product's own directory — but a run that cannot write it has
#: to say so, because the next launch reads the answer it failed to change.
RECORD_NAME = "installation-record"


def _inspect_all(config_directory: Path, interpreter: Path) -> list[Outcome]:
    return [claude_hooks.inspect(config_directory, interpreter)]


def _install_all(config_directory: Path, interpreter: Path) -> list[Outcome]:
    return [claude_hooks.install(config_directory, interpreter)]


def _uninstall_all(config_directory: Path) -> list[Outcome]:
    return [claude_hooks.uninstall(config_directory)]


def parse(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bridge-install",
        description=(
            "Put this product's artifacts where the coding agents will find them, "
            "take them back, or say what is there now."
        ),
        epilog=(
            "reconcile  install what is missing or out of date, unless uninstalled\n"
            "install    install, and record that this machine wants it\n"
            "uninstall  take back exactly what was written, and record that\n"
            "status     say what is on this machine, and write nothing"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("verb", choices=VERBS)
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    base_dir: Path | None = None,
    interpreter: Path | None = None,
) -> int:
    """Entry point for the ``bridge-install`` console script.

    The three keyword arguments exist so a test can run the whole verb without
    installing this product on the machine running the test.
    """
    verb = parse(argv).verb
    environ = os.environ if environ is None else environ
    interpreter = Path(sys.executable) if interpreter is None else interpreter
    config_directory = claude_hooks.default_config_directory(environ)

    if verb == "status":
        outcomes = _inspect_all(config_directory, interpreter)
        print(_intent_line(read_intent(base_dir)))
    elif verb == "uninstall":
        outcomes = _uninstall_all(config_directory)
        # Only when it really came back out. `wanted: false` is what stops every
        # later reconcile from touching this machine, so recording it over an
        # uninstall that failed would leave our hooks in the user's settings file
        # with nothing left that would ever repair or remove them.
        if all(outcome.ok for outcome in outcomes):
            outcomes = _with_intent(outcomes, wanted=False, base_dir=base_dir)
    elif verb == "install":
        # Recorded whether or not the artifacts landed, and that asymmetry is the
        # honest one: this file holds what the user *wants*, and a failed install
        # is a want that the next reconcile should retry rather than forget.
        outcomes = _install_all(config_directory, interpreter)
        outcomes = _with_intent(outcomes, wanted=True, base_dir=base_dir)
    else:
        intent = read_intent(base_dir)
        if not intent.install_wanted:
            print("uninstalled on this machine — reconcile leaves it alone")
            return EXIT_OK
        outcomes = _install_all(config_directory, interpreter)
        if intent.first_run and all(outcome.ok for outcome in outcomes):
            outcomes = _with_intent(outcomes, wanted=True, base_dir=base_dir)

    for outcome in outcomes:
        print(outcome.line(), file=sys.stdout if outcome.ok else sys.stderr)
    return EXIT_OK if all(outcome.ok for outcome in outcomes) else EXIT_FAILED


def _with_intent(outcomes: list[Outcome], *, wanted: bool, base_dir: Path | None) -> list[Outcome]:
    """Record the answer, and report a record that could not be written.

    A failed record is not a failed install: the artifacts are where they belong.
    It is reported anyway, because the next launch would read the old answer.
    """
    failure = write_intent(wanted, base_dir)
    if not failure:
        return outcomes
    return [*outcomes, Outcome(RECORD_NAME, State.ABSENT, ok=False, note=failure)]


def _intent_line(intent: Intent) -> str:
    if intent.wanted is None:
        return f"{RECORD_NAME}: nothing recorded — the next reconcile installs"
    return f"{RECORD_NAME}: wanted={str(intent.wanted).lower()}"


if __name__ == "__main__":  # pragma: no cover - the console script calls `main`
    raise SystemExit(main())
