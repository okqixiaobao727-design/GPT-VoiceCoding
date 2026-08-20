"""The Codex Agent adapter, against a scripted app-server.

The edge cases here are the ones the build issue named, because each of them is
a way a Relay can look successful and not be: a readback that never shows the
words, an app-server that dies with them in flight, a permission prompt whose
routing sends it to a subagent, and two Relays racing into one Session.

No real codex runs. The payload shapes are the ones codex 0.148.0's own
generated schema uses, and the behaviours the fake reproduces — the readback
receipt, the approval fan-out, `serverRequest/resolved`, the stale-turn refusal —
were each observed against a real app-server before being written down here.
"""

from __future__ import annotations

import asyncio
import itertools
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from codex_fake import FakeAppServer, FakeRemoteError
from gpt_voicecoding.adapters.agent.codex import codex_agent
from gpt_voicecoding.adapters.agent.codex.adapter import NOTICE_FRAME, CodexAgentAdapter
from gpt_voicecoding.adapters.agent.codex.approvals import voice_menu
from gpt_voicecoding.adapters.agent.codex.threads import ApprovalRouting
from gpt_voicecoding.adapters.codex_app_server.settings import CodexSettings, SettingsError
from gpt_voicecoding.seams.agent import (
    ApprovalVerdict,
    AwaitingApproval,
    RelayRoute,
    ReplyWindow,
    ReplyWindowChanged,
    SessionEnded,
    SessionStopped,
)
from gpt_voicecoding.seams.delivery import Delivery
from gpt_voicecoding.seams.identity import AgentKind, RequestId, SessionTarget

THREAD = "01a02110-d18f-74a0-916d-de1208e9977a"
TARGET = SessionTarget(agent=AgentKind.CODEX, session_id=THREAD)
TURN = "01a02110-d9ab-7763-a185-7079c7fbffe0"
APPROVAL = "item/commandExecution/requestApproval"

_names = itertools.count()


