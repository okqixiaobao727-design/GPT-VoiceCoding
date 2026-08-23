"""The Claude Agent adapter, against a scripted Session Channel.

The edge cases here are the ones the build issue named, because each is a way a
Relay can look successful and not be: an acknowledgement that never comes, one
that comes for somebody else's request, a channel that refuses the words, and a
channel that vanishes with them in flight.

No real Claude Code runs. The wire shapes are the ones `protocol.py` transcribes
from the implementation proven live against Claude Code 2.1.235.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from claude_fake import FakeChannel
from gpt_voicecoding.adapters.agent.claude import ClaudeAgentAdapter, claude_agent
from gpt_voicecoding.adapters.agent.claude.adapter import APPROVAL_UNROUTED
from gpt_voicecoding.adapters.agent.claude.protocol import (
    CHANNEL_KIND_BY_VERB,
    channel_kind_for,
)
from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings, SettingsError
from gpt_voicecoding.core.lifecycle import Lifecycle
from gpt_voicecoding.core.relay_queue import RelayQueue
from gpt_voicecoding.core.relays import RelayPipeline
from gpt_voicecoding.core.sessions import Session, SessionRegistry
from gpt_voicecoding.seams.agent import (
    ApprovalRequest,
    ApprovalVerdict,
    RelayReceipt,
    RelayRoute,
    ReplyWindow,
)
from gpt_voicecoding.seams.delivery import Delivery
from gpt_voicecoding.seams.identity import AgentKind, RequestId, SessionLabel, SessionTarget
from gpt_voicecoding.seams.verify import VerifyOutcome

SESSION = "0b7cf6f2-0f3c-4f5e-9d1f-8a2b3c4d5e6f"
TARGET = SessionTarget(agent=AgentKind.CLAUDE, session_id=SESSION, pid=4321)

_names = itertools.count()


class Sink:
    """The event sink, recording what the adapter raised upward."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: Any) -> None:
        self.events.append(event)

    def of(self, kind: type) -> list[Any]:
        return [event for event in self.events if isinstance(event, kind)]


@pytest.fixture
def socket_path() -> Iterator[Path]:
    """A private directory, under a root short enough to bind.

    Darwin caps an ``AF_UNIX`` path at 103 bytes, so it cannot live under
    pytest's ``tmp_path``; and it needs a directory only this user can enter,
    because the adapter refuses to carry the user's words over a socket sitting
    where every account on the machine could swap it out from under the check.
    """
    home = Path("/tmp") / f"vc-claude-{next(_names)}-{id(object())}"
    home.mkdir(mode=0o700)
    yield home / "channel.sock"
    shutil.rmtree(home, ignore_errors=True)


def quick(**overrides: Any) -> ClaudeSettings:
    """Settings whose waits are short enough for a test to actually spend them."""
    defaults: dict[str, Any] = {
        "request_timeout_seconds": 2.0,
        "ack_timeout_seconds": 0.3,
        "late_ack_timeout_seconds": 2.0,
    }
    defaults.update(overrides)
    return ClaudeSettings(**defaults)


def reaching(path: Path, sink: Sink, settings: ClaudeSettings | None = None) -> ClaudeAgentAdapter:
    """An adapter that already knows where one Session's channel listens."""
    adapter = ClaudeAgentAdapter(sink=sink, settings=settings or quick())
    adapter.register_session(TARGET, path)
    return adapter


def rid(text: str = "r-1") -> RequestId:
    return RequestId(text)


async def _until_closed(channel: FakeChannel) -> None:
    """Wait for the channel to see the bridge's end of the socket go away."""
    while channel.open_connections:
        await asyncio.sleep(0.01)


class TestRegisteringSessions:
    def test_a_registered_session_channel_is_recorded(self, socket_path: Path, caplog) -> None:
        caplog.set_level("INFO", logger="gpt_voicecoding.adapters.agent.claude.adapter")
        adapter = ClaudeAgentAdapter(sink=Sink(), settings=quick())

        adapter.register_session(TARGET, socket_path)

        assert [record.getMessage() for record in caplog.records] == [
            "registered Session channel "
            f"agent=claude session_id={SESSION} pid=4321 socket={socket_path}"
        ]


