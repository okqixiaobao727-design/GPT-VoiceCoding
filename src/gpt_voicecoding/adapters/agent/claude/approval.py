"""The Claude hook listener: one wire, selected behind two Agent-seam routes.

**The route is a `PermissionRequest` hook, and the hook is the wire.** Claude Code
runs it for a displayed permission and for `AskUserQuestion`, hands it the tool
and its input on stdin, and waits up to its own budget for the process to print a
decision. So the hook is not a notifier that reports and leaves: it is a held-open
connection whose *return value* is the verdict, and the whole design falls out of
that. The hook dials this listener, this listener parks it, Bridge Core takes as
long as the user needs, and the answer travels back down the same connection the
dialog is still waiting on.

**`ask` is silence, and that is a wire fact rather than a preference.** Read out
of Claude Code 2.1.238: both the interactive and the headless permission paths
consume `hookSpecificOutput.decision` and treat `behavior === "allow"` as allow
and **anything else as deny**; a hook that emits no decision falls through to the
on-screen dialog. There is no `behavior: "ask"`, and emitting one would be a
denial wearing the wrong word. Handing a request back is therefore implemented by
printing nothing at all — the same shape the Codex spoke's approvals module
arrived at independently, for the same reason.

At the seam, permissions use the Approval Relay's `allow`, `deny`, or `ask`.
Questions use the Answer Relay's ordinary words; the Claude adapter selects this
hook instead of the inbox while the exact prompt remains parked. At the wire,
that answer is a denial whose framed message Claude consumes as the question's
tool result. `ask` is the one verdict said by saying nothing.

**The listener timestamps; Bridge Core owns the duration.** A parked question
records the listener's injected monotonic clock. Bridge Core passes its configured
`approval_budget_seconds` into `sweep_question_budget`, so this module imports no
policy and mirrors no default. Expiry pops first and writes `ASK`. Claude Code's
own default hook budget happens to be 600 s, the same number `CorePolicy` defaults
to — a coincidence worth knowing and not a constant to copy.

**The grant ceiling is a policy this file keeps, not a limit the route imposes.**
`permission_suggestions` arrives on the hook payload and an `allow` decision may
carry `updatedPermissions`, which would write a session-scoped rule. The locked
decision is that the heaviest grant a spoken word may produce here is one-shot,
so the suggestions are read and dropped and the decision this module builds
carries neither field. That the mechanism would allow more is exactly why it is
asserted in the tests rather than left to whoever edits this next.

**Every failure is silence.** A refusal, an unparseable request, a Session this
engine does not hold — each one closes the hook's connection without a decision,
and the dialog the human is already looking at keeps the request. That is the
fail-open posture the never-deny rule demands, and it is the reason this module
returns nothing rather than raising into a process whose stdout is a verdict.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, Final

from gpt_voicecoding.adapters.agent.claude import stop_analysis
from gpt_voicecoding.adapters.agent.claude.privacy import (
    PRIVATE_SOCKET_MODE,
    ChannelPathError,
    prepare_private_directory,
    verify_bindable_length,
)
from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings
from gpt_voicecoding.private_socket import start_private_unix_server
from gpt_voicecoding.seams.agent import (
    AgentEvent,
    ApprovalRequest,
    ApprovalVerdict,
    AwaitingApproval,
    ReplyWindow,
    ReplyWindowChanged,
    WaitingFor,
    WaitingKind,
)
from gpt_voicecoding.seams.delivery import Delivery, DeliveryReceipt
from gpt_voicecoding.seams.identity import RequestId, SessionTarget

_log = logging.getLogger(__name__)

# -- the hook contract, as Claude Code defines it ------------------------

#: The hook event this route rides. Claude Code fires it when a permission
#: dialog is displayed, and never for a call an existing rule pre-approved.
HOOK_EVENT: Final = "PermissionRequest"

#: The one decision word that means allow. Every other value — including the
#: word "ask" — is read as a denial, which is why `ASK` prints nothing.
ALLOW_BEHAVIOR: Final = "allow"
DENY_BEHAVIOR: Final = "deny"

#: The Claude Code build this route's byte shapes were read out of. Documentation
#: rather than a gate, in the same spirit as the registry's version note: what a
#: re-probe after an upgrade should compare against. The `--plugin-dir` loading
#: of a plugin's `hooks/hooks.json` is part of this pin and was verified live.
PROVEN_AGAINST_VERSION: Final = "2.1.238"

#: What a denied tool call is told. The user said no; the Session hears why.
DENIED_BY_VOICE: Final = "denied by the user, by voice, through GPT-VoiceCoding"

#: Claude's dialog correlator when the wire supplies one. `WaitingFor` reports
#: this value exactly; the listener mints a separate private key when it is absent.
PROMPT_ID_FIELD: Final = "prompt_id"


# **Drift in this shape is silent, and that is why the receipt is where it is.**
# Measured on 2.1.246 (#77): the `permissionDecision` / `permissionDecisionReason`
# shape the public hooks reference reaches for has *no effect at all* — the dialog
# simply stands there, for `AskUserQuestion` and for `Write` alike. The shape
# below is the one Claude Code consumes, and a build that stopped consuming it
# would be indistinguishable from a hook that answered `ask`. Nothing on the wire
# would say so, which is exactly why this route's proof of delivery is the
# `approval_ack` frame over our own socket (`ACK_TYPE`) and never the hook's exit.
def hook_decision(verdict: ApprovalVerdict, *, message: str | None = None) -> dict[str, Any] | None:
    """What the hook prints for one verdict, or `None` when it prints nothing.

    Neither `updatedInput` nor `updatedPermissions` ever appears. The first would
    rewrite the call the user was asked about, so their yes would be a yes to
    something else; the second is a session-scoped rule, and the locked ceiling
    for a spoken grant is one call.
    """
    if verdict is ApprovalVerdict.ASK:
        return None
    decision: dict[str, Any] = (
        {"behavior": ALLOW_BEHAVIOR}
        if verdict is ApprovalVerdict.ALLOW
        else {"behavior": DENY_BEHAVIOR, "message": message or DENIED_BY_VOICE}
    )
    return {"hookSpecificOutput": {"hookEventName": HOOK_EVENT, "decision": decision}}


# -- the wire between the hook process and this engine -------------------

#: One request in, one answer back, and nothing else is speakable on this socket.
#: A closed grammar on purpose: this is a machine-facing wire reached from a
#: process Claude Code starts, and a generic request channel there would be a
#: second control plane nobody designed.
REQUEST_TYPE: Final = "approval_request"

#: The `SessionStart` hook's one line. It rides **this** socket rather than one
#: of its own: ADR 0011 installs two hooks and the engine publishes one address,
#: so a second listener would be a second address to publish, a second thing to
#: fail to bind, and a second place a Session could be half-known.
REGISTRATION_TYPE: Final = "session_registration"

#: What the registration acknowledges with. The hook does not wait for it — a
#: `SessionStart` hook that blocked would delay every Session in the config
#: directory — but the engine sends it so a probe can prove the line landed.
REGISTERED_TYPE: Final = "session_registered"
VERDICT_TYPE: Final = "approval_verdict"
REFUSAL_TYPE: Final = "approval_refused"

#: The hook's receipt, and the only positive proof this route has.
#:
#: It exists because the obvious cheaper proof is not one. Grading a verdict
#: DELIVERED on the connection ending looks sound — the hook reads the line and
#: exits — but the end of a connection has two causes and they mean opposite
#: things: the hook leaving with its verdict, and the human answering the dialog
#: on screen, after which Claude Code takes the hook process away. An engine
#: reading EOF as a receipt therefore reports "approved by voice" for a tool call
#: that never ran. The hook says so explicitly instead.
#:
#: How much the connection alone can tell, measured on 2.1.245 (#71): a pre-empt
#: that lands *before* the verdict is written announces itself — with the engine
#: holding for 25 s and the human answering *No* at 9 s, the late write raised
#: `BrokenPipeError` and the tool never ran. A pre-empt that lands after the
#: write does not: the bytes go, and nothing on this side says whether the hook
#: lived to read them. The ack covers both, which is why the grade hangs on it
#: rather than on the write succeeding.
ACK_TYPE: Final = "approval_ack"

TYPE_FIELD: Final = "type"
SESSION_ID_FIELD: Final = "session_id"
CWD_FIELD: Final = "cwd"
TOOL_NAME_FIELD: Final = "tool_name"
TOOL_INPUT_FIELD: Final = "tool_input"
VERDICT_FIELD: Final = "verdict"
MESSAGE_FIELD: Final = "message"
REASON_FIELD: Final = "reason"

#: Claude consumes a denied `AskUserQuestion` call's message as the tool result.
#: Frame remote words so the Session can distinguish their source from a local
#: keyboard answer while preserving the user's words inside the frame.
QUESTION_ANSWER_PREFIX: Final = "The user answered from GPT-VoiceCoding: "

#: The registration's own fields. `transcript_path` is the one that earns this
#: hook its place (#71): Claude Code's own registry does not carry it, and it
#: cannot be derived without guessing at the directory-name flattening — which
#: replaces `/`, `.` **and** `_` with `-` (#73, measured the hard way).
#: The pid of the `claude` process this hook ran under. Load-bearing rather than
#: informational: a Claude `SessionTarget` needs a pid (`seams/identity.py:124`)
#: because `--resume` forks a second process under one session id, and the hook
#: payload has never carried one. Measured 2026-08-26: Claude Code exports it as
#: `CLAUDE_PID`, and it is the same number the official roster reports and the
#: same number the Session's inbox socket is named after.
PID_FIELD: Final = "pid"
TRANSCRIPT_PATH_FIELD: Final = "transcript_path"
MESSAGING_SOCKET_FIELD: Final = "messaging_socket"
MESSAGING_TOKEN_FIELD: Final = "messaging_token"

#: The directory the approval socket lives in, one per engine process. It has to
#: be a directory of our own because `privacy.py` requires the parent to be
#: enterable by nobody else, and the configured socket root is shared.
APPROVAL_DIRECTORY_PREFIX: Final = "vc-approvals-"
APPROVAL_SOCKET_NAME: Final = "approvals.sock"

#: The longest hook payload this listener will read. The hook sends one line
#: carrying a tool's input, which can be a whole file's contents on a Write.
MAX_HOOK_REQUEST_BYTES: Final = 1 << 20


def approval_socket_path(directory: Path, pid: int) -> Path:
    """Where one engine's approval socket lives, derived rather than configured.

    Per-pid because two engines on one machine must not fight over one path, and
    derived because the launcher has to be able to name it for a Session it is
    about to start without asking a running adapter.
    """
    return directory / f"{APPROVAL_DIRECTORY_PREFIX}{pid}" / APPROVAL_SOCKET_NAME


def request_from(
    payload: Mapping[str, Any], *, target: SessionTarget, approval_id: str
) -> ApprovalRequest:
    """One displayed permission dialog, as the Agent seam describes it.

    `options` stays empty, and that is the honest report: this route offers
    allow and deny and nothing else, so there is no ready-made menu to read out.
    `permission_suggestions` is deliberately not consulted — every suggestion it
    carries is a *rule*, and a rule outlives the one call the user was asked
    about.

    **`detail` is P5's extractor, and there is only one of it.** This module had
    its own until #75, which read `command` first and returned it whole — so the
    Approval Relay spoke a shell command into a Live Call and pushed it to a
    phone, while the transcript-derived `WaitingFor.detail` excluded exactly
    that. The signed port table rules the reference implementation's way
    (`legacy@1d32845:bridge/transcript.py:1779-1811,126-143`): the arguments
    proper — command text, file contents, edit strings — never appear, and
    anything over 200 characters is passed over rather than cut, because a cut
    lands mid-secret as readily as mid-word. Both of the old arguments held less
    than they looked: the exclusion is not about length, so Bridge Core could not
    have made it later. What the user hears when nothing readable is left is the
    tool's name, which is enough to say *allow*, *deny*, or `ASK` — the verdict
    that hands the dialog back to the screen in front of them.
    """
    tool_name = payload.get(TOOL_NAME_FIELD)
    return ApprovalRequest(
        approval_id=approval_id,
        target=target,
        tool_name=tool_name if isinstance(tool_name, str) and tool_name.strip() else "a tool",
        detail=stop_analysis.summarise(payload.get(TOOL_INPUT_FIELD)),
    )


def question_from(payload: Mapping[str, Any]) -> WaitingFor | None:
    """One `AskUserQuestion` dialog as a `WaitingFor`, or `None` for anything else.

    **`AskUserQuestion` rides the permission hook**, measured on 2.1.246 (#77):
    the payload carries the whole structured question in `tool_input.questions`.
    Claude Code 2.1.248 carries no usable `prompt_id` on this request, while an
    ordinary permission does. This projector reports that distinction exactly;
    the listener separately mints a private key for its held writer. The
    transcript only says a question was asked once the tool call has flushed,
    and by then the person at the keyboard has usually answered it.

    **The shape is parsed by #75's parser and nothing else.** The hook's
    `tool_input` and the transcript's `tool_use.input` are the same object, so a
    second projector here would be two readings of one payload that could
    disagree about what an option is — the thing `stop_analysis.question_in`
    exists to prevent. What this adds is Claude's `prompt_id` when present,
    without substituting the listener's private correlator when it is absent.

    **`Option.recommended` is read, and it is a fact rather than an inference.**
    `AskUserQuestion` has no recommendation *field* — the tool's own instructions
    tell the model to put `(recommended)` at the end of that option's label
    (`stop_analysis.RECOMMENDED_MARKER`), so the mark travels inside the label
    and this payload carries it whenever the model wrote one. Reading it here is
    therefore the Session's own words, not a guess, and it is the same reading
    the roster row gets from the same object — a payload with no marker yields
    `False` throughout, and the order the options were written in is never
    consulted on either route. What the user hears and says back is the label
    without the mark, which is what `WaitingFor.options` carries.

    **An unreadable question is still a question**, which is where this parts
    company with the transcript route. There, a call whose input has not
    finished being written is skipped, because a reader may not claim a kind it
    has not read. Here the payload *is* the record: the dialog is provably on
    screen and provably an `AskUserQuestion`, so it is announced as a question
    with nothing to read out rather than falling back to a permission — which
    would restore the allow/deny menu this route exists to withhold.
    """
    if payload.get(TOOL_NAME_FIELD) != stop_analysis.QUESTION_TOOL:
        return None
    prompt_id = payload.get(PROMPT_ID_FIELD)
    approval_id = prompt_id if isinstance(prompt_id, str) and prompt_id.strip() else None
    asked = stop_analysis.question_in(payload.get(TOOL_INPUT_FIELD))
    if asked is None:
        return WaitingFor(kind=WaitingKind.QUESTION, approval_id=approval_id)
    return replace(asked, approval_id=approval_id)


class ApprovalError(Exception):
    """The approval socket could not be bound, or could not be taken back out."""


class _Waiting:
    """One hook process, parked mid-dialog, and the connection it is holding open."""

    __slots__ = (
        "acknowledged",
        "answered",
        "gone",
        "parked_at",
        "permission",
        "question",
        "target",
        "writer",
    )

    def __init__(
        self,
        target: SessionTarget,
        writer: asyncio.StreamWriter,
        *,
        permission: ApprovalRequest | None = None,
        question: WaitingFor | None = None,
        parked_at: float,
    ) -> None:
        if (permission is None) == (question is None):
            raise ValueError("a parked hook is exactly one permission or question")
        self.target = target
        self.writer = writer
        self.permission = permission
        #: What this dialog asked, when it is an `AskUserQuestion` rather than a
        #: permission. Parsed once, here, when the payload arrives: two parses of
        #: one message are two answers that can disagree.
        self.question = question
        self.parked_at = parked_at
        #: Set once a verdict has been written to this hook. It is what tells the
        #: connection's own task that the end it is about to see is an ordinary
        #: goodbye rather than a human winning the race.
        self.answered = asyncio.Event()
        #: Set when the hook says, in so many words, that it has the verdict.
        #: The only thing DELIVERED is ever granted on.
        self.acknowledged = asyncio.Event()
        #: Set when the hook's end of the socket closes, whatever the reason. It
        #: is what stops the wait; it is never, on its own, a proof.
        self.gone = asyncio.Event()


class ApprovalListener:
    """The engine's half of the hook route: park the dialog, then answer into it.

    One socket per engine, in a directory of our own under the configured socket
    root. It is bound on `start` and removed on `aclose`, and every hook still
    parked on it when this closes is released with `ask` — an engine going away
    must never be the reason a dialog resolves without its human.
    """

    def __init__(
        self,
        *,
        settings: ClaudeSettings,
        resolve: Callable[[str], SessionTarget | None],
        emit: Callable[[AgentEvent], None],
        pid: int | None = None,
        register: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        #: What to do with a `SessionStart` line. `None` drops it, which is what
        #: an engine assembled without a Claude adapter should do with one.
        self._register = register
        #: Session id, as the hook payload reports it, to the target this engine
        #: holds a registration for — or `None`, which is the authority check.
        self._resolve = resolve
        self._emit = emit
        self._clock = clock
        self._path = approval_socket_path(settings.socket_directory, pid or os.getpid())
        self._server: asyncio.Server | None = None
        self._waiting: dict[str, _Waiting] = {}
        #: Requests whose hook went away before a verdict reached it — the human
        #: at the keyboard won the race. Kept so a verdict arriving a moment
        #: later can be told which race it lost instead of "no such request".
        self._answered_elsewhere: set[str] = set()
        #: A released question remains a closed route until discovery observes
        #: the Session move on. This prevents a late Answer Relay falling
        #: through to the ordinary inbox as a new turn.
        self._released_questions: dict[SessionTarget, str] = {}

    @property
    def path(self) -> Path:
        return self._path

    @property
    def listening(self) -> bool:
        return self._server is not None

    def pending(self) -> tuple[ApprovalRequest, ...]:
        """Every permission currently parked on this socket, in arrival order."""
        return tuple(
            waiting.permission
            for waiting in self._waiting.values()
            if waiting.permission is not None
        )

    def newest_for(self, target: SessionTarget) -> ApprovalRequest | None:
        """The dialog one exact Session is held up on, or `None` for none.

        The newest, because a Session that raised two is held up on the one it
        raised last. Keyed by the exact target rather than the session id:
        `--resume` forks two processes under one id, and a dialog belongs to one
        of them.

        Asked of this listener rather than filtered out of `pending()` by a
        caller, because which parked dialog belongs to a Session is a question
        about what this listener is holding.
        """
        for waiting in reversed(self._waiting.values()):
            if waiting.target == target:
                return waiting.permission
        return None

    def newest_question_for(self, target: SessionTarget) -> WaitingFor | None:
        """The question one exact Session's newest parked dialog asked, if it is one.

        `None` covers both "nothing is parked for that Session" and "what is
        parked is a permission" — a Session held up on a permission is not held
        up on a question, whatever older dialog is still on this socket.

        The newest, and keyed by the exact target, for `newest_for`'s reasons.
        """
        held = self.held_question_for(target)
        return held[1] if held is not None else None

    def held_question_for(self, target: SessionTarget) -> tuple[str, WaitingFor] | None:
        """The listener-private key and question for one target's newest held writer.

        The key is Claude's `prompt_id` when the wire supplied one, otherwise an
        opaque UUID minted by this listener. The generated value never replaces
        `WaitingFor.approval_id`, whose value remains exactly what Claude sent.
        """
        for question_id, waiting in reversed(self._waiting.items()):
            if waiting.target != target:
                continue
            if waiting.question is None:
                return None
            return question_id, waiting.question
        return None

    def question_answerable(self, target: SessionTarget) -> bool:
        """Whether this listener still holds the exact question route."""
        return self.held_question_for(target) is not None

    def released_question_reason(self, target: SessionTarget) -> str | None:
        """Why the latest question route closed, while its waiting row remains."""
        return self._released_questions.get(target)

    def clear_released_question(self, target: SessionTarget) -> None:
        """Forget a closed route after discovery proves the Session moved on."""
        self._released_questions.pop(target, None)

    async def start(self) -> None:
        """Bind the socket, in a directory only this user can enter."""
        if self._server is not None:
            return
        try:
            verify_bindable_length(self._path)
            prepare_private_directory(self._path.parent)
        except ChannelPathError as refused:
            raise ApprovalError(str(refused)) from None
        # Our own directory, named after our own pid, so anything at that exact
        # path is this engine's leftover and nobody else's live socket.
        with contextlib.suppress(OSError):
            self._path.unlink()
        try:
            self._server = await start_private_unix_server(
                self._serve, self._path, mode=PRIVATE_SOCKET_MODE, limit=MAX_HOOK_REQUEST_BYTES
            )
        except OSError as refused:
            self._server = None
            raise ApprovalError(
                f"cannot bind the approval socket at {self._path}: {refused}"
            ) from None

    async def aclose(self) -> None:
        """Stop listening, release every parked dialog to its human, tidy up.

        Releasing with `ask` rather than simply dropping the connections is the
        difference between a dialog that is handed back and one that waits out
        Claude Code's whole hook budget staring at a socket nobody is on.

        Nothing is waited for here: this is a shutdown, the receipt would have
        nobody to go to, and a hook that has already gone is exactly the case
        this is trying not to block on.
        """
        server, self._server = self._server, None
        for approval_id, waiting in list(self._waiting.items()):
            self._waiting.pop(approval_id, None)
            with contextlib.suppress(OSError, ConnectionError):
                await self._write_verdict(waiting, ApprovalVerdict.ASK)
            self._question_released(waiting)
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        self._waiting.clear()
        self._answered_elsewhere.clear()
        self._released_questions.clear()
        with contextlib.suppress(OSError):
            self._path.unlink()
        with contextlib.suppress(OSError):
            self._path.parent.rmdir()

    async def answer(
        self, approval_id: str, verdict: ApprovalVerdict, *, request_id: RequestId
    ) -> DeliveryReceipt:
        """Carry one verdict down the connection the dialog is waiting on.

        **The grade follows the bytes, and then follows the hook.** Handing the
        verdict to something that will write it later and reporting DELIVERED
        would be reporting a plan; DELIVERED means positively proven to have
        arrived, and nothing less may claim it. So the write happens here, and
        then this waits — bounded — for the hook's end of the socket to close,
        which on this wire is the proof that the hook read the line and left to
        print it. A write that succeeds and a hook that never goes is UNKNOWN:
        the words are in its buffer and nothing says it acted on them.

        `ask` is graded HELD however that goes, and the word is exact — the
        on-screen dialog still holds the request. It is written all the same, so
        the hook stops waiting and prints nothing: the difference between handing
        a dialog back and abandoning a process on a socket.

        Popping first is what makes this exactly once, and what makes a second
        verdict for the same dialog a refusal rather than a second write.
        """
        return await self._answer(
            approval_id,
            verdict,
            request_id=request_id,
            message=None,
            question_only=False,
        )

    async def answer_question(
        self, question_id: str, words: str, *, request_id: RequestId
    ) -> DeliveryReceipt:
        """Carry the user's words into the exact question hook still parked."""
        waiting = self._waiting.get(question_id)
        if waiting is None or waiting.question is None:
            if question_id in self._answered_elsewhere:
                return _failed(request_id, "that question was answered elsewhere")
            return _failed(request_id, f"no question {question_id} is answerable on this Session")
        collapsed = " ".join(words.split())
        canonical = next(
            (
                option.text
                for option in waiting.question.options
                if " ".join(option.text.split()).casefold() == collapsed.casefold()
            ),
            words,
        )
        return await self._answer(
            question_id,
            ApprovalVerdict.DENY,
            request_id=request_id,
            message=QUESTION_ANSWER_PREFIX + canonical,
            question_only=True,
        )

    async def _answer(
        self,
        approval_id: str,
        verdict: ApprovalVerdict,
        *,
        request_id: RequestId,
        message: str | None,
        question_only: bool,
    ) -> DeliveryReceipt:
        """Pop one parked hook first, then carry one framed wire decision."""
        waiting = self._waiting.pop(approval_id, None)
        if waiting is None:
            if approval_id in self._answered_elsewhere:
                return _failed(request_id, "the on-screen dialog already answered that request")
            return _failed(
                request_id,
                f"no permission request {approval_id} is waiting on this Session",
            )
        if question_only and waiting.question is None:
            self._waiting[approval_id] = waiting
            return _failed(request_id, f"{approval_id} is a permission, not a question")
        if not question_only and waiting.question is not None:
            self._waiting[approval_id] = waiting
            return _failed(request_id, f"{approval_id} is a question, not a permission")
        self._question_released(waiting)

        held = DeliveryReceipt(
            request_id=request_id,
            outcome=Delivery.HELD,
            reason="handed back to the on-screen dialog, which still holds it",
        )
        try:
            await self._write_verdict(waiting, verdict, message=message)
        except (OSError, ConnectionError) as broken:
            self._answered_elsewhere.add(approval_id)
            if verdict is ApprovalVerdict.ASK:
                # Nothing was owed to the hook here: `ask` asks it to do nothing,
                # and a hook that has already gone has already done it.
                return held
            return _failed(request_id, f"the hook holding that dialog went away: {broken}")

        acknowledged = await self._hook_acknowledged(waiting)
        if verdict is ApprovalVerdict.ASK:
            return held
        if not acknowledged:
            return _unknown(
                request_id,
                "the verdict was written but the hook never acknowledged it, so nothing "
                "proves the dialog was answered rather than abandoned",
            )
        return DeliveryReceipt(request_id=request_id, outcome=Delivery.DELIVERED)

    async def _write_verdict(
        self, waiting: _Waiting, verdict: ApprovalVerdict, *, message: str | None = None
    ) -> None:
        """Put one verdict on one hook's connection. Raises if it did not go."""
        frame: dict[str, Any] = {TYPE_FIELD: VERDICT_TYPE, VERDICT_FIELD: str(verdict)}
        if message is not None:
            frame[MESSAGE_FIELD] = message
        await self._reply(waiting.writer, frame)
        waiting.answered.set()

    async def sweep_question_budget(
        self, budget_seconds: float
    ) -> tuple[tuple[SessionTarget, WaitingFor], ...]:
        """Pop and release every held question past Core's configured budget."""
        now = self._clock()
        expired = [
            (approval_id, waiting)
            for approval_id, waiting in self._waiting.items()
            if waiting.question is not None and waiting.parked_at + budget_seconds <= now
        ]
        released: list[tuple[SessionTarget, WaitingFor]] = []
        for approval_id, waiting in expired:
            self._waiting.pop(approval_id, None)
            question = waiting.question
            assert question is not None
            with contextlib.suppress(OSError, ConnectionError):
                await self._write_verdict(waiting, ApprovalVerdict.ASK)
            self._question_released(waiting)
            released.append((waiting.target, question))
        return tuple(released)

    def _question_released(
        self,
        waiting: _Waiting,
        *,
        reason: str = "that question is no longer answerable from here; answer it in the terminal",
    ) -> None:
        if waiting.question is None:
            return
        target = waiting.target
        self._released_questions[target] = reason
        self._emit(ReplyWindowChanged(target=target, window=ReplyWindow.CLOSED))

    async def _hook_acknowledged(self, waiting: _Waiting) -> bool:
        """Wait, bounded, for the hook's own receipt. Nothing else is a proof.

        The wait ends on the receipt, on the connection closing, or on the budget
        — and only the first of those is an answer. A connection that ends is
        what stops this waiting for something that is never coming; it says
        nothing about whether the verdict arrived, because the close may have
        been in flight before the verdict was written.
        """
        waiter = asyncio.ensure_future(
            asyncio.wait(
                (
                    asyncio.ensure_future(waiting.acknowledged.wait()),
                    asyncio.ensure_future(waiting.gone.wait()),
                ),
                return_when=asyncio.FIRST_COMPLETED,
            )
        )
        try:
            done, pending = await asyncio.wait_for(
                waiter, timeout=self._settings.request_timeout_seconds
            )
        except TimeoutError:
            return waiting.acknowledged.is_set()
        for task in pending:
            task.cancel()
        for task in done:
            task.cancel()
        return waiting.acknowledged.is_set()

    # -- one hook process, from its dial to its decision ------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One parked dialog's whole life. Every exit that is not a verdict is silence."""
        approval_id: str | None = None
        try:
            payload = await self._read_request(reader)
            if payload is None:
                return
            if payload.get(TYPE_FIELD) == REGISTRATION_TYPE:
                await self._registered(payload, writer)
                return
            target = self._target_for(payload)
            if target is None:
                await self._refuse(writer, "no Session this engine holds reported that dialog")
                return

            question = question_from(payload)
            if question is not None:
                approval_id = question.approval_id or str(uuid.uuid4())
                request = None
            else:
                approval_id = str(uuid.uuid4())
                request = request_from(payload, target=target, approval_id=approval_id)
            waiting = _Waiting(
                target,
                writer,
                permission=request,
                question=question,
                parked_at=self._clock(),
            )
            self._waiting[approval_id] = waiting
            if question is not None:
                self._released_questions.pop(target, None)

            # Raised only once the request is parked, so a verdict answered the
            # same tick has somewhere to land. A question never enters the
            # Approval Relay. This held writer privately addresses the next
            # Answer Relay, using Claude's prompt id or this listener's opaque
            # fallback key.
            if question is None:
                assert request is not None
                self._emit(AwaitingApproval(request=request))
            if question is not None:
                self._emit(ReplyWindowChanged(target=target, window=ReplyWindow.OPEN))
            if question is not None and question.approval_id is not None:
                _log.info(
                    "a question is parked for %s (prompt_id=%s); Reply Window opened",
                    target,
                    question.approval_id,
                )
            elif question is not None:
                _log.info(
                    "a question is parked for %s without a prompt_id; "
                    "engine-private correlator=%s; Reply Window opened",
                    target,
                    approval_id,
                )

            # From here this task does exactly one thing: watch for the hook's
            # end of the socket to close. The verdict is written by `answer`, on
            # whichever task Bridge Core called it from, because the grade it
            # returns has to follow the bytes rather than precede them.
            #
            # The end arrives for one of two reasons and they mean opposite
            # things: after a verdict, it is the hook reading the line and
            # leaving to print it — the only positive proof this wire offers.
            # Before one, it is the human answering the dialog on screen and
            # Claude Code cancelling the hook.
            acknowledged = await self._await_ack(reader)
            if acknowledged:
                waiting.acknowledged.set()
            waiting.gone.set()
            if not waiting.answered.is_set():
                self._answered_elsewhere.add(approval_id)
                self._question_released(
                    waiting,
                    reason="that question was answered elsewhere at the on-screen dialog",
                )
                _log.info("approval %s left with its dialog before a verdict arrived", approval_id)
        except (OSError, ConnectionError, ValueError) as broken:
            _log.info("an approval hook connection failed: %s", broken)
        finally:
            if approval_id is not None:
                self._waiting.pop(approval_id, None)
            writer.close()
            with contextlib.suppress(OSError, ConnectionError):
                await writer.wait_closed()

    def _target_for(self, payload: Mapping[str, Any]) -> SessionTarget | None:
        """The authority check: only a Session this engine registered is answerable.

        A hook that reached us for anything else is refused rather than served,
        which is what keeps a foreign dialog from becoming an event Bridge Core
        announces to a user who never launched it.
        """
        session_id = payload.get(SESSION_ID_FIELD)
        if not isinstance(session_id, str) or not session_id.strip():
            return None
        return self._resolve(session_id.strip())

    async def _await_ack(self, reader: asyncio.StreamReader) -> bool:
        """Read until the hook says it has the verdict, or until it is gone.

        Unbounded on purpose, like everything else on this connection: the hook
        is parked for as long as Bridge Core's budget lasts, and a timer here
        would be a second budget. The connection ending is what ends it.
        """
        while True:
            try:
                line = await reader.readline()
            except (OSError, ConnectionError, ValueError):
                return False
            if not line:
                return False
            try:
                document: Any = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(document, dict) and document.get(TYPE_FIELD) == ACK_TYPE:
                return True

    async def _read_request(self, reader: asyncio.StreamReader) -> dict[str, Any] | None:
        """The hook's one line, or `None` for anything this socket does not speak."""
        try:
            line = await asyncio.wait_for(
                reader.readline(), timeout=self._settings.request_timeout_seconds
            )
        except TimeoutError:
            _log.info("a connection to the approval socket sent nothing in time")
            return None
        except ValueError:
            # `readline` refuses a line past the reader's buffer. The buffer is
            # set to this module's own cap at bind time, so this is the cap being
            # spent rather than a smaller default being hit — but it is caught by
            # name because the difference between those two was a real defect.
            _log.info("a connection to the approval socket sent a line past the cap")
            return None
        if not line or len(line) > MAX_HOOK_REQUEST_BYTES:
            return None
        try:
            document: Any = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as unreadable:
            _log.info("the approval socket was sent invalid JSON: %s", unreadable)
            return None
        if not isinstance(document, dict) or document.get(TYPE_FIELD) not in (
            REQUEST_TYPE,
            REGISTRATION_TYPE,
        ):
            _log.info("the approval socket was sent something it does not speak")
            return None
        return document

    async def _registered(self, payload: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        """Record one Session's own report of where it can be reached.

        **It adds no row.** The roster comes from `claude agents --json`, which
        sees every Session in this config directory whether or not its hook ran
        — including the ones that started before this engine did. What this adds
        is the two things that command does not carry: the Session's inbox
        socket, and the `transcript_path` #75 and #76 read.
        """
        if self._register is not None:
            self._register(payload)
        # This branch owns its own ending. `SessionStart` runs before the Session
        # is usable, so `registration.tell_engine` sends one line, half-closes and
        # leaves without reading (ADR-0011) — the acknowledgement is offered, and
        # finding nobody there is the ordinary way this branch ends, not a
        # failure. Letting it reach `_serve`'s arm, which exists for parked
        # approval dialogs, labelled every healthy registration as an approval
        # failure (#207) and misdirected #200's first diagnosis.
        try:
            await self._reply(writer, {TYPE_FIELD: REGISTERED_TYPE})
        except (OSError, ConnectionError) as gone:
            _log.debug(
                "the session that registered left before reading the acknowledgement: %s", gone
            )

    async def _refuse(self, writer: asyncio.StreamWriter, reason: str) -> None:
        await self._reply(writer, {TYPE_FIELD: REFUSAL_TYPE, REASON_FIELD: reason})

    async def _reply(self, writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
        payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        writer.write(payload + b"\n")
        await writer.drain()


def _failed(request_id: RequestId, reason: str) -> DeliveryReceipt:
    return DeliveryReceipt(request_id=request_id, outcome=Delivery.FAILED, reason=reason)


def _unknown(request_id: RequestId, reason: str) -> DeliveryReceipt:
    return DeliveryReceipt(request_id=request_id, outcome=Delivery.UNKNOWN, reason=reason)