class Sink:
    """The event sink, recording what the adapter raised upward."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: Any) -> None:
        self.events.append(event)

    def of(self, kind: type) -> list[Any]:
        return [event for event in self.events if isinstance(event, kind)]


class Codex(FakeAppServer):
    """A fake app-server scripted to behave like one holding a live thread."""

    def script(
        self,
        *,
        thread_id: str = THREAD,
        approval_policy: str = "on-request",
        reviewer: str = "user",
        status: str = "idle",
    ) -> Codex:
        self.delivered: list[str] = []
        self.thread_id = thread_id
        self.reviewer = reviewer
        self.approval_policy = approval_policy
        self.status = status
        #: When False, `turn/start` records the words but the readback never
        #: shows them — the "no proof either way" case.
        self.readback_shows_words = True
        #: How many copies the readback claims. More than one contradicts.
        self.readback_copies = 1
        #: Whether `turn/start`'s approval override actually takes effect.
        self.honours_the_pin = True

        self.answers("initialize", {})
        self.answers(
            "thread/resume",
            lambda _p: {
                "thread": {"id": self.thread_id, "status": {"type": self.status}},
                "approvalPolicy": self.approval_policy,
                "approvalsReviewer": self.reviewer,
            },
        )
        self.answers("turn/start", self._turn_start)
        self.answers("turn/steer", self._turn_steer)
        self.answers("thread/read", self._thread_read)
        self.answers("thread/loaded/list", {"data": [thread_id]})
        return self

    def _turn_start(self, params: dict) -> dict:
        # A real server applies the turn's approval overrides to the thread —
        # unless `honours_the_pin` is off, which is the case that matters: the
        # request succeeds, the override is silently dropped, and only the
        # readback can tell. A fake that always applied it could never fail.
        if self.honours_the_pin:
            if params.get("approvalsReviewer"):
                self.reviewer = params["approvalsReviewer"]
            if params.get("approvalPolicy"):
                self.approval_policy = params["approvalPolicy"]
        self.delivered.append(params["clientUserMessageId"])
        return {"turn": {"id": TURN, "status": "inProgress"}}

    def _turn_steer(self, params: dict) -> dict:
        if params["expectedTurnId"] != TURN:
            raise FakeRemoteError(
                f"expected active turn id `{params['expectedTurnId']}` but found `{TURN}`"
            )
        self.delivered.append(params["clientUserMessageId"])
        return {"turnId": TURN}

    def _thread_read(self, _params: dict) -> dict:
        items = []
        if self.readback_shows_words:
            for landed in self.delivered:
                items.extend(
                    [{"type": "userMessage", "clientId": landed}] * self.readback_copies
                )
        return {"thread": {"id": self.thread_id, "turns": [{"items": items}]}}


@pytest.fixture
def socket_path() -> Iterator[Path]:
    """A private directory, under a root short enough to bind.

    Darwin caps an ``AF_UNIX`` path at 103 bytes, so it cannot live under
    pytest's ``tmp_path``; and it needs a directory only this user can enter,
    because the adapter refuses to speak to a socket sitting anywhere every
    account on the machine could swap it out from under the check.
    """
    home = Path("/tmp") / f"vc-agent-{next(_names)}-{id(object())}"
    home.mkdir(mode=0o700)
    yield home / "app-server.sock"
    shutil.rmtree(home, ignore_errors=True)


def quick(**overrides: Any) -> CodexSettings:
    """Settings whose waits are short enough for a test to actually spend them."""
    return CodexSettings(
        receipt_timeout_seconds=0.3,
        receipt_poll_seconds=0.01,
        verdict_timeout_seconds=0.3,
        request_timeout_seconds=2.0,
        **overrides,
    )


async def watching(server: Codex, sink: Sink, settings: CodexSettings | None = None):
    """An adapter already attached to one scripted Session."""
    adapter = CodexAgentAdapter(sink=sink, settings=settings or quick())
    await adapter.register_session(TARGET, server.path)
    return adapter


def rid(text: str = "r-1") -> RequestId:
    return RequestId(text)


class TestCarryingTheUsersWords:
    def test_a_relay_is_delivered_only_once_the_thread_shows_the_words(
        self, socket_path: Path
    ) -> None:
        """A successful `turn/start` is not proof; the readback is."""

        async def scenario():
            async with Codex(socket_path).script() as server:
                adapter = await watching(server, Sink())
                try:
                    receipt = await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                    return receipt, server.calls_to("turn/start")
                finally:
                    await adapter.aclose()

        receipt, starts = asyncio.run(scenario())
        assert receipt.outcome is Delivery.DELIVERED
        assert starts[0]["clientUserMessageId"] == "r-1"
        assert starts[0]["input"] == [{"type": "text", "text": "ship it"}]
        assert starts[0]["threadId"] == THREAD

    def test_every_turn_asserts_where_approvals_go(self, socket_path: Path) -> None:
        """The pin cannot be set at resume time, so it rides every turn instead."""

        async def scenario():
            async with Codex(socket_path).script(
                approval_policy="never", reviewer="auto_review"
            ) as server:
                adapter = await watching(server, Sink())
                try:
                    await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                    return server.calls_to("turn/start")[0], server.reviewer
                finally:
                    await adapter.aclose()

        started, reviewer = asyncio.run(scenario())
        assert started["approvalsReviewer"] == "user"
        assert started["approvalPolicy"] == "on-request"
        assert reviewer == "user"

    def test_a_readback_that_never_shows_the_words_is_unknown_not_delivered(
        self, socket_path: Path
    ) -> None:
        """The case the build issue names first: no proof either way is UNKNOWN."""

        async def scenario():
            async with Codex(socket_path).script() as server:
                server.readback_shows_words = False
                adapter = await watching(server, Sink())
                try:
                    return await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                finally:
                    await adapter.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.UNKNOWN
        assert "never showed the words" in receipt.reason

    def test_a_readback_holding_two_copies_contradicts_and_is_unknown(
        self, socket_path: Path
    ) -> None:
        """A contradiction is UNKNOWN, never FAILED: the words may well be in there."""

        async def scenario():
            async with Codex(socket_path).script() as server:
                server.readback_copies = 2
                adapter = await watching(server, Sink())
                try:
                    return await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                finally:
                    await adapter.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.UNKNOWN
        assert "contradicted" in receipt.reason

    def test_a_refused_turn_never_reached_the_thread_so_it_failed(
        self, socket_path: Path
    ) -> None:
        """A rejected request started no turn, so the words provably did not land."""

        async def scenario():
            async with Codex(socket_path).script() as server:
                server.answers(
                    "turn/start",
                    lambda _p: (_ for _ in ()).throw(FakeRemoteError("thread is archived")),
                )
                adapter = await watching(server, Sink())
                try:
                    return await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                finally:
                    await adapter.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert "thread is archived" in receipt.reason

    def test_two_rapid_relays_reach_the_session_in_the_order_they_were_made(
        self, socket_path: Path
    ) -> None:
        """One Session, two Relays, no interleaving and no lost id."""

        async def scenario():
            async with Codex(socket_path).script() as server:
                adapter = await watching(server, Sink())
                try:
                    first, second = await asyncio.gather(
                        adapter.answer_relay(TARGET, "one", request_id=rid("r-1")),
                        adapter.answer_relay(TARGET, "two", request_id=rid("r-2")),
                    )
                    return (
                        [first.outcome, second.outcome],
                        [call["clientUserMessageId"] for call in server.calls_to("turn/start")],
                        [call["input"][0]["text"] for call in server.calls_to("turn/start")],
                    )
                finally:
                    await adapter.aclose()

        outcomes, ids, texts = asyncio.run(scenario())
        assert outcomes == [Delivery.DELIVERED, Delivery.DELIVERED]
        assert ids == ["r-1", "r-2"]
        assert texts == ["one", "two"]

    def test_an_unregistered_session_fails_closed(self, socket_path: Path) -> None:
        async def scenario():
            adapter = CodexAgentAdapter(sink=Sink(), settings=quick())
            return await adapter.answer_relay(TARGET, "ship it", request_id=rid())

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert "no Codex Session is registered" in receipt.reason


class TestSupplement:
    def test_both_routes_are_offered_because_steer_is_stable(self) -> None:
        adapter = CodexAgentAdapter()
        assert adapter.supported_routes() == frozenset(
            {RelayRoute.DELIVER, RelayRoute.SUPPLEMENT}
        )

    def test_a_supplement_goes_into_the_running_turn(self, socket_path: Path) -> None:
        async def scenario():
            async with Codex(socket_path).script(status="active") as server:
                adapter = await watching(server, Sink())
                try:
                    await server.notify_all(
                        "turn/started", {"threadId": THREAD, "turn": {"id": TURN}}
                    )
                    await _settled()
                    receipt = await adapter.answer_relay(
                        TARGET, "also fix the tests",
                        request_id=rid(), route=RelayRoute.SUPPLEMENT,
                    )
                    return receipt, server.calls_to("turn/steer")
                finally:
                    await adapter.aclose()

        receipt, steers = asyncio.run(scenario())
        assert receipt.outcome is Delivery.DELIVERED
        assert steers[0]["expectedTurnId"] == TURN
        assert steers[0]["clientUserMessageId"] == "r-1"
        assert steers[0]["input"] == [{"type": "text", "text": "also fix the tests"}]

    def test_a_supplement_with_no_running_turn_fails_and_says_so(
        self, socket_path: Path
    ) -> None:
        """What to do instead is Bridge Core's policy, so the adapter only reports."""

        async def scenario():
            async with Codex(socket_path).script() as server:
                adapter = await watching(server, Sink())
                try:
                    return await adapter.answer_relay(
                        TARGET, "now", request_id=rid(), route=RelayRoute.SUPPLEMENT
                    )
                finally:
                    await adapter.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert "no turn is running" in receipt.reason

    def test_a_turn_that_ended_first_fails_closed_quoting_codex(
        self, socket_path: Path
    ) -> None:
        """The race: the turn ended between the user speaking and the words landing."""

        async def scenario():
            async with Codex(socket_path).script(status="active") as server:
                adapter = await watching(server, Sink())
                try:
                    await server.notify_all(
                        "turn/started", {"threadId": THREAD, "turn": {"id": "a-stale-turn"}}
                    )
                    await _settled()
                    return await adapter.answer_relay(
                        TARGET, "now", request_id=rid(), route=RelayRoute.SUPPLEMENT
                    )
                finally:
                    await adapter.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert "expected active turn id `a-stale-turn`" in receipt.reason


