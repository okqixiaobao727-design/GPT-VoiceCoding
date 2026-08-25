"""Claude-lane hook probe for #71 — the approval half, with no wrapper.

The Relay half is proven (see the #71 progress comment). This is the other half:
a Session's *question* can never be answered over the inbox socket — upstream
enforces that a peer message is not the user's approval — so approval rides
hooks or it rides nothing.

The product already owns most of this route. `adapters/agent/claude/approval.py`
holds the listener, `approval_hook.py` the hook process, `hook_plugin.py` the
packaging. What none of them can do without a wrapper is the two things this
probe exists to establish:

1. **How the hook finds the engine when nobody set an environment variable.**
   Today `approval_hook.py` reads the engine's address out of
   `GPT_VOICECODING_CLAUDE_CHANNEL_CONFIG`, which the launch wrapper set. A
   Session the user started by hand has no such variable, so the hook would
   exit before opening a socket. This probe reads an **address file** at a
   location both sides derive the same way, and proves a hand-started Session
   reaches the engine through it.

2. **A `SessionStart` registration hook**, which the product does not have at
   all. Claude Code's own per-Session registry carries pid, sessionId, cwd and
   `messagingSocketPath`, but **not `transcript_path`** — and the transcript is
   one of the two honest sources of DELIVERED on this lane. The hook payload
   carries it. Legacy's registration model is `legacy@1d32845:bridge/hook.py:119-207`;
   what is ported is its shape — forward the product's own fields, never raise,
   never block the Session — without its launch marker or wrapper.

**Safety boundary, structural rather than careful.** A user-scope hook fires for
every Session in its config directory, including the ones Simon is working in.
Every mode here therefore refuses, as its first act, any payload whose `cwd` is
not under SANDBOX: it prints nothing, writes nothing and exits 0, which is the
same thing Claude Code sees when no hook is installed at all. This mirrors the
Relay probe's rule, which exists because #70's probe reached two real Sessions.

    # one terminal: the stand-in engine
    python3 scripts/claude_lane_hooks_probe.py --engine --answer allow

    # then install the hooks for the sandbox only, and start a target there
    python3 scripts/claude_lane_hooks_probe.py --install-project
    cd /tmp/gptvc-71-probe && command claude

    python3 scripts/claude_lane_hooks_probe.py --ledger      # what fired, and when
    python3 scripts/claude_lane_hooks_probe.py --uninstall-project

Version pin: Claude Code 2.1.245 on macOS, 2026-08-25. Throwaway; not the product.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import socket
import socketserver
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

#: The only cwd a Session may have for this probe to do anything at all.
#: Resolved, because macOS reports `/tmp` back as `/private/tmp` and a boundary
#: a symlink can walk around is not a boundary.
SANDBOX = Path("/tmp/gptvc-71-probe").resolve()

#: Everything every mode observed, one JSON object per line. The probe's own
#: record — deliberately not the engine's, because the question "did the hook
#: fire at all" has to be answerable when no engine was running.
LEDGER = SANDBOX / "hooks-probe.jsonl"

#: Where the engine publishes its address and the hook goes looking for it.
#: This is the shape the product needs instead of the wrapper's environment
#: variable: a file at a path both sides derive, holding the socket path of
#: whichever engine is currently up. The probe keeps it inside the sandbox; the
#: product's would sit under its own runtime directory.
ADDRESS_FILE = SANDBOX / "engine-address.json"
ENGINE_SOCKET = SANDBOX / "engine.sock"

#: The wire, copied field for field from `adapters/agent/claude/approval.py`, so
#: that what this probe proves is what the product already speaks.
REQUEST_TYPE = "approval_request"
VERDICT_TYPE = "approval_verdict"
ACK_TYPE = "approval_ack"
TYPE_FIELD = "type"
SESSION_ID_FIELD = "session_id"
CWD_FIELD = "cwd"
TOOL_NAME_FIELD = "tool_name"
TOOL_INPUT_FIELD = "tool_input"
VERDICT_FIELD = "verdict"

HOOK_EVENT = "PermissionRequest"
REGISTRATION_EVENT = "SessionStart"

#: The environment Claude Code is said to export before a hook runs. Whether it
#: actually does is one of the things this probe is here to find out, so the
#: names are recorded as present-or-absent rather than relied upon.
MESSAGING_VARIABLES = (
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_PROJECT_DIR",
)


def now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def note(event: str, **fields: Any) -> None:
    """Append one line to the ledger. Never raises: a hook must not fail a Session."""
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"at": now(), "event": event, **fields}, ensure_ascii=False) + "\n"
            )
    except OSError:
        pass


def inside_sandbox(cwd: Any) -> bool:
    """The boundary. Anything that is not provably inside it is outside it."""
    if not isinstance(cwd, str) or not cwd.strip():
        return False
    try:
        resolved = Path(cwd).resolve()
    except OSError:
        return False
    return resolved == SANDBOX or SANDBOX in resolved.parents


def read_payload() -> dict[str, Any] | None:
    """The hook's stdin, or None for anything that is not a JSON object."""
    try:
        raw = sys.stdin.read()
    except (OSError, UnicodeDecodeError):
        return None
    if not raw.strip():
        return None
    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def environment_snapshot() -> dict[str, Any]:
    """Which of the messaging variables a hook process actually inherits.

    The socket path is recorded in full because it is a path, and the token only
    as a length: a token that reaches the ledger is a token that leaked.
    """
    snapshot: dict[str, Any] = {}
    for name in MESSAGING_VARIABLES:
        value = os.environ.get(name)
        if value is None:
            snapshot[name] = None
        elif name.endswith("TOKEN"):
            snapshot[name] = f"<{len(value)} chars>"
        else:
            snapshot[name] = value
    return snapshot


