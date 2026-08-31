"""Claude adapter boundary facts supplied without a real process in tests."""

from __future__ import annotations

import asyncio
import json

from gpt_voicecoding.adapters.agent.claude import discovery as claude_discovery
from gpt_voicecoding.seams.agent import ApprovalRequest, LaneDiscovery, WaitingFor
from gpt_voicecoding.seams.identity import SessionTarget


class ParkedApproval:
    """One dialog held open, as `ApprovalListener._waiting` holds it."""

    def __init__(self, request: ApprovalRequest, question: WaitingFor | None = None) -> None:
        self.target = request.target
        self.permission = request if question is None else None
        self.question = question


def claude_waiting_roster(target: SessionTarget, label: str) -> LaneDiscovery:
    """Project one raw `claude agents --json` waiting row with production code."""

    async def run(_argv: list[str]) -> claude_discovery.CommandResult:
        return claude_discovery.CommandResult(
            code=0,
            stdout=json.dumps(
                [
                    {
                        "pid": target.pid,
                        "cwd": "/tmp/workspace",
                        "kind": "interactive",
                        "sessionId": target.session_id,
                        "status": "waiting",
                        "waitingFor": label,
                    }
                ]
            ),
            stderr="",
        )

    return asyncio.run(claude_discovery.discover(run=run))
