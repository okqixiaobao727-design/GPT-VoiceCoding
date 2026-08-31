"""What `inspect` answers when the lane it reads cannot be read.

`discover` reports its own trouble as data, because "this lane is unavailable"
is a row the roster can show. `inspect` has no such channel — `SessionInspection`
has no error field — so an `inspect` that returned a value here would be
asserting a lifecycle and a state nobody read. Both lanes therefore raise.

The failure being closed is specific and was live in both adapters: a lane that
could not enumerate read as `lifecycle=ENDED`, so one failed `claude agents
--json` said, in the product's own vocabulary, that the Session is over.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from fakes import PROGRESS_CAPTURE
from gpt_voicecoding.adapters.agent.claude import ClaudeAgentAdapter
from gpt_voicecoding.adapters.agent.codex import CodexAgentAdapter
from gpt_voicecoding.seams.agent import (
    LaneDiscovery,
    LaneUnavailable,
    SessionInspection,
    SessionLifecycle,
)
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget

CLAUDE = SessionTarget(agent=AgentKind.CLAUDE, session_id="d3a776ae", pid=3538)
CODEX = SessionTarget(agent=AgentKind.CODEX, session_id=None, pid=90981)
WORKSPACE = Path("/tmp/workspace")


def answering(adapter: object, lane: LaneDiscovery) -> object:
    """Hold that adapter's whole-lane reading still, so `inspect` is what is tested."""

    async def discover() -> LaneDiscovery:
        return lane

    adapter.discover = discover  # type: ignore[attr-defined]
    return adapter


class TestALaneThatCouldNotLook:
    """Raising, on both lanes, and carrying the lane's own words."""

    def test_the_claude_lane_refuses_rather_than_calling_the_session_over(self) -> None:
        adapter = answering(
            ClaudeAgentAdapter(
                progress_capture=PROGRESS_CAPTURE,
            ),
            LaneDiscovery(error="`claude` is not on the PATH"),
        )

        with pytest.raises(LaneUnavailable) as refused:
            asyncio.run(adapter.inspect(CLAUDE))  # type: ignore[attr-defined]

        assert refused.value.reason == "`claude` is not on the PATH"
        assert refused.value.agent is AgentKind.CLAUDE

    def test_the_codex_lane_does_the_same(self) -> None:
        adapter = answering(
            CodexAgentAdapter(progress_capture=PROGRESS_CAPTURE),
            LaneDiscovery(error="the process table is shut"),
        )

        with pytest.raises(LaneUnavailable) as refused:
            asyncio.run(adapter.inspect(CODEX))  # type: ignore[attr-defined]

        assert refused.value.reason == "the process table is shut"
        assert refused.value.agent is AgentKind.CODEX


class TestALaneThatLookedAndDidNotFindIt:
    """The other half of the rule: a lane that *did* look may end a row."""

    def test_the_claude_lane_calls_it_ended(self) -> None:
        adapter = answering(
            ClaudeAgentAdapter(
                progress_capture=PROGRESS_CAPTURE,
            ),
            LaneDiscovery(),
        )

        row = asyncio.run(adapter.inspect(CLAUDE))  # type: ignore[attr-defined]

        assert row.lifecycle is SessionLifecycle.ENDED

    def test_the_codex_lane_calls_it_ended(self) -> None:
        adapter = answering(CodexAgentAdapter(progress_capture=PROGRESS_CAPTURE), LaneDiscovery())

        row = asyncio.run(adapter.inspect(CODEX))  # type: ignore[attr-defined]

        assert row.lifecycle is SessionLifecycle.ENDED

    def test_a_degraded_reading_is_still_a_reading(self) -> None:
        """`degraded` says the rows came from a weaker source, not that they are doubtful."""
        row = SessionInspection(target=CODEX, workspace=WORKSPACE)
        adapter = answering(
            CodexAgentAdapter(progress_capture=PROGRESS_CAPTURE),
            LaneDiscovery(rows=(row,), degraded="shared daemon absent"),
        )

        assert asyncio.run(adapter.inspect(CODEX)) == row  # type: ignore[attr-defined]
