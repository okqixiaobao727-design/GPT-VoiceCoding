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

from gpt_voicecoding.core.relays import NO_GRADE, receipt_line
from gpt_voicecoding.seams.control_plane import USAGE, Action, Reply, Request
from gpt_voicecoding.seams.identity import ADDRESS_SEPARATOR, address_of

#: The word that asks for the page before one already given. Spelled out, and
#: taking the ordinal beside it, because a bare number after an address would
#: read as a count of entries — which is configuration, not a caller's to choose.
BEFORE_FLAG = "--before"

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
        case Action.STATUS | Action.LIVE | Action.VERIFY:
            return {}
        case Action.BRIEF:
            return _brief(arguments)
        case Action.SWITCH:
            name, state = _exactly(action, arguments, 2)
            return {"name": name, "on": _state(state)}
        case Action.RELAY:
            return _relay(arguments)
        case Action.APPROVE:
            approval_id, verdict = _exactly(action, arguments, 2)
            return {"approval_id": approval_id, "verdict": verdict}
        case Action.HISTORY:
            return _history(arguments)
    raise CommandError(f"no command called {action!r}")  # unreachable: the set is closed


def _brief(arguments: list[str]) -> dict[str, object]:
    """`brief` is the Roster Brief; `brief <address>` is one Session Brief.

    The address is optional and never inferred — not from a Focus Session, not
    from the last one asked about. Two questions, one verb, and which one is
    being asked is written on the line.
    """
    if not arguments:
        return {}
    (address,) = _exactly(Action.BRIEF, arguments, 1)
    return {"target": parse_address(address)}


def _history(arguments: list[str]) -> dict[str, object]:
    """`history <address>` is the newest page; `--before <ordinal>` is the one before it.

    The page size is never on the line: it is `[policy] history_page_entries`,
    so two surfaces cannot ask one engine for two different pages of one Session.
    """
    remaining = list(arguments)
    before: object = None
    if BEFORE_FLAG in remaining:
        flag = remaining.index(BEFORE_FLAG)
        if flag + 1 >= len(remaining):
            raise CommandError(f"say it as: {USAGE[Action.HISTORY]}")
        before = _ordinal(remaining[flag + 1])
        del remaining[flag : flag + 2]
    (address,) = _exactly(Action.HISTORY, remaining, 1)
    return {"target": parse_address(address), "before": before}


def _ordinal(word: str) -> int:
    """One entry's place in a Session's record, as a surface wrote it back.

    **Read by asking `int`, not by deciding it looks like a number.** `str.isdigit`
    is true of every decimal digit Unicode has — `"\u00b2"` among them — and it is
    also true of 4,301 ASCII digits, which `int` refuses under CPython's own
    conversion limit. Both reached `int` behind a spelling test and came back as a
    traceback where the surface had asked for a refusal it could print. The
    conversion is the test, so there is nothing left for a second one to miss.
    """
    try:
        ordinal = int(word)
    except ValueError:
        raise CommandError(f"not an entry's ordinal: {word!r}") from None
    if ordinal < 0:
        raise CommandError("an ordinal counts from the oldest entry, at 0")
    return ordinal


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
    return {"agent": agent, "session_id": named, "pid": _pid(rest[0])}


def _pid(word: str) -> int:
    """The process half of an address, read by asking `int` rather than by spelling.

    The same shape `_ordinal` is drawn around, for the same reason: `str.isdigit`
    is true of every decimal digit Unicode has — `"\u00b2"` among them — and of
    4,301 ASCII digits, which `int` refuses under CPython's own conversion limit.
    Both reached `int` behind a spelling test and came back as a traceback where
    the surface had asked for a refusal it could print (#211).

    A pid at or below zero is refused here in the words `SessionTarget` refuses
    it in, so an address is turned away by the parser that read it rather than
    one seam later.
    """
    try:
        pid = int(word)
    except ValueError:
        raise CommandError(f"not a process id: {word!r}") from None
    if pid <= 0:
        raise CommandError(f"not a process id: {word!r}")
    return pid


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
        case Action.BRIEF:
            # The engine's own rendering, printed unchanged. `briefing.text` is
            # the only renderer of Session state there is (#166 B6), and this
            # surface reading the structured fields back into a line of its own
            # is exactly the second voice that rule exists to prevent.
            return str(data["text"])
        case Action.HISTORY:
            return "\n".join(_history_lines(data))
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
            # The verdict, then the same three codes a Relay prints. An Approval
            # Relay is a Relay, and it closes no loop with a sentence: what the
            # user hears is the Voice's to compose from these facts (#175, #192).
            return f"verdict={data['verdict']} " + _relay_line(data)
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
    lines.extend(_status_roster_lines(data["sessions"]))
    lines.extend(_lane_lines(data.get("lanes")))
    lines.extend(_degraded_lane_lines(data.get("degraded_lanes")))
    pending_relays = data["pending_relays"]
    assert isinstance(pending_relays, list)
    lines.append(f"waiting: {len(pending_relays)} relays")
    return lines