class TestNoticeRelay:
    def test_every_notice_is_framed_as_the_bridge_speaking(self, socket_path: Path) -> None:
        """The frame is what stops system words being read as the user's."""

        async def scenario():
            async with Codex(socket_path).script() as server:
                adapter = await watching(server, Sink())
                try:
                    receipt = await adapter.notice_relay(
                        TARGET, "you are needed", request_id=rid()
                    )
                    return receipt, server.calls_to("turn/start")[0]["input"][0]["text"]
                finally:
                    await adapter.aclose()

        receipt, sent = asyncio.run(scenario())
        assert receipt.outcome is Delivery.DELIVERED
        assert sent.startswith(NOTICE_FRAME)
        assert "you are needed" in sent

    def test_the_frame_says_it_carries_no_authority_and_approves_nothing(self) -> None:
        """Both halves are load-bearing; the second is the approval ceiling."""
        assert "not by your user" in " ".join(NOTICE_FRAME.split())
        assert "approves nothing" in NOTICE_FRAME


class TestApprovals:
    def test_a_permission_prompt_is_raised_upward_with_its_voice_menu(
        self, socket_path: Path
    ) -> None:
        sink = Sink()

        async def scenario():
            async with Codex(socket_path).script() as server:
                adapter = await watching(server, sink)
                try:
                    await server.ask_all(
                        APPROVAL,
                        {
                            "threadId": THREAD,
                            "turnId": TURN,
                            "itemId": "call_1",
                            "reason": "Do you want to allow me to run this command?",
                            "command": "/bin/zsh -lc 'touch out.txt'",
                            "availableDecisions": [
                                "accept",
                                {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": []}},
                                "acceptForSession",
                                "decline",
                            ],
                        },
                    )
                    await _settled()
                finally:
                    await adapter.aclose()

        asyncio.run(scenario())
        raised = sink.of(AwaitingApproval)
        assert len(raised) == 1
        request = raised[0].request
        assert request.target == TARGET
        assert request.detail == "Do you want to allow me to run this command?"
        assert request.options == ("accept", "acceptForSession", "decline")

    def test_the_voice_menu_never_offers_a_grant_that_outlives_the_session(self) -> None:
        """The ceiling is `acceptForSession`; a persistent rule is not offerable."""
        offered = voice_menu(
            [
                "accept",
                {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["touch"]}},
                "acceptAlways",
                "acceptForSession",
                "decline",
            ]
        )
        assert offered == ("accept", "acceptForSession", "decline")

    def test_allow_answers_the_prompt_and_is_delivered_once_codex_confirms(
        self, socket_path: Path
    ) -> None:
        sink = Sink()

        async def scenario():
            async with Codex(socket_path).script() as server:
                adapter = await watching(server, sink)
                try:
                    wire_id = await server.ask_all(
                        APPROVAL, {"threadId": THREAD, "turnId": TURN, "itemId": "call_1"}
                    )
                    await _settled()
                    request = sink.of(AwaitingApproval)[0].request
                    verdict = asyncio.ensure_future(
                        adapter.approval_relay(
                            request, ApprovalVerdict.ALLOW, request_id=rid("v-1")
                        )
                    )
                    await _until(lambda: server.answered(wire_id))
                    await server.notify_all(
                        "serverRequest/resolved", {"threadId": THREAD, "requestId": wire_id}
                    )
                    return await verdict
                finally:
                    await adapter.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.DELIVERED

    def test_ask_answers_nothing_at_all_and_is_held(self, socket_path: Path) -> None:
        """A budget expiry must never become a denial, so `ask` is silence."""
        sink = Sink()

        async def scenario():
            async with Codex(socket_path).script() as server:
                adapter = await watching(server, sink)
                try:
                    wire_id = await server.ask_all(
                        APPROVAL, {"threadId": THREAD, "turnId": TURN, "itemId": "call_1"}
                    )
                    await _settled()
                    request = sink.of(AwaitingApproval)[0].request
                    receipt = await adapter.approval_relay(
                        request, ApprovalVerdict.ASK, request_id=rid("v-1")
                    )
                    await _settled()
                    return receipt, server.answered(wire_id)
                finally:
                    await adapter.aclose()

        receipt, answered = asyncio.run(scenario())
        assert receipt.outcome is Delivery.HELD
        assert answered is False

    def test_a_verdict_the_on_screen_dialog_already_answered_is_refused(
        self, socket_path: Path
    ) -> None:
        """The prompt fans out, so the dialog may get there first. Never answer twice."""
        sink = Sink()

        async def scenario():
            async with Codex(socket_path).script() as server:
                adapter = await watching(server, sink)
                try:
                    wire_id = await server.ask_all(
                        APPROVAL, {"threadId": THREAD, "turnId": TURN, "itemId": "call_1"}
                    )
                    await _settled()
                    request = sink.of(AwaitingApproval)[0].request
                    await server.notify_all(
                        "serverRequest/resolved", {"threadId": THREAD, "requestId": wire_id}
                    )
                    await _settled()
                    return await adapter.approval_relay(
                        request, ApprovalVerdict.ALLOW, request_id=rid("v-1")
                    )
                finally:
                    await adapter.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert "already" in receipt.reason