# -- the SessionStart half ----------------------------------------------


def session_start(argv_note: str | None = None) -> int:
    """Register the Session, or leave without a trace. Never raises, never blocks.

    What legacy's hook client sent (`bridge/hook.py:119-207`) was the product's
    own fields forwarded unchanged — session id, transcript path, cwd, tmux —
    plus its judgement of whether this Session was one of ours. The judgement is
    what changes here: with no wrapper there is no launch marker to look for, and
    the answer to "is this Session ours" is now "every Session is", bounded only
    by the config directory the hook is installed in. Which is exactly why the
    sandbox check below is the first thing that runs.
    """
    payload = read_payload()
    if payload is None:
        return 0
    if not inside_sandbox(payload.get("cwd")):
        return 0
    note(
        REGISTRATION_EVENT,
        note=argv_note,
        session_id=payload.get("session_id"),
        transcript_path=payload.get("transcript_path"),
        cwd=payload.get("cwd"),
        source=payload.get("source"),
        permission_mode=payload.get("permission_mode"),
        pid=os.getpid(),
        ppid=os.getppid(),
        environment=environment_snapshot(),
        payload_keys=sorted(payload),
    )
    return 0


# -- the PermissionRequest half -----------------------------------------


def engine_address() -> Path | None:
    """Where the engine says it is listening, or None if nobody said.

    This is the wrapper-free replacement for the bootstrap environment variable.
    A missing or unreadable file means no engine: the hook prints nothing and the
    dialog the human is looking at keeps the request.
    """
    try:
        document: Any = json.loads(ADDRESS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    path = document.get("socketPath")
    return Path(path) if isinstance(path, str) and path.strip() else None


def ask_engine(request: dict[str, Any], path: Path, dial_timeout: float) -> str | None:
    """One request out, one verdict back. Every failure answers None, meaning silence."""
    try:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError:
        return None
    with connection:
        try:
            connection.settimeout(dial_timeout)
            connection.connect(str(path))
            connection.sendall(
                json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                + b"\n"
            )
            # Past the dial there is no clock here on purpose: the budget belongs
            # to the engine, which answers `ask` when it runs out. A second timer
            # would be a second budget racing the first.
            connection.settimeout(None)
            line = _one_line(connection)
            if line.strip():
                with _suppress_oserror():
                    connection.sendall(
                        json.dumps({TYPE_FIELD: ACK_TYPE}, separators=(",", ":")).encode("utf-8")
                        + b"\n"
                    )
        except (OSError, ValueError):
            return None
    try:
        document: Any = json.loads(line.split(b"\n", 1)[0])
    except (UnicodeDecodeError, json.JSONDecodeError, IndexError):
        return None
    if not isinstance(document, dict) or document.get(TYPE_FIELD) != VERDICT_TYPE:
        return None
    verdict = document.get(VERDICT_FIELD)
    return verdict if verdict in {"allow", "deny"} else None


class _suppress_oserror:  # noqa: N801 - a context manager spelled like a verb
    def __enter__(self) -> None:
        return None

    def __exit__(self, kind: Any, *_rest: Any) -> bool:
        return kind is not None and issubclass(kind, OSError)


def _one_line(connection: socket.socket) -> bytes:
    chunks: list[bytes] = []
    read = 0
    while read < (1 << 20):
        chunk = connection.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        read += len(chunk)
        if b"\n" in chunk:
            break
    return b"".join(chunks)


def permission_request(dial_timeout: float) -> int:
    """The hook Claude Code waits on. Prints a decision, or prints nothing.

    Anything on stdout that is not a decision object is read by Claude Code as a
    *denial*, so this function is the only place that writes there, and every
    path that is not a verdict writes nothing at all.
    """
    started = time.monotonic()
    payload = read_payload()
    if payload is None:
        return 0
    if not inside_sandbox(payload.get("cwd")):
        # The whole cost a foreign Session pays: read stdin, resolve one path,
        # exit 0. No socket, no file, not even a ledger line.
        return 0
    address = engine_address()
    note(
        HOOK_EVENT,
        session_id=payload.get("session_id"),
        cwd=payload.get("cwd"),
        tool_name=payload.get("tool_name"),
        tool_input=payload.get("tool_input"),
        permission_mode=payload.get("permission_mode"),
        permission_suggestions=payload.get("permission_suggestions"),
        payload_keys=sorted(payload),
        environment=environment_snapshot(),
        engine=str(address) if address else None,
    )
    if address is None:
        note("verdict", verdict=None, reason="no engine address", waited=_since(started))
        return 0
    request = {
        TYPE_FIELD: REQUEST_TYPE,
        SESSION_ID_FIELD: payload.get("session_id"),
        CWD_FIELD: payload.get("cwd"),
        TOOL_NAME_FIELD: payload.get("tool_name"),
        TOOL_INPUT_FIELD: payload.get("tool_input"),
    }
    verdict = ask_engine(request, address, dial_timeout)
    note("verdict", verdict=verdict, waited=_since(started))
    if verdict is None:
        return 0
    decision: dict[str, Any] = (
        {"behavior": "allow"}
        if verdict == "allow"
        else {"behavior": "deny", "message": "denied through the GPT-VoiceCoding probe"}
    )
    sys.stdout.write(
        json.dumps(
            {"hookSpecificOutput": {"hookEventName": HOOK_EVENT, "decision": decision}},
            ensure_ascii=False,
        )
    )
    return 0


def _since(started: float) -> float:
    return round(time.monotonic() - started, 3)


# -- the stand-in engine ------------------------------------------------


class _Engine(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True
    answer = "allow"
    delay = 0.0


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline(1 << 20)
        try:
            request: Any = json.loads(line)
        except json.JSONDecodeError:
            return
        server: Any = self.server
        note("engine.request", request=request, answer=server.answer, delay=server.delay)
        print(f"[{now()}] request: {json.dumps(request, ensure_ascii=False)[:400]}", flush=True)
        if server.delay:
            time.sleep(server.delay)
        if server.answer == "silent":
            # The engine holding the connection open and saying nothing is what
            # "the user has not answered yet" looks like on this wire.
            while True:
                time.sleep(3600)
        if server.answer == "hangup":
            return
        self.wfile.write(
            json.dumps(
                {TYPE_FIELD: VERDICT_TYPE, VERDICT_FIELD: server.answer}, separators=(",", ":")
            ).encode("utf-8")
            + b"\n"
        )
        self.wfile.flush()
        acknowledged = self.rfile.readline(4096)
        note("engine.ack", raw=acknowledged.decode("utf-8", "replace").strip())
        print(f"[{now()}] ack: {acknowledged!r}", flush=True)


def run_engine(answer: str, delay: float) -> int:
    """Bind, publish the address, and answer every dialog the same way."""
    SANDBOX.mkdir(parents=True, exist_ok=True)
    if ENGINE_SOCKET.exists():
        ENGINE_SOCKET.unlink()
    _Engine.answer = answer
    _Engine.delay = delay
    server = _Engine(str(ENGINE_SOCKET), _Handler)
    ADDRESS_FILE.write_text(
        json.dumps({"socketPath": str(ENGINE_SOCKET), "pid": os.getpid(), "at": now()}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    note("engine.up", socket=str(ENGINE_SOCKET), answer=answer, delay=delay, pid=os.getpid())
    print(f"[{now()}] engine on {ENGINE_SOCKET}, answering {answer!r} after {delay}s", flush=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print(f"[{now()}] engine down", flush=True)
    finally:
        server.shutdown()
        server.server_close()
        with _suppress_oserror():
            ENGINE_SOCKET.unlink()
        with _suppress_oserror():
            ADDRESS_FILE.unlink()
        note("engine.down", pid=os.getpid())
    return 0


# -- installing the two hooks for the sandbox only ----------------------


def hook_command(mode: str, interpreter: str) -> str:
    """The command line one hook event runs. Quoted, because paths carry spaces."""
    script = Path(__file__).resolve()
    return f"'{interpreter}' '{script}' {mode}"


def project_settings(interpreter: str) -> dict[str, Any]:
    """Both hooks, in the shape a settings file takes.

    No matcher on `PermissionRequest`: narrowing by tool name here would be this
    probe deciding which of the user's dialogs may be answered remotely, and the
    product's own reasoning (`hook_plugin.py`) already settled that it should
    not. The narrowing that matters is the sandbox check, which is by cwd — and
    `if` filters only by tool, so it cannot do this job.
    """
    return {
        "hooks": {
            REGISTRATION_EVENT: [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": hook_command("--session-start", interpreter),
                            "timeout": 5,
                        }
                    ]
                }
            ],
            HOOK_EVENT: [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": hook_command("--permission-request", interpreter),
                            "timeout": 600,
                        }
                    ]
                }
            ],
        }
    }


