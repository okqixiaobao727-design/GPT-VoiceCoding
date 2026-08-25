"""Fake adapters behind every seam.

ADR 0001, principle 4: all of Bridge Core must be exercisable with a fake call,
fake agents and a fake channel — no network, no audio, no real adapter. These are
that fake set, kept honest rather than convenient:

- they answer with the same four-state vocabulary a real adapter must use;
- they refuse what a real adapter must refuse (an unsupported route, speaking
  into a call that is not up, a Relay into a Session nothing registered);
- they record what they were asked to do, so a policy test can assert that Bridge
  Core actually called out rather than merely deciding to.

They live in `tests/` on purpose: they are a testing tool, not a shipped null
adapter. The Companion Channel's *null implementation* is a different thing — a
real implementation of the seam that reports honestly — and belongs to that
adapter's issue.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from gpt_voicecoding.core.instructions import ControlPlaneCli, InstructionContext
from gpt_voicecoding.seams.agent import (
    ApprovalRequest,
    ApprovalVerdict,
    RelayRoute,
    ReplyWindow,
)
from gpt_voicecoding.seams.call import CallSnapshot, CallState, DelegatedReply
from gpt_voicecoding.seams.delivery import Delivery, DeliveryReceipt
from gpt_voicecoding.seams.events import Event, EventSink
from gpt_voicecoding.seams.identity import RequestId, SessionTarget
from gpt_voicecoding.seams.verify import VerifyOutcome, VerifyResult


class RecordingSink:
    """An `EventSink` that keeps what it was given, in order."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)


@dataclass(frozen=True, slots=True)
class RelayCall:
    """One thing a fake agent was asked to carry."""

    verb: str
    target: SessionTarget
    request_id: RequestId
    text: str = ""
    route: RelayRoute | None = None
    verdict: ApprovalVerdict | None = None


class FakeAgent:
    """An Agent adapter that carries nothing and reports honestly about it."""

    def __init__(
        self,
        *,
        routes: frozenset[RelayRoute] = frozenset({RelayRoute.DELIVER}),
        outcome: Delivery = Delivery.DELIVERED,
        reason: str = "fake adapter",
        verify_result: VerifyResult | None = None,
        sink: EventSink | None = None,
    ) -> None:
        self._routes = routes
        self.outcome = outcome
        self.reason = reason
        self.verify_result = verify_result or VerifyResult(
            outcome=VerifyOutcome.PASS, loaded="tests.fakes.FakeAgent"
        )
        self.sink = sink
        self.calls: list[RelayCall] = []
        #: What this fake answers `reply_window` with, per target. Anything not
        #: named here is CLOSED.
        self.windows: dict[SessionTarget, ReplyWindow] = {}
        #: Every target the hub asked about, in order, so a test can prove the
        #: level was pulled at all rather than infer it from the result.
        self.asked_windows: list[SessionTarget] = []

    def supported_routes(self) -> frozenset[RelayRoute]:
        return self._routes

    async def answer_relay(
        self,
        target: SessionTarget,
        text: str,
        *,
        request_id: RequestId,
        route: RelayRoute = RelayRoute.DELIVER,
    ) -> DeliveryReceipt:
        self.calls.append(
            RelayCall(
                verb="answer_relay", target=target, request_id=request_id, text=text, route=route
            )
        )
        if route not in self._routes:
            return DeliveryReceipt(
                request_id=request_id,
                outcome=Delivery.FAILED,
                reason=f"the {route} route is not available on this adapter",
            )
        return self._receipt(request_id)

    async def approval_relay(
        self, request: ApprovalRequest, verdict: ApprovalVerdict, *, request_id: RequestId
    ) -> DeliveryReceipt:
        self.calls.append(
            RelayCall(
                verb="approval_relay",
                target=request.target,
                request_id=request_id,
                verdict=verdict,
            )
        )
        return self._receipt(request_id)

    def reply_window(self, target: SessionTarget) -> ReplyWindow:
        """Whatever a test told this fake to answer. CLOSED unless it was told.

        Fail-closed by default so a hub asking this fake at registration lands on
        the same starting level it had before the seam gained the verb, and no
        test inherits an open window it never asked for.
        """
        self.asked_windows.append(target)
        return self.windows.get(target, ReplyWindow.CLOSED)

    async def verify(self) -> VerifyResult:
        return self.verify_result

    def _receipt(self, request_id: RequestId) -> DeliveryReceipt:
        return DeliveryReceipt(request_id=request_id, outcome=self.outcome, reason=self.reason)


