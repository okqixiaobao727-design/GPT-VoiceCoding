"""One command line, parsed once — and one way of saying what came back.

`bridgectl status` and `/status` typed into the Companion Channel are the same
action, so they are the same parser and the same renderer. Two implementations
of one command set is how a CLI ends up carrying a command the channel does not
have, and how a legacy alias survives a rewrite.

Two shapes are worth naming:

- **An address, not a label.** `codex:abc` and `claude:def:1234` are how a
  surface writes a `SessionTarget` on one line. A Session Label is for speech and
  for matching, and turning one into a target is the router's job on the way in
  — never this parser's, which would be addressing by label through the back
  door.
- **A refusal is rendered verbatim.** Bridge Core's words come back unchanged;
  this file never rephrases one. Honest wording lives in one place, and a
  surface that improved on it would be a second voice deciding what the user is
  told.
"""

from __future__ import annotations

from collections.abc import Sequence

from gpt_voicecoding.seams.control_plane import Action, Reply, Request
from gpt_voicecoding.seams.identity import SessionLabel

#: How each action is written on one line. Also what a refusal quotes back.
USAGE: dict[Action, str] = {
    Action.STATUS: "status",
    Action.SWITCH: "switch <name> on|off",
    Action.SESSIONS: "sessions",
    Action.LIVE: "live",
    Action.LAUNCH: "launch <agent> <workspace> <project · task>",
    Action.CLOSE: "close <agent>:<session id>[:<pid>]",
    Action.RELAY: "relay <agent>:<session id>[:<pid>] [--supplement] <words>",
    Action.APPROVE: "approve <approval id> allow|deny|ask",
    Action.VERIFY: "verify",
}

#: The word that asks for the mid-turn route. Spelled out, because route follows
#: the user's explicit intent and is never inferred from how busy a Session is.
SUPPLEMENT_FLAG = "--supplement"

ADDRESS_SEPARATOR = ":"


class CommandError(Exception):
    """The line cannot be read as a command. Carries what to say instead."""


def build_request(command: str, arguments: Sequence[str]) -> Request:
    """Turn one command word and its arguments into one request."""
    try:
        action = Action(command.strip().casefold())
    except ValueError:
        known = ", ".join(sorted(str(name) for name in Action))
        raise CommandError(f"no command called {command!r}. There is: {known}") from None

    return Request(action=action, payload=_payload(action, list(arguments)))


def _payload(action: Action, arguments: list[str]) -> dict[str, object]:
    match action:
        case Action.STATUS | Action.SESSIONS | Action.LIVE | Action.VERIFY:
            return {}
        case Action.SWITCH:
            name, state = _exactly(action, arguments, 2)
            return {"name": name, "on": _state(state)}
        case Action.LAUNCH:
            if len(arguments) < 3:
                raise CommandError(f"say it as: {USAGE[action]}")
            agent, workspace, *rest = arguments
            return {"agent": agent, "workspace": workspace, "label": _label(" ".join(rest))}
        case Action.CLOSE:
            (address,) = _exactly(action, arguments, 1)
            return {"target": parse_address(address)}
        case Action.RELAY:
            return _relay(arguments)
        case Action.APPROVE:
            approval_id, verdict = _exactly(action, arguments, 2)
            return {"approval_id": approval_id, "verdict": verdict}
    raise CommandError(f"no command called {action!r}")  # unreachable: the set is closed


def _relay(arguments: list[str]) -> dict[str, object]:
    route = "deliver"
    remaining = list(arguments)
    if SUPPLEMENT_FLAG in remaining:
        remaining.remove(SUPPLEMENT_FLAG)
        route = "supplement"
    if len(remaining) < 2:
        raise CommandError(f"say it as: {USAGE[Action.RELAY]}")
    address, *words = remaining
    return {"target": parse_address(address), "text": " ".join(words), "route": route}


def _exactly(action: Action, arguments: list[str], count: int) -> list[str]:
    if len(arguments) != count:
        raise CommandError(f"say it as: {USAGE[action]}")
    return arguments


def _state(word: str) -> bool:
    """A switch has exactly two states, so exactly two words name them."""
    if word.casefold() in ("on", "off"):
        return word.casefold() == "on"
    raise CommandError(f"a switch is on or off; {word!r} is neither")


