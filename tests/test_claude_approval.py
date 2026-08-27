"""The Approval Relay, against a fake hook invocation.

No real Claude Code runs and no real permission dialog is displayed. What stands
in for both is the hook's own entry point, `approval_hook.decide`, called with the
payload Claude Code would have written to its stdin — so the thing under test is
the whole route from "a dialog appeared" to "this is what the hook printed",
including the socket in the middle.

The four cases the build issue named are the acceptance floor and each has a test
here: a verdict inside the budget is written back; a budget already spent answers
`ask` and the late verdict is discarded safely; a hook that fires while the engine
is down leaves the dialog alone; and no dialog means no hook, which means no
event Bridge Core ever hears about.

Two wire facts are asserted rather than trusted, because both are silent when
wrong and both were read out of Claude Code 2.1.238 rather than documented:
`ask` prints *nothing* — a decision saying "ask" would be read as a denial — and
an `allow` never carries `updatedPermissions`, which is the one-shot grant ceiling
kept as a test now that the mechanism no longer keeps it.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from gpt_voicecoding.adapters.agent.claude import ClaudeAgentAdapter
from gpt_voicecoding.adapters.agent.claude.approval import (
    ALLOW_BEHAVIOR,
    DENY_BEHAVIOR,
    HOOK_EVENT,
    MAX_HOOK_REQUEST_BYTES,
    ApprovalListener,
    approval_socket_path,
    hook_decision,
    question_from,
    request_from,
)
from gpt_voicecoding.adapters.agent.claude.approval_hook import decide, request_for
from gpt_voicecoding.adapters.agent.claude.bootstrap import CHANNEL_CONFIG_VARIABLE
from gpt_voicecoding.adapters.agent.claude.privacy import PRIVATE_SOCKET_MODE
from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings
from gpt_voicecoding.adapters.agent.claude.stop_analysis import QUESTION_TOOL
from gpt_voicecoding.seams.agent import (
    ApprovalRequest,
    ApprovalVerdict,
    ApprovalVerdictKind,
    AwaitingApproval,
    WaitingKind,
)
from gpt_voicecoding.seams.delivery import Delivery
from gpt_voicecoding.seams.identity import AgentKind, RequestId, SessionTarget

SESSION = "0b7cf6f2-0f3c-4f5e-9d1f-8a2b3c4d5e6f"
TARGET = SessionTarget(agent=AgentKind.CLAUDE, session_id=SESSION, pid=4321)

_names = itertools.count()


class Sink:
    """The event sink, recording what the listener raised upward."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: Any) -> None:
        self.events.append(event)

    def of(self, kind: type) -> list[Any]:
        return [event for event in self.events if isinstance(event, kind)]


@pytest.fixture
def socket_root() -> Iterator[Path]:
    """A short private root, for the reason `privacy.py` gives a length limit at all.

    Darwin caps an ``AF_UNIX`` path at 103 bytes, so this cannot live under
    pytest's ``tmp_path``.
    """
    home = Path("/tmp") / f"vc-approval-{next(_names)}-{id(object())}"
    home.mkdir(mode=0o700)
    yield home
    shutil.rmtree(home, ignore_errors=True)


def settings_for(root: Path) -> ClaudeSettings:
    return ClaudeSettings(socket_directory=root, request_timeout_seconds=2.0)


def dialog(tool_name: str = "Bash", **tool_input: Any) -> dict[str, Any]:
    """The payload Claude Code writes to a `PermissionRequest` hook's stdin."""
    return {
        "hook_event_name": HOOK_EVENT,
        "session_id": SESSION,
        "cwd": "/somewhere",
        "tool_name": tool_name,
        "tool_input": tool_input or {"command": "rm -rf build"},
        # Carried by the real payload and deliberately never consulted: every
        # suggestion is a rule, and a rule outlives the call being asked about.
        "permission_suggestions": ["Bash(rm:*)"],
    }


def environment(path: Path, root: Path) -> dict[str, str]:
    """Where this engine is, handed to the hook process directly.

    The variable rather than the published file, because a test that used the
    file would be reaching for the one address on this machine — and every hook
    in this module must reach *its own* listener. `publish_address` is
    `test_claude_published_address.py`'s.
    """
    del root
    return {CHANNEL_CONFIG_VARIABLE: json.dumps({"approvalSocketPath": str(path)})}


def _request_line() -> bytes:
    """The one line a hook puts on the wire, without a hook process to put it there."""
    return json.dumps(request_for(dialog()), separators=(",", ":")).encode("utf-8") + b"\n"


async def _until(settled) -> None:
    """Wait for one observable fact, or fail saying which one never became true."""
    for _ in range(400):
        if settled():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the listener never reached the state this test waits for")


