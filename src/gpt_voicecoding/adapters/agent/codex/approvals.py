"""Turning a Codex permission request into an `ApprovalRequest`, and back again.

Three of Codex's server requests are permission prompts, and they are the
**stable** ones: `item/commandExecution/requestApproval`,
`item/fileChange/requestApproval` and `item/permissions/requestApproval`. The
legacy `execCommandApproval` / `applyPatchApproval` pair still exists on the wire
and is deliberately not consumed.

Two rules are enforced here rather than left to the caller.

**The grant ceiling.** `availableDecisions` arrives as a mixed list: bare strings
like `"accept"` and `"acceptForSession"`, and *structured* members like
`{"acceptWithExecpolicyAmendment": {...}}`. Only the strings become the voice
menu, and not because the menu cannot render an object — because an execpolicy
amendment is a **persistent rule**, and the heaviest grant any voice route may
carry is the session-scoped `acceptForSession`. Nothing this system offers by
voice may outlive the session. A future change that "fixes" this into offering
the amendment would be raising the ceiling, not improving the rendering.

**`ask` is silence.** Codex fans one approval out to every subscribed client with
the same request id, first answer wins, and the losers are told by a
`serverRequest/resolved` notification — verified against codex 0.148.0. So
handing a request back to the on-screen dialog is implemented by *not answering
it*, which is the only reading of `ApprovalVerdict.ASK` that does not quietly
turn a timeout into a denial.
"""

from __future__ import annotations

from typing import Any

from gpt_voicecoding.adapters.agent import _summary
from gpt_voicecoding.seams.agent import ApprovalRequest, ApprovalVerdict, WaitingKind
from gpt_voicecoding.seams.identity import SessionTarget

#: The permission prompts this adapter consumes, and the tool name each is
#: announced under. The legacy pair is absent on purpose — see the module note.
COMMAND_EXECUTION = "item/commandExecution/requestApproval"
APPROVAL_METHODS: dict[str, str] = {
    COMMAND_EXECUTION: "a shell command",
    "item/fileChange/requestApproval": "a file change",
    "item/permissions/requestApproval": "extra permissions",
}

#: The `kind` a `commandExecution` approval carries when what is being approved
#: is **text typed into an already-running process** rather than a command being
#: started. Added at codex-cli 0.150.0 with `#[serde(default)] = "command"` for
#: older servers (`v2/item.rs:1495-1512`), so an absent `kind` is a command and
#: this lane still reads a 0.149.1 daemon correctly. Announced apart because
#: "a shell command" describes the parent it points at, not the stdin under
#: review, and a user answering by voice has only the sentence to go on
#: (`docs/research/2026-08-27-codex-0150-probe.md` § 4).
WRITE_STDIN = "writeStdin"
WRITE_STDIN_NAME = "input to a running command"

#: The approval-params fields a Codex prompt may be summarised from. One field,
#: and `command` is **not** in it: `reason` is a sentence Codex writes for a human
#: to read, and the command is the shell text `_summary` exists to keep out of a
#: notice that is read aloud into a Live Call and pushed to a phone. Until #109
#: this lane read `command` and the Claude lane did not, which is a safety rule
#: enforced on one path and not the other.
SUMMARY_FIELDS: tuple[str, ...] = ("reason",)

#: What each verdict answers on the wire. `ASK` is absent because it answers
#: nothing at all; a mapping entry for it would be the denial this must never be.
DECISIONS: dict[ApprovalVerdict, str] = {
    ApprovalVerdict.ALLOW: "accept",
    ApprovalVerdict.DENY: "decline",
}

#: The heaviest grant a voice route may carry. Session-scoped, never persistent.
GRANT_CEILING = "acceptForSession"

#: Decisions that would outlive the Session. Named so the refusal can say which.
BEYOND_THE_CEILING = frozenset({"acceptAlways", "acceptWithExecpolicyAmendment"})