# -- the user-scope settings-file route, ported from legacy ------------
#
# `legacy@1d32845:bridge/hookconfig.py:68-125` is the part worth porting whole:
# a merge that replaces this project's own handlers and leaves every other hook
# untouched, including the other handlers *inside the same matcher group*.
#
# One thing had to be adapted rather than copied. Legacy's test for "ours" is the
# program the command actually runs — `Path(tokens[0]).name == "bridge-hook"` —
# which works because legacy's hook is its own launcher. Here, and in the
# product's `hook_plugin.py`, the program is an interpreter and the identity is
# in a later argument. So the test moves one token along, and keeps legacy's
# rule: it matches on a *token*, never on a substring of the command line, so a
# neighbouring command that merely mentions the name survives untouched.


def is_our_handler(handler: Any) -> bool:
    """True only for a command handler that runs this probe's own script."""
    if not isinstance(handler, dict) or handler.get("type") != "command":
        return False
    command = handler.get("command")
    if not isinstance(command, str):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:  # unbalanced quoting is somebody else's command
        return False
    return any(Path(token).name == Path(__file__).name for token in tokens)


def without_our_handlers(group: Any) -> Any:
    """The group with our handlers removed, or None when nothing of it remains.

    A matcher group can hold several handlers and the others in it are the
    user's. Dropping the whole group because one handler is ours would delete
    configuration this probe never wrote.
    """
    if not isinstance(group, dict):
        return group
    handlers = group.get("hooks")
    if not isinstance(handlers, list):
        return group
    kept = [handler for handler in handlers if not is_our_handler(handler)]
    if not kept:
        return None
    if len(kept) == len(handlers):
        return group
    return {**group, "hooks": kept}


