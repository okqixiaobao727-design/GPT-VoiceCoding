"""ADR 0003 from the hub's side: what was configured, against what is loaded.

The reference implementation's status line printed the value the *client* had
just read from disk, so a daemon that never loaded a channel looked exactly like
a healthy one. The rule that replaced it: the engine reports what it actually
loaded, every pluggable seam is asked for itself, and only the hub knows what
configuration asked for — so naming the disagreement is the hub's job.

Three outcomes, and the third is the one that matters: nothing configured
anywhere is handed to the operator rather than passed or failed.
"""

from __future__ import annotations

import asyncio

from fakes import FakeAgent, FakeCall, FakeCompanionChannel, FakeSessionLauncher
from gpt_voicecoding.core.bridge import BridgeCore
from gpt_voicecoding.core.relay_queue import RelayQueue
from gpt_voicecoding.core.sessions import SessionRegistry
from gpt_voicecoding.core.state import BridgeState
from gpt_voicecoding.core.switches import Switchboard
from gpt_voicecoding.core.verification import SeamLoad
from gpt_voicecoding.seams.identity import AgentKind
from gpt_voicecoding.seams.verify import VerifyOutcome, VerifyResult

CALL_SEAM = "call"
CHANNEL_SEAM = "companion_channel"
LAUNCHER_SEAM = "session_launcher"


def hub(
    *,
    inventory: tuple[SeamLoad, ...],
    call: FakeCall | None = None,
    channel: FakeCompanionChannel | None = None,
    launcher: FakeSessionLauncher | None = None,
) -> BridgeCore:
    state = BridgeState(switches=Switchboard(), sessions=SessionRegistry(), relays=RelayQueue())
    return BridgeCore(
        state=state,
        call=call or FakeCall(),
        channel=channel or FakeCompanionChannel(),
        agents={AgentKind.CODEX: FakeAgent()},
        launcher=launcher,
        inventory=inventory,
    )


def verified(core: BridgeCore) -> dict[str, tuple[VerifyOutcome, str]]:
    return {report.seam: (report.outcome, report.detail) for report in asyncio.run(core.verify())}


def loaded(seam: str, configured: str = "an.adapter") -> SeamLoad:
    return SeamLoad(seam=seam, configured=configured)


class TestEveryPluggableSeamAnswersForItself:
    def test_an_adapter_that_is_there_and_answers_passes(self) -> None:
        core = hub(inventory=(loaded(CALL_SEAM),))

        assert verified(core)[CALL_SEAM][0] is VerifyOutcome.PASS

    def test_a_call_whose_far_side_is_down_is_not_reported_healthy(self) -> None:
        """The whole point of ADR 0003: the engine's answer, not the file's."""
        call = FakeCall(
            verify_result=VerifyResult(
                outcome=VerifyOutcome.FAIL, loaded="a.call", detail="the far side never answered"
            )
        )
        core = hub(inventory=(loaded(CALL_SEAM, "a.call"),), call=call)

        outcome, detail = verified(core)[CALL_SEAM]

        assert outcome is VerifyOutcome.FAIL
        assert "the far side never answered" in detail

    def test_an_agent_is_asked_too(self) -> None:
        core = hub(inventory=(loaded("agent.codex", "tests.fakes.FakeAgent"),))

        assert verified(core)["agent.codex"][0] is VerifyOutcome.PASS

    def test_an_agent_this_engine_does_not_hold_reports_nothing_loaded(self) -> None:
        core = hub(inventory=(loaded("agent.claude", "a.claude"),))

        assert verified(core)["agent.claude"][0] is VerifyOutcome.FAIL


class TestThePresenceOfAnAdapter:
    def test_a_configured_seam_with_nothing_behind_it_fails(self) -> None:
        """The Launcher was named and never built: "should have one, has none"."""
        core = hub(inventory=(loaded(LAUNCHER_SEAM, "a.launcher"),), launcher=None)

        outcome, detail = verified(core)[LAUNCHER_SEAM]

        assert outcome is VerifyOutcome.FAIL
        assert "a.launcher" in detail and "nothing" in detail

    def test_the_null_implementation_answering_for_itself_is_not_an_outage(self) -> None:
        """Empty `loaded` is the null implementation, which is present and answering.

        This once failed, on the reading that an empty `loaded` beside a
        configured name *was* the outage. It is not: the two ways a seam can
        have "nothing real" behind it are different facts, and the seam contract
        already says which is which. An engine that deliberately runs without
        text reach names the null adapter to say so — and was then told it had
        loaded nothing, which was false and looked like the failure it had been
        configured to avoid.
        """
        channel = FakeCompanionChannel(
            verify_result=VerifyResult(
                outcome=VerifyOutcome.MANUAL, loaded="", detail="no text reach, deliberately"
            )
        )
        core = hub(inventory=(loaded(CHANNEL_SEAM, "a.null_channel"),), channel=channel)

        outcome, detail = verified(core)[CHANNEL_SEAM]

        assert outcome is VerifyOutcome.MANUAL
        assert detail == "no text reach, deliberately"

    def test_an_adapter_that_names_no_implementation_and_claims_to_pass_fails(self) -> None:
        """The one shape `VerifyResult` cannot refuse for itself is refused here.

        It forbids MANUAL beside a real module string, and cannot forbid PASS
        beside an empty one. Trusting that through would let an adapter report
        health while naming nothing that could be healthy.
        """
        channel = FakeCompanionChannel(
            verify_result=VerifyResult(outcome=VerifyOutcome.PASS, loaded="")
        )
        core = hub(inventory=(loaded(CHANNEL_SEAM, "a.channel"),), channel=channel)

        outcome, detail = verified(core)[CHANNEL_SEAM]

        assert outcome is VerifyOutcome.FAIL
        assert "no implementation" in detail

    def test_nothing_configured_and_nothing_loaded_is_handed_to_the_operator(self) -> None:
        core = hub(inventory=(SeamLoad(seam=LAUNCHER_SEAM, configured=""),), launcher=None)

        assert verified(core)[LAUNCHER_SEAM][0] is VerifyOutcome.MANUAL

    def test_something_loaded_that_nothing_configured_fails(self) -> None:
        core = hub(inventory=(SeamLoad(seam=CALL_SEAM, configured=""),))

        assert verified(core)[CALL_SEAM][0] is VerifyOutcome.FAIL


class TestSpelling:
    def test_an_adapter_naming_itself_its_own_way_is_not_a_disagreement(self) -> None:
        """Configuration names a factory; an adapter names its implementation."""
        channel = FakeCompanionChannel(
            verify_result=VerifyResult(outcome=VerifyOutcome.PASS, loaded="a.channel.Telegram")
        )
        core = hub(inventory=(loaded(CHANNEL_SEAM, "a.channel:make"),), channel=channel)

        assert verified(core)[CHANNEL_SEAM][0] is VerifyOutcome.PASS


class TestAnEngineAskedNothing:
    def test_an_engine_with_no_inventory_reports_nothing_rather_than_guessing(self) -> None:
        assert asyncio.run(hub(inventory=()).verify()) == ()
