"""Both lanes' journey, against real agents the harness started by hand.

## Two layers, and there is no third

* **Per-ticket, one step.** A build ticket's "Red first" line names a step, and
  that is what a developer runs while building it:

      .venv/bin/python -m pytest -m acceptance tests/acceptance \\
          --lane claude --step "stable name"

  The step is graded. Its prerequisites (`journey.PREREQUISITES`) run first as
  **ungraded setup**, on a fresh engine and a hand-started Session of their own,
  because a step read on ground the walk did not arrange is a step read on
  nothing. `verdict.json` names both kinds, so a green step is never mistaken for
  a green lane.

* **Before merging to `main`, the whole thing.** No options: every step of every
  lane, the two lanes walking **concurrently** on two Telegram bots. A human
  triggers it; it never runs in CI.

That is the whole ladder. There is no separate release layer, and the
consequence is deliberate (#180 §2 decision 4): a step that needs a human runs on
every full run, or it is not in the acceptance.

## One run per machine, whatever `--lane` it names

Two lanes run at once **inside one run**. Two *runs* must not: they share things
no option separates, and nothing in the harness refuses the second one yet, so
this is a rule a person keeps.

* **The Telegram user account is one client.** Its session is an SQLite file
  holding a bearer auth key, and a second process opening it gets `database is
  locked` at best (`telegram_person.PersonConnection`). Two lanes share one
  connection for exactly this reason; two runs cannot.
* **The trust gate writes the user's own files.** `support.TrustGate`
  read-modify-writes `~/.claude.json` and `~/.codex/config.toml`, and the lock
  that keeps two lanes off each other there is a **thread** lock — it means
  nothing to a second pytest process, whose revoke can drop the other run's
  entry from a file that is not the harness's to lose.

## One test, parametrised, rather than one module per lane

The two used to be separate files whose bodies were the same forty lines with one
name changed — and the same forty lines is where a fix applied to one lane and not
the other comes from. The lane is a value (`journey.Lane`), so the difference
between the lanes lives entirely in that value; pytest's own parametrisation names
the lane in the test id, so a failure still says which lane failed without a
module per lane to say it.

**The walking happens off this test, and the test joins it.** Both lanes are
started together by the `lane_runs` fixture, one thread each (`conftest.py`
explains why the concurrency lives there); this test waits for its own lane and
grades what that lane wrote down. So the run costs one lane's wall clock rather
than the sum of two, and the verdict is still one file with a block per lane.

What each lane is expected to find on today's `main` is stated on #73 rather than
guessed at here: the nine steps are the red lines #74–#80 clear, and each is red
until the ticket that owns it lands.
"""

from __future__ import annotations

import pytest
import support

pytestmark = pytest.mark.acceptance


def test_the_lane(lane, lane_runs, verdict) -> None:  # noqa: ANN001
    run = lane_runs[lane.name]
    if run.thread is not None:
        run.thread.join()
    if run.failure is not None:
        raise run.failure

    recorded = verdict.lanes.get(lane.name, [])
    assert recorded, f"the {lane.name} lane recorded nothing at all"
    failed = [step for step in recorded if step.result != support.PASS]
    assert not failed, "\n".join(
        f"{step.step}{'' if step.graded else ' (setup)'}: {step.result} — {step.evidence}"
        for step in failed
    )