class TestCarryingTheUsersWords:
    def test_a_relay_is_delivered_only_once_the_session_acknowledges_it(
        self, socket_path: Path
    ) -> None:
        """The queued line proves the push; only the tool call proves the receipt."""

        async def scenario():
            async with FakeChannel(socket_path) as channel:
                adapter = reaching(socket_path, Sink())
                try:
                    receipt = await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                    return receipt, channel.received
                finally:
                    await adapter.aclose()

        receipt, received = asyncio.run(scenario())
        assert receipt.outcome is Delivery.DELIVERED
        assert received == [{"request_id": "r-1", "kind": "user_message", "text": "ship it"}]

    def test_a_queued_line_alone_is_never_delivery(self, socket_path: Path) -> None:
        """The channel accepted it; the Session may never have read it."""

        async def scenario():
            async with FakeChannel(socket_path, acknowledge_after=None):
                adapter = reaching(socket_path, Sink())
                try:
                    return await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                finally:
                    await adapter.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.UNKNOWN
        assert "did not acknowledge" in receipt.reason

    def test_an_acknowledgement_after_the_budget_is_raised_as_a_late_receipt(
        self, socket_path: Path
    ) -> None:
        """The upgrade that stops Bridge Core re-delivering words that arrived.

        Anything not proven delivered is retained and sent again when the Reply
        Window next opens. So a late acknowledgement that was heard and dropped
        would become a duplicate message — the receipt is what lets the hub
        stop holding it.
        """

        async def scenario():
            sink = Sink()
            async with FakeChannel(socket_path, acknowledge_after=0.6):
                adapter = reaching(socket_path, sink)
                try:
                    receipt = await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                    await asyncio.sleep(0.6)
                    return receipt, sink.of(RelayReceipt)
                finally:
                    await adapter.aclose()

        receipt, late = asyncio.run(scenario())
        assert receipt.outcome is Delivery.UNKNOWN
        assert [event.receipt.outcome for event in late] == [Delivery.DELIVERED]
        assert late[0].receipt.request_id == rid()
        assert late[0].target == TARGET

    def test_a_late_reply_that_is_not_an_acknowledgement_raises_nothing(
        self, socket_path: Path
    ) -> None:
        """A late receipt may only ever be an upgrade, never a re-grade."""

        async def scenario():
            sink = Sink()
            async with FakeChannel(
                socket_path, acknowledge_after=0.6, answer_about="somebody-else"
            ):
                adapter = reaching(socket_path, sink)
                try:
                    receipt = await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                    await asyncio.sleep(0.6)
                    return receipt, sink.of(RelayReceipt)
                finally:
                    await adapter.aclose()

        receipt, late = asyncio.run(scenario())
        assert receipt.outcome is Delivery.UNKNOWN
        assert late == []

    def test_closing_the_adapter_ends_every_late_listener(self, socket_path: Path) -> None:
        """No connection to a Session's own process may outlive this adapter."""

        async def scenario():
            async with FakeChannel(socket_path, acknowledge_after=None):
                adapter = reaching(socket_path, Sink())
                await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                listening = len(adapter._listening)
                await adapter.aclose()
                await asyncio.sleep(0)
                return listening, len(adapter._listening)

        before, after = asyncio.run(scenario())
        assert (before, after) == (1, 0)

    def test_closing_the_adapter_closes_the_connection_the_far_side_holds(
        self, socket_path: Path
    ) -> None:
        """Cancelling a listener is a request; the channel only sees it once it lands.

        The channel reads until end-of-file. An adapter that cancelled its
        listener and walked away would leave that read waiting forever on a
        close nobody was going to perform — and the Session's own process is
        the one left holding it.
        """

        async def scenario():
            async with FakeChannel(socket_path, acknowledge_after=None) as channel:
                adapter = reaching(socket_path, Sink())
                await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                during = channel.open_connections
                await adapter.aclose()
                # Bounded, not unbounded: the far side needs one cycle to see
                # end-of-file, but a close that was only *asked* for never
                # arrives at all, however long the wait.
                await asyncio.wait_for(_until_closed(channel), timeout=1.0)
                return during, channel.open_connections

        during, after = asyncio.run(scenario())
        assert (during, after) == (1, 0)

    def test_an_acknowledgement_for_another_request_contradicts_rather_than_proves(
        self, socket_path: Path
    ) -> None:
        async def scenario():
            async with FakeChannel(socket_path, answer_about="somebody-else"):
                adapter = reaching(socket_path, Sink())
                try:
                    return await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                finally:
                    await adapter.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.UNKNOWN
        assert "different request" in receipt.reason

    def test_a_refused_line_never_reached_the_session_so_it_failed(self, socket_path: Path) -> None:
        """This channel only refuses before pushing, so its refusal is proof."""

        async def scenario():
            async with FakeChannel(socket_path, refuse_with="kind must name what this is"):
                adapter = reaching(socket_path, Sink())
                try:
                    return await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                finally:
                    await adapter.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert "kind must name what this is" in receipt.reason

    def test_a_channel_that_dies_mid_relay_is_unknown_and_is_not_dialled_again(
        self, socket_path: Path
    ) -> None:
        """The words may have been read before the process went. Nothing proves either way."""

        async def scenario():
            async with FakeChannel(socket_path, close_on_relay=True) as channel:
                adapter = reaching(socket_path, Sink())
                try:
                    receipt = await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                    return receipt, len(channel.received)
                finally:
                    await adapter.aclose()

        receipt, dials = asyncio.run(scenario())
        assert receipt.outcome is Delivery.UNKNOWN
        assert dials == 1, "a dead channel must not be dialled again in a loop"

    def test_two_rapid_relays_keep_their_order_and_their_own_receipts(
        self, socket_path: Path
    ) -> None:
        async def scenario():
            async with FakeChannel(socket_path) as channel:
                adapter = reaching(socket_path, Sink())
                try:
                    first, second = await asyncio.gather(
                        adapter.answer_relay(TARGET, "first", request_id=rid("r-1")),
                        adapter.answer_relay(TARGET, "second", request_id=rid("r-2")),
                    )
                    return first, second, channel.received
                finally:
                    await adapter.aclose()

        first, second, received = asyncio.run(scenario())
        assert first.outcome is Delivery.DELIVERED
        assert second.outcome is Delivery.DELIVERED
        assert [line["text"] for line in received] == ["first", "second"]
        assert (first.request_id, second.request_id) == ("r-1", "r-2")