def merge_hooks(existing: dict, ours: dict) -> dict:
    """Replace this probe's entries, keep every other hook untouched."""
    merged = {event: list(groups) for event, groups in existing.items()}
    for event in set(merged) | set(ours):
        kept = []
        for group in merged.get(event, []):
            remainder = without_our_handlers(group)
            if remainder is not None:
                kept.append(remainder)
        kept.extend(ours.get(event, []))
        if kept:
            merged[event] = kept
        else:
            merged.pop(event, None)
    return merged


def render_settings(path: Path, hooks: dict) -> str:
    """The file's new contents. `indent=2` plus a trailing newline reproduces
    Simon's file byte for byte, which is what makes an uninstall checkable."""
    document: dict = {}
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            document = json.loads(text)
            if not isinstance(document, dict):
                raise ValueError(f"{path} does not contain a JSON object")
    current = document.get("hooks")
    document["hooks"] = merge_hooks(current if isinstance(current, dict) else {}, hooks)
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def user_settings_path(config_directory: str) -> Path:
    return Path(config_directory).expanduser() / "settings.json"


def install_user(config_directory: str, interpreter: str) -> int:
    """Merge both hooks into a user settings file, keeping everything else."""
    path = user_settings_path(config_directory)
    rendered = render_settings(path, project_settings(interpreter)["hooks"])
    path.write_text(rendered, encoding="utf-8")
    note("install.user", path=str(path))
    print(f"merged the probe's hooks into {path}")
    return 0


