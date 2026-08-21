"""Proof that a real Claude Code session takes an Approval Relay through the hook.

Everything this route knows was read out of the shipped binary: that a
`PermissionRequest` hook fires when a dialog is displayed, that only
`behavior: "allow"` means allow and everything else means deny, that emitting no
decision hands the dialog back, and that `--plugin-dir` loads a plugin's
`hooks/hooks.json` for one session and no other. CI runs no Claude Code, so the
tests can prove the socket and the verdict and cannot prove any of that. This
script is that gate, and it is a **manual, local job deliberately outside the
test suite**, like the Answer Relay's and the Notice Relay's.

    python3 scripts/claude_approval_proof.py            # render, park, and wait
    python3 scripts/claude_approval_proof.py --deny     # answer the dialog "no"

**You launch the session yourself, in your own terminal.** An interactive TUI has
startup prompts, and a script that answers them blind can install software nobody
asked for. The command you need is printed for you and none of it is run.

**Use a throwaway session, and ask it to do something you do not mind happening.**
An `allow` here is a real allow: the session runs the command. The suggestion
below touches one file in `/tmp`, and it is a write rather than a print because
that is the gotcha this whole route carries: the hook never fires for a call that
was let through without a dialog. `echo` is let through — by Claude Code's own
reading of what is harmless, not by any rule you could look up — and both the
research that established this route and this script's own first live run lost a
probe to exactly that.

What a pass looks like, in the operator's own window: the permission dialog
appears, this script prints the dialog it was told about, the script answers, and
the dialog resolves by itself with `Allowed by PermissionRequest hook`. The human
can pre-empt it at any moment by answering the dialog — that race is the design,
not a defect, and pre-empting is reported here as the hook leaving without a
verdict rather than as a failure.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gpt_voicecoding.adapters.agent.claude.approval import (  # noqa: E402
    PROVEN_AGAINST_VERSION,
    ApprovalListener,
)
from gpt_voicecoding.adapters.agent.claude.bootstrap import (  # noqa: E402
    CHANNEL_CONFIG_VARIABLE,
    bootstrap_value,
)
from gpt_voicecoding.adapters.agent.claude.hook_plugin import (  # noqa: E402
    HOOK_PLUGIN_NAME,
    MANIFEST_DIRECTORY,
    hook_plugin_version,
    remove_hook_plugin,
    write_hook_plugin,
)
from gpt_voicecoding.adapters.agent.claude.privacy import (  # noqa: E402
    prepare_private_directory,
)
from gpt_voicecoding.adapters.agent.claude.settings import (  # noqa: E402
    DEFAULT_SOCKET_DIRECTORY,
    ClaudeSettings,
)
from gpt_voicecoding.seams.agent import (  # noqa: E402
    ApprovalRequest,
    ApprovalVerdict,
    AwaitingApproval,
)
from gpt_voicecoding.seams.delivery import Delivery  # noqa: E402
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget, new_request_id  # noqa: E402

#: A command chosen to be harmless and, more importantly, *not* pre-approved.
#: The hook only ever sees what would have stalled, so a proof that asks for
#: something already allowed proves nothing and looks exactly like a broken hook.
#:
#: **It is a write, and that is the whole point.** `echo` was the obvious choice
#: and it is the wrong one twice over: the research that established this route
#: recorded its first probe failing on exactly that, and this script's first live
#: run repeated the mistake — `echo` was let through with no dialog and no hook,
#: by Claude Code's own classification of harmless commands rather than by any
#: rule a user could look up. A command that touches the filesystem is what
#: reliably stops for a human.
SUGGESTED_PROMPT = (
    "Run this exact shell command and show me its output: "
    "touch /tmp/gpt-voicecoding-approval-proof && ls -l /tmp/gpt-voicecoding-approval-proof"
)


def _claude_says(*arguments: str) -> str:
    """One `claude` invocation, or a refusal naming what could not be run."""
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


def _report_claude_version() -> None:
    """Say which build this run witnesses, and never let it pass for the pinned one."""
    found = _claude_says("--version").strip()
    print(f"claude: {found}")
    if found.split(" ")[0] != PROVEN_AGAINST_VERSION:
        print(
            f"NOTE: this route's wire shapes — and the --plugin-dir loading of a plugin's "
            f"hooks/hooks.json — were established against {PROVEN_AGAINST_VERSION}. A pass "
            "here witnesses the build above, and does not establish the pinned one."
        )


def _refuse_to_replace_anything(plugin_directory: Path) -> None:
    """Stop on any state this run would replace. It only ever adds.

    Checked before a byte is written, for the reason the channel proof states: a
    guard that fires after the manifests are rendered has already replaced
    whatever was there.
    """
    manifest = plugin_directory / MANIFEST_DIRECTORY / "plugin.json"
    if manifest.exists():
        raise SystemExit(
            f"{manifest} is already there. This script only ever adds; point --plugin-dir "
            "somewhere empty, or remove that one yourself if it is not wanted."
        )


def _instructions(plugin_directory: Path, socket_path: Path, settings: ClaudeSettings) -> str:
    bootstrap = bootstrap_value(
        socket_path.parent / "channel.sock", settings, approval_socket_path=socket_path
    )
    return f"""
Run this yourself. Nothing below is run for you.

  {CHANNEL_CONFIG_VARIABLE}='{bootstrap}' \\
    claude --plugin-dir {plugin_directory}