def voice_menu(available: Any) -> tuple[str, ...]:
    """The decisions a user may be offered by voice, in the order Codex gave them.

    Structured members are dropped: they are the persistent grants, and the
    ceiling is `acceptForSession`. See this module's docstring for why that is a
    policy invariant and not a rendering convenience.
    """
    if not isinstance(available, list):
        return ()
    return tuple(
        decision
        for decision in available
        if isinstance(decision, str) and decision not in BEYOND_THE_CEILING
    )


def tool_name_for(method: str, params: dict[str, Any]) -> str:
    """What the announcement calls the thing being approved.

    The method decides it, with one exception the method cannot express: a
    `commandExecution` prompt whose `kind` is `writeStdin` is not a command being
    started but text being fed to one already running, and it points at the
    *parent* command's `itemId`. Announcing it as "a shell command" describes
    something else that is genuinely happening, which is worse than vague.
    """
    if method == COMMAND_EXECUTION and params.get("kind") == WRITE_STDIN:
        return WRITE_STDIN_NAME
    return APPROVAL_METHODS.get(method, method)


def summary_of(method: str, params: dict[str, Any]) -> str:
    """One line describing what is waiting, taken from Codex's own words.

    The shared rule (`adapters/agent/_summary`): description-class text only,
    whole or not at all. `reason` is the one such field Codex offers — a sentence
    it wrote for a human to read.

    **`command` used to be the fallback and is now excluded**, which is a
    behaviour change and the point of it. It is shell text, and the reference
    implementation kept exactly that out of a summary because reading it aloud is
    neither safe nor useful (`legacy@1d32845:bridge/transcript.py:1779-1790`).
    The Claude lane had always obeyed that rule; this one never had (#109).

    Naming the prompt is what is left when there is nothing readable to say. It
    repeats the tool name, so `a file change — a file change` is what a prompt
    with no `reason` reads as; the payload carries no path to do better with, and
    that is recorded on #109 rather than papered over.

    Whitespace is collapsed **after** the ceiling is applied, not before, and
    this lane collapses where the Claude one does not: Codex writes `reason` as
    prose that can wrap, and a field that is over-long before collapsing is not
    the one-line summary this reads for either way. Erring towards naming the
    prompt is the safe direction; erring the other way is what reads a wrapped
    paragraph into a Live Call.
    """
    summary = _summary.summarise(params, SUMMARY_FIELDS)
    if summary:
        return " ".join(summary.split())
    return tool_name_for(method, params)


def approval_id_of(method: str, params: dict[str, Any]) -> str:
    """This adapter's own handle for one pending dialog.

    Codex's own `approvalId` is used when it is there. It is optional in the
    schema, so the fallback is the pair that does identify the prompt uniquely
    while it is open — the turn it belongs to and the item that raised it.
    """
    stated = params.get("approvalId")
    if isinstance(stated, str) and stated.strip():
        return stated.strip()
    return f"{method}:{params.get('turnId', '')}:{params.get('itemId', '')}"


def request_from(method: str, params: dict[str, Any], *, target: SessionTarget) -> ApprovalRequest:
    """One Codex permission prompt, as the Agent seam describes it."""
    return ApprovalRequest(
        approval_id=approval_id_of(method, params),
        target=target,
        tool_name=tool_name_for(method, params),
        kind=WaitingKind.PERMISSION,
        detail=summary_of(method, params),
        options=voice_menu(params.get("availableDecisions")),
    )


def carries_a_decision(method: str) -> bool:
    """Whether a verdict can be answered on this prompt at all.

    `item/permissions/requestApproval` answers with a granted *permission
    profile*, not with a decision, and this adapter has no profile to grant.
    Saying so is the honest move: the alternative is inventing a grant on the
    user's behalf, which is the one thing an Approval Relay may never do.
    """
    return method in APPROVAL_METHODS and method != "item/permissions/requestApproval"


def answer_for(verdict: ApprovalVerdict) -> dict[str, Any] | None:
    """What to send back, or `None` when Codex has no wire answer for it."""
    decision = DECISIONS.get(verdict)
    if decision is None:
        return None
    return {"decision": decision}
