"""The inbox wire's own facts — the ones that are silent when they are wrong.

Each of these was found by comparing against a live Session rather than assumed
(#71), and each fails in the same quiet way: the message is delivered, nothing
errors, and the receipt this product needs simply never appears.

The route itself is exercised end to end in `test_claude_agent.py` against a real
socket. What is here is the shapes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from gpt_voicecoding.adapters.agent.claude import inbox
from gpt_voicecoding.adapters.agent.claude.inbox import (
    InboxError,
    ReplyInbox,
    correlated,
    own_process_start,
    published_start,
    user_frame,
)


@pytest.fixture
def sockets() -> Iterator[Path]:
    """A private directory short enough to bind, standing in for `/tmp/cc-socks`."""
    home = Path("/tmp") / f"vc-inbox-{os.getpid()}-{id(object())}"
    home.mkdir(mode=0o700)
    yield home
    shutil.rmtree(home, ignore_errors=True)


class TestTheUserFrame:
    def test_the_words_ride_where_a_peer_message_carries_them(self) -> None:
        frame = user_frame("ship it", msg_id="m-1", reply_to="uds:/tmp/x.sock")

        assert frame["type"] == "user"
        assert frame["message"] == {"role": "user", "content": "ship it"}
        assert frame["msgV"] == inbox.MESSAGE_VERSION

    def test_nothing_claims_a_priority_or_attests_a_permission_mode(self) -> None:
        """Two fields the wire accepts and this product may not send.

        `priority` would let this engine push in front of what a person queued.
        `from_mode` looks like an attestation and is not one: #71 sent the same
        message with and without it and it was held identically, because an
        external process cannot assert a permission class. Sending it would be
        this engine claiming something upstream does not believe.
        """
        frame = user_frame("ship it", msg_id="m-1", reply_to="uds:/tmp/x.sock")

        assert "priority" not in frame
        assert "from_mode" not in frame


class TestTheCorrelator:
    ADDRESS = "uds:/tmp/cc-socks/vc-relay-1.sock"

    def record(self, **origin: object) -> dict[str, object]:
        return {"type": "user", "isMeta": True, "origin": {"kind": "peer", **origin}}

    def test_our_own_message_arriving_is_the_proof(self) -> None:
        records = (self.record(**{"from": self.ADDRESS, "msg_id": "m-1"}),)

        assert correlated(records, msg_id="m-1", address=self.ADDRESS)

    def test_the_same_id_from_another_sender_is_not_ours(self) -> None:
        records = (self.record(**{"from": "uds:/tmp/cc-socks/someone.sock", "msg_id": "m-1"}),)

        assert not correlated(records, msg_id="m-1", address=self.ADDRESS)

    def test_a_transcript_nobody_could_read_proves_nothing(self) -> None:
        """`None` is "not read", never "read and found nothing"."""
        assert not correlated(None, msg_id="m-1", address=self.ADDRESS)

    def test_records_without_an_origin_are_passed_over(self) -> None:
        assert not correlated(({"type": "user"}, {"origin": "peer"}), msg_id="m", address="uds:/x")


class TestTheKeyThisEnginePublishes:
    """Publishing a key is how a process that is not a Session becomes a peer."""

    def test_the_key_is_named_for_the_exact_socket_path(self, sockets: Path) -> None:
        """`<pid>.<sha256(path)>.key`, and the hash is of the path we really bound.

        The receiver resolves a reply address the same way round — path, hash,
        key file, owning pid — so a key naming any other path is a key it will
        never find, and the receipt never comes.
        """
        registry = sockets / "sessions"

        async def scenario():
            replies = ReplyInbox(directory=sockets, registry_directory=registry, pid=4242)
            await replies.start()
            try:
                return replies.path, sorted(path.name for path in registry.iterdir())
            finally:
                await replies.aclose()

        path, names = asyncio.run(scenario())
        digest = hashlib.sha256(str(path).encode()).hexdigest()
        assert names == [f"4242.{digest}.key"]

    def test_it_publishes_a_key_and_never_a_session_record(self, sockets: Path) -> None:
        """Only a key: no `<pid>.json`, so no phantom row in anybody's roster."""
        registry = sockets / "sessions"

        async def scenario():
            replies = ReplyInbox(directory=sockets, registry_directory=registry, pid=4242)
            await replies.start()
            try:
                return [path.suffix for path in registry.iterdir()]
            finally:
                await replies.aclose()

        assert asyncio.run(scenario()) == [".key"]

    def test_the_key_carries_a_token_and_this_process_s_real_start_time(
        self, sockets: Path
    ) -> None:
        registry = sockets / "sessions"

        async def scenario():
            replies = ReplyInbox(directory=sockets, registry_directory=registry, pid=4242)
            await replies.start()
            try:
                return json.loads(next(registry.iterdir()).read_text())
            finally:
                await replies.aclose()

        published = asyncio.run(scenario())
        assert published["peerToken"]
        assert published["procStart"] == own_process_start()

    def test_the_address_and_the_bound_path_are_the_same_string(self, sockets: Path) -> None:
        async def scenario():
            replies = ReplyInbox(
                directory=sockets, registry_directory=sockets / "sessions", pid=4242
            )
            await replies.start()
            try:
                return replies.address, replies.path
            finally:
                await replies.aclose()

        address, path = asyncio.run(scenario())
        assert address == f"uds:{path}"

    def test_closing_takes_both_back_out(self, sockets: Path) -> None:
        """Both live in directories that are not ours, so neither may be left."""
        registry = sockets / "sessions"

        async def scenario():
            replies = ReplyInbox(directory=sockets, registry_directory=registry, pid=4242)
            await replies.start()
            await replies.aclose()
            return replies.path.exists(), list(registry.iterdir())

        assert asyncio.run(scenario()) == (False, [])

    def test_a_directory_anyone_could_enter_is_refused(self, sockets: Path) -> None:
        """A reply socket there is one a stranger could forge a `delivered` into."""
        os.chmod(sockets, 0o777)

        async def scenario():
            replies = ReplyInbox(
                directory=sockets, registry_directory=sockets / "sessions", pid=4242
            )
            with pytest.raises(InboxError, match="reachable by other accounts"):
                await replies.start()

        asyncio.run(scenario())


