"""Proof that a real Claude Code session takes an Answer Relay through the channel.

The channel server here is a Python rewrite of an implementation that was proven
in Node (ADR 0006), and the one risk that rewrite re-opens is the handshake:
whether *real* Claude Code accepts this server as a channel, pushes its
notifications into the session, and calls the tool back. No fixture can answer
that, and CI runs no Claude Code — so this script is the gate, and it is a
**manual, local job deliberately outside the test suite**.

    python3 scripts/claude_channel_proof.py            # free: prepare and observe
    python3 scripts/claude_channel_proof.py --relay    # spends real model tokens

**You launch the session yourself, in your own terminal**, exactly as the Codex
proof does and for the same reason: an interactive TUI has startup prompts, so a
script that answers them blind can install software nobody asked for — and a
window you opened yourself is the better witness anyway.

What this script does, and what it deliberately does not:

- it renders the plugin (`plugin.json` + `marketplace.json`) into a directory and
  prints every command you need to install it, **and runs none of them**:
  installing a plugin and widening a machine-wide allow-list are your decisions,
  not a script's;
- it chooses the channel socket path and prints the bootstrap variable, standing
  in for the launch wrapper the Session Launcher will own (issue #9);
- it waits for the channel to bind that socket, which is the first real proof:
  Claude Code found the plugin, admitted the channel, and started this server;
- with `--relay` it carries one Answer Relay in and prints the receipt.

One thing is outside this repository's reach and must be arranged by hand:
channels are gated by **managed settings** (`channelsEnabled`, and an
`allowedChannelPlugins` allow-list naming marketplace and plugin). That file is
machine-wide and administrator-owned. Until it names this plugin, Claude Code
will not admit the channel — and the honest symptom is exactly what this script
reports: the socket never gets bound.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gpt_voicecoding.adapters.agent.claude import ClaudeAgentAdapter  # noqa: E402
from gpt_voicecoding.adapters.agent.claude.bootstrap import (  # noqa: E402
    CHANNEL_CONFIG_VARIABLE,
    bootstrap_value,
)
from gpt_voicecoding.adapters.agent.claude.plugin import (  # noqa: E402
    MANIFEST_DIRECTORY,
    MARKETPLACE_NAME,
    PLUGIN_NAME,
    channel_selector,
    plugin_version,
    write_plugin,
)
from gpt_voicecoding.adapters.agent.claude.privacy import (  # noqa: E402
    prepare_private_directory,
)
from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings  # noqa: E402
from gpt_voicecoding.seams.identity import (  # noqa: E402
    AgentKind,
    SessionTarget,
    new_request_id,
)

#: The Claude Code this adapter's wire shapes were established against. A run on
#: any other build still proves something — that *that* build accepts them — but
#: it does not establish the pinned version's acceptance, and this script says so
#: rather than letting the two be confused.
PINNED_CLAUDE_VERSION = "2.1.235"

#: What the Relay says. Short, and it asks for a visible answer, so the proof is
#: readable in the operator's own window rather than only in a receipt here.
RELAY_TEXT = (
    "This message came from the GPT-VoiceCoding bridge through your Session Channel. "
    "Please reply with the single word ACKNOWLEDGED and do nothing else."
)


def _instructions(plugin_directory: Path, socket_path: Path, settings: ClaudeSettings) -> str:
    allow_entry = json.dumps({"marketplace": MARKETPLACE_NAME, "plugin": PLUGIN_NAME})
    bootstrap = bootstrap_value(socket_path, settings)
    return f"""
Run these yourself. Nothing below is run for you.

1. Register the plugin's marketplace and install it:

     claude plugin marketplace add {plugin_directory}
     claude plugin install {PLUGIN_NAME}@{MARKETPLACE_NAME}

2. Make sure managed settings admit this channel. In
   /Library/Application Support/ClaudeCode/managed-settings.json (administrator
   owned), "channelsEnabled" must be true and "allowedChannelPlugins" must
   contain:

     {allow_entry}

3. Launch a throwaway session in any terminal, with the bootstrap variable set:

     {CHANNEL_CONFIG_VARIABLE}='{bootstrap}' \\
       claude --channels {channel_selector()}

   The variable is what tells the channel which socket to bind, and it is the
   only thing the channel is told before it can read anything else.