async def hook_in_flight(
    listener: ApprovalListener, root: Path, payload: dict[str, Any] | None = None
) -> asyncio.Task[dict[str, Any] | None]:
    """Start one hook and wait until its dialog is parked, as Claude Code would.

    The hook blocks, so it runs in a worker thread: this is a real socket, and a
    test that faked the client half would be testing the half that cannot be
    wrong.
    """
    task = asyncio.create_task(
        asyncio.to_thread(decide, payload or dialog(), environment(listener.path, root))
    )
    await _until(lambda: bool(listener.pending()))
    return task


class TestWhatTheHookPrints:
    """The wire contract, which is silent in exactly the way that hurts."""

    def test_allow_is_the_one_word_that_means_allow(self) -> None:
        decision = hook_decision(ApprovalVerdict.ALLOW)
        assert decision is not None
        inner = decision["hookSpecificOutput"]
        assert inner["hookEventName"] == HOOK_EVENT
        assert inner["decision"]["behavior"] == ALLOW_BEHAVIOR

    def test_a_typed_allow_value_keeps_allow_semantics(self) -> None:
        decision = hook_decision(ApprovalVerdict(ApprovalVerdictKind.ALLOW))

        assert decision is not None
        assert decision["hookSpecificOutput"]["decision"]["behavior"] == ALLOW_BEHAVIOR

    def test_deny_says_why_so_the_session_can_report_it(self) -> None:
        decision = hook_decision(ApprovalVerdict.DENY)
        assert decision is not None
        inner = decision["hookSpecificOutput"]["decision"]
        assert inner["behavior"] == DENY_BEHAVIOR
        assert inner["message"].strip()

    def test_an_answer_is_denial_prose_with_the_users_words_verbatim(self) -> None:
        assert hook_decision(ApprovalVerdict.answer("tabs")) == {
            "hookSpecificOutput": {
                "hookEventName": HOOK_EVENT,
                "decision": {"behavior": DENY_BEHAVIOR, "message": "tabs"},
            }
        }

    def test_ask_prints_nothing_at_all(self) -> None:
        """The locked never-deny rule, as a wire fact.

        Claude Code reads `behavior == "allow"` as allow and *anything else* as
        deny, so a decision carrying the word "ask" would be a denial the user
        never spoke. Handing the dialog back is printing nothing.
        """
        assert hook_decision(ApprovalVerdict.ASK) is None

    def test_no_verdict_ever_grants_beyond_the_one_call(self) -> None:
        """The ceiling, kept as a test now that the route no longer keeps it.

        2.1.238 accepts `updatedPermissions` on an allow, which would write a
        session-scoped rule, and `updatedInput`, which would rewrite the call the
        user said yes to. Neither may ever appear.
        """
        for verdict in (
            ApprovalVerdict.ALLOW,
            ApprovalVerdict.DENY,
            ApprovalVerdict.ASK,
            ApprovalVerdict.answer("tabs"),
        ):
            decision = hook_decision(verdict)
            if decision is None:
                continue
            inner = decision["hookSpecificOutput"]["decision"]
            assert "updatedPermissions" not in inner
            assert "updatedInput" not in inner


class TestWhatIsAnnounced:
    """What `request_from` puts in front of the user, and what it refuses to.

    Asserted through `request_from` rather than through the extractor, because
    what matters here is that *this route* carries the rule; the extractor's own
    field order and bound are `test_claude_stop_analysis.py`'s.
    """

    @staticmethod
    def detail_for(**tool_input: Any) -> str:
        payload = {**dialog(), "tool_input": tool_input}
        return request_from(payload, target=TARGET, approval_id="a-1").detail

    def test_the_detail_is_the_tool_s_own_words(self) -> None:
        assert self.detail_for(description="clean the build directory") == (
            "clean the build directory"
        )
        assert self.detail_for(file_path="/tmp/x") == "/tmp/x"

    def test_an_input_that_says_nothing_readable_adds_nothing(self) -> None:
        """The tool name is already announced; inventing a sentence would be guessing."""
        assert self.detail_for(weird=12) == ""
        assert (
            request_from(
                {**dialog(), "tool_input": "not an object"}, target=TARGET, approval_id="a-1"
            ).detail
            == ""
        )

    def test_the_command_the_user_is_being_asked_about_is_never_in_the_words(self) -> None:
        """P5's exclusion, on the path that actually speaks and pushes (#75).

        This asserted the opposite until #75: this module had its own extractor,
        which read `command` first and returned it whole — so a shell command was
        spoken into a Live Call and pushed to a phone, while the
        transcript-derived `WaitingFor.detail` excluded exactly that. The signed
        port table rules the reference implementation's way
        (`legacy@1d32845:bridge/transcript.py:1779-1790`), and there is now one
        extractor rather than two disagreeing ones.
        """
        leak = "curl -H 'Authorization: Bearer sk-live-secret' https://example.test"
        assert self.detail_for(command=leak) == ""
        assert self.detail_for(content=leak) == ""
        assert self.detail_for(old_string=leak, new_string=leak) == ""
        assert leak not in self.detail_for(command=leak, description="call the API")

    def test_a_detail_over_the_bound_is_passed_over_rather_than_cut(self) -> None:
        """The other half of P5, and the reason it rejects instead of trimming.

        This asserted the opposite until #75, on the argument that trimming is
        Bridge Core's decision. It is not a trimming rule: a cut lands mid-secret
        as readily as mid-word, so something over the bound is not the one-line
        summary this reads for and is passed over whole. What the user hears then
        is the tool's name, which the announcement already carries.
        """
        assert self.detail_for(file_path="/x" * 4000) == ""

    def test_the_route_offers_no_menu_and_says_so(self) -> None:
        """`options` empty is the honest report: this route has allow and deny."""
        request = request_from(dialog(), target=TARGET, approval_id="a-1")
        assert request.options == ()
        assert request.tool_name == "Bash"