class TestTheStartTimeShape:
    """The conversion, against a fixed zone rather than whatever today is.

    Pinned this way because the wrong answer is invisible from the right one for
    half the day: a twelve-hour reading and a UTC reading of a local *afternoon*
    are the same string, and only a local morning separates them — by a whole
    day. A test that read the clock would have passed a twelve-hour
    implementation every afternoon and failed a correct one every night, which is
    exactly what it did until this was written.
    """

    #: UTC+12 in August, which is the offset that made #71 misread the field.
    ZONE = "Pacific/Auckland"

    @pytest.fixture(autouse=True)
    def _in_that_zone(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("TZ", self.ZONE)
        time.tzset()
        yield
        monkeypatch.undo()
        time.tzset()

    def test_a_local_afternoon_is_where_the_two_readings_agree(self) -> None:
        """21:21 local is 09:21 UTC — and `%I` of 21 is also 09. The blind spot."""
        assert published_start("Wed Aug 26 21:21:17 2026") == "Wed Aug 26 09:21:17 2026"

    def test_a_local_morning_lands_on_the_previous_day(self) -> None:
        """The case that settles it: no twelve-hour clock moves the date."""
        assert published_start("Wed Aug 26 11:15:21 2026") == "Tue Aug 25 23:15:21 2026"

    def test_the_hour_is_never_folded_into_twelve(self) -> None:
        """A start time whose UTC hour is past noon keeps it."""
        assert published_start("Wed Aug 26 05:04:03 2026") == "Tue Aug 25 17:04:03 2026"

    def test_the_day_is_space_padded_in_a_three_wide_field(self) -> None:
        """`asctime`'s shape, which is what all eleven published keys are.

        The one character here that inference rather than measurement chose:
        every sample fell on the 25th or 26th, where `%e` and `%d` agree.
        """
        assert published_start("Wed Aug  5 10:30:07 2026") == "Tue Aug  4 22:30:07 2026"

    def test_the_live_reading_is_that_conversion_of_this_process_s_own_line(self) -> None:
        """The half that is not pure, checked for shape rather than for value."""
        printed = own_process_start()

        assert re.fullmatch(r"[A-Z][a-z]{2} [A-Z][a-z]{2} [ \d]\d \d{2}:\d{2}:\d{2} \d{4}", printed)
