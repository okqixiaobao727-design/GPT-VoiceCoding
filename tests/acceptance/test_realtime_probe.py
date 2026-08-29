"""Step 0b — the realtime contract, probed with the engine out of the way.

`docs/app-bundle.md` is explicit that no voice failure may be attributed to this
engine before the contract is re-verified outside it: the realtime methods are an
alpha backend surface, absent from the official app-server docs and gated
server-side, so the contract can move without anything in this repository
changing. A probe that fails identically outside the engine has told you, in
seconds, that the engine is not the subject.

The probe is **ported, not rewritten**: `scripts/rt_prototype.py --silent` in the
legacy checkout is the maintainer's own probe and the one
`docs/acceptance-design.md` names. It spawns its own `codex app-server` child,
its own thread and its own peer connection, opens no audio device, and uses
codex's own ChatGPT auth — there is no OpenAI key anywhere in this run. Copying
it into this repository would fork a script whose whole value is that it is the
one that was actually run against the backend, so it is invoked where it lives.

It runs on the **bundle's own interpreter**, which is where `aiortc` and `av`
are: the thing being accepted is the `.app`, and a probe run by some other Python
would prove the contract for some other copy of the stack.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
from pathlib import Path

import pytest
import support

pytestmark = pytest.mark.acceptance

#: Thirty seconds, the figure `docs/app-bundle.md` gives for this probe. It has
#: no duration flag — it runs until interrupted — so the harness interrupts it,
#: which is the `Ctrl-C hangs up cleanly` path its own usage line describes.
PROBE_SECONDS = 30.0
PROBE_GRACE_SECONDS = 20.0

#: What the probe prints when a remote track ends. The count is the observation
#: `docs/acceptance-design.md` asks for: "the count of frames received".
TOTAL_FRAMES = re.compile(r"total remote frames:\s*(\d+)")
FIRST_FRAME = re.compile(r"first remote audio frame received")


def test_the_realtime_contract(
    realtime_probe: Path, run_directory, journal, verdict, engine_path
) -> None:
    transcript = run_directory / "realtime-probe.log"
    command = [
        str(support.bundled_python()),
        str(realtime_probe),
        "--silent",
        "--cwd",
        str(run_directory),
    ]
    journal("realtime.probe.start", command=command, seconds=PROBE_SECONDS)
    with transcript.open("wb") as sink:
        process = subprocess.Popen(
            command,
            stdout=sink,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "PATH": engine_path},
            start_new_session=True,
        )
        try:
            process.wait(timeout=PROBE_SECONDS)
        except subprocess.TimeoutExpired:
            # Its own clean hang-up path, not a kill: the frame total is printed
            # by the shutdown, so a killed probe reports nothing at all.
            os.killpg(process.pid, signal.SIGINT)
            try:
                process.wait(timeout=PROBE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=PROBE_GRACE_SECONDS)

    output = transcript.read_text(errors="replace")
    total = TOTAL_FRAMES.search(output)
    frames = int(total.group(1)) if total else 0
    heard = bool(FIRST_FRAME.search(output))
    journal("realtime.probe.done", returncode=process.returncode, frames=frames, heard=heard)

    evidence = (
        f"{frames} remote audio frames in {PROBE_SECONDS:.0f}s, first frame "
        f"{'seen' if heard else 'never seen'}; transcript {transcript}"
    )
    verdict.record(
        "probe",
        "0b realtime contract probe",
        support.PASS if frames > 0 else support.FAIL,
        evidence,
    )
    assert frames > 0, (
        f"the engine-free realtime probe received no audio frames — the contract has moved, "
        f"and no voice failure this run sees may be attributed to the engine. {evidence}"
    )