class TestTheHookClient:
    def test_a_payload_without_a_session_id_is_not_a_request(self) -> None:
        assert request_for({"tool_name": "Bash"}) is None

    def test_no_bootstrap_variable_means_the_hook_never_opens_a_socket(self) -> None:
        """Gate one: a Session this engine did not launch is not ours to answer."""
        assert decide(dialog(), {}) is None

    def test_a_value_that_names_no_approval_address_is_the_same_silence(self) -> None:
        """Gate one again: a value this build can read and that says nothing."""
        assert decide(dialog(), {CHANNEL_CONFIG_VARIABLE: json.dumps({})}) is None

    def test_a_hook_that_fires_while_the_engine_is_down_leaves_the_dialog_alone(
        self, socket_root: Path
    ) -> None:
        """Fails open to the screen: no engine, no decision, nothing printed."""
        absent = socket_root / "vc-approvals-1" / "approvals.sock"
        assert decide(dialog(), environment(absent, socket_root)) is None


class TestCarryingOneVerdict:
    def test_a_verdict_inside_the_budget_is_written_back(self, socket_root: Path) -> None:
        async def scenario():
            sink = Sink()
            listener = ApprovalListener(
                settings=settings_for(socket_root),
                resolve=lambda session_id: TARGET if session_id == SESSION else None,
                emit=sink.emit,
                pid=1,
            )
            await listener.start()
            try:
                hook = await hook_in_flight(listener, socket_root)
                announced = sink.of(AwaitingApproval)
                receipt = await listener.answer(
                    announced[0].request.approval_id,
                    ApprovalVerdict.ALLOW,
                    request_id=RequestId("r-1"),
                )
                return receipt, await hook, announced
            finally:
                await listener.aclose()

        receipt, printed, announced = asyncio.run(scenario())
        assert len(announced) == 1, "one displayed dialog raises exactly one event"
        assert announced[0].request.target == TARGET
        assert receipt.outcome is Delivery.DELIVERED
        assert printed is not None
        assert printed["hookSpecificOutput"]["decision"]["behavior"] == ALLOW_BEHAVIOR

    def test_a_deny_reaches_the_dialog_as_a_deny(self, socket_root: Path) -> None:
        async def scenario():
            sink = Sink()
            listener = ApprovalListener(
                settings=settings_for(socket_root),
                resolve=lambda _: TARGET,
                emit=sink.emit,
                pid=2,
            )
            await listener.start()
            try:
                hook = await hook_in_flight(listener, socket_root)
                await listener.answer(
                    sink.of(AwaitingApproval)[0].request.approval_id,
                    ApprovalVerdict.DENY,
                    request_id=RequestId("r-1"),
                )
                return await hook
            finally:
                await listener.aclose()

        printed = asyncio.run(scenario())
        assert printed is not None
        assert printed["hookSpecificOutput"]["decision"]["behavior"] == DENY_BEHAVIOR

    def test_an_expired_budget_hands_the_dialog_back_and_a_late_verdict_is_discarded(
        self, socket_root: Path
    ) -> None:
        """The never-deny fallback, and the late answer that must not undo it.

        Bridge Core's `sweep_expired` answers `ask`; that reaches the hook as
        silence, so the on-screen dialog keeps the request. A verdict that
        arrives afterwards finds nothing parked and is refused rather than
        carried — the alternative is a dialog resolving twice.
        """

        async def scenario():
            sink = Sink()
            listener = ApprovalListener(
                settings=settings_for(socket_root),
                resolve=lambda _: TARGET,
                emit=sink.emit,
                pid=3,
            )
            await listener.start()
            try:
                hook = await hook_in_flight(listener, socket_root)
                approval_id = sink.of(AwaitingApproval)[0].request.approval_id
                expired = await listener.answer(
                    approval_id, ApprovalVerdict.ASK, request_id=RequestId("r-1")
                )
                printed = await hook
                late = await listener.answer(
                    approval_id, ApprovalVerdict.ALLOW, request_id=RequestId("r-2")
                )
                return expired, printed, late
            finally:
                await listener.aclose()

        expired, printed, late = asyncio.run(scenario())
        assert expired.outcome is Delivery.HELD
        assert printed is None, "handing a dialog back is printing nothing"
        assert late.outcome is Delivery.FAILED
        assert late.request_id == RequestId("r-2")

    def test_a_verdict_for_a_dialog_nobody_is_holding_is_a_classified_failure(
        self, socket_root: Path
    ) -> None:
        async def scenario():
            listener = ApprovalListener(
                settings=settings_for(socket_root),
                resolve=lambda _: TARGET,
                emit=Sink().emit,
                pid=4,
            )
            await listener.start()
            try:
                return await listener.answer(
                    "never-existed", ApprovalVerdict.ALLOW, request_id=RequestId("r-1")
                )
            finally:
                await listener.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert "never-existed" in receipt.reason

    def test_a_human_who_answers_first_wins_and_the_late_verdict_is_told_so(
        self, socket_root: Path
    ) -> None:
        """The race the design is built around, from the side that loses it.

        Claude Code cancels a `PermissionRequest` hook once the dialog is
        answered on screen, and the only sign of that out here is the connection
        ending. A verdict arriving afterwards is told which race it lost, rather
        than being given the same "no such request" as a typo.
        """

        async def scenario():
            sink = Sink()
            listener = ApprovalListener(
                settings=settings_for(socket_root),
                resolve=lambda _: TARGET,
                emit=sink.emit,
                pid=5,
            )
            await listener.start()
            try:
                # A raw connection rather than the hook client, because what is
                # being simulated is Claude Code *killing* the hook: the process
                # goes, the socket ends, and that end is the whole signal.
                _, writer = await asyncio.open_unix_connection(str(listener.path))
                writer.write(_request_line())
                await writer.drain()
                await _until(lambda: bool(listener.pending()))
                approval_id = listener.pending()[0].approval_id

                writer.close()  # the human reached for the keyboard
                with contextlib.suppress(OSError, ConnectionError):
                    await writer.wait_closed()
                await _until(lambda: not listener.pending())

                return await listener.answer(
                    approval_id, ApprovalVerdict.ALLOW, request_id=RequestId("r-1")
                )
            finally:
                await listener.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert "on-screen dialog already answered" in receipt.reason


