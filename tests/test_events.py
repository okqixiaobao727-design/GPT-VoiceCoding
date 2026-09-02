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


def test_unread_answers_only_about_the_kinds_it_was_asked_about() -> None:
    """A decision defers on the news it cannot be taken without, and nothing else.

    Bridge Core's ceiling asks this before measuring silence, because its own
    dispatch loop is a separate task (`engine/composition.py`) and an emitted
    `UserSpeech` has not reached the interlock yet. Asking "is anything
    waiting" instead let a queued Session event hold a silent call open (#184).
    """
    queue = EventQueue()
    queue.emit(SessionStopped(target=CODEX))

    assert queue.unread(SessionStopped) is True
    assert queue.unread(UserSpeech) is False
    assert queue.unread(UserSpeech, SessionStopped) is True


def test_news_stops_being_unread_as_soon_as_it_is_taken() -> None:
    """Taken, not finished: the dispatch loop records what an event means with
    no await between taking it and recording it, so there is no third state."""

    async def scenario() -> tuple[bool, bool]:
        queue = EventQueue()
        queue.emit(UserSpeech(text="are you there"))
        before = queue.unread(UserSpeech)
        await queue.next_event()
        return before, queue.unread(UserSpeech)

    before, after = asyncio.run(scenario())
    assert before is True
    assert after is False


def test_draining_leaves_nothing_unread() -> None:
    queue = EventQueue()
    queue.emit(UserSpeech(text="first"))
    queue.emit(SessionStopped(target=CODEX))

    queue.drain()

    assert queue.unread(UserSpeech, SessionStopped) is False