"""


def _claude_says(*arguments: str) -> str:
    """One `claude` invocation, or a refusal naming what could not be run.

    Every check below fails closed on this: a guard that cannot see the machine's
    state must stop, because "I could not tell" is not "there is nothing there".
    """
    try:
        finished = subprocess.run(
            ["claude", *arguments], capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.SubprocessError) as unrunnable:
        raise SystemExit(f"cannot run `claude {' '.join(arguments)}`: {unrunnable}") from None
    if finished.returncode != 0:
        raise SystemExit(
            f"`claude {' '.join(arguments)}` failed: {finished.stderr.strip() or 'no reason given'}"
        )
    return finished.stdout


def _refuse_to_replace_anything(interpreter: str, plugin_directory: Path) -> None:
    """Stop on any state this run would replace. It only ever adds.

    The marketplace this plugin belongs to is deliberately named so it cannot
    collide with the reference implementation's — but "deliberately" is not
    "checked", and the thing a wrong answer here takes down is the operator's
    working bridge. So it is checked, and anything already holding either name
    stops the run with what was found.

    **Every one of these runs before a single byte is written**, including the
    directory check: a guard that fires after the manifests have been rendered
    has already replaced whatever was there, which is the exact thing it exists
    to prevent.
    """
    manifest = plugin_directory / MANIFEST_DIRECTORY / "plugin.json"
    if manifest.exists():
        raise SystemExit(
            f"{manifest} is already there. This script only ever adds; point --plugin-dir "
            "somewhere empty, or remove that one yourself if it is not wanted."
        )
    if MARKETPLACE_NAME in _claude_says("plugin", "marketplace", "list"):
        raise SystemExit(
            f"a marketplace named {MARKETPLACE_NAME} is already registered. This script "
            "only ever adds; remove or rename that one yourself if it is not this plugin's."
        )
    installed = _claude_says("plugin", "list")
    handle = f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"
    if handle in installed:
        raise SystemExit(
            f"{handle} is already installed. Its version carries a fingerprint of the "
            "manifest, so reinstalling over it would replace something; uninstall it "
            "yourself first if you mean to."
        )
    print(f"nothing to replace: no {MARKETPLACE_NAME} marketplace, no {handle} installed")
    print(f"plugin manifest version: {plugin_version(interpreter)}")


def _report_claude_version() -> None:
    """Say which build this run witnesses, and never let it pass for the pinned one."""
    found = _claude_says("--version").strip()
    print(f"claude: {found}")
    # The exact version, not a prefix of it: `2.1.2350` starts with `2.1.235`
    # and is a different build, and the whole point of this line is that two
    # builds are never quietly treated as one.
    if found.split(" ")[0] != PINNED_CLAUDE_VERSION:
        print(
            f"NOTE: this adapter's wire shapes were established against {PINNED_CLAUDE_VERSION}. "
            "A pass here witnesses the build above, and does not establish the pinned one.",
        )


async def _await_socket(path: Path, *, seconds: float) -> bool:
    """Wait, bounded, for the channel to bind. Its absence is the honest symptom."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while loop.time() < deadline:
        if path.exists():
            return True
        await asyncio.sleep(0.5)
    return path.exists()


async def run(arguments: argparse.Namespace) -> int:
    settings = ClaudeSettings()
    home = Path(tempfile.mkdtemp(prefix="vc-claude-proof-", dir="/tmp"))
    socket_path = home / "channel.sock"
    prepare_private_directory(home)
    plugin_directory = (
        Path(arguments.plugin_dir).expanduser() if arguments.plugin_dir else home / "plugin"
    )
    _report_claude_version()
    _refuse_to_replace_anything(sys.executable, plugin_directory)
    write_plugin(plugin_directory, sys.executable)
    print(f"plugin rendered at: {plugin_directory}")
    print(f"channel socket:     {socket_path}")
    print(_instructions(plugin_directory, socket_path, settings))
    print(f"waiting up to {arguments.wait:.0f}s for the channel to bind ...", flush=True)

    bound = await _await_socket(socket_path, seconds=arguments.wait)
    if not bound:
        print(
            "\nthe channel never bound its socket. Claude Code did not start this "
            "server — the usual reason is that managed settings do not name this "
            "plugin in allowedChannelPlugins, or channelsEnabled is not true.",
            file=sys.stderr,
        )
        return 1
    print("channel socket: bound")

    target = SessionTarget(
        agent=AgentKind.CLAUDE,
        session_id=arguments.session_id or str(uuid.uuid4()),
        # The real pid arrives from the launcher's registration (issue #9). Here
        # it only has to be a real process, since it is the map key this proof
        # addresses the Session by.
        pid=arguments.pid or os.getpid(),
    )
    adapter = ClaudeAgentAdapter(settings=settings)
    adapter.register_session(target, socket_path)
    try:
        result = await adapter.verify()
        print(f"verify: {result.outcome} — {result.detail}")

        if not arguments.relay:
            print("\nno Relay was sent. Re-run with --relay to spend a real model turn.")
            return 0

        print("\ncarrying one Answer Relay in ...", flush=True)
        receipt = await adapter.answer_relay(target, RELAY_TEXT, request_id=new_request_id())
        print(f"receipt: {receipt.outcome}{(' — ' + receipt.reason) if receipt.reason else ''}")
        if not receipt.is_delivered:
            print(
                "\nnot proven delivered. Watch the session's own window: an unread "
                "notification and an unacknowledged one look the same from here, "
                "which is exactly why this grades UNKNOWN rather than guessing.",
                file=sys.stderr,
            )
            return 1
        print("\nthe words reached the session, and that session said so itself.")
        return 0
    finally:
        await adapter.aclose()
        if not arguments.keep:
            shutil.rmtree(home, ignore_errors=True)
        else:
            print(f"\nleft in place: {home}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--relay",
        action="store_true",
        help="carry one Answer Relay into the session. Spends real model tokens.",
    )
    parser.add_argument("--wait", type=float, default=300.0, help="seconds to wait for the bind")
    parser.add_argument("--plugin-dir", default=None, help="where to render the plugin")
    parser.add_argument("--session-id", default=None, help="the Session's id, if you know it")
    parser.add_argument("--pid", type=int, default=None, help="the Session's pid, if you know it")
    parser.add_argument("--keep", action="store_true", help="keep the temporary directory")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