class TestWhoMayBeAnswered:
    def test_a_session_this_engine_does_not_hold_raises_nothing(self, socket_root: Path) -> None:
        """Gate two, and the "no phantom events" case: refused, and silently so."""

        async def scenario():
            sink = Sink()
            listener = ApprovalListener(
                settings=settings_for(socket_root),
                resolve=lambda _: None,
                emit=sink.emit,
                pid=6,
            )
            await listener.start()
            try:
                printed = await asyncio.to_thread(
                    decide, dialog(), environment(listener.path, socket_root)
                )
                return printed, sink.events, listener.pending()
            finally:
                await listener.aclose()

        printed, events, pending = asyncio.run(scenario())
        assert printed is None, "a dialog we may not answer stays with its human"
        assert events == [], "an unregistered Session must never reach Bridge Core"
        assert pending == ()

    def test_no_dialog_means_no_hook_means_no_event(self, socket_root: Path) -> None:
        """The gotcha the grilling carried forward, asserted as a property.

        The hook never fires for a call an existing rule pre-approved, so an
        engine that is up and listening and simply never dialled has nothing to
        announce. A listener that raised anything here would be inventing stalls.
        """

        async def scenario():
            sink = Sink()
            listener = ApprovalListener(
                settings=settings_for(socket_root),
                resolve=lambda _: TARGET,
                emit=sink.emit,
                pid=7,
            )
            await listener.start()
            try:
                await asyncio.sleep(0.05)
                return sink.events, listener.pending()
            finally:
                await listener.aclose()

        events, pending = asyncio.run(scenario())
        assert events == []
        assert pending == ()

    def test_a_connection_that_does_not_speak_this_grammar_is_dropped(
        self, socket_root: Path
    ) -> None:
        """A closed grammar: this socket answers approvals and refuses everything else."""

        async def scenario():
            sink = Sink()
            listener = ApprovalListener(
                settings=settings_for(socket_root),
                resolve=lambda _: TARGET,
                emit=sink.emit,
                pid=8,
            )
            await listener.start()
            try:
                reader, writer = await asyncio.open_unix_connection(str(listener.path))
                writer.write(b'{"type":"status"}\n')
                await writer.drain()
                answer = await asyncio.wait_for(reader.read(), timeout=2.0)
                writer.close()
                return answer, sink.events
            finally:
                await listener.aclose()

        answer, events = asyncio.run(scenario())
        assert answer == b"", "an unspeakable request is closed, not answered"
        assert events == []


