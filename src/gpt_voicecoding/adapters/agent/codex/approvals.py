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

from gpt_voicecoding.seams.agent import ApprovalRequest, ApprovalVerdict
from gpt_voicecoding.seams.identity import SessionTarget

#: The permission prompts this adapter consumes, and the tool name each is
#: announced under. The legacy pair is absent on purpose — see the module note.
APPROVAL_METHODS: dict[str, str] = {
    "item/commandExecution/requestApproval": "a shell command",
    "item/fileChange/requestApproval": "a file change",
    "item/permissions/requestApproval": "extra permissions",
}

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


def summary_of(method: str, params: dict[str, Any]) -> str:
    """One line describing what is waiting, taken from Codex's own words.

    `reason` is a sentence Codex wrote for a human to read, so it is preferred
    over anything assembled here; the command is the fallback because it is the
    only other thing that says what is actually about to happen.
    """
    for key in ("reason", "command"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return APPROVAL_METHODS.get(method, "a permission request")


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
        tool_name=APPROVAL_METHODS.get(method, method),
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
    """What to send back, or `None` when the verdict is answered by silence."""
    if verdict is ApprovalVerdict.ASK:
        return None
    return {"decision": DECISIONS[verdict]}