class FakeCall:
    """A Call adapter that holds a call and knows nothing about the one-call rule."""

    def __init__(
        self,
        *,
        delegated_text: str = "the delegated answer",
        reachable: bool = True,
        verify_result: VerifyResult | None = None,
        sink: EventSink | None = None,
    ) -> None:
        self.delegated_text = delegated_text
        #: False makes every attempt stall at CONNECTING — a call that never
        #: comes up, which is not the same as one that came up and went away.
        self.reachable = reachable
        self.verify_result = verify_result or VerifyResult(
            outcome=VerifyOutcome.PASS, loaded="tests.fakes.FakeCall"
        )
        self.sink = sink
        self.spoken: list[str] = []
        self.delegated: list[tuple[str, str]] = []
        #: The house rules every call was opened on, in order. Bridge Core is
        #: the only source of these, so a test can prove they came from it.
        self.opened_on: list[str] = []
        #: The same, for the threads Delegated Turns run on.
        self.delegated_on: list[str] = []
        self._snapshot = CallSnapshot(state=CallState.DOWN)
        #: How many calls this adapter actually brought up. A policy test asserts
        #: on it to prove the one-call invariant stopped a second one.
        self.calls_started = 0

    async def ensure_call(self, instructions: str) -> CallSnapshot:
        if self._snapshot.is_up:
            return self._snapshot
        self.opened_on.append(instructions)
        if not self.reachable:
            self._snapshot = CallSnapshot(state=CallState.CONNECTING)
            return self._snapshot
        self.calls_started += 1
        self._snapshot = CallSnapshot(state=CallState.UP, call_id=f"call-{self.calls_started}")
        return self._snapshot

    async def end_call(self) -> CallSnapshot:
        self._snapshot = CallSnapshot(state=CallState.DOWN)
        return self._snapshot

    async def call_state(self) -> CallSnapshot:
        return self._snapshot

    async def speak(self, text: str, *, request_id: RequestId) -> DeliveryReceipt:
        if not self._snapshot.is_up:
            return DeliveryReceipt(
                request_id=request_id,
                outcome=Delivery.FAILED,
                reason="no call is up to speak into",
            )
        self.spoken.append(text)
        return DeliveryReceipt(
            request_id=request_id, outcome=Delivery.DELIVERED, reason="spoken into the call"
        )

    async def delegate(
        self, text: str, *, model: str, instructions: str, request_id: RequestId
    ) -> DelegatedReply:
        self.delegated.append((text, model))
        self.delegated_on.append(instructions)
        return DelegatedReply(text=self.delegated_text, model=model)

    async def verify(self) -> VerifyResult:
        return self.verify_result


class UnreachableFarSide(Exception):
    """What an adapter raises when the thing behind it is not there.

    Deliberately **not** an `OSError`: the shipped adapters raise their own
    exception types — `AppServerError` when `codex` is not on `PATH`, for one —
    and a runner that only caught `OSError` turned those into an exit code of 1
    and a traceback instead of the refusal it promises.
    """


class RefusingCall(FakeCall):
    """A Call adapter whose `connect` fails the way a real one does."""

    async def connect(self) -> None:
        raise UnreachableFarSide("the far side of this seam is not there")

    async def aclose(self) -> None:
        return None


class AdapterSettingsRefused(Exception):
    """What a settings-carrying adapter raises on a table it cannot accept.

    Its own type, like every shipped adapter's — the Telegram spoke raises
    `SettingsError` when the variable named by `token_env` is not set, which is
    the most likely thing to go wrong on anybody's first run.
    """


def unbuildable_call(**_: object) -> FakeCall:
    """A factory that refuses, the way an adapter refuses a settings table."""
    raise AdapterSettingsRefused("the bot token is read from $NOTHING_SETS_THIS")


class FakeCompanionChannel:
    """A Companion Channel that records pushes and can be told to fail."""

    def __init__(
        self,
        *,
        outcome: Delivery = Delivery.DELIVERED,
        reason: str = "fake channel",
        verify_result: VerifyResult | None = None,
        sink: EventSink | None = None,
    ) -> None:
        self.outcome = outcome
        self.reason = reason
        self.verify_result = verify_result or VerifyResult(
            outcome=VerifyOutcome.PASS, loaded="tests.fakes.FakeCompanionChannel"
        )
        self.sink = sink
        self.sent: list[str] = []

    async def send(self, text: str, *, request_id: RequestId) -> DeliveryReceipt:
        self.sent.append(text)
        return DeliveryReceipt(request_id=request_id, outcome=self.outcome, reason=self.reason)

    async def verify(self) -> VerifyResult:
        return self.verify_result


#: What a test passes where Bridge Core would pass generated house rules. Any
#: non-empty string will do: what the Call seam promises about instructions is
#: that they arrive from the hub at the call site, not what they say.
HOUSE_RULES = "the voice thread's house rules"


def instruction_context(
    *,
    command: Path = Path("/usr/bin/true"),
    socket_path: Path = Path("/tmp/gpt-voicecoding-tests/control.sock"),
) -> InstructionContext:
    """A generation context for a hub under test.

    Both fields are only required to be absolute, so nothing has to exist on
    disk. A hub built without one generates no instructions and therefore opens
    no call, which is correct and is its own test — it is just not what most of
    these tests are about.
    """
    return InstructionContext(
        cli=ControlPlaneCli(command=command, version="0", socket_path=socket_path),
    )
