"""The event mechanism: an adapter-side `emit`, a hub-side queue, one order."""

from __future__ import annotations

import asyncio
from typing import cast

from gpt_voicecoding.core.events import EventQueue
from gpt_voicecoding.seams.agent import ReplyWindow, ReplyWindowChanged, SessionStopped
from gpt_voicecoding.seams.call import UserSpeech
from gpt_voicecoding.seams.companion_channel import InboundText
from gpt_voicecoding.seams.events import EventSink
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget

CODEX = SessionTarget(agent=AgentKind.CODEX, session_id="abc")


def test_the_queue_is_the_sink_adapters_are_handed() -> None:
    queue = EventQueue()
    assert isinstance(queue, EventSink)


def test_emitting_needs_no_running_loop() -> None:
    """An adapter may emit from a hook process or a reader thread's callback."""
    queue = EventQueue()
    queue.emit(UserSpeech(text="are you done yet"))
    assert len(queue) == 1


def test_events_arrive_in_the_order_they_were_emitted() -> None:
    queue = EventQueue()
    first = SessionStopped(target=CODEX)
    second = ReplyWindowChanged(target=CODEX, window=ReplyWindow.OPEN)
    third = InboundText(text="ship it")

    for event in (first, second, third):
        queue.emit(event)

    assert queue.drain() == (first, second, third)


def test_draining_leaves_the_queue_empty() -> None:
    queue = EventQueue()
    queue.emit(UserSpeech(text="hello"))
    queue.drain()
    assert queue.drain() == ()


def test_a_dispatch_loop_awaits_events_one_at_a_time() -> None:
    queue = EventQueue()

    async def scenario() -> list[UserSpeech]:
        queue.emit(UserSpeech(text="first"))
        queue.emit(UserSpeech(text="second"))
        return [
            cast(UserSpeech, await queue.next_event()),
            cast(UserSpeech, await queue.next_event()),
        ]

    assert [event.text for event in asyncio.run(scenario())] == ["first", "second"]


def test_events_emitted_before_the_loop_starts_are_waiting_when_it_does() -> None:
    queue = EventQueue()
    queue.emit(UserSpeech(text="said too early"))

    async def scenario() -> object:
        return await queue.next_event()

    assert asyncio.run(scenario()) == UserSpeech(text="said too early")