class TestWhatTheWireSays:
    def test_the_relay_kind_is_true_of_everything_the_verb_carries(self) -> None:
        """The defect repair: nothing is stamped as an answer to an unasked question."""
        assert channel_kind_for("answer_relay") == "user_message"
        assert "user_answer" not in set(CHANNEL_KIND_BY_VERB.values())

    def test_a_verb_that_does_not_ride_this_route_is_refused_rather_than_defaulted(self) -> None:
        """No second Relay kind may be smuggled in under the first one's name."""
        with pytest.raises(ValueError, match="does not ride the MCP channel"):
            channel_kind_for("notice_relay")


class TestTheRoutesThisBuildReallyHas:
    def test_supplement_is_declared_absent_and_refuses_rather_than_pretending(
        self, socket_path: Path
    ) -> None:
        """Deciding what to do instead is Bridge Core's policy, not this adapter's."""

        async def scenario():
            async with FakeChannel(socket_path) as channel:
                adapter = reaching(socket_path, Sink())
                try:
                    receipt = await adapter.answer_relay(
                        TARGET, "and one more thing", request_id=rid(), route=RelayRoute.SUPPLEMENT
                    )
                    return receipt, channel.received
                finally:
                    await adapter.aclose()

        receipt, received = asyncio.run(scenario())
        assert RelayRoute.SUPPLEMENT not in claude_agent().supported_routes()
        assert receipt.outcome is Delivery.FAILED
        assert received == [], "a route this adapter lacks must put nothing on the wire"

    def test_the_approval_relay_refuses_by_name_when_no_hook_route_exists(
        self, socket_path: Path
    ) -> None:
        """It rides the PermissionRequest hook, which this Session's launch never opened.

        A registered Session is not automatically an answerable one: the hook
        arrives only for a launch that carried both the hook plugin and the
        approval socket's address, so the refusal names those rather than
        reporting an empty race.
        """

        async def scenario():
            async with FakeChannel(socket_path) as channel:
                adapter = reaching(socket_path, Sink())
                try:
                    verdict = await adapter.approval_relay(
                        ApprovalRequest(approval_id="a-1", target=TARGET, tool_name="Bash"),
                        ApprovalVerdict.ALLOW,
                        request_id=rid("r-2"),
                    )
                    return verdict, channel.received
                finally:
                    await adapter.aclose()

        verdict, received = asyncio.run(scenario())
        assert verdict.outcome is Delivery.FAILED and verdict.reason == APPROVAL_UNROUTED
        assert received == [], "a verdict never travels over the channel wire"

    def test_a_notice_relay_never_touches_the_channel(self, socket_path: Path) -> None:
        """It rides the peer socket. The two wires must not leak into each other.

        The target here is in no registry, so the Notice Relay fails before the
        wire — which is exactly the point: whatever it does, it does somewhere
        else. `test_claude_notice.py` covers what it does on its own route.
        """

        async def scenario():
            async with FakeChannel(socket_path) as channel:
                adapter = reaching(socket_path, Sink())
                try:
                    notice = await adapter.notice_relay(TARGET, "it stopped", request_id=rid())
                    return notice, channel.received
                finally:
                    await adapter.aclose()

        notice, received = asyncio.run(scenario())
        assert notice.outcome is Delivery.FAILED
        assert notice.reason, "a failure must always say why"
        assert received == [], "the Notice Relay must put nothing on the channel"


