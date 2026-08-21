"""The Approval Relay: a hook process holds the dialog open, and we answer into it.

**The route is a `PermissionRequest` hook, and the hook is the wire.** Claude Code
runs it when — and only when — a permission dialog is displayed, hands it the
tool and its input on stdin, and waits up to its own budget for the process to
print a decision. So the hook is not a notifier that reports and leaves: it is a
held-open connection whose *return value* is the verdict, and the whole design
falls out of that. The hook dials this listener, this listener parks it, Bridge
Core takes as long as the user needs, and the answer travels back down the same
connection the dialog is still waiting on.

**`ask` is silence, and that is a wire fact rather than a preference.** Read out
of Claude Code 2.1.238: both the interactive and the headless permission paths
consume `hookSpecificOutput.decision` and treat `behavior === "allow"` as allow
and **anything else as deny**; a hook that emits no decision falls through to the
on-screen dialog. There is no `behavior: "ask"`, and emitting one would be a
denial wearing the wrong word. Handing a request back is therefore implemented by
printing nothing at all — the same shape the Codex spoke's approvals module
arrived at independently, for the same reason.

The ticket's own wording, "output is `allow` / `deny` / `ask`", is true at the
seam and false at the wire. Both stay: `ApprovalVerdict` keeps three members
because Bridge Core has three things to say, and exactly one of them is said by
saying nothing.

**Nothing here has a clock.** The hook waits for this listener and this listener
waits for Bridge Core, whose `approval_budget_seconds` is the one budget in the
system; expiry arrives as an ordinary `ASK`, carried down the connection like any
other verdict. Claude Code's own default hook budget happens to be 600 s, the
same number `CorePolicy` defaults to — a coincidence worth knowing and not a
constant to mirror, because two copies of one number are two numbers.

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
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

from gpt_voicecoding.adapters.agent.claude.privacy import (
    PRIVATE_SOCKET_MODE,
    ChannelPathError,
    prepare_private_directory,
    verify_bindable_length,
)
from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings
from gpt_voicecoding.seams.agent import (
    AgentEvent,
    ApprovalRequest,
    ApprovalVerdict,
    AwaitingApproval,
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


def hook_decision(verdict: ApprovalVerdict) -> dict[str, Any] | None:
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
        else {"behavior": DENY_BEHAVIOR, "message": DENIED_BY_VOICE}
    )
    return {"hookSpecificOutput": {"hookEventName": HOOK_EVENT, "decision": decision}}


# -- the wire between the hook process and this engine -------------------

#: One request in, one answer back, and nothing else is speakable on this socket.
#: A closed grammar on purpose: this is a machine-facing wire reached from a
#: process Claude Code starts, and a generic request channel there would be a
#: second control plane nobody designed.
REQUEST_TYPE: Final = "approval_request"
VERDICT_TYPE: Final = "approval_verdict"
REFUSAL_TYPE: Final = "approval_refused"

#: The hook's receipt, and the only positive proof this route has.
#:
#: It exists because the obvious cheaper proof is not one. Grading a verdict
#: DELIVERED on the connection ending looks sound — the hook reads the line and
#: exits — but the end of a connection has two causes and they mean opposite
#: things: the hook leaving with its verdict, and the human answering the dialog
#: on screen, after which Claude Code cancels the hook. A close that was already
#: in flight when the verdict was written is indistinguishable from a close the
#: verdict caused, so an engine reading EOF as a receipt reports "approved by
#: voice" for a tool call that never ran. The hook says so explicitly instead.
ACK_TYPE: Final = "approval_ack"

TYPE_FIELD: Final = "type"
SESSION_ID_FIELD: Final = "session_id"
CWD_FIELD: Final = "cwd"
TOOL_NAME_FIELD: Final = "tool_name"
TOOL_INPUT_FIELD: Final = "tool_input"
VERDICT_FIELD: Final = "verdict"
REASON_FIELD: Final = "reason"

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


def summary_of(tool_input: Any) -> str:
    """One line saying what is actually about to happen, in the tool's own words.

    The fields are tried in the order that puts the most specific thing first: a
    shell command says everything, a path says most of it, and a description is
    the tool's own summary of itself. Anything else is left to the tool name,
    which the announcement already carries — inventing a sentence out of an
    arbitrary input object would be guessing at the user's risk.

    **It is not shortened here.** How long a thing may be before it is read aloud
    or pushed to a phone is a presentation decision, and presentation decisions
    are Bridge Core's (ADR 0001). The Codex spoke's equivalent takes the same
    position; an adapter that trimmed would be two components deciding one thing.
    """
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "file_path", "path", "url", "description"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return ""


def request_from(
    payload: Mapping[str, Any], *, target: SessionTarget, approval_id: str
) -> ApprovalRequest:
    """One displayed permission dialog, as the Agent seam describes it.

    `options` stays empty, and that is the honest report: this route offers
    allow and deny and nothing else, so there is no ready-made menu to read out.
    `permission_suggestions` is deliberately not consulted — every suggestion it
    carries is a *rule*, and a rule outlives the one call the user was asked
    about.
    """
    tool_name = payload.get(TOOL_NAME_FIELD)
    return ApprovalRequest(
        approval_id=approval_id,
        target=target,
        tool_name=tool_name if isinstance(tool_name, str) and tool_name.strip() else "a tool",
        detail=summary_of(payload.get(TOOL_INPUT_FIELD)),
    )


class ApprovalError(Exception):
    """The approval socket could not be bound, or could not be taken back out."""


class _Waiting:
    """One hook process, parked mid-dialog, and the connection it is holding open."""

    __slots__ = ("acknowledged", "answered", "gone", "request", "writer")

    def __init__(self, request: ApprovalRequest, writer: asyncio.StreamWriter) -> None:
        self.request = request
        self.writer = writer
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
    ) -> None:
        self._settings = settings
        #: Session id, as the hook payload reports it, to the target this engine
        #: holds a registration for — or `None`, which is the authority check.
        self._resolve = resolve
        self._emit = emit
        self._path = approval_socket_path(settings.socket_directory, pid or os.getpid())
        self._server: asyncio.Server | None = None
        self._waiting: dict[str, _Waiting] = {}
        #: Requests whose hook went away before a verdict reached it — the human
        #: at the keyboard won the race. Kept so a verdict arriving a moment
        #: later can be told which race it lost instead of "no such request".
        self._answered_elsewhere: set[str] = set()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def listening(self) -> bool:
        return self._server is not None

    def pending(self) -> tuple[ApprovalRequest, ...]:
        """Every dialog currently parked on this socket, in arrival order."""
        return tuple(waiting.request for waiting in self._waiting.values())

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
            self._server = await asyncio.start_unix_server(
                self._serve, path=str(self._path), limit=MAX_HOOK_REQUEST_BYTES
            )
            os.chmod(self._path, PRIVATE_SOCKET_MODE)
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
        for waiting in list(self._waiting.values()):
            with contextlib.suppress(OSError, ConnectionError):
                await self._write_verdict(waiting, ApprovalVerdict.ASK)
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        self._waiting.clear()
        self._answered_elsewhere.clear()
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
        waiting = self._waiting.pop(approval_id, None)
        if waiting is None:
            if approval_id in self._answered_elsewhere:
                return _failed(request_id, "the on-screen dialog already answered that request")
            return _failed(
                request_id,
                f"no permission request {approval_id} is waiting on this Session",
            )

        held = DeliveryReceipt(
            request_id=request_id,
            outcome=Delivery.HELD,
            reason="handed back to the on-screen dialog, which still holds it",
        )
        try:
            await self._write_verdict(waiting, verdict)
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

    async def _write_verdict(self, waiting: _Waiting, verdict: ApprovalVerdict) -> None:
        """Put one verdict on one hook's connection. Raises if it did not go."""
        await self._reply(waiting.writer, {TYPE_FIELD: VERDICT_TYPE, VERDICT_FIELD: str(verdict)})
        waiting.answered.set()

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
            target = self._target_for(payload)
            if target is None:
                await self._refuse(writer, "no Session this engine holds reported that dialog")
                return

            approval_id = str(uuid.uuid4())
            request = request_from(payload, target=target, approval_id=approval_id)
            waiting = _Waiting(request, writer)
            self._waiting[approval_id] = waiting

            # Raised only once the request is parked, so a verdict answered the
            # same tick has somewhere to land.
            self._emit(AwaitingApproval(request=request))

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
        if not isinstance(document, dict) or document.get(TYPE_FIELD) != REQUEST_TYPE:
            _log.info("the approval socket was sent something it does not speak")
            return None
        return document

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