class TestApprovalRouting:
    def test_a_thread_nobody_has_pinned_is_unpinned_not_misrouted(
        self, socket_path: Path
    ) -> None:
        """Never spoken to is a different fact from provably mis-routed."""

        async def scenario():
            async with Codex(socket_path).script(
                approval_policy="never", reviewer="auto_review"
            ) as server:
                adapter = await watching(server, Sink())
                try:
                    return adapter._threads[TARGET].routing
                finally:
                    await adapter.aclose()

        assert asyncio.run(scenario()) is ApprovalRouting.UNPINNED

    def test_a_readback_disagreeing_after_we_pinned_refuses_the_next_relay(
        self, socket_path: Path
    ) -> None:
        """The words that arrived are still DELIVERED; the next Relay fails pre-wire."""

        async def scenario():
            async with Codex(socket_path).script() as server:
                adapter = await watching(server, Sink())
                try:
                    first = await adapter.answer_relay(TARGET, "one", request_id=rid("r-1"))
                    await server.notify_all(
                        "thread/settings/updated",
                        {
                            "threadId": THREAD,
                            "threadSettings": {
                                "approvalPolicy": "never",
                                "approvalsReviewer": "auto_review",
                            },
                        },
                    )
                    await _settled()
                    second = await adapter.answer_relay(TARGET, "two", request_id=rid("r-2"))
                    return first, second, server.calls_to("turn/start")
                finally:
                    await adapter.aclose()

        first, second, starts = asyncio.run(scenario())
        assert first.outcome is Delivery.DELIVERED
        assert second.outcome is Delivery.FAILED
        assert "routed away from the user" in second.reason
        assert "auto_review" in second.reason
        # Nothing of the refused Relay ever went on the wire.
        assert [call["clientUserMessageId"] for call in starts] == ["r-1"]

    def test_a_misrouted_session_refuses_a_verdict_too(self, socket_path: Path) -> None:
        sink = Sink()

        async def scenario():
            async with Codex(socket_path).script() as server:
                adapter = await watching(server, sink)
                try:
                    await server.ask_all(
                        APPROVAL, {"threadId": THREAD, "turnId": TURN, "itemId": "call_1"}
                    )
                    await _settled()
                    request = sink.of(AwaitingApproval)[0].request
                    await adapter.answer_relay(TARGET, "one", request_id=rid("r-1"))
                    await server.notify_all(
                        "thread/settings/updated",
                        {
                            "threadId": THREAD,
                            "threadSettings": {
                                "approvalPolicy": "on-request",
                                "approvalsReviewer": "guardian_subagent",
                            },
                        },
                    )
                    await _settled()
                    return await adapter.approval_relay(
                        request, ApprovalVerdict.ALLOW, request_id=rid("v-1")
                    )
                finally:
                    await adapter.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert "guardian_subagent" in receipt.reason