def uninstall_user(config_directory: str) -> int:
    """Take back exactly this probe's handlers, and nothing else."""
    path = user_settings_path(config_directory)
    rendered = render_settings(path, {})
    path.write_text(rendered, encoding="utf-8")
    note("uninstall.user", path=str(path))
    print(f"removed the probe's hooks from {path}")
    return 0


def install_project(interpreter: str) -> int:
    """Write the hooks into the sandbox's own project settings.

    Project scope is the deliberate first step: it reaches Sessions whose cwd is
    the sandbox and no others, so the behaviour gates can be answered before
    anything is written into a file Simon owns.
    """
    path = SANDBOX / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(project_settings(interpreter), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    note("install.project", path=str(path), interpreter=interpreter)
    print(f"wrote {path}")
    print(path.read_text(encoding="utf-8"))
    return 0


def uninstall_project() -> int:
    path = SANDBOX / ".claude" / "settings.json"
    with _suppress_oserror():
        path.unlink()
    note("uninstall.project", path=str(path))
    print(f"removed {path}")
    return 0


#: The second install mechanism the gate has to choose between: the hooks live
#: in a plugin, and the settings file only names the plugin. Proven here at
#: project scope — `extraKnownMarketplaces` and `enabledPlugins` are accepted
#: from a project's `.claude/settings.json` — so the mechanism can be compared
#: before anything is written into a file Simon owns.
PLUGIN_MARKETPLACE = "gptvc-71-probe-marketplace"
PLUGIN_NAME = "gptvc-71-approval-probe"
PLUGIN_DIRECTORY = SANDBOX / "probe-plugin"


def install_project_plugin(interpreter: str) -> int:
    """Lay down a one-plugin local marketplace and enable it for the sandbox only.

    The layout is the product's own, verified against the Session Channel plugin
    already installed on this machine: one directory is both the marketplace and
    the plugin, with `source: "./"`.
    """
    manifest_directory = PLUGIN_DIRECTORY / ".claude-plugin"
    manifest_directory.mkdir(parents=True, exist_ok=True)
    (manifest_directory / "marketplace.json").write_text(
        json.dumps(
            {
                "name": PLUGIN_MARKETPLACE,
                "owner": {"name": "GPT-VoiceCoding #71 probe"},
                "description": "Throwaway marketplace carrying only the #71 approval probe.",
                "plugins": [
                    {"name": PLUGIN_NAME, "source": "./", "description": "Approval probe."}
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (manifest_directory / "plugin.json").write_text(
        json.dumps(
            {
                "name": PLUGIN_NAME,
                "version": "1.0.0",
                "description": "Routes a permission dialog to the #71 stand-in engine and back.",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    hooks_directory = PLUGIN_DIRECTORY / "hooks"
    hooks_directory.mkdir(parents=True, exist_ok=True)
    (hooks_directory / "hooks.json").write_text(
        json.dumps(project_settings(interpreter), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    settings = SANDBOX / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps(
            {
                "extraKnownMarketplaces": {
                    PLUGIN_MARKETPLACE: {
                        "source": {"source": "directory", "path": str(PLUGIN_DIRECTORY)}
                    }
                },
                "enabledPlugins": {f"{PLUGIN_NAME}@{PLUGIN_MARKETPLACE}": True},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    note("install.project_plugin", plugin=str(PLUGIN_DIRECTORY), settings=str(settings))
    print(f"wrote {PLUGIN_DIRECTORY} and {settings}")
    return 0


def show_ledger(limit: int) -> int:
    try:
        lines = LEDGER.read_text(encoding="utf-8").splitlines()
    except OSError:
        print(f"no ledger at {LEDGER}")
        return 1
    for line in lines[-limit:]:
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--session-start", action="store_true", help="run as the SessionStart hook")
    mode.add_argument(
        "--permission-request", action="store_true", help="run as the PermissionRequest hook"
    )
    mode.add_argument("--engine", action="store_true", help="run the stand-in engine")
    mode.add_argument(
        "--install-project", action="store_true", help="install both hooks for the sandbox only"
    )
    mode.add_argument(
        "--install-project-plugin", action="store_true", help="the same hooks, carried by a plugin"
    )
    mode.add_argument(
        "--uninstall-project", action="store_true", help="take the sandbox hooks back"
    )
    mode.add_argument(
        "--install-user", action="store_true", help="merge both hooks into a user settings file"
    )
    mode.add_argument(
        "--uninstall-user", action="store_true", help="take this probe's handlers back"
    )
    mode.add_argument("--ledger", action="store_true", help="print what the hooks recorded")
    parser.add_argument("--answer", default="allow", choices=("allow", "deny", "silent", "hangup"))
    parser.add_argument(
        "--delay", type=float, default=0.0, help="seconds the engine waits before answering"
    )
    parser.add_argument("--dial-timeout", type=float, default=2.0)
    parser.add_argument("--interpreter", default=sys.executable)
    parser.add_argument(
        "--config-dir", default="~/.claude-b", help="which config directory to install into"
    )
    parser.add_argument("--limit", type=int, default=40)
    arguments = parser.parse_args(argv)

    if arguments.session_start:
        return session_start()
    if arguments.permission_request:
        return permission_request(arguments.dial_timeout)
    if arguments.engine:
        return run_engine(arguments.answer, arguments.delay)
    if arguments.install_project:
        return install_project(arguments.interpreter)
    if arguments.install_project_plugin:
        return install_project_plugin(arguments.interpreter)
    if arguments.install_user:
        return install_user(arguments.config_dir, arguments.interpreter)
    if arguments.uninstall_user:
        return uninstall_user(arguments.config_dir)
    if arguments.uninstall_project:
        return uninstall_project()
    return show_ledger(arguments.limit)


if __name__ == "__main__":
    raise SystemExit(main())
