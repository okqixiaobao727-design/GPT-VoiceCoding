"""What this adapter knows about one Codex thread it is watching.

Every field here is something *observed* on the wire, never something assumed:
the Reply Window comes from `thread/status/changed`, the active turn id from
`turn/started` and `turn/completed`, and the approval routing from the settings
Codex echoes back. An adapter that guessed any of them would be inventing the
evidence the hub grades delivery on.

**"Not yet pinned" and "mis-routed" are different facts.** Probing codex 0.148.0
established that `thread/resume` *cannot* change a live thread's approval routing
— the override is accepted and silently ignored — while `turn/start` can, and the
change sticks for subsequent turns. So a thread we have never taken a turn on is
honestly **unpinned**, and its very first Relay will assert the pin; a thread
whose readback still disagrees *after* we asserted it is **mis-routed**, and its
approvals are going to a subagent instead of to the user. Collapsing the two into
one state would report a session we simply have not spoken to yet as broken.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from gpt_voicecoding.adapters.codex_app_server.wire import AppServerConnection
from gpt_voicecoding.seams.agent import ApprovalRequest, ReplyWindow
from gpt_voicecoding.seams.identity import SessionTarget

#: What the approval policy may not be if a permission prompt is ever to appear.
NO_APPROVALS = "never"

#: The only reviewer that routes an approval to the user rather than to a
#: subagent. `auto_review` and the legacy `guardian_subagent` both mean the
#: prompt never reaches this adapter at all.
USER_REVIEWER = "user"

#: What is asserted on every turn this adapter starts. `on-request` is the
#: lightest policy that still raises a prompt the user can answer by voice.
PINNED_POLICY = "on-request"


class ApprovalRouting(StrEnum):
    """Where this thread's permission prompts actually go, as last read back."""

    #: No turn of ours has asserted the pin yet, so nothing has been proven
    #: either way. The next Relay asserts it.
    UNPINNED = "unpinned"
    #: Read back as `approvalsReviewer = user` with approvals enabled.
    PINNED = "pinned"
    #: Read back as something else *after* we asserted the pin. Prompts from this
    #: thread are being answered by a subagent and will never reach the user.
    MISROUTED = "misrouted"


@dataclass(slots=True)
class WatchedThread:
    """One Codex thread, and everything observed about it."""

    target: SessionTarget
    socket_path: Path
    connection: AppServerConnection
    #: Whether this thread rides a connection somebody else owns — the shared
    #: daemon's, which every thread on the machine shares (#77).
    #:
    #: **It exists so that letting go of one thread does not let go of all of
    #: them.** `forget_session` and `aclose` close the connection a thread was
    #: watched on, which is right for the per-Session app-server this adapter
    #: dialled itself and catastrophic for one connection shared by nine
    #: Sessions. The shared connection is closed exactly once, by the component
    #: that opened it (`SharedDaemon.aclose`).
    shared: bool = False
    #: Whether `thread/resume` has been called, which is what subscribes this
    #: client to the thread's turn and item stream.
    subscribed: bool = False
    #: Why it could not be, when it could not. A thread the user has just
    #: launched has no rollout on disk yet and cannot be resumed at all, which
    #: is a *not yet* rather than a failure — so it is recorded and retried
    #: instead of raised.
    subscribe_blocked: str = ""
    reply_window: ReplyWindow = ReplyWindow.CLOSED
    #: Whether any status has been observed yet. The first one is what this
    #: thread *is*, not something it just became: announcing it as a transition
    #: would report every registration as a Session that had stopped.
    observed: bool = False
    #: Monotonic turn generation, incremented only when the thread goes back to
    #: active. A Stop from an earlier turn cannot announce under a newer one.
    turn_revision: int = 0
    #: The turn `turn/steer` must name as its precondition, when one is running.
    active_turn_id: str | None = None
    routing: ApprovalRouting = ApprovalRouting.UNPINNED
    #: The words the readback used, so a refusal can quote them rather than
    #: paraphrase what was wrong.
    routing_detail: str = ""
    #: Pending permission prompts, by this adapter's handle for each.
    pending: dict[str, PendingApproval] = field(default_factory=dict)
    #: Prompts somebody else answered — the on-screen dialog, almost always.
    #: Kept so a verdict arriving afterwards is told what happened rather than
    #: told the prompt never existed.
    answered_elsewhere: set[str] = field(default_factory=set)

    @property
    def thread_id(self) -> str:
        return self.target.session_id

    def read_routing(self, settings: dict[str, Any]) -> None:
        """Record what Codex echoed back about where approvals go.

        An echo that arrives before we ever asserted the pin cannot make a thread
        mis-routed — it can only leave it unpinned, or, if it happens to already
        be right, pinned.
        """
        policy = settings.get("approvalPolicy")
        reviewer = settings.get("approvalsReviewer")
        if reviewer is None and policy is None:
            return
        if reviewer == USER_REVIEWER and policy != NO_APPROVALS:
            self.routing = ApprovalRouting.PINNED
            self.routing_detail = ""
            return
        self.routing_detail = (
            f"codex reports approvalPolicy={policy!r} and approvalsReviewer={reviewer!r}"
        )
        # Only a disagreement *after* an assertion is a mis-route; before that it
        # is simply a thread nobody has pinned yet.
        if self.routing is ApprovalRouting.PINNED:
            self.routing = ApprovalRouting.MISROUTED
        elif self.routing is ApprovalRouting.UNPINNED:
            self.routing = ApprovalRouting.UNPINNED

    def assert_pinned(self) -> None:
        """Record that a turn we started carried the pin, so a later echo counts."""
        if self.routing is ApprovalRouting.UNPINNED:
            self.routing = ApprovalRouting.PINNED
            self.routing_detail = ""


@dataclass(frozen=True, slots=True)
class PendingApproval:
    """One permission prompt this adapter is holding, and how to answer it.

    `wire_id` is the JSON-RPC id the prompt arrived under. It is kept apart from
    the seam's `approval_id` on purpose: one names a message on a wire, the other
    names a dialog in the domain, and only one of them survives a reconnect.
    """

    approval_id: str
    wire_id: Any
    method: str
    #: The prompt as the Agent seam describes it, parsed once when it arrived.
    #: Kept so the roster can say what a Codex Session stopped on (#77) without
    #: a second reading of the same params — two parses of one message are two
    #: answers that can disagree, which is the defect `SessionInspection` exists
    #: to close.
    request: ApprovalRequest | None = None