class TestWhatItRaisesUpward:
    def test_a_turn_ending_closes_the_loop_as_a_stop_and_an_open_window(
        self, socket_path: Path
    ) -> None:
        sink = Sink()

        async def scenario():
            async with Codex(socket_path).script(status="idle") as server:
                adapter = await watching(server, sink)
                try:
                    await server.notify_all(
                        "thread/status/changed",
                        {"threadId": THREAD, "status": {"type": "active", "activeFlags": []}},
                    )
                    await _settled()
                    await server.notify_all(
                        "thread/status/changed",
                        {"threadId": THREAD, "status": {"type": "idle"}},
                    )
                    await _settled()
                finally:
                    await adapter.aclose()

        asyncio.run(scenario())
        # The first entry is registration reporting what the Session already is;
        # only the transitions after it are things that happened.
        assert [event.window for event in sink.of(ReplyWindowChanged)] == [
            ReplyWindow.OPEN,
            ReplyWindow.CLOSED,
            ReplyWindow.OPEN,
        ]
        assert len(sink.of(SessionStopped)) == 1

    def test_a_session_that_never_ran_is_not_announced_as_having_stopped(
        self, socket_path: Path
    ) -> None:
        """Otherwise registering a quiet Session would ring the user's phone."""
        sink = Sink()

        async def scenario():
            async with Codex(socket_path).script(status="idle") as server:
                adapter = await watching(server, sink)
                try:
                    await server.notify_all(
                        "thread/status/changed",
                        {"threadId": THREAD, "status": {"type": "idle"}},
                    )
                    await _settled()
                finally:
                    await adapter.aclose()

        asyncio.run(scenario())
        assert sink.of(SessionStopped) == []

    def test_a_closed_thread_is_reported_as_a_session_that_ended(
        self, socket_path: Path
    ) -> None:
        sink = Sink()

        async def scenario():
            async with Codex(socket_path).script() as server:
                adapter = await watching(server, sink)
                try:
                    await server.notify_all("thread/closed", {"threadId": THREAD})
                    await _settled()
                finally:
                    await adapter.aclose()

        asyncio.run(scenario())
        assert [event.target for event in sink.of(SessionEnded)] == [TARGET]

    def test_a_notification_about_a_thread_nobody_watches_is_ignored(
        self, socket_path: Path
    ) -> None:
        sink = Sink()

        async def scenario() -> int:
            async with Codex(socket_path).script() as server:
                adapter = await watching(server, sink)
                try:
                    already = len(sink.events)
                    await server.notify_all(
                        "thread/status/changed",
                        {"threadId": "some-other-thread", "status": {"type": "idle"}},
                    )
                    await _settled()
                    return len(sink.events) - already
                finally:
                    await adapter.aclose()

        assert asyncio.run(scenario()) == 0