class TestTheSocketItself:
    def test_the_address_is_derived_so_a_launcher_can_name_it_first(
        self, socket_root: Path
    ) -> None:
        """A launch states the address of a socket it is not looking at.

        The adapter answers this whether or not anything is bound, because a
        launcher asking where to point a hook is asking about this engine's
        identity rather than about its current state.
        """
        adapter = ClaudeAgentAdapter(settings=ClaudeSettings(socket_directory=socket_root))
        assert adapter.approval_socket_path() == approval_socket_path(socket_root, os.getpid())

    def test_two_engines_do_not_share_one_socket(self, socket_root: Path) -> None:
        assert approval_socket_path(socket_root, 1) != approval_socket_path(socket_root, 2)

    def test_closing_removes_the_private_socket_directory(self, socket_root: Path) -> None:
        async def scenario() -> Path:
            listener = ApprovalListener(
                settings=settings_for(socket_root),
                resolve=lambda _: TARGET,
                emit=Sink().emit,
                pid=14,
            )
            await listener.start()
            directory = listener.path.parent
            assert directory.exists(), "start creates the private socket directory"
            await listener.aclose()
            return directory

        directory = asyncio.run(scenario())
        assert not directory.exists(), "close removes the private socket directory"

    def test_closing_the_engine_releases_every_parked_dialog_to_its_human(
        self, socket_root: Path
    ) -> None:
        """An engine going away must never be why a permission prompt resolves."""

        async def scenario():
            sink = Sink()
            listener = ApprovalListener(
                settings=settings_for(socket_root),
                resolve=lambda _: TARGET,
                emit=sink.emit,
                pid=9,
            )
            await listener.start()
            hook = await hook_in_flight(listener, socket_root)
            await listener.aclose()
            return await hook, listener.path.exists()

        printed, still_there = asyncio.run(scenario())
        assert printed is None, "a released dialog is handed back, never denied"
        assert not still_there, "the socket is taken back out of the directory it was in"


class TestWhichSessionRaisedIt:
    """The roster is the authority, and an ambiguous roster is not an answer."""

    def test_a_registered_session_is_answerable(self, socket_root: Path) -> None:
        adapter = ClaudeAgentAdapter(settings=ClaudeSettings(socket_directory=socket_root))
        adapter.register_session(TARGET, socket_root / "channel.sock")
        assert adapter._registered_as(SESSION) == TARGET

    def test_a_session_this_engine_never_launched_is_not(self, socket_root: Path) -> None:
        adapter = ClaudeAgentAdapter(settings=ClaudeSettings(socket_directory=socket_root))
        assert adapter._registered_as(SESSION) is None

    def test_a_resumed_session_id_naming_two_processes_is_refused(self, socket_root: Path) -> None:
        """`--resume` forks a second process under one session id; a payload has no pid.

        Delivering anyway would still reach the right dialog — the verdict rides
        the hook's own connection — but it would announce it against the wrong
        process, and a notice naming the wrong Session is worse than a dialog
        the human answers themselves.
        """
        adapter = ClaudeAgentAdapter(settings=ClaudeSettings(socket_directory=socket_root))
        adapter.register_session(TARGET, socket_root / "channel.sock")
        adapter.register_session(
            SessionTarget(agent=AgentKind.CLAUDE, session_id=SESSION, pid=TARGET.pid + 1),
            socket_root / "channel.sock",
        )
        assert adapter._registered_as(SESSION) is None


