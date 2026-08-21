"""Proof that a real Session launches, registers with Bridge Core, and closes.

The contract tests establish the launcher's own rules against a fake process and
tmux layer. What they cannot establish is the half that is a fact about two other
products rather than about this repository: that a real `claude` started this way
registers itself where the launcher reads, in the workspace it was told, and that
a real `codex --remote` announces its thread before anybody types.

So this is a **manual, local job — deliberately outside the test suite**, like the
three Relay proofs beside it. It starts real Sessions. It spends no model tokens:
nothing here sends a turn.

    python3 scripts/session_launcher_proof.py --agent claude
    python3 scripts/session_launcher_proof.py --agent codex
    python3 scripts/session_launcher_proof.py --agent claude --tmux

What it does, in order:

1. builds the launcher named on the command line;
2. launches one Session into a throwaway workspace, through **Bridge Core**, so
   what is proved is the registry ending up correct rather than the adapter
   returning a nice object;
3. prints the identity Core registered, and — for the headless adapter — what the
   Session actually put on its own terminal;
4. closes it, and prints the outcome including any per-child destinations.

**The tmux run leaves nothing behind either**, but it is worth knowing that it
really does open a window in a session called `gpt-voicecoding`: attach with
`tmux attach -t gpt-voicecoding` while it is waiting, and you will see the Session
the way a user would.

**A first Codex launch into a brand-new workspace is expected to fail**, and that
is the design rather than a defect: codex shows a directory-trust dialog for a
directory it has not seen, a headless Session cannot answer one, and the launcher
reports that truthfully instead of pretending (ADR 0008). Run it with `--tmux`,
answer the dialog yourself, and the headless run works from then on.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gpt_voicecoding.adapters.agent.claude import claude_agent  # noqa: E402
from gpt_voicecoding.adapters.agent.codex import codex_agent  # noqa: E402
from gpt_voicecoding.adapters.session_launcher import (  # noqa: E402
    direct_child_launcher,
    tmux_launcher,
)
from gpt_voicecoding.core.bridge import BridgeCore  # noqa: E402
from gpt_voicecoding.core.relay_queue import RelayQueue  # noqa: E402
from gpt_voicecoding.core.sessions import SessionRegistry  # noqa: E402
from gpt_voicecoding.core.state import BridgeState  # noqa: E402
from gpt_voicecoding.core.switches import Switchboard  # noqa: E402
from gpt_voicecoding.seams.identity import AgentKind, SessionLabel  # noqa: E402

#: Long enough for a TUI to start and register on a cold cache.
LOOK_AT_IT_SECONDS = 20.0


class Recorder:
    """The event sink, printing what the adapters raise as it arrives."""

    def emit(self, event: object) -> None:
        print(f"    event: {type(event).__name__}: {event}")


def say(step: str, detail: str = "") -> None:
    print(f"\n== {step} ==" + (f"\n   {detail}" if detail else ""))


async def run(arguments: argparse.Namespace) -> int:
    agent = AgentKind(arguments.agent)
    if shutil.which(str(agent)) is None:
        raise SystemExit(f"no `{agent}` on PATH; this proof needs the real CLI")

    # A throwaway workspace by default. `--workspace` exists for the Codex case:
    # a directory codex has never seen stops on its trust dialog, so proving the
    # *success* path needs a directory the operator has already trusted. It is
    # only ever read and started in — nothing here sends a turn.
    made_it = arguments.workspace is None
    workspace = (
        Path(tempfile.mkdtemp(prefix="vc-proof-ws-", dir="/tmp"))
        if made_it
        else Path(arguments.workspace).expanduser().resolve()
    )
    if not workspace.is_dir():
        raise SystemExit(f"no workspace at {workspace}")
    runtime = Path(tempfile.mkdtemp(prefix="vc-proof-rt-", dir="/tmp"))
    settings = {"runtime_directory": str(runtime)}

    recorder = Recorder()
    build = tmux_launcher if arguments.tmux else direct_child_launcher
    launcher = build(sink=recorder, settings=settings)
    claude = claude_agent(sink=recorder)
    codex = codex_agent(sink=recorder)
    launcher.use_claude(claude)
    launcher.use_codex(codex)

    core = BridgeCore(
        state=BridgeState(switches=Switchboard(), sessions=SessionRegistry(), relays=RelayQueue()),
        call=None,
        channel=None,
        agents={AgentKind.CLAUDE: claude, AgentKind.CODEX: codex},
        launcher=launcher,
    )

    say(
        f"1. launching a real {agent} Session",
        f"adapter: {'tmux' if arguments.tmux else 'direct child (headless)'}   "
        f"workspace: {workspace}",
    )
    if arguments.tmux:
        print("   attach with:  tmux attach -t gpt-voicecoding")

    try:
        await claude.connect()
        outcome = await core.launch_session(
            agent=agent,
            workspace=workspace,
            label=SessionLabel(project="gpt-voicecoding", task="session launcher proof"),
        )
        print(f"\n   outcome: {outcome.status}")
        if outcome.target is None:
            print(f"   detail:\n{outcome.detail}")
            return 1

        say("2. what Bridge Core registered")
        for held in core.status().sessions:
            print(f"   {held.target}   state={held.state}   workspace={held.workspace}")
            print(f"   label: {held.label}")

        say("3. the Session is live", f"looking at it for {LOOK_AT_IT_SECONDS:.0f}s")
        if not arguments.tmux:
            held = launcher._live[outcome.target]
            await asyncio.sleep(LOOK_AT_IT_SECONDS)
            print("   the last of what it put on its own terminal:")
            print("   " + "\n   ".join(held.console.tail().splitlines()[-12:]))
        else:
            await asyncio.sleep(LOOK_AT_IT_SECONDS)
            print("   still there — look at the tmux window before this closes it.")

        say("4. closing it")
        closed = await core.close_session(outcome.target)
        print(f"   outcome: {closed.status}   {closed.detail}")
        for child in closed.children:
            print(f"   child: {child.ref}   closed={child.closed}   {child.detail}")
        print(f"   registry now says: {core.status().sessions[0].state}")
        return 0 if closed.status.value in ("closed", "already_closed") else 1

    finally:
        say("cleaning up")
        for closing in (launcher, claude, codex):
            try:
                await closing.aclose()
            except Exception as refused:  # a proof reports rather than hides
                print(f"   closing {type(closing).__name__} raised: {refused}")
        # Only ever remove a workspace this script made. A directory the
        # operator named is theirs.
        if made_it:
            shutil.rmtree(workspace, ignore_errors=True)
        shutil.rmtree(runtime, ignore_errors=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", choices=[str(kind) for kind in AgentKind], required=True)
    parser.add_argument(
        "--tmux",
        action="store_true",
        help="use the optional tmux launcher instead of the headless default",
    )
    parser.add_argument(
        "--workspace",
        help=(
            "launch into this directory instead of a throwaway one. Needed to prove the "
            "Codex success path: codex stops on a trust dialog for a directory it has "
            "not seen, so name one you have already opened. Never removed by this script."
        ),
    )
    raise SystemExit(asyncio.run(run(parser.parse_args())))
