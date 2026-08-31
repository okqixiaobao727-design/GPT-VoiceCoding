"""What one Claude Session stopped on, read out of its own transcript records.

One function, no I/O. It is handed records — already parsed, in the order the
Session wrote them — and answers the seam's `WaitingFor`: a question with its
options, a tool awaiting permission with a one-line summary, or the honest
admission that the record has not caught up yet.

**Three port-table rows are one module because they are one pass** (P3, P4, P5).
Which kind a stop is, is not a property of either kind: it is decided by pairing
every `tool_use` against the later `tool_result` that closes it and then asking
what is left outstanding *in the tail*. Two passes over a file that runs to tens
of thousands of records would also let the two answers describe two different
moments of a file the Session is still appending to
(`legacy@1d32845:bridge/transcript.py:1184-1208`).

**The tail is what makes a stop *this* stop.** Only calls that nothing the user
would hear came after are considered; an older outstanding call — a question
answered at the keyboard, so no result was written — belongs to a moment the
Session has moved past (`legacy@1d32845:bridge/transcript.py:1683-1712`). Which
is why the visibility rules are here: not to decide what is worth reading aloud,
but to give "the tail" a boundary. **A question beats a permission call beside
it**, because the decision is the thing only the user can supply (`:1691-1692`).

**Nothing here reads a file, a socket, a store or Bridge Core.** The caller reads
the transcript the `SessionStart` registration named and overlays the two facts
this module cannot know: `approval_id`, which arrives with the
`PermissionRequest` hook holding the dialog open, and the difference between
"waiting on nothing" and "waiting, and we cannot yet say what", which is the
roster's `state` rather than the transcript's business.

Ported from `legacy@1d32845:bridge/transcript.py:126-143,1477-1605,1610-1866`,
which ran in production and proved these rules (ADR 0010). **Dropped**: the
durable Stop Request id, the catch-up cache tied to ledger rows, the model-facing
fragment history, the 2.9k-line module topology, and the whole-file identity
check — the caller opens the exact transcript the Session's own hook named, and a
pure parser has no identity to compare against, so a child's record is caught by
the sidechain rule below instead.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from gpt_voicecoding.adapters.agent import _summary
from gpt_voicecoding.seams.agent import Option, WaitingFor, WaitingKind

#: The one tool whose call is part of the visible conversation, and the only one
#: that asks for a decision rather than for permission to act
#: (`legacy@1d32845:bridge/transcript.py:1565`).
QUESTION_TOOL: Final = "AskUserQuestion"

#: How Claude Code marks the option a Session recommends: the tool's own
#: instructions tell the model to put this at the end of that option's label.
#: Nothing else in the call says which option is recommended, so a call without
#: this marker has no recommendation to report.
RECOMMENDED_MARKER: Final = "(recommended)"

#: The input fields a Claude permission request may be summarised from, in the
#: order they are preferred. Each is a short human-facing string the product
#: already writes for a person to read. The arguments proper — `command`,
#: `content`, `old_string` — are **deliberately absent**, and that rule lives in
#: `_summary` now rather than here: the Codex lane needs the same one, and while
#: it lived here that lane read the shell command verbatim.
SUMMARY_FIELDS: Final = ("description", "file_path", "path", "notebook_path")

#: Re-exported, not redefined. `_progress.bounded` cites this name for the rule
#: it follows, and `tests/test_progress_bound.py` holds the two together.
SUMMARY_MAX_CHARS: Final = _summary.SUMMARY_MAX_CHARS

#: The wrappers Claude Code writes around the two local-command pipeline records,
#: and the opening line of an expanded skill body. All three are `user` records
#: with no `promptSource`, so none is already excluded as product-injected
#: plumbing (`legacy@1d32845:bridge/transcript.py:1504-1512`).
_COMMAND_CAVEAT_TAG: Final = "<local-command-caveat>"
_COMMAND_STDOUT_TAG: Final = "<local-command-stdout>"
_SKILL_EXPANSION_OPENING: Final = "Base directory for this skill:"


def analyse(records: Sequence[Mapping[str, Any]]) -> WaitingFor:
    """What the tail of these records says this Session is waiting on.

    `WaitingKind.NONE` means the tail is not held up on anything — which is a
    finished turn *and* a Session whose awaited record has not been flushed yet.
    The transcript cannot tell those apart, and neither pretends to here: the
    caller knows the Session's state from the roster and is the one that turns a
    `NONE` from a Session the roster calls `waiting` into `UNKNOWN` with
    `caught_up=False`, which is the seam's word for *ask again, never guess*.
    """
    open_calls: dict[str, _OpenCall] = {}
    last_spoken_at = -1
    for ordinal, record in enumerate(records):
        if not isinstance(record, Mapping) or record.get("isSidechain") is True:
            # A sidechain record is an Agent-created child's work, not this
            # Session's turn — 32% of real records — and a child's tool call is
            # not something the user is being asked about (#68's Child Process
            # rule, `legacy@1d32845:bridge/transcript.py:1131-1133`).
            continue
        kind = record.get("type")
        if kind not in ("user", "assistant"):
            continue
        message = record.get("message")
        if not isinstance(message, Mapping) or message.get("role") != kind:
            continue
        content = message.get("content")
        # Pairing runs *before* the visibility rules, not after them: a result
        # closes the call it names whether or not it is worth reading aloud,
        # and a call is followed whether or not its record is visible.
        _follow(content, open_calls, ordinal)
        if is_visible(record) and not is_pipeline_noise(record, content) and visible_text(content):
            last_spoken_at = ordinal
    return _tail_wait(open_calls, last_spoken_at=last_spoken_at)


def summarise(tool_input: Any) -> str:
    """The one readable thing a Claude tool call says about itself, or nothing.

    P5. This lane's fields, the shared rule (`_summary`): description-class text
    only, whole or not at all, and empty is an answer. The Approval Relay builds
    its `detail` with this too (`approval.request_from`), so a rule enforced here
    and not there would be no rule — which is exactly what the Codex lane's own
    extractor turned out to be.
    """
    return _summary.summarise(tool_input, SUMMARY_FIELDS)


@dataclass(frozen=True, slots=True)
class _OpenCall:
    """One tool call this Session has written down and had no result for."""

    #: Where the call sits, so "is it in the tail" is answerable.
    ordinal: int
    #: The decision this call asks for, for the one tool that asks for any.
    #: `None` means it asks for permission to act instead.
    question: WaitingFor | None
    tool_name: str
    detail: str


def _follow(content: Any, open_calls: dict[str, _OpenCall], ordinal: int) -> None:
    """Update which calls this Session has had no result back for.

    Called once per record in transcript order, so `open_calls` ends the walk
    holding exactly the outstanding ones, newest last, each remembering where it
    was written (`legacy@1d32845:bridge/transcript.py:1634-1680`).

    A question is entered only once it offers a readable option: an input still
    being streamed arrives as `__unparsedToolInput` with no `questions` at all —
    precisely the not-yet-flushed record the caller is waiting for — so counting
    it would declare the Session caught up on a question nobody can read. Every
    other call is entered with no question, and that is what makes it a
    permission request rather than a decision.
    """
    if not isinstance(content, list):
        return
    for item in content:
        if not isinstance(item, Mapping):
            continue
        match item.get("type"):
            case "tool_use":
                identifier = item.get("id")
                if not isinstance(identifier, str) or not identifier:
                    continue
                name = item.get("name")
                tool_input = item.get("input")
                question = question_in(tool_input) if name == QUESTION_TOOL else None
                if name == QUESTION_TOOL and question is None:
                    continue
                open_calls[identifier] = _OpenCall(
                    ordinal=ordinal,
                    question=question,
                    tool_name=name if isinstance(name, str) else "",
                    detail=summarise(tool_input),
                )
            case "tool_result":
                identifier = item.get("tool_use_id")
                if isinstance(identifier, str):
                    open_calls.pop(identifier, None)


def _tail_wait(open_calls: dict[str, _OpenCall], *, last_spoken_at: int) -> WaitingFor:
    """Read what the Session is waiting on out of the outstanding calls."""
    tail = [call for call in open_calls.values() if call.ordinal >= last_spoken_at]
    asking = next((call for call in reversed(tail) if call.question is not None), None)
    if asking is not None:
        assert asking.question is not None  # exactly what `asking` selected on
        return asking.question
    if not tail:
        return WaitingFor()
    # The newest outstanding call is the one the Session is held up on: an older
    # one it wrote first is already waiting behind this. A call this scan cannot
    # describe is still a call the Session is waiting on, and saying so with no
    # detail beats saying nothing (`legacy@1d32845:bridge/transcript.py:1699-1712`).
    newest = tail[-1]
    return WaitingFor(
        kind=WaitingKind.PERMISSION,
        tool_name=newest.tool_name or None,
        detail=newest.detail or None,
    )


@dataclass(frozen=True, slots=True)
class _QuestionGroup:
    """One prompt and the typed options offered under it."""

    prompt: str
    options: tuple[Option, ...]


def question_in(tool_input: Any) -> WaitingFor | None:
    """Everything one `AskUserQuestion` call gives an announcement to say.

    **Public because there are two sources of this exact object and one parser
    of it.** The transcript's `tool_use.input` and the `PermissionRequest` hook's
    `tool_input` are the same payload — `AskUserQuestion` raises that hook,
    measured on 2.1.246 (#77) — so `approval.question_from` reads it through
    here rather than through a second projector of its own. The hook adds one
    thing the transcript cannot carry, the dialog's `prompt_id`, and adds it
    beside this answer instead of inside it.

    One call can hold several groups. Options are flattened into one list —
    that is the list the user hears read out — and prompts are joined the same
    way, so nothing the Session asked goes unsaid.

    Every marked option carries its mark. `recommendation` names one on top of
    that **only when the whole call marked exactly one**: several groups each
    recommending something are several recommendations, and picking one to be
    *the* recommendation would credit the Session with a conclusion it did not
    reach (`legacy@1d32845:bridge/transcript.py:1736-1741`). Nothing is lost —
    the options still say which ones it marked.
    """
    prompts: list[str] = []
    options: list[Option] = []
    recommended: list[str] = []
    for group in _groups(tool_input):
        if group.prompt:
            prompts.append(group.prompt)
        options.extend(group.options)
        recommended.extend(option.text for option in group.options if option.recommended)
    if not options:
        return None
    return WaitingFor(
        kind=WaitingKind.QUESTION,
        prompt="\n".join(prompts) or None,
        options=tuple(options),
        recommendation=recommended[0] if len(recommended) == 1 else None,
    )


def _groups(tool_input: Any) -> list[_QuestionGroup]:
    """One `AskUserQuestion` call's readable content: prompts and offered options.

    The single place this call's shape is parsed, so what is announced and how
    many options there are can never disagree about what an option is.

    A description is the Session's own short explanation and travels with its
    label (#151). `preview` remains deliberately left out: it holds the mockups,
    diffs and code snippets that make an option comparable *on screen* — the one
    thing a spoken menu can neither convey nor afford.

    A call whose input never finished being written arrives as
    `__unparsedToolInput` instead of `questions`. That is unreadable rather than
    hostile, so like every other unreadable item it contributes nothing.
    """
    if not isinstance(tool_input, Mapping):
        return []
    questions = tool_input.get("questions")
    if not isinstance(questions, list):
        return []
    groups: list[_QuestionGroup] = []
    for question in questions:
        if not isinstance(question, Mapping):
            continue
        prompt = question.get("question")
        options: list[Option] = []
        raw = question.get("options")
        if isinstance(raw, list):
            for option in raw:
                if not isinstance(option, Mapping):
                    continue
                label = option.get("label")
                if isinstance(label, str) and label.strip():
                    text, is_recommended = _split_recommendation(label.strip())
                    raw_description = option.get("description")
                    description = (
                        raw_description.strip()
                        if isinstance(raw_description, str) and raw_description.strip()
                        else None
                    )
                    options.append(
                        Option(
                            text=text,
                            recommended=is_recommended,
                            description=description,
                        )
                    )
        groups.append(
            _QuestionGroup(
                prompt=prompt.strip() if isinstance(prompt, str) else "",
                options=tuple(options),
            )
        )
    return groups


def _split_recommendation(label: str) -> tuple[str, bool]:
    """The option's spoken words, and whether it is the marked recommendation."""
    if label.lower().endswith(RECOMMENDED_MARKER):
        text = label[: -len(RECOMMENDED_MARKER)].strip()
        if text:
            return text, True
    return label, False


def is_visible(record: Mapping[str, Any]) -> bool:
    """Whether this record is part of the conversation the user can see.

    Three semantic exclusions, not format checks: a sidechain record is a
    child's work, a record that is not `external` is not the user's own turn, and
    a `system` prompt source is product-injected plumbing that would otherwise be
    reported back to the user as something they said
    (`legacy@1d32845:bridge/transcript.py:1477-1500`).

    The `system` exclusion is applied to every record type rather than only to
    `user` ones, so a future Claude Code that starts injecting on the assistant
    side is excluded by default instead of leaking until somebody notices.
    """
    return (
        record.get("isSidechain") is False
        and record.get("userType") == "external"
        and record.get("promptSource") != "system"
    )


def is_pipeline_noise(record: Mapping[str, Any], content: Any) -> bool:
    """Whether this record is slash-command plumbing rather than conversation.

    Running a slash command writes machinery into the transcript beside the
    user's own turn — the caveat, the command's stdout, and, for a skill, the
    whole skill body as an `isMeta` record. All three are marked `user`, so
    without this they count as the user having spoken and the tail boundary moves
    past the call the Session is actually held up on
    (`legacy@1d32845:bridge/transcript.py:1515-1562`).

    What the user *typed* is kept: the command record proper holds
    `<command-args>`, which is their real intent.

    A marker is believed only on the record shape that actually writes it —
    `user`, with no `promptSource` at all. Without that guard the markers are
    just text, and an assistant explaining this very format would vanish from
    its own conversation.
    """
    if record.get("type") != "user" or record.get("promptSource") is not None:
        return False
    if isinstance(content, str):
        opening = content.lstrip()
        return opening.startswith(_COMMAND_CAVEAT_TAG) or opening.startswith(_COMMAND_STDOUT_TAG)
    if not record.get("isMeta") or not isinstance(content, list):
        return False
    for item in content:
        if isinstance(item, Mapping) and item.get("type") == "text":
            text = item.get("text")
            return isinstance(text, str) and text.lstrip().startswith(_SKILL_EXPANSION_OPENING)
    return False


def visible_text(content: Any) -> str:
    """What this message put in front of the user, or nothing at all.

    **Two callers, one rule, and that is the point.** `analyse` needs only
    whether a message spoke, because that is what gives "the tail" a boundary;
    `transcript_tail` needs the words themselves, because that is what `Progress`
    is made of (#76). Answering the boolean from a second scan of the same
    content is how the reference implementation ended up with readers that
    disagreed about the same record — so there is one extractor and the boundary
    is `bool(visible_text(...))`.

    Every content shape Claude Code writes is *collected from* rather than
    enumerated: real transcripts carry at least six user shapes and five
    assistant ones and the product adds more, so an unknown item contributes
    nothing instead of making the message unreadable
    (`legacy@1d32845:bridge/transcript.py:1568-1607`).

    `tool_use` is readable for exactly one tool name. The Session's own words say
    what decision it is waiting on, but the choices live in `AskUserQuestion`'s
    input, so this call *is* something the user is shown — without it the user
    hears the question and never hears the options. Every other `tool_use`
    carries commands, code and file contents, which must not be read aloud; and,
    for the tail boundary, counting one would put the boundary after the very
    call the Session is waiting on.
    """
    if isinstance(content, str):
        return content if content.strip() else ""
    if not isinstance(content, list):
        return ""
    readable = ""
    for item in content:
        if not isinstance(item, Mapping):
            continue
        if item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                readable += text
        elif item.get("type") == "tool_use" and item.get("name") == QUESTION_TOOL:
            asked = _question_text(item.get("input"))
            if asked:
                readable += f"\n{asked}" if readable else asked
    return readable


def _question_text(tool_input: Any) -> str:
    """What one `AskUserQuestion` call asks, and what it offers as answers.

    Rendered off the same `_groups` the seam's `Option` values are built from, so
    the words the user hears and the choices they may say back can never come
    from two different readings of one call
    (`legacy@1d32845:bridge/transcript.py:1861-1874`).

    **A call with no readable option renders as nothing**, which is the same
    test `_question` fails on. An input still being streamed arrives with no
    options at all, and a prompt without choices read aloud is a question the
    user cannot answer — and, for `analyse`'s tail boundary, would move the
    boundary past the very call the Session is waiting on.
    """
    groups = _groups(tool_input)
    if not any(group.options for group in groups):
        return ""
    lines: list[str] = []
    for group in groups:
        if group.prompt:
            lines.append(f"Question: {group.prompt}")
        lines.extend(f"Option: {option.text}" for option in group.options)
    return "\n".join(lines)