def _status_roster_lines(sessions: object) -> list[str]:
    """The roster inside `status`, which is a different question from `brief`.

    `status` answers "what is this engine holding" — every row, ended ones
    included, with the workspace and the Reply Window an operator debugging a
    lane needs. `brief` answers "what should the user be told", and Briefing
    owns every word of that. Two questions, so two renderings; what retired with
    `sessions` was a *third*, which said the second in the first's words.

    """
    assert isinstance(sessions, list)
    if not sessions:
        return ["sessions: none"]
    return ["sessions:"] + [
        f"  {session['name'] or '(unnamed)'} — "
        f"{format_address(session['target'])} — {session['workspace']} "
        f"({session['state']}, window {session['reply_window']})"
        for session in sessions
    ]


def _history_lines(data: dict[str, object]) -> list[str]:
    """One page of what a Session said, newest first, and when it was read.

    **Every entry is printed, including one whose text could not be carried.**
    An omitted entry keeps its ordinal and its role and says so, because the
    page's promise is that it advances: a reader who saw a slot vanish would
    take the entry before it as the one that followed.

    `read_at` is said out loud rather than left implicit — a page's whole
    meaning is when it was true — and the oldest ordinal on the page is what the
    next request passes to `--before`, so the line naming it is the cursor.
    """
    entries = data["entries"]
    assert isinstance(entries, list)
    if not entries:
        # Said without reference to a cursor, because the same empty page answers
        # a first read of a Session that has said nothing and a read past the
        # oldest entry. Every page is complete on its own (#171), and a line that
        # leaned on "that" would only be true for one of the two.
        lines = ["no entries on this page"]
    else:
        lines = [
            f"  {entry['ordinal']} {entry['role']}: "
            + (
                "(too large to carry)"
                if entry.get("omission") == "oversize"
                else str(entry["text"])
            )
            for entry in entries
        ]
        oldest = min(int(entry["ordinal"]) for entry in entries)
        lines.append(
            f"  older entries remain — ask again with --before {oldest}"
            if data["older"]
            else "  that is the whole history"
        )
    lines.append(f"  read at {data['read_at']}")
    return lines


def _lane_lines(lanes: object) -> list[str]:
    """Said only when a lane is down, because silence is what "fine" looks like."""
    if not isinstance(lanes, dict) or not lanes:
        return []
    return [f"  {agent} lane unavailable — {reason}" for agent, reason in sorted(lanes.items())]


def _degraded_lane_lines(lanes: object) -> list[str]:
    """Say when rows are retained but one authoritative source was unreadable."""
    if not isinstance(lanes, dict) or not lanes:
        return []
    return [f"  {agent} lane degraded — {reason}" for agent, reason in sorted(lanes.items())]


def _relay_line(data: dict[str, object]) -> str:
    """The receipt's three codes, in the one format every surface prints.

    No sentence: a relay's receipt is a grade and a reason, and the words the
    user hears are the Voice's to compose from them (#175). The attempt's own
    evidence stays on the wire and in the log rather than being read out.
    """
    receipt = data["receipt"]
    grade = receipt["outcome"] if isinstance(receipt, dict) else NO_GRADE
    return receipt_line(state=str(data["state"]), grade=str(grade), reason=str(data["reason"]))


def _verify_lines(seams: object) -> list[str]:
    assert isinstance(seams, list)
    return [
        f"{row['seam']}: {row['outcome']}" + (f" — {row['detail']}" if row["detail"] else "")
        for row in seams
    ]