class TestTheGradeFollowsTheBytes:
    """DELIVERED means proven to have arrived, and this wire has to earn it."""

    def test_a_hook_that_went_away_is_never_reported_delivered(self, socket_root: Path) -> None:
        """The defect this exists to prevent: reporting a plan as an arrival.

        Handing the verdict to something that would write it later and returning
        DELIVERED would let Bridge Core announce "approved by voice" for a hook
        that had already died, and the tool call would never run.
        """

        async def scenario():
            sink = Sink()
            listener = ApprovalListener(
                settings=settings_for(socket_root),
                resolve=lambda _: TARGET,
                emit=sink.emit,
                pid=10,
            )
            await listener.start()
            try:
                _, writer = await asyncio.open_unix_connection(str(listener.path))
                writer.write(_request_line())
                await writer.drain()
                await _until(lambda: bool(listener.pending()))
                approval_id = listener.pending()[0].approval_id

                writer.close()
                with contextlib.suppress(OSError, ConnectionError):
                    await writer.wait_closed()
                await _until(lambda: not listener.pending())

                return await listener.answer(
                    approval_id, ApprovalVerdict.ALLOW, request_id=RequestId("r-1")
                )
            finally:
                await listener.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is not Delivery.DELIVERED

    def test_a_hook_that_reads_and_never_says_so_is_unknown_rather_than_delivered(
        self, socket_root: Path
    ) -> None:
        """A client that takes the line and says nothing proves nothing about acting on it.

        The words are in its buffer, which is the textbook UNKNOWN: past the
        write, no proof either way. Only the hook's own acknowledgement is proof.
        """

        async def scenario():
            sink = Sink()
            listener = ApprovalListener(
                settings=ClaudeSettings(socket_directory=socket_root, request_timeout_seconds=0.3),
                resolve=lambda _: TARGET,
                emit=sink.emit,
                pid=11,
            )
            await listener.start()
            try:
                _, writer = await asyncio.open_unix_connection(str(listener.path))
                writer.write(_request_line())
                await writer.drain()
                await _until(lambda: bool(listener.pending()))
                receipt = await listener.answer(
                    listener.pending()[0].approval_id,
                    ApprovalVerdict.ALLOW,
                    request_id=RequestId("r-1"),
                )
                writer.close()
                return receipt
            finally:
                await listener.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.UNKNOWN
        assert receipt.reason.strip()

    def test_a_request_at_the_stated_cap_is_read(self, socket_root: Path) -> None:
        """The cap this module states is the cap the reader actually has.

        `readline` refuses anything past the stream's own buffer, whose default
        is 64 KiB — an eighth of what this module advertises. A `Write` of a
        large file would have vanished with no event and no reply.
        """

        async def scenario():
            sink = Sink()
            listener = ApprovalListener(
                settings=settings_for(socket_root),
                resolve=lambda _: TARGET,
                emit=sink.emit,
                pid=12,
            )
            await listener.start()
            try:
                payload = dialog("Write", file_path="/tmp/big", content="x" * (200 << 10))
                assert len(json.dumps(payload)) > (64 << 10), "the probe must exceed the default"
                hook = asyncio.create_task(
                    asyncio.to_thread(decide, payload, environment(listener.path, socket_root))
                )
                await _until(lambda: bool(listener.pending()))
                await listener.answer(
                    listener.pending()[0].approval_id,
                    ApprovalVerdict.ALLOW,
                    request_id=RequestId("r-1"),
                )
                return await hook, sink.of(AwaitingApproval)
            finally:
                await listener.aclose()

        printed, announced = asyncio.run(scenario())
        assert len(announced) == 1
        assert printed is not None
        assert printed["hookSpecificOutput"]["decision"]["behavior"] == ALLOW_BEHAVIOR

    def test_a_request_past_the_cap_is_dropped_rather_than_crashing_the_listener(
        self, socket_root: Path
    ) -> None:
        async def scenario():
            sink = Sink()
            listener = ApprovalListener(
                settings=settings_for(socket_root),
                resolve=lambda _: TARGET,
                emit=sink.emit,
                pid=13,
            )
            await listener.start()
            try:
                reader, writer = await asyncio.open_unix_connection(str(listener.path))
                writer.write(b"{" + b"x" * (MAX_HOOK_REQUEST_BYTES + 1024) + b"}\n")
                with contextlib.suppress(OSError, ConnectionError):
                    await writer.drain()
                answer = await asyncio.wait_for(reader.read(), timeout=3.0)
                writer.close()
                return answer, sink.events
            finally:
                await listener.aclose()

        answer, events = asyncio.run(scenario())
        assert answer == b""
        assert events == []


async def _closed_before_the_verdict(socket_root: Path, pid: int) -> Delivery:
    """One run of the interleave: the hook's end closes, then the engine answers."""
    listener = ApprovalListener(
        settings=ClaudeSettings(socket_directory=socket_root, request_timeout_seconds=0.3),
        resolve=lambda _: TARGET,
        emit=Sink().emit,
        pid=pid,
    )
    await listener.start()
    try:
        _, writer = await asyncio.open_unix_connection(str(listener.path))
        writer.write(_request_line())
        await writer.drain()
        await _until(lambda: bool(listener.pending()))
        approval_id = listener.pending()[0].approval_id
        # Close, then answer at once — before the listener's own task has
        # consumed the end of the connection.
        writer.close()
        receipt = await listener.answer(
            approval_id, ApprovalVerdict.ALLOW, request_id=RequestId("r-1")
        )
        return receipt.outcome
    finally:
        await listener.aclose()