def _label(text: str) -> dict[str, str]:
    try:
        label = SessionLabel.parse(text)
    except ValueError as refusal:
        raise CommandError(str(refusal)) from None
    return {"project": label.project, "task": label.task}


def parse_address(address: str) -> dict[str, object]:
    """`agent:session_id[:pid]` — the exact identity, written on one line."""
    parts = address.split(ADDRESS_SEPARATOR)
    if len(parts) not in (2, 3) or not all(part.strip() for part in parts):
        raise CommandError(
            f"name the Session as <agent>{ADDRESS_SEPARATOR}<session id>"
            f"[{ADDRESS_SEPARATOR}<pid>]; {address!r} is not that"
        )
    agent, session_id, *rest = parts
    if not rest:
        return {"agent": agent, "session_id": session_id, "pid": None}
    if not rest[0].isdigit():
        raise CommandError(f"not a process id: {rest[0]!r}")
    return {"agent": agent, "session_id": session_id, "pid": int(rest[0])}


def format_address(target: dict[str, object]) -> str:
    pid = target.get("pid")
    tail = f"{ADDRESS_SEPARATOR}{pid}" if pid else ""
    return f"{target['agent']}{ADDRESS_SEPARATOR}{target['session_id']}{tail}"


def render(reply: Reply) -> str:
    """Say what came back, in as few lines as tell the whole truth."""
    if not reply.ok:
        assert reply.error is not None
        return reply.error.message  # the refusal's own words, unchanged

    data = dict(reply.data)
    match reply.action:
        case Action.STATUS:
            return "\n".join(_status_lines(data))
        case Action.SESSIONS:
            return "\n".join(_roster_lines(data["sessions"]))
        case Action.SWITCH:
            was = "on" if data["previous"] else "off"
            return f"{data['name']} is {'on' if data['on'] else 'off'} (was {was})"
        case Action.LIVE:
            return (
                f"the Live Call is up ({data['call_id']})"
                if data["state"] == "up"
                else f"no Live Call is up ({data['state']})"
            )
        case Action.LAUNCH:
            if data["status"] != "launched":
                return f"launch {data['status']}: {data['detail']}"
            return f"launched {format_address(data['target'])}"
        case Action.CLOSE:
            detail = f": {data['detail']}" if data["detail"] else ""
            return f"{str(data['status']).replace('_', ' ')}{detail}"
        case Action.RELAY:
            return _relay_line(data)
        case Action.APPROVE:
            return data["closing_notice"] or f"{data['verdict']} carried ({data['state']})"
        case Action.VERIFY:
            return "\n".join(_verify_lines(data["seams"]))
    return ""


def _status_lines(data: dict[str, object]) -> list[str]:
    switches = data["switches"]
    assert isinstance(switches, dict)
    lines = [
        "switches: " + ", ".join(f"{name} {'on' if on else 'off'}" for name, on in switches.items())
    ]
    call_id = data["call_id"]
    lines.append(f"call: {call_id}" if call_id else "call: none")
    lines.extend(_roster_lines(data["sessions"]))
    pending_relays = data["pending_relays"]
    pending_approvals = data["pending_approvals"]
    assert isinstance(pending_relays, list) and isinstance(pending_approvals, list)
    lines.append(f"waiting: {len(pending_relays)} relays, {len(pending_approvals)} approvals")
    return lines


def _roster_lines(sessions: object) -> list[str]:
    assert isinstance(sessions, list)
    if not sessions:
        return ["sessions: none"]
    return ["sessions:"] + [
        f"  {session['label']} — {format_address(session['target'])} — {session['workspace']} "
        f"({session['state']}, window {session['reply_window']})"
        for session in sessions
    ]


def _relay_line(data: dict[str, object]) -> str:
    """Queued is not delivered, and the line says which it was."""
    line = f"{data['state']} via {data['route']} ({data['outcome']})"
    for extra in (data["confirmation"], data["report"]):
        if extra:
            line += f" — {extra}"
    return line


def _verify_lines(seams: object) -> list[str]:
    assert isinstance(seams, list)
    return [
        f"{row['seam']}: {row['outcome']}" + (f" — {row['detail']}" if row["detail"] else "")
        for row in seams
    ]
