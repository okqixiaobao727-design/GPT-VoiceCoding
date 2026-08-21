"""Proof that the Companion Channel really reaches a real bot, in both directions.

CI has no bot, no token and no network to Telegram, so the contract tests run
against a fake API and this script is the gate that the real wire still works.
It is a **manual, local job deliberately outside the test suite**, like the
Claude channel's proofs beside it.

    export GPT_VOICECODING_TELEGRAM_TOKEN=...            # never in a file
    python3 scripts/telegram_channel_proof.py --chat-id <id>
    python3 scripts/telegram_channel_proof.py --chat-id <id> --send
    python3 scripts/telegram_channel_proof.py --chat-id <id> --send --listen 60

What each mode proves, against the three verbs the seam has:

- **free** (no flags): `verify` — the token is accepted and the configured chat
  is reachable — followed by the same check with a deliberately wrong token,
  which must fail and must name the *credentials* layer rather than shrugging.
  Read-only; nothing is sent anywhere.
- `--send`: one real message arrives in the chat. It is short, and it says what
  it is, because somebody's phone is about to buzz.
- `--listen SECONDS`: waits for you to reply in that chat, and prints the
  `InboundText` the adapter raised — text and origin, unclassified, which is the
  whole of what the seam promises Bridge Core.

A run with all three is the Done-when this ticket asks for: send, inbound and
verify, proved once against a real bot.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gpt_voicecoding.adapters.companion_channel.telegram import (  # noqa: E402
    FailureLayer,
    SettingsError,
    telegram_channel,
)
from gpt_voicecoding.seams.companion_channel import InboundText  # noqa: E402
from gpt_voicecoding.seams.identity import new_request_id  # noqa: E402

#: The variable this script reads the token from unless told otherwise. A name,
#: never a value — the token itself only ever lives in the environment.
DEFAULT_TOKEN_VARIABLE = "GPT_VOICECODING_TELEGRAM_TOKEN"

#: What `--send` puts in somebody's chat. Short, and honest about what it is.
PROOF_MESSAGE = "GPT-VoiceCoding companion channel proof — you can ignore this."

#: A token that is well-formed and wrong, for the failure half of the proof.
WRONG_TOKEN = "111111:this-token-was-never-real"


class Collecting:
    """Bridge Core's end of the seam, printing what it hears."""

    def __init__(self) -> None:
        self.events: list[InboundText] = []

    def emit(self, event: InboundText) -> None:
        self.events.append(event)
        print(f"  inbound: text={event.text!r} origin={event.origin!r}")


def main() -> int:
    parsed = _arguments()
    table = {"token_env": parsed.token_env, "chat_id": parsed.chat_id}
    try:
        sink = Collecting()
        channel = telegram_channel(sink=sink, settings=table)
    except SettingsError as refusal:
        print(f"FAIL  {refusal}")
        return 2
    return asyncio.run(_proving(channel, sink, parsed, table))


async def _proving(channel, sink: Collecting, parsed, table: dict[str, str]) -> int:
    failures = 0

    print("verify — the token and the chat")
    result = await channel.verify()
    print(f"  {result.outcome}: loaded={result.loaded} {result.detail}")
    failures += 0 if result.outcome.value == "pass" else 1

    print("verify — a deliberately wrong token must fail, and say which layer")
    wrong = telegram_channel(
        settings=table, environ={parsed.token_env: WRONG_TOKEN}
    )
    refused = await wrong.verify()
    print(f"  {refused.outcome}: {refused.detail}")
    if refused.outcome.value != "fail" or FailureLayer.CREDENTIALS not in refused.detail:
        print("  FAIL  a wrong token must fail at the credentials layer")
        failures += 1

    if parsed.send:
        print("send — one real message")
        receipt = await channel.send(PROOF_MESSAGE, request_id=new_request_id())
        print(f"  {receipt.outcome}: {receipt.reason or 'delivered'}")
        failures += 0 if receipt.is_delivered else 1

    if parsed.listen:
        print(f"inbound — reply in that chat within {parsed.listen}s")
        await channel.connect()
        try:
            await _waiting(sink, parsed.listen)
        finally:
            await channel.aclose()
        if not sink.events:
            print("  FAIL  nothing arrived")
            failures += 1

    print("proved" if not failures else f"{failures} check(s) failed")
    return 0 if not failures else 1


async def _waiting(sink: Collecting, seconds: float) -> None:
    """Wait for the first inbound text, or for the clock to run out."""
    deadline = asyncio.get_running_loop().time() + seconds
    while not sink.events and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.2)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat-id", required=True, help="the chat this bot should reach")
    parser.add_argument(
        "--token-env",
        default=DEFAULT_TOKEN_VARIABLE,
        help=f"the variable holding the bot token (default {DEFAULT_TOKEN_VARIABLE})",
    )
    parser.add_argument("--send", action="store_true", help="really send one message")
    parser.add_argument(
        "--listen", type=float, default=0.0, help="wait this many seconds for a reply"
    )
    parsed = parser.parse_args()
    if not os.environ.get(parsed.token_env):
        parser.error(f"${parsed.token_env} is not set; export the bot token there")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
