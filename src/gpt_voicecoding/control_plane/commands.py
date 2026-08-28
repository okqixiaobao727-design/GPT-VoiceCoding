"""One command line, parsed once — and one way of saying what came back.

`bridgectl status` and `/status` typed into the Companion Channel are the same
action, so they are the same parser and the same renderer. Two implementations
of one command set is how a CLI ends up carrying a command the channel does not
have, and how a legacy alias survives a rewrite.

Two shapes are worth naming:

- **An address, not a name.** `codex:abc` and `claude:def:1234` are how a
  surface writes a `SessionTarget` on one line. A Session Name is for speech and
  for matching, and turning one into a target is the router's job on the way in
  — never this parser's, which would be addressing by name through the back
  door.
- **A refusal is rendered verbatim.** Bridge Core's words come back unchanged;
  this file never rephrases one. Honest wording lives in one place, and a
  surface that improved on it would be a second voice deciding what the user is
  told.
"""

from __future__ import annotations

from collections.abc import Sequence

from gpt_voicecoding.seams.control_plane import Action, Reply, Request
from gpt_voicecoding.seams.identity import ADDRESS_SEPARATOR, address_of

#: How each action is written on one line. Also what a refusal quotes back.
USAGE: dict[Action, str] = {
    Action.STATUS: "status",
    Action.SWITCH: "switch <name> on|off",
    Action.SESSIONS: "sessions",
    Action.PROGRESS: "progress <agent>:<session id>[:<pid>]",
    Action.LIVE: "live",
    Action.RELAY: "relay <agent>:<session id>[:<pid>] [--supplement] <words>",
    Action.APPROVE: "approve <approval id> allow|deny|ask",
    Action.VERIFY: "verify",
}

#: The word that asks for the mid-turn route. Spelled out, because route follows
#: the user's explicit intent and is never inferred from how busy a Session is.
SUPPLEMENT_FLAG = "--supplement"


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
        case Action.RELAY:
            return _relay(arguments)
        case Action.APPROVE:
            approval_id, verdict = _exactly(action, arguments, 2)
            return {"approval_id": approval_id, "verdict": verdict}
        case Action.PROGRESS:
            (address,) = _exactly(action, arguments, 1)
            return {"target": parse_address(address)}
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


def parse_address(address: str) -> dict[str, object]:
    """`agent:session_id[:pid]` — the exact identity, written on one line.

    **The session id half may be empty, and only when a pid follows it.** A
    `codex` Session writes the rollout that names it at its first *turn* (#73,
    measured), so a Session that exists and has not been spoken to is addressed
    as `codex::6548` — the process is the only thing either side can agree on
    yet. An empty id with no pid names nothing and is still refused.
    """
    parts = address.split(ADDRESS_SEPARATOR)
    agent, session_id, *rest = parts if len(parts) in (2, 3) else ("", "", "")
    if not agent.strip() or not (session_id.strip() or rest):
        raise CommandError(
            f"name the Session as <agent>{ADDRESS_SEPARATOR}<session id>"
            f"[{ADDRESS_SEPARATOR}<pid>]; {address!r} is not that"
        )
    named = session_id.strip() or None
    if not rest:
        return {"agent": agent, "session_id": named, "pid": None}
    if not rest[0].isdigit():
        raise CommandError(f"not a process id: {rest[0]!r}")
    return {"agent": agent, "session_id": named, "pid": int(rest[0])}


def format_address(target: dict[str, object]) -> str:
    """The address a surface reads off a roster row and hands straight back.

    Written by the seam that owns the format (`identity.address_of`), because
    this module and `SessionTarget.__str__` render the same one thing and two
    implementations of a format are a format only until one of them changes.
    This one takes a **document** rather than a target: a surface reads rows off
    the wire and never holds the type.
    """
    pid = target.get("pid")
    return address_of(
        target["agent"],
        str(target["session_id"]) if target.get("session_id") else None,
        int(pid) if isinstance(pid, int) and not isinstance(pid, bool) else None,
    )


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
        case Action.PROGRESS:
            return "\n".join(_progress_lines(data["session"]))
        case Action.SWITCH:
            was = "on" if data["previous"] else "off"
            return f"{data['name']} is {'on' if data['on'] else 'off'} (was {was})"
        case Action.LIVE:
            return (
                f"the Live Call is up ({data['call_id']})"
                if data["state"] == "up"
                else f"no Live Call is up ({data['state']})"
            )
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
    lines.extend(_lane_lines(data.get("lanes")))
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
        f"  {session['name'] or '(unnamed)'} — "
        f"{format_address(session['target'])} — {session['workspace']} "
        f"({session['state']}, window {session['reply_window']})"
        for session in sessions
    ]


def _progress_lines(session: object) -> list[str]:
    """One Session's own words, newest last, and when they were read.

    `read_at` is said out loud rather than left implicit: a progress line's whole
    meaning is when it was true, and a surface that printed it bare would let a
    reading taken before a five-minute silence read as one taken just now.
    """
    assert isinstance(session, dict)
    #: The same one-line summary the roster prints for this Session, without the
    #: `sessions:` heading a list of them carries — so the two surfaces cannot
    #: describe one Session two ways.
    lines = _roster_lines([session])[1:]
    progress = session["progress"]
    if progress is None:
        return [*lines, "  progress: not read"]
    lines.append(f"  last activity: {session['last_activity'] or 'not read'}")
    if progress["truncated"]:
        lines.append("  (older entries dropped)")
    lines.extend(f"  {entry['role']}: {entry['text']}" for entry in progress["recent"])
    if not progress["recent"]:
        lines.append("  nothing said yet")
    lines.append(f"  read at {progress['read_at']}")
    return lines


def _lane_lines(lanes: object) -> list[str]:
    """Said only when a lane is down, because silence is what "fine" looks like."""
    if not isinstance(lanes, dict) or not lanes:
        return []
    return [f"  {agent} lane unavailable — {reason}" for agent, reason in sorted(lanes.items())]


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