class TestFailingBeforeTheWordsLeave:
    def test_an_unregistered_session_fails_closed(self, socket_path: Path) -> None:
        async def scenario():
            adapter = ClaudeAgentAdapter(sink=Sink(), settings=quick())
            try:
                return await adapter.answer_relay(TARGET, "ship it", request_id=rid())
            finally:
                await adapter.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert "no Claude Session is registered" in receipt.reason

    def test_a_channel_nothing_listens_on_fails_and_names_the_layer(
        self, socket_path: Path
    ) -> None:
        async def scenario():
            adapter = reaching(socket_path, Sink())
            try:
                return await adapter.answer_relay(TARGET, "ship it", request_id=rid())
            finally:
                await adapter.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert "channel is unreachable" in receipt.reason

    def test_words_larger_than_both_ends_allow_never_reach_the_wire(
        self, socket_path: Path
    ) -> None:
        async def scenario():
            async with FakeChannel(socket_path) as channel:
                adapter = reaching(socket_path, Sink(), quick(max_text_bytes=8))
                try:
                    receipt = await adapter.answer_relay(
                        TARGET, "far more than eight bytes", request_id=rid()
                    )
                    return receipt, channel.received
                finally:
                    await adapter.aclose()

        receipt, received = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert received == []

    def test_a_socket_other_accounts_can_reach_is_refused(self, socket_path: Path) -> None:
        """The privacy check is not advice: a widened socket is not spoken to."""

        async def scenario():
            async with FakeChannel(socket_path):
                os.chmod(socket_path, 0o666)
                adapter = reaching(socket_path, Sink())
                try:
                    return await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                finally:
                    await adapter.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert "reachable by other accounts" in receipt.reason

    def test_a_socket_reached_through_a_symlink_is_refused(self, socket_path: Path) -> None:
        """`lstat`, not `stat`: a link may aim at something this user really owns."""

        async def scenario():
            async with FakeChannel(socket_path):
                link = socket_path.parent / "link.sock"
                link.symlink_to(socket_path)
                adapter = reaching(link, Sink())
                try:
                    return await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                finally:
                    await adapter.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert "symbolic link" in receipt.reason

    def test_a_path_too_long_to_bind_is_refused_with_its_byte_count(
        self, socket_path: Path
    ) -> None:
        """The one thing that separates "could not bind" from "the path is too long"."""

        async def scenario():
            adapter = reaching(socket_path.parent / ("x" * 120), Sink())
            try:
                return await adapter.answer_relay(TARGET, "ship it", request_id=rid())
            finally:
                await adapter.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert "may not exceed 103" in receipt.reason

    def test_a_codex_session_is_not_this_adapters_to_reach(self) -> None:
        adapter = ClaudeAgentAdapter()
        with pytest.raises(ValueError, match="not this adapter's"):
            adapter.register_session(
                SessionTarget(agent=AgentKind.CODEX, session_id="t-1"), Path("/tmp/x.sock")
            )