class TestTheProofIsTheHooksOwnWord:
    """Why the acknowledgement exists, stated as the case that made it necessary."""

    def test_a_close_already_in_flight_is_never_mistaken_for_a_receipt(
        self, socket_root: Path
    ) -> None:
        """The interleave that made the cheaper proof wrong.

        Grading DELIVERED on the connection ending reads soundly — the hook takes
        the line and exits — until the close was already on its way when the
        verdict was written, which is exactly what a human answering the dialog
        produces. Both look identical from here, so an engine reading EOF as a
        receipt announces "approved by voice" for a tool call that never ran.
        The hook says it has the verdict, or nothing does.

        Repeated, because the interleave is a race and one green run of a race
        is not a result.
        """

        async def scenario():
            return [await _closed_before_the_verdict(socket_root, 1000 + n) for n in range(5)]

        outcomes = asyncio.run(scenario())
        assert Delivery.DELIVERED not in outcomes, "a hook that had already gone was not told"


def question_dialog(*groups: dict[str, Any], prompt_id: str | None = "p-1") -> dict[str, Any]:
    """The payload an `AskUserQuestion` raises, as measured on 2.1.246 (#77).

    The same `PermissionRequest` hook a `Write` raises — that is the measurement
    this whole route hangs on — carrying the structured question rather than a
    command, and a `prompt_id` beside it.
    """
    payload = {
        **dialog(tool_name=QUESTION_TOOL),
        "tool_input": {"questions": list(groups)},
    }
    if prompt_id is not None:
        payload["prompt_id"] = prompt_id
    return payload


def group(question: str, *labels: str, multi: bool = False) -> dict[str, Any]:
    return {
        "question": question,
        "header": question[:12],
        "options": [{"label": label, "description": f"{label}, at length"} for label in labels],
        "multiSelect": multi,
    }


