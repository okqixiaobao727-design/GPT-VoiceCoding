"""What no test in this suite is allowed to reach.

**The real `launchctl`.** The Codex installation item loads a login job into
launchd, and a job loaded from a test is loaded into the launchd of the person
running the test — a real login session, a real `~/Library/LaunchAgents`, and a
real Codex daemon started under it. That is not a thought experiment: two drafts
of `installation/codex_launch_agent.py` did exactly that while it was being
written, once naming a plist pytest deleted a second later.

Passing a fake in is the design (`Launchd` has no default and every entry point
demands one), but a design only holds while everyone remembers it, and a test
that forgets does not fail — it silently changes the machine and passes. So the
real runner is taken away from the whole suite here, and a test that wants a
subprocess has to say so by supplying its own.

**This file holds fixtures and nothing else.** The fake and the helpers it needs
live in `launchd_fake.py`, because a test module that imports a `conftest` by
name is importing whichever `conftest` reached `sys.path` first — and with the
`[acceptance]` extra installed that is `tests/acceptance/conftest.py`, not this
one ([#93](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/93)).
`tests/test_layout.py` holds the rule so it cannot come back.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from gpt_voicecoding.installation import codex_launch_agent
from launchd_fake import FakeLaunchd


@pytest.fixture(autouse=True)
def _no_real_launchctl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every subprocess this module would run, refused in the loudest way there is."""

    def refuse(arguments: Sequence[str]) -> tuple[int, str]:
        raise AssertionError(
            "a test reached the real machine through "
            f"gpt_voicecoding.installation.codex_launch_agent: {list(arguments)}. "
            "Pass a Launchd with a `run` of its own, or a `run=` to daemon_versions."
        )

    monkeypatch.setattr(codex_launch_agent, "_run", refuse)


@pytest.fixture
def launchd() -> FakeLaunchd:
    return FakeLaunchd()
