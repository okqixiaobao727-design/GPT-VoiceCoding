"""Proof that the microphone grant lands on the app, not on an interpreter.

This is the one part of the app-bundle pipeline a machine cannot finish. macOS
shows the TCC prompt to a person and waits for them to click it, so the script
prepares everything, watches the log, and tells the maintainer exactly what they
should be seeing — it does not, and cannot, decide the answer.

    scripts/build-app.sh
    python3 scripts/microphone_grant_proof.py

What it proves, mirroring the probe that gated ADR 0005:

1. **The prompt names the app.** `AUTHREQ_SUBJECT` in the log is the bundle
   identifier, and the sentence the user reads is the app's own
   `NSMicrophoneUsageDescription` — `python3.12` never appears, even though
   `python3.12` is the process that opens the device.
2. **The grant survives a second run**, so it attached to the bundle rather than
   to one launch.
3. **The engine is the shell's own child, from inside the bundle**, which is the
   containment ADR 0005 says is load-bearing.

The probe's **negative control is deliberately not re-run**: it established a
property of macOS — an interpreter outside any `.app` collapses to the bare
binary path and re-prompts — not a property of this bundle, and that conclusion
does not decay when our layout changes. Re-running it would cost a `tccutil
reset` and a second grant cycle to learn nothing new.

Nothing here is destructive without saying so first: `--reset` is the only flag
that touches the user's existing grant, and it is not the default.
"""

from __future__ import annotations

import argparse
import plistlib
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app_bundle import inputs  # noqa: E402
from app_bundle.plan import BuildPlan  # noqa: E402

#: What the log calls a TCC decision, and the field that says who it is about.
LOG_PREDICATE = 'subsystem == "com.apple.TCC" AND eventMessage CONTAINS "AUTHREQ"'


def say(step: str, body: str) -> None:
    print(f"\n\033[1m{step}\033[0m\n{body}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--app",
        type=Path,
        default=None,
        help="the bundle to check; defaults to the one scripts/build-app.sh builds",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="forget the existing microphone grant first, so the prompt appears again",
    )
    parsed = parser.parse_args(argv)

    app = parsed.app or BuildPlan.resolve().app
    if not app.is_dir():
        print(f"no bundle at {app} — run scripts/build-app.sh first", file=sys.stderr)
        return 2

    with (app / inputs.CONTENTS / "Info.plist").open("rb") as handle:
        plist = plistlib.load(handle)
    identifier = plist["CFBundleIdentifier"]
    usage = plist["NSMicrophoneUsageDescription"]

    say(
        "The bundle under test",
        f"  {app}\n  identifier: {identifier}\n  it will ask: “{usage}”",
    )

    if parsed.reset:
        say(
            "Forgetting the existing grant",
            f"  tccutil reset Microphone {identifier}",
        )
        subprocess.run(["tccutil", "reset", "Microphone", identifier], check=False)
    else:
        say(
            "Not resetting",
            "  This run will only show a prompt if the grant was never given.\n"
            f"  Pass --reset to forget it first: tccutil reset Microphone {identifier}",
        )

    say(
        "1. Watch the log, in another terminal",
        f"  log stream --style compact --predicate '{LOG_PREDICATE}'\n\n"
        "  The line that matters names AUTHREQ_SUBJECT. It must be\n"
        f"    {identifier}\n"
        "  and NOT a path ending in python3.12. If you see the interpreter's path,\n"
        "  bundle containment has broken and ADR 0005's mechanism is not holding.",
    )

    say(
        "2. Launch it, and start a Live Call",
        f"  open {app}\n\n"
        "  macOS should show a prompt naming the app, with the sentence above.\n"
        "  Grant it.",
    )

    say(
        "3. Check the engine is the shell's own child, from inside the bundle",
        "  pgrep -P $(pgrep -f MacOS/GPTVoiceCodingShell | head -1)\n"
        "  ps -o comm= -p $(pgrep -f gpt_voicecoding.engine | head -1)\n\n"
        "  The second must print a path inside the .app. A framework CPython\n"
        "  re-executes itself from its original location, which is why the bundle\n"
        "  ships python-build-standalone and not a virtual environment.",
    )

    say(
        "4. Kill the shell, and watch the engine outlive it",
        "  kill -9 $(pgrep -f MacOS/GPTVoiceCodingShell | head -1)\n"
        "  pgrep -f gpt_voicecoding.engine        # still there, now under launchd\n\n"
        "  Quitting from the menu is NOT this test: a clean quit stops the engine\n"
        "  on the way out, by design. Only an abnormal death orphans it, and that\n"
        "  is the case the probe checked — the grant belongs to the bundle, so it\n"
        "  survives the process that spawned the engine going away. Ask the\n"
        "  orphan to speak (bridgectl live) and it must still reach the\n"
        "  microphone with no prompt.\n\n"
        "  Then launch the app again: it must NOT spawn a second engine against\n"
        "  the live socket. Exit 2 with something already listening is not a\n"
        "  crash — the shell says so and offers Retry.",
    )

    say(
        "5. Quit it properly, launch it again, start another Live Call",
        "  bridgectl or the menu's Quit, then open the app again.\n"
        "  There must be NO second prompt. A grant that has to be given twice\n"
        "  attached to a launch rather than to the bundle.",
    )

    say(
        "Known v0 cost, not a failure",
        "  Ad-hoc signatures change per build, so macOS may ask again after you\n"
        "  install a NEW build. That is charter decision 9, accepted; it is not\n"
        "  the same thing as being asked twice for the same build.",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
