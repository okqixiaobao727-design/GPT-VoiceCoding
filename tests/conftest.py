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

**The real shared Codex daemon.** `shared_daemon.locate` shells out to `codex
app-server daemon version` to find the socket the machine's daemon is listening
on, and #77 put that lookup on the path every Relay and every Approval now
takes. A test that reached it would not merely read: it would attach to the
daemon holding the Sessions the person running the tests has open, and a Relay
is a `turn/start`. Injecting a `locate` or a `run` is the design; taking the
real runner away is what makes forgetting it fail loudly instead of quietly
starting a turn in somebody's work.

**The real Claude Session registry.** #77 put this engine on Claude Code's own
cross-session wire, and being answerable there means publishing a peer key into
`~/.claude/sessions` — the registry holding the live Sessions of whoever is
running the tests. A test that bound a reply inbox with default settings would
publish that key, and a key in that directory is this process announcing itself
as a peer of their open work. Passing a `registry_directory` is the design;
refusing the real one is what makes forgetting it fail loudly.

**This file holds fixtures and nothing else.** The fake and the helpers it needs
live in `launchd_fake.py`, because a test module that imports a `conftest` by
name is importing whichever `conftest` reached `sys.path` first — and with the
`[acceptance]` extra installed that is `tests/acceptance/conftest.py`, not this
one ([#93](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/93)).
`tests/test_layout.py` holds the rule so it cannot come back.
"""

from __future__ import annotations

import os
import socket
import stat
from collections.abc import Iterator, Sequence

import pytest

from gpt_voicecoding.adapters.agent.claude.inbox import ReplyInbox
from gpt_voicecoding.adapters.agent.claude.registry import DEFAULT_REGISTRY_DIRECTORY
from gpt_voicecoding.adapters.agent.codex import shared_daemon
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


@pytest.fixture(autouse=True)
def _no_real_codex_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one lookup that leads to the machine's own Codex Sessions, refused.

    **Scoped to that command rather than to the runner**, and the distinction is
    the hazard rather than a convenience. `_run` is a general subprocess helper
    whose own timeout is proved against `/bin/sleep`, which names no daemon and
    reaches nobody's Sessions. What must never happen is the *lookup* —
    `codex app-server daemon version` answers with the socket the machine's
    daemon is listening on, and from there a Relay is a `turn/start` in somebody's
    open work.
    """
    real = shared_daemon._run  # noqa: SLF001

    async def refuse(arguments: list[str]) -> tuple[int, str]:
        if tuple(arguments[1:]) == shared_daemon.DAEMON_VERSION_ARGUMENTS:
            raise AssertionError(
                "a test looked for the machine's own Codex daemon through "
                f"gpt_voicecoding.adapters.agent.codex.shared_daemon: {arguments}. "
                "Pass a SharedDaemon with a `locate`/`attach` of its own, or a `run=` to locate."
            )
        return await real(arguments)

    monkeypatch.setattr(shared_daemon, "_run", refuse)


@pytest.fixture(autouse=True)
def _no_real_claude_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test publishes this process as a peer of the real machine's Sessions."""
    real = ReplyInbox._publish_key  # noqa: SLF001

    def refuse(self: ReplyInbox) -> None:
        if self._key_path.parent == DEFAULT_REGISTRY_DIRECTORY:  # noqa: SLF001
            raise AssertionError(
                "a test published a peer key into the machine's own Claude registry at "
                f"{DEFAULT_REGISTRY_DIRECTORY}. Pass a `registry_directory` in ClaudeSettings."
            )
        real(self)

    monkeypatch.setattr(ReplyInbox, "_publish_key", refuse)


@pytest.fixture
def mode_at_bind(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, int]]:
    """Every `AF_UNIX` socket's permission bits in the instant `bind` returned.

    The property `start_private_unix_server` exists for is not "ends up 0600" —
    binding wide and narrowing with a chmod ends up 0600 too, and that is the
    defect, not the fix (#116). The property is "was never anything else", so the
    reading has to happen in the one instant nothing can have followed the bind:
    `bind` is what creates the file, and the mode it is created at is the whole
    question.

    So the seam is `bind` itself, not whatever calls it. Reading at
    `asyncio.start_unix_server` instead would be reading after the helper had
    already bound *and* listened, which is late enough that a helper narrowing
    its own socket in between would go unnoticed — the test would pass and the
    window would be open. Patching here also covers the callers that hand
    `asyncio` a `path` and let it do the binding, with no second branch.

    The umask is opened all the way for the duration, so a socket that took its
    mode from the umask is unmistakable rather than coincidentally right.
    """
    recorded: dict[str, int] = {}
    bind = socket.socket.bind

    def recording(self: socket.socket, address: object) -> None:
        bind(self, address)
        if self.family == socket.AF_UNIX and isinstance(address, str):
            recorded[address] = stat.S_IMODE(os.stat(address).st_mode)

    monkeypatch.setattr(socket.socket, "bind", recording)
    wide_open = os.umask(0o000)
    try:
        yield recorded
    finally:
        os.umask(wide_open)


@pytest.fixture
def launchd() -> FakeLaunchd:
    return FakeLaunchd()