class TestWhenTheAppServerDies:
    def test_a_relay_in_flight_is_classified_and_nothing_retries_it(
        self, socket_path: Path
    ) -> None:
        """The build issue's third edge case: classified, marked, no retry storm."""
        sink = Sink()

        async def scenario():
            async with Codex(socket_path).script() as server:
                async def never_answers(_params: dict) -> dict:
                    await asyncio.sleep(30)
                    return {}

                server.answers("turn/start", never_answers)
                adapter = await watching(server, sink)
                try:
                    relaying = asyncio.ensure_future(
                        adapter.answer_relay(TARGET, "ship it", request_id=rid())
                    )
                    await _until(lambda: bool(server.calls_to("turn/start")))
                    await server.drop_everyone()
                    receipt = await asyncio.wait_for(relaying, 3)
                    return receipt, server.calls_to("turn/start")
                finally:
                    await adapter.aclose()

        receipt, starts = asyncio.run(scenario())
        # The words were on the wire, so nothing here may claim they did not go.
        assert receipt.outcome is Delivery.UNKNOWN
        # Classified, Session marked, and sent exactly once — no retry storm.
        assert [event.target for event in sink.of(SessionEnded)] == [TARGET]
        assert len(starts) == 1

    def test_a_relay_into_a_session_whose_socket_is_gone_never_reached_it(
        self, socket_path: Path
    ) -> None:
        """Failing before anything is sent is the one case that proves non-arrival."""

        async def scenario():
            async with Codex(socket_path).script() as server:
                adapter = await watching(server, Sink())
                try:
                    await adapter._threads[TARGET].connection.aclose()
                    adapter._threads[TARGET].subscribed = False
                    return await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                finally:
                    await adapter.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert "unreachable" in receipt.reason


class TestSettings:
    def test_an_absent_table_is_every_default(self) -> None:
        assert CodexSettings.of(None) == CodexSettings()
        assert CodexSettings.of({}) == CodexSettings()

    def test_a_key_this_adapter_does_not_have_refuses_to_start(self) -> None:
        """A misspelled timeout that silently defaults is the silent fallback ban."""
        with pytest.raises(SettingsError, match="receipt_timeout_second"):
            CodexSettings.of({"receipt_timeout_second": 5})

    def test_the_refusal_names_the_keys_it_does_have(self) -> None:
        with pytest.raises(SettingsError, match="receipt_timeout_seconds"):
            CodexSettings.of({"nonsense": 1})

    def test_a_duration_that_is_not_a_number_is_refused(self) -> None:
        with pytest.raises(SettingsError, match="must be a number of seconds"):
            CodexSettings.of({"receipt_timeout_seconds": "soon"})

    def test_a_zero_duration_is_refused(self) -> None:
        with pytest.raises(SettingsError, match="positive"):
            CodexSettings.of({"receipt_timeout_seconds": 0})

    def test_paths_and_executables_are_read_as_what_they_are(self) -> None:
        settings = CodexSettings.of(
            {"executable": "/opt/codex", "socket_directory": "~/sockets"}
        )
        assert settings.executable == "/opt/codex"
        assert settings.socket_directory == Path("~/sockets").expanduser()

    def test_the_factory_builds_an_adapter_from_a_table(self) -> None:
        adapter = codex_agent(sink=Sink(), settings={"receipt_timeout_seconds": 2})
        assert isinstance(adapter, CodexAgentAdapter)