class TestAQuestionRidesTheApprovalHook:
    """`AskUserQuestion` is typed as a question on the shared hook route.

    Measured on 2.1.246 (#77): the tool raises a `PermissionRequest`, and a hook
    `deny` carrying a message is consumed by the Session *as the user's answer*.
    #103 therefore parks it under `prompt_id` and enters the Approval Relay with
    an answer verdict, never the permission route's allow/deny menu.
    """

    def test_a_question_payload_projects_the_whole_question(self) -> None:
        waiting = question_from(question_dialog(group("Tabs or spaces?", "Spaces", "Tabs")))

        assert waiting is not None
        assert waiting.kind is WaitingKind.QUESTION
        assert waiting.prompt == "Tabs or spaces?"
        assert [option.text for option in waiting.options] == ["Spaces", "Tabs"]
        assert waiting.approval_id == "p-1", "the dialog's own correlator, for #103"

    def test_an_answer_crosses_the_real_hook_socket_under_its_prompt_id(
        self, socket_root: Path
    ) -> None:
        async def scenario():
            listener = ApprovalListener(
                settings=settings_for(socket_root),
                resolve=lambda _: TARGET,
                emit=Sink().emit,
                pid=43,
            )
            await listener.start()
            try:
                hook = await hook_in_flight(
                    listener,
                    socket_root,
                    question_dialog(group("Tabs or spaces?", "spaces", "tabs")),
                )
                receipt = await listener.answer(
                    "p-1", ApprovalVerdict.answer("tabs"), request_id=RequestId("r-1")
                )
            finally:
                await listener.aclose()
            return receipt, await hook

        receipt, decision = asyncio.run(scenario())

        assert (receipt.outcome, decision) == (
            Delivery.DELIVERED,
            {
                "hookSpecificOutput": {
                    "hookEventName": HOOK_EVENT,
                    "decision": {"behavior": DENY_BEHAVIOR, "message": "tabs"},
                }
            },
        )

    def test_the_session_s_own_mark_on_a_label_is_read_as_one(self) -> None:
        """`AskUserQuestion` has no recommendation field; the mark is in the label.

        The tool's own instructions tell the model to end that option's label
        with `(recommended)`, so the payload carries it whenever there is one and
        reading it is the Session's own words rather than an inference. Advisor,
        2026-08-26: one parser, one reading — the earlier "always `False` on this
        route" was written believing the payload could not carry the mark.
        """
        waiting = question_from(
            question_dialog(group("Which base?", "main (recommended)", "develop"))
        )

        assert waiting is not None
        assert [(o.text, o.recommended) for o in waiting.options] == [
            ("main", True),
            ("develop", False),
        ]
        assert waiting.recommendation == "main"

    def test_order_is_never_a_recommendation(self) -> None:
        """The half of the rule that stands: no mark, no recommendation, ever."""
        waiting = question_from(question_dialog(group("Which base?", "main", "develop")))

        assert waiting is not None
        assert waiting.recommendation is None
        assert not any(option.recommended for option in waiting.options)

    def test_a_multi_question_call_says_everything_it_asked(self) -> None:
        """One parser of this shape, so the two routes cannot disagree (#75)."""
        waiting = question_from(
            question_dialog(group("Tabs or spaces?", "Spaces"), group("Which base?", "main"))
        )

        assert waiting is not None
        assert waiting.prompt == "Tabs or spaces?\nWhich base?"
        assert [option.text for option in waiting.options] == ["Spaces", "main"]

    def test_a_permission_payload_is_no_question_at_all(self) -> None:
        assert question_from(dialog()) is None

    def test_an_unreadable_question_is_still_a_question(self) -> None:
        """A payload whose input never finished being written names no options.

        It must not fall through to the permission branch: the tool name says
        what it is, and announcing it as a dialog to allow or deny would restore
        exactly the menu this route exists to withhold.
        """
        waiting = question_from({**dialog(tool_name=QUESTION_TOOL), "tool_input": {}})

        assert waiting is not None
        assert waiting.kind is WaitingKind.QUESTION
        assert waiting.options == ()

    def test_a_question_without_a_prompt_id_stays_with_the_on_screen_dialog(
        self, socket_root: Path
    ) -> None:
        async def scenario():
            sink = Sink()
            listener = ApprovalListener(
                settings=settings_for(socket_root),
                resolve=lambda _: TARGET,
                emit=sink.emit,
                pid=44,
            )
            await listener.start()
            try:
                hook = await hook_in_flight(
                    listener,
                    socket_root,
                    question_dialog(group("Tabs or spaces?", "spaces", "tabs"), prompt_id=None),
                )
                parked = listener.newest_question_for(TARGET)
                announced = sink.of(AwaitingApproval)
            finally:
                await listener.aclose()
            return parked, announced, await hook

        parked, announced, decision = asyncio.run(scenario())

        assert parked is not None and (parked.approval_id, announced, decision) == (
            None,
            [],
            None,
        )

    def test_a_question_is_parked_and_enters_the_approval_relay(self, socket_root: Path) -> None:
        async def scenario():
            sink = Sink()
            listener = ApprovalListener(
                settings=settings_for(socket_root),
                resolve=lambda _: TARGET,
                emit=sink.emit,
                pid=41,
            )
            await listener.start()
            try:
                hook = await hook_in_flight(
                    listener,
                    socket_root,
                    question_dialog(group("Tabs or spaces?", "Spaces (recommended)", "Tabs")),
                )
                parked = listener.newest_question_for(TARGET)
                announced = sink.of(AwaitingApproval)
            finally:
                # Releases the parked dialog to its human, which is what lets
                # the hook thread finish: nothing here ever answers a question.
                await listener.aclose()
            assert await hook is None, "no verdict was carried, so nothing was printed"
            return parked, announced

        parked, announced = asyncio.run(scenario())
        assert [event.request for event in announced] == [
            ApprovalRequest(
                approval_id="p-1",
                target=TARGET,
                tool_name=QUESTION_TOOL,
                kind=WaitingKind.QUESTION,
                prompt="Tabs or spaces?",
                options=("Spaces", "Tabs"),
                recommendation="Spaces",
            )
        ]
        assert parked is not None
        assert parked.kind is WaitingKind.QUESTION
        assert [option.text for option in parked.options] == ["Spaces", "Tabs"]
        # Through the real hook process, because the request it sends is a copy
        # rather than a forward: a field the hook does not name is absent on this
        # side and nothing errors. The projector's own test cannot see that.
        assert parked.approval_id == "p-1", "the dialog's correlator crossed the wire"

    def test_a_permission_still_raises_awaiting_approval(self, socket_root: Path) -> None:
        """The control: nothing about the ordinary dialog changed."""

        async def scenario():
            sink = Sink()
            listener = ApprovalListener(
                settings=settings_for(socket_root),
                resolve=lambda _: TARGET,
                emit=sink.emit,
                pid=42,
            )
            await listener.start()
            try:
                hook = await hook_in_flight(listener, socket_root)
                announced = sink.of(AwaitingApproval)
                question = listener.newest_question_for(TARGET)
            finally:
                await listener.aclose()
            await hook
            return announced, question

        announced, question = asyncio.run(scenario())
        assert len(announced) == 1
        assert question is None, "a permission is not a question, whatever else is parked"


def test_the_approval_socket_is_private_from_the_moment_it_exists(
    socket_root: Path, mode_at_bind: dict[str, int]
) -> None:
    """#116: this carries the user's authority over a permission request."""

    async def scenario() -> Path:
        listener = ApprovalListener(
            settings=settings_for(socket_root),
            resolve=lambda session_id: TARGET if session_id == SESSION else None,
            emit=Sink().emit,
            pid=1,
        )
        await listener.start()
        try:
            return listener.path
        finally:
            await listener.aclose()

    path = asyncio.run(scenario())

    assert oct(mode_at_bind[str(path)]) == oct(PRIVATE_SOCKET_MODE)