Two things travel with that launch and the Approval Relay needs both:

  --plugin-dir              installs the hook for THIS session and no other. No
                            marketplace, no `claude plugin install`, nothing
                            machine-wide, and no managed-settings entry.
  {CHANNEL_CONFIG_VARIABLE}
                            tells the hook where this engine is listening. A
                            session launched without it has a hook that exits
                            silently, which is the same as having none.

Then, in that session, ask for something that is NOT already allowed — otherwise
no dialog is shown, no hook fires, and there is nothing here to prove:

  {SUGGESTED_PROMPT}
"""


class Sink:
    """The event sink, so a parked dialog can be reported the moment it is parked."""

    def __init__(self) -> None:
        self.dialogs: list[ApprovalRequest] = []

    def emit(self, event: object) -> None:
        if isinstance(event, AwaitingApproval):
            self.dialogs.append(event.request)


async def _await_dialog(sink: Sink, *, seconds: float) -> ApprovalRequest | None:
    """Wait, bounded, for a hook to park a dialog here."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while loop.time() < deadline:
        if sink.dialogs:
            return sink.dialogs[0]
        await asyncio.sleep(0.5)
    return None


async def run(arguments: argparse.Namespace) -> int:
    settings = ClaudeSettings(socket_directory=Path(arguments.socket_root))
    sink = Sink()

    home = Path(tempfile.mkdtemp(prefix="vc-approval-proof-", dir=DEFAULT_SOCKET_DIRECTORY))
    prepare_private_directory(home)
    plugin_directory = (
        Path(arguments.plugin_dir).expanduser() if arguments.plugin_dir else home / "hook-plugin"
    )
    _report_claude_version()
    _refuse_to_replace_anything(plugin_directory)
    write_hook_plugin(plugin_directory, sys.executable)
    print(f"hook plugin rendered at: {plugin_directory}")
    print(f"hook plugin version:     {hook_plugin_version(sys.executable)} ({HOOK_PLUGIN_NAME})")

    # The session id is not known until the session exists, so the roster this
    # engine answers for is opened here rather than pinned: the proof's job is
    # the wire, and standing in for the launcher's registration is what the
    # channel proof already does with the same honesty note.
    target = SessionTarget(
        agent=AgentKind.CLAUDE, session_id=str(uuid.uuid4()), pid=arguments.pid or os.getpid()
    )
    seen: dict[str, SessionTarget] = {}

    def resolve(session_id: str) -> SessionTarget:
        """Answer for whichever session dials in — this run has exactly one.

        A real adapter refuses a session id it holds no registration for, and
        that gate is covered by the tests. Here the launcher does not exist yet
        (issue #9), so there is nothing to have registered, and refusing every
        dial would refuse the very session this script asked you to start.
        """
        if session_id not in seen:
            seen[session_id] = SessionTarget(
                agent=AgentKind.CLAUDE, session_id=session_id, pid=target.pid
            )
            print(f"session dialled in: {session_id}")
        return seen[session_id]

    listener = ApprovalListener(settings=settings, resolve=resolve, emit=sink.emit)
    await listener.start()
    print(f"approval socket:         {listener.path}")
    print(_instructions(plugin_directory, listener.path, settings))
    print(f"waiting up to {arguments.wait:.0f}s for a permission dialog ...", flush=True)

    try:
        request = await _await_dialog(sink, seconds=arguments.wait)
        if request is None:
            print(
                "\nno dialog was parked here. The usual reasons, in order of likelihood:\n"
                "  - the tool you asked for was already allowed by a rule, so no dialog was\n"
                "    shown and no hook fired (this route only ever sees what would stall);\n"
                "  - the session was launched without --plugin-dir, so it has no hook;\n"
                f"  - the session was launched without {CHANNEL_CONFIG_VARIABLE}, so the\n"
                "    hook ran and exited silently because it had no engine to ask.",
                file=sys.stderr,
            )
            return 1

        print(f"\ndialog parked: {request.tool_name} — {request.detail or '(no detail)'}")
        verdict = ApprovalVerdict.DENY if arguments.deny else ApprovalVerdict.ALLOW
        print(f"answering {verdict} ...", flush=True)
        receipt = await listener.answer(request.approval_id, verdict, request_id=new_request_id())
        print(f"receipt: {receipt.outcome}{(' — ' + receipt.reason) if receipt.reason else ''}")

        if receipt.outcome is Delivery.DELIVERED:
            print(
                "\nLook at your session. The dialog should have resolved by itself, and the\n"
                f"transcript should say it was {'denied' if arguments.deny else 'allowed'} "
                "by the PermissionRequest hook."
            )
            return 0
        print(
            "\nthe verdict did not reach a waiting hook. If you answered the dialog\n"
            "yourself in the meantime, that is the race working as designed — the human\n"
            "always wins it — and not a failure of this route.",
            file=sys.stderr,
        )
        return 1
    finally:
        await listener.aclose()
        if not arguments.keep:
            remove_hook_plugin(plugin_directory)
            shutil.rmtree(home, ignore_errors=True)
            print("hook plugin removed")
        else:
            print(f"kept: {plugin_directory}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--deny", action="store_true", help="answer the dialog no instead of yes")
    parser.add_argument(
        "--wait", type=float, default=300.0, help="seconds to wait for a dialog to appear"
    )
    parser.add_argument("--plugin-dir", default=None, help="render the hook plugin here")
    parser.add_argument(
        "--socket-root",
        default=str(DEFAULT_SOCKET_DIRECTORY),
        help="where this engine's approval socket lives",
    )
    parser.add_argument("--pid", type=int, default=None, help="the session's pid, if known")
    parser.add_argument(
        "--keep", action="store_true", help="leave the rendered plugin directory behind"
    )
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