async def _settled() -> None:
    """Let the reader task deliver whatever the far side just sent."""
    for _ in range(10):
        await asyncio.sleep(0)
    await asyncio.sleep(0.02)


async def _until(condition, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the far side never got there")


class TestWhenCodexIgnoresThePin:
    def test_a_turn_whose_pin_was_dropped_is_caught_by_the_readback(
        self, socket_path: Path
    ) -> None:
        """`turn/start` answers with the turn only, so the assertion needs an echo."""

        async def scenario():
            async with Codex(socket_path).script(
                approval_policy="never", reviewer="auto_review"
            ) as server:
                server.honours_the_pin = False
                adapter = await watching(server, Sink())
                try:
                    first = await adapter.answer_relay(TARGET, "one", request_id=rid("r-1"))
                    routing = adapter._threads[TARGET].routing
                    second = await adapter.answer_relay(TARGET, "two", request_id=rid("r-2"))
                    return first, routing, second, server.calls_to("turn/start")
                finally:
                    await adapter.aclose()

        first, routing, second, starts = asyncio.run(scenario())
        # The words did arrive, and the receipt says only that.
        assert first.outcome is Delivery.DELIVERED
        # The mis-route is caught without waiting for a notification to land.
        assert routing is ApprovalRouting.MISROUTED
        assert second.outcome is Delivery.FAILED
        assert "auto_review" in second.reason
        assert [call["clientUserMessageId"] for call in starts] == ["r-1"]

    def test_a_server_that_honours_the_pin_leaves_the_session_usable(
        self, socket_path: Path
    ) -> None:
        async def scenario():
            async with Codex(socket_path).script(
                approval_policy="never", reviewer="auto_review"
            ) as server:
                adapter = await watching(server, Sink())
                try:
                    first = await adapter.answer_relay(TARGET, "one", request_id=rid("r-1"))
                    second = await adapter.answer_relay(TARGET, "two", request_id=rid("r-2"))
                    return first, second, adapter._threads[TARGET].routing
                finally:
                    await adapter.aclose()

        first, second, routing = asyncio.run(scenario())
        assert first.outcome is Delivery.DELIVERED
        assert second.outcome is Delivery.DELIVERED
        assert routing is ApprovalRouting.PINNED


class TestWhenTheSessionsAppServerDies:
    def test_the_session_is_marked_ended_exactly_once(self, socket_path: Path) -> None:
        """A TUI is a thin client of its app-server: no app-server, no Session."""
        sink = Sink()

        async def scenario():
            async with Codex(socket_path).script() as server:
                adapter = await watching(server, sink)
                try:
                    await server.drop_everyone()
                    await _until(lambda: bool(sink.of(SessionEnded)))
                    await _settled()
                    return sink.of(SessionEnded), adapter.watching()
                finally:
                    await adapter.aclose()

        ended, still_watched = asyncio.run(scenario())
        assert [event.target for event in ended] == [TARGET]
        assert ended[0].detail
        # Dropped from the roster, so nothing keeps addressing a dead socket.
        assert still_watched == ()

    def test_a_relay_after_it_died_fails_before_anything_is_sent(
        self, socket_path: Path
    ) -> None:
        sink = Sink()

        async def scenario():
            async with Codex(socket_path).script() as server:
                adapter = await watching(server, sink)
                try:
                    await server.drop_everyone()
                    await _until(lambda: bool(sink.of(SessionEnded)))
                    return await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                finally:
                    await adapter.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert "no Codex Session is registered" in receipt.reason

    def test_an_orderly_close_is_not_reported_as_a_session_ending(
        self, socket_path: Path
    ) -> None:
        """Shutting the engine down must not tell the hub every Session died."""
        sink = Sink()

        async def scenario():
            async with Codex(socket_path).script() as server:
                adapter = await watching(server, sink)
                await adapter.aclose()
                await _settled()

        asyncio.run(scenario())
        assert sink.of(SessionEnded) == []
