"""Fake adapters behind every seam.

ADR 0001, principle 4: all of Bridge Core must be exercisable with a fake call,
fake agents and a fake channel — no network, no audio, no real adapter. These are
that fake set, kept honest rather than convenient:

- they answer with the same four-state vocabulary a real adapter must use;
- they refuse what a real adapter must refuse (an unsupported route, speaking
  into a call that is not up, a repeated launch request id, closing an identity
  that was never launched);
- they record what they were asked to do, so a policy test can assert that Bridge
  Core actually called out rather than merely deciding to.

They live in `tests/` on purpose: they are a testing tool, not a shipped null
adapter. The Companion Channel's *null implementation* is a different thing — a
real implementation of the seam that reports honestly — and belongs to that
adapter's issue.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gpt_voicecoding.seams.agent import (
    ApprovalRequest,
    ApprovalVerdict,
    RelayRoute,
)
from gpt_voicecoding.seams.call import CallSnapshot, CallState, DelegatedReply
from gpt_voicecoding.seams.delivery import Delivery, DeliveryReceipt
from gpt_voicecoding.seams.events import Event, EventSink
from gpt_voicecoding.seams.identity import RequestId, SessionTarget
from gpt_voicecoding.seams.session_launcher import (
    CloseOutcome,
    CloseRequest,
    CloseStatus,
    LaunchOutcome,
    LaunchRequest,
    LaunchStatus,
)
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

    async def notice_relay(
        self, target: SessionTarget, text: str, *, request_id: RequestId
    ) -> DeliveryReceipt:
        self.calls.append(
            RelayCall(verb="notice_relay", target=target, request_id=request_id, text=text)
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
        self._snapshot = CallSnapshot(state=CallState.DOWN)
        #: How many calls this adapter actually brought up. A policy test asserts
        #: on it to prove the one-call invariant stopped a second one.
        self.calls_started = 0

    async def ensure_call(self) -> CallSnapshot:
        if self._snapshot.is_up:
            return self._snapshot
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

    async def delegate(self, text: str, *, model: str, request_id: RequestId) -> DelegatedReply:
        self.delegated.append((text, model))
        return DelegatedReply(text=self.delegated_text, model=model)

    async def verify(self) -> VerifyResult:
        return self.verify_result


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


@dataclass
class FakeSessionLauncher:
    """A Launcher that mints one child per request id and closes idempotently."""

    targets: list[SessionTarget] = field(default_factory=list)
    available: bool = True
    verify_result: VerifyResult | None = None
    sink: EventSink | None = None

    def __post_init__(self) -> None:
        self.launched: dict[RequestId, LaunchOutcome] = {}
        self.opened: set[SessionTarget] = set()
        self.closed: set[SessionTarget] = set()
        self.environments: list[dict[str, str]] = []

    async def launch(self, request: LaunchRequest) -> LaunchOutcome:
        if not self.available:
            return LaunchOutcome(
                request_id=request.request_id,
                status=LaunchStatus.UNAVAILABLE,
                detail="this fake launcher was told it cannot run here",
            )
        if request.request_id in self.launched:
            return self.launched[request.request_id]
        if not self.targets:
            outcome = LaunchOutcome(
                request_id=request.request_id,
                status=LaunchStatus.FAILED,
                detail="this fake launcher has no target left to hand out",
            )
        else:
            self.environments.append(dict(request.env))
            target = self.targets.pop(0)
            self.opened.add(target)
            outcome = LaunchOutcome(
                request_id=request.request_id,
                status=LaunchStatus.LAUNCHED,
                target=target,
            )
        self.launched[request.request_id] = outcome
        return outcome

    async def close(self, request: CloseRequest) -> CloseOutcome:
        if not self.available:
            return CloseOutcome(
                request_id=request.request_id,
                status=CloseStatus.UNAVAILABLE,
                detail="this fake launcher was told it cannot run here",
            )
        if request.target in self.closed:
            return CloseOutcome(request_id=request.request_id, status=CloseStatus.ALREADY_CLOSED)
        if request.target not in self.opened:
            return CloseOutcome(
                request_id=request.request_id,
                status=CloseStatus.FAILED,
                detail=f"this launcher never launched {request.target}",
            )
        self.closed.add(request.target)
        return CloseOutcome(request_id=request.request_id, status=CloseStatus.CLOSED)

    async def verify(self) -> VerifyResult:
        return self.verify_result or VerifyResult(
            outcome=VerifyOutcome.PASS, loaded="tests.fakes.FakeSessionLauncher"
        )