class TestReportingWhatIsLoaded:
    def test_verify_names_this_implementation_and_passes_with_nothing_registered(self) -> None:
        result = asyncio.run(ClaudeAgentAdapter().verify())
        assert result.outcome is VerifyOutcome.PASS
        assert result.loaded.endswith(":ClaudeAgentAdapter")
        assert "no Claude Session is registered" in result.detail

    def test_verify_passes_when_a_registered_channel_answers(self, socket_path: Path) -> None:
        async def scenario():
            async with FakeChannel(socket_path):
                adapter = reaching(socket_path, Sink())
                try:
                    return await adapter.verify()
                finally:
                    await adapter.aclose()

        result = asyncio.run(scenario())
        assert result.outcome is VerifyOutcome.PASS
        assert "1 of 1" in result.detail

    def test_verify_fails_and_names_the_layer_when_no_channel_answers(
        self, socket_path: Path
    ) -> None:
        async def scenario():
            adapter = reaching(socket_path, Sink())
            try:
                return await adapter.verify()
            finally:
                await adapter.aclose()

        result = asyncio.run(scenario())
        assert result.outcome is VerifyOutcome.FAIL
        assert SESSION in result.detail


class TestWhatThisSpokeMayBeTold:
    def test_an_unknown_key_refuses_to_start_and_lists_what_there_is(self) -> None:
        with pytest.raises(SettingsError, match="ack_timeout_secondz"):
            claude_agent(settings={"ack_timeout_secondz": 5})

    def test_a_text_budget_larger_than_the_line_budget_can_never_be_spent(self) -> None:
        with pytest.raises(SettingsError, match="must fit inside"):
            ClaudeSettings(max_message_bytes=100, max_text_bytes=200)

    def test_a_timeout_that_is_not_a_number_of_seconds_is_refused(self) -> None:
        with pytest.raises(SettingsError, match="number of seconds"):
            claude_agent(settings={"ack_timeout_seconds": "soon"})


class TestTheHubStopsHoldingWhatArrived:
    """The reason the late receipt exists, asserted against the real Bridge Core.

    Everything not proven delivered is retained and sent again when the Reply
    Window next opens. So the question the late receipt answers is not "was the
    grade tidy" — it is whether the hub is still holding words the Session has
    already acted on, and would say them a second time.
    """

    def test_a_late_acknowledgement_takes_the_retained_relay_out_of_the_queue(
        self, socket_path: Path
    ) -> None:
        async def scenario():
            async with FakeChannel(socket_path, acknowledge_after=0.6):
                sink = Sink()
                adapter = reaching(socket_path, sink)
                registry = SessionRegistry()
                registry.register(
                    Session(
                        target=TARGET,
                        label=SessionLabel("GPT-VoiceCoding", "port the log"),
                        workspace=Path("/tmp/workspace"),
                        registered_at=0.0,
                        reply_window=ReplyWindow.OPEN,
                    )
                )
                queue = RelayQueue()
                pipeline = RelayPipeline(
                    agents={AgentKind.CLAUDE: adapter}, sessions=registry, relays=queue
                )
                try:
                    outcome = await pipeline.relay(TARGET, "ship it", request_id=rid())
                    retained = queue.pending_for(TARGET)

                    await asyncio.sleep(0.6)
                    for event in sink.of(RelayReceipt):
                        queue.classify(event.receipt.request_id, event.receipt.outcome)
                    return outcome, retained, queue.pending_for(TARGET)
                finally:
                    await adapter.aclose()

        outcome, retained, left = asyncio.run(scenario())
        assert outcome.state is Lifecycle.RETAINED, "an unproven Relay is held, not delivered"
        assert [held.request_id for held in retained] == ["r-1"]
        assert left == (), "the hub must stop holding words the Session acknowledged"
