"""Proof that a Codex Session outside tmux registers, is observed, and takes a Relay.

This is the claim the Codex adapter is built on, and it is the one thing a fake
app-server cannot establish: the reference implementation refused to host or
register a Codex session unless `TMUX` and `TMUX_PANE` were both set, and the
decoupling verdict removed that requirement. Whether it is really gone is a fact
about a real `codex`, not about this repository's test doubles.

So this is a **manual, local job — deliberately not part of the test suite**. It
starts a real app-server and, with `--relay`, spends real model tokens.

    python3 scripts/codex_bare_terminal_proof.py            # free
    python3 scripts/codex_bare_terminal_proof.py --relay    # spends tokens

**You launch the Session yourself, in your own terminal.** An earlier version of
this script drove a Codex TUI on a synthetic pseudo-terminal, and that is not
worth doing: an interactive TUI has startup prompts — the in-app update prompt
is one, and its default option runs an installer — so a script that answers them
blind is a script that can install software nobody asked for. A terminal window
you opened yourself is also the better witness for the claim being made, which
is precisely that an ordinary terminal is enough.

What happens, in order, with no tmux anywhere:

1. this script spawns `codex app-server` as a **direct child**, on a private
   Unix socket, standing in for the launch wrapper that will eventually own it
   (that wrapper belongs to the Session Launcher, not to this adapter);
2. you run one printed command in any terminal that is not tmux;
3. the adapter registers the thread that session starts, and reports what it
   observes — the Reply Window, the approval routing it read back;
4. with `--relay`, it carries one Answer Relay in and prints the receipt.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gpt_voicecoding.adapters.agent.codex import codex_agent  # noqa: E402
from gpt_voicecoding.adapters.codex_app_server.process import attach  # noqa: E402
from gpt_voicecoding.adapters.codex_app_server.settings import CodexSettings  # noqa: E402
from gpt_voicecoding.seams.identity import (  # noqa: E402
    AgentKind,
    SessionTarget,
    new_request_id,
)

#: What the tmux coupling used to look like. Cleared from the app-server's
#: environment so the "no tmux" claim is not quietly false.
TMUX_VARIABLES = ("TMUX", "TMUX_PANE")

#: Long enough for a person to open a window and paste a command.
WAIT_FOR_A_HUMAN_SECONDS = 300.0


class EventRecorder:
    """Collects what the adapter raised upward, and prints it as it arrives."""

    def __init__(self) -> None:
        self.events: list[object] = []

    def emit(self, event: object) -> None:
        self.events.append(event)
        print(f"    event: {type(event).__name__}: {event}")


def say(step: str, detail: str = "") -> None:
    print(f"\n== {step} ==" + (f"\n   {detail}" if detail else ""))


async def wait_for(condition, seconds: float, what: str):
    """Wait for something outside this process to happen, or give up saying what."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while loop.time() < deadline:
        found = condition()
        if found:
            return found
        await asyncio.sleep(0.2)
    raise SystemExit(f"gave up waiting for {what} after {seconds:.0f}s")


def bare_environment() -> dict[str, str]:
    """This process's environment with every trace of tmux removed."""
    environment = dict(os.environ)
    for name in TMUX_VARIABLES:
        environment.pop(name, None)
    return environment


async def main(relay: bool) -> int:
    codex = shutil.which("codex")
    if codex is None:
        raise SystemExit("no `codex` on PATH; this proof needs the real CLI")

    workspace = Path(tempfile.mkdtemp(prefix="codex-bare-proof-"))
    socket_path = Path(tempfile.mkdtemp(prefix="cbp-", dir="/tmp")) / "app-server.sock"
    settings = CodexSettings(receipt_timeout_seconds=60.0)

    app_server: subprocess.Popen[bytes] | None = None
    session_connection = None
    adapter = None
    recorder = EventRecorder()

    try:
        say("1. app-server, as a direct child, with no tmux in its environment")
        app_server = subprocess.Popen(
            [codex, "app-server", "--listen", f"unix://{socket_path}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            env=bare_environment(),
        )
        await wait_for(socket_path.is_socket, 30, "the app-server's socket")
        print(f"   listening on {socket_path}   (pid {app_server.pid})")

        say("2. watching for a Session to appear")
        started: list[str] = []

        def heard(message: dict) -> None:
            if message.get("method") == "thread/started":
                thread = (message.get("params") or {}).get("thread") or {}
                if isinstance(thread.get("id"), str):
                    started.append(thread["id"])

        session_connection = await attach(
            socket_path, version="proof", settings=settings, on_notification=heard
        )
        print("\n   Open a terminal that is NOT inside tmux, and run:\n")
        print(f"       cd {workspace} && codex --remote unix://{socket_path}\n")
        print("   Then type anything into it — a Session with no history cannot be")
        print("   resumed, so it has to do one thing before it can be observed.")
        print("   (`!echo hello` runs a shell command and costs nothing.)\n")

        thread_id = (
            await wait_for(lambda: started[:1], WAIT_FOR_A_HUMAN_SECONDS, "a Codex Session")
        )[0]
        print(f"   a Session appeared: {thread_id}")

        say("3. registering it, and observing it")
        target = SessionTarget(agent=AgentKind.CODEX, session_id=thread_id)
        adapter = codex_agent(sink=recorder, settings={"receipt_timeout_seconds": 60.0})
        await adapter.register_session(target, socket_path)
        held = adapter._threads[target]
        print(f"   registered: {target}")
        if not held.subscribed:
            print(f"   not observable yet: {held.subscribe_blocked}")
            print("   waiting for that Session to do something...")
            await wait_for(
                lambda: held.subscribed, WAIT_FOR_A_HUMAN_SECONDS, "the Session to do anything"
            )
        print(f"   observed. reply window: {held.reply_window}")
        print(f"   approval routing, as read back: {held.routing}")
        print(f"   events raised upward so far: {len(recorder.events)}")

        if not relay:
            say(
                "4. SKIPPED — a Relay is a real model turn",
                "re-run with --relay to carry one Answer Relay and print its receipt.",
            )
            return 0

        say("4. one Answer Relay into that Session")
        receipt = await adapter.answer_relay(
            target,
            "Reply with exactly the word ACKNOWLEDGED and do nothing else.",
            request_id=new_request_id(),
        )
        print(f"\n   receipt: {receipt.outcome}   {receipt.reason}")
        print("   look at the terminal you opened: the words should be in it.")
        await asyncio.sleep(5)
        return 0 if receipt.is_delivered else 1

    finally:
        say("cleaning up")
        if adapter is not None:
            with contextlib.suppress(Exception):
                await adapter.aclose()
        if session_connection is not None:
            with contextlib.suppress(Exception):
                await session_connection.aclose()
        if app_server is not None and app_server.poll() is None:
            # This takes the Session down with it — which is exactly the coupling
            # the engine's real topology avoids. The engine never owns the
            # app-server a user's Session is a client of; here the script does,
            # because here the script is standing in for the launch wrapper.
            app_server.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                app_server.wait(timeout=10)
            if app_server.poll() is None:
                app_server.kill()
        shutil.rmtree(workspace, ignore_errors=True)
        shutil.rmtree(socket_path.parent, ignore_errors=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--relay",
        action="store_true",
        help="also carry one Answer Relay. This runs a real model turn and costs tokens.",
    )
    raise SystemExit(asyncio.run(main(parser.parse_args().relay)))
