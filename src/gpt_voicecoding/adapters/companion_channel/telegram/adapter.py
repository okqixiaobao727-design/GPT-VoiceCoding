"""The generic Telegram Companion Channel — text reach when no Live Call is up.

Mechanism only. *When* this channel is chosen, and what happens to a notice it
could not deliver, are Bridge Core's policy and live nowhere in this file. What
lives here is one link to one chat, in both directions, and an honest answer
about whether it works.

**A long poll, not a webhook.** `getUpdates` hangs open on a worker thread and
the answer comes back to the event loop; a webhook would need a public TLS
endpoint and an inbound listener on a laptop behind NAT, for no capability this
does not already have. There is deliberately no transport option, no enum and no
dormant parameter for the road not taken.

**The backlog is discarded once, at the first contact after start.** Telegram
holds undelivered updates for about a day, so an engine that has been off since
morning would otherwise wake up and act on "turn duty off" from three hours ago,
against a state that has moved — and because inbound text is unclassified by
contract, this adapter could not be selective about it even if it wanted to be.
The accepted cost is recorded rather than hidden: **a message sent while the
engine was not running is lost.** A blip *during* a run is not the same thing —
the cursor is still in memory, the engine's state never reset, and those messages
arrive when connectivity returns.

**Unreachable is not fatal.** `connect` opens the reader and raises nothing, so a
laptop that boots with no network still gets its voice path and its control
plane; the reader retries on a bounded backoff, and `verify` says FAIL out loud
for as long as the outage lasts. A missing *token*, by contrast, refuses the
start — a variable that is not set never heals on its own, and that refusal
happens in the factory, before this class exists.

**The reader is a daemon thread this adapter owns, and that is measured rather
than preferred.** `asyncio.run` joins the default executor before it returns, so
a poll parked on `asyncio.to_thread` holds the *process* open long after the
engine let go of it — measured at 0.20s to close the adapter and 3.01s to leave
`asyncio.run`, with a 3s request in flight. At the default 25s poll that is a quit
which visibly hangs, in a process the menu-bar shell spawns as its own child
(ADR 0005). A daemon thread cannot hold an exit open, and what it loses when the
interpreter takes it mid-flight is a read-only GET: the cursor lives in memory
and dies with the process either way, and Telegram re-serves anything it was
never acknowledged for. `send` and `verify` stay on `asyncio.to_thread`, where a
request timeout bounds them.

**Inbound is filtered to the configured chat, and a stranger is met with
silence.** Bridge Core routes inbound text into the control-plane command set,
and `origin` is opaque to it by design, so the only component that can tell the
user's chat from a passer-by's is this one. A reply — even a refusal — would
confirm to a prober that the bot is alive and attended, so the drop is silent
and goes to the log.
"""

from __future__ import annotations

import asyncio
import logging
import threading

from gpt_voicecoding.adapters.companion_channel.telegram.api import (
    TelegramError,
    Transport,
)
from gpt_voicecoding.adapters.companion_channel.telegram.settings import (
    MESSAGE_LIMIT_UTF16_UNITS,
    TelegramSettings,
)
from gpt_voicecoding.seams.companion_channel import InboundText
from gpt_voicecoding.seams.delivery import Delivery, DeliveryReceipt
from gpt_voicecoding.seams.events import EventSink
from gpt_voicecoding.seams.identity import RequestId
from gpt_voicecoding.seams.verify import VerifyOutcome, VerifyResult

_log = logging.getLogger(__name__)

#: The one update kind this channel is about. Asking for it by name keeps every
#: other thing Telegram might invent out of the reader loop entirely.
ALLOWED_UPDATES = ["message"]

#: What `getUpdates` is passed to make it hand back the last pending update and
#: nothing else, which is how the backlog's far end is found in one call.
LAST_UPDATE = -1

#: How long `aclose` gives the reader to notice it was stopped. Short on purpose:
#: it covers the ordinary case, where the reader is between polls, and it is
#: never the thing that lets the process exit — the daemon flag is.
JOIN_SECONDS = 0.2


def utf16_length(text: str) -> int:
    """How long Telegram thinks this string is. `len()` is the wrong ruler.

    The API's 4096 cap counts UTF-16 code units, so an emoji costs two and a
    message of 3000 emoji is over the limit while `len()` says it is not.
    """
    return len(text.encode("utf-16-le")) // 2


def split_message(text: str, *, limit: int = MESSAGE_LIMIT_UTF16_UNITS) -> tuple[str, ...]:
    """Cut one message into parts the API will accept, losing not one character.

    Truncation was considered and rejected: silently amputating the tail of a
    notice the user is meant to act on is a worse failure than sending two
    messages. The cut prefers the last line break, then the last space, then
    falls where it must — and it walks code points, so a surrogate pair is never
    split down the middle.
    """
    if utf16_length(text) <= limit:
        return (text,) if text else ()

    parts: list[str] = []
    rest = text
    while rest:
        if utf16_length(rest) <= limit:
            parts.append(rest)
            break
        hard = _prefix_within(rest, limit)
        window = rest[:hard]
        boundary = max(window.rfind("\n"), window.rfind(" "))
        cut = boundary + 1 if boundary > 0 else hard
        parts.append(rest[:cut])
        rest = rest[cut:]
    return tuple(parts)


def _prefix_within(text: str, limit: int) -> int:
    """How many characters fit, counted the way the API counts them."""
    units = 0
    for index, character in enumerate(text):
        width = 2 if ord(character) > 0xFFFF else 1
        if units + width > limit:
            return index
        units += width
    return len(text)


class TelegramCompanionChannel:
    """One bot, one chat, both ways. Implements `CompanionChannel` and `Connectable`."""

    def __init__(
        self, *, sink: EventSink | None, settings: TelegramSettings, transport: Transport
    ) -> None:
        self._sink = sink
        self._settings = settings
        self._transport = transport
        #: The next update to ask for. In memory only: it has value for the life
        #: of this process and none beyond it, and a cursor on disk would widen
        #: the durable state to something no restart should trust.
        self._offset: int | None = None
        #: Whether this process has already thrown the backlog away. Once, at
        #: first contact — never again on a mid-run reconnect.
        self._joined = False
        self._reader: threading.Thread | None = None
        #: How the reader is told to stop, and the only thing `aclose` waits on.
        self._stop = threading.Event()
        #: The loop the sink belongs to, learned at `connect`. The reader never
        #: touches anything of Bridge Core's directly — one hand-off, through
        #: `call_soon_threadsafe`, and nothing else is shared.
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- what the composition root opens and closes ------------------------

    async def connect(self) -> None:
        """Start listening. Idempotent, and never fails over an unreachable network."""
        if self._reader is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._stop.clear()
        self._reader = threading.Thread(
            target=self._polling, name="telegram-companion-channel", daemon=True
        )
        self._reader.start()

    async def aclose(self) -> None:
        """Stop listening. Idempotent, and never waits out a poll that is still open.

        The stop is a signal and a **bounded** join, not a wait: a poll can be
        parked on the network for half a minute, and an engine shutting down
        must not be held there. What guarantees the process can still leave is
        the thread being a daemon, not this join finishing — the join exists so
        that in the ordinary case, where the reader is between polls, it is
        really gone before this returns.
        """
        reader, self._reader = self._reader, None
        self._stop.set()
        if reader is None:
            return
        reader.join(timeout=JOIN_SECONDS)

    # -- the seam ---------------------------------------------------------

    async def send(self, text: str, *, request_id: RequestId) -> DeliveryReceipt:
        """Push one message, in as many parts as the API's cap requires.

        One request id, one receipt, whatever the message was cut into. The
        classification is the whole point of this method:

        - every part landed → **DELIVERED**;
        - the first part failed, so nothing reached anyone → **FAILED**, with the
          layer that refused;
        - a part failed *after* an earlier one landed → **UNKNOWN**, naming how
          much arrived. Never FAILED: words did reach the user, and FAILED means
          a positive reason to believe they did not.

        A failure **stops the send**. A later part delivered on top of a missing
        one is a message with a hole in the middle, which reads as a different
        message rather than as a broken one.

        Nothing is queued here. An unreachable network is a classified failure
        returned at once — the engine's loop is never held — and whether an
        undelivered notice is retried or retained is Bridge Core's to decide.
        """
        parts = split_message(text)
        if not parts:
            return DeliveryReceipt(
                request_id=request_id,
                outcome=Delivery.FAILED,
                reason="there were no words to send",
            )

        landed = 0
        for part in parts:
            try:
                await self._ask(
                    "sendMessage",
                    {"chat_id": self._settings.chat_id, "text": part},
                    timeout_seconds=self._settings.request_timeout_seconds,
                )
            except TelegramError as refused:
                if landed == 0:
                    return DeliveryReceipt(
                        request_id=request_id, outcome=Delivery.FAILED, reason=refused.detail
                    )
                return DeliveryReceipt(
                    request_id=request_id,
                    outcome=Delivery.UNKNOWN,
                    reason=(
                        f"{landed} of {len(parts)} parts reached the chat, then {refused.detail}"
                    ),
                )
            landed += 1
        return DeliveryReceipt(request_id=request_id, outcome=Delivery.DELIVERED)

    async def verify(self) -> VerifyResult:
        """Prove reachability positively, or name the layer that stopped it.

        Both halves are asked, because they fail differently and an operator
        needs to know which: `getMe` proves the token, `getChat` proves this bot
        can actually reach the chat it is configured for. A valid token pointed
        at a chat the bot was never added to is precisely the outage that looks
        healthiest from the outside. Both are read-only.

        `loaded` is this implementation's module string whatever the answer is —
        something real *is* loaded even when its far side is unreachable, so this
        is never MANUAL. MANUAL belongs to the null implementation alone.
        """
        loaded = f"{type(self).__module__}:{type(self).__name__}"
        try:
            await self._ask("getMe", {}, timeout_seconds=self._settings.request_timeout_seconds)
            await self._ask(
                "getChat",
                {"chat_id": self._settings.chat_id},
                timeout_seconds=self._settings.request_timeout_seconds,
            )
        except TelegramError as refused:
            return VerifyResult(outcome=VerifyOutcome.FAIL, loaded=loaded, detail=refused.detail)
        return VerifyResult(
            outcome=VerifyOutcome.PASS,
            loaded=loaded,
            detail=f"the bot answers and chat {self._settings.chat_id} is reachable",
        )

    # -- the reader -------------------------------------------------------

    def _polling(self) -> None:
        """The reader thread's whole life. Nothing raised in here may end it.

        Everything in this method runs off the event loop, including the cursor
        it advances — which is why the cursor needs no lock: it is read and
        written by this thread alone. The one thing that crosses back is a piece
        of text, and it crosses the only way it may.
        """
        while not self._stop.is_set():
            try:
                if not self._joined:
                    self._skip_backlog()
                    self._joined = True
                updates = self._transport(
                    "getUpdates", self._poll(), timeout_seconds=self._patience()
                )
            except TelegramError as unreachable:
                _log.warning("the companion channel is not reachable: %s", unreachable.detail)
                self._stop.wait(self._settings.retry_seconds)
                continue
            except Exception:  # a reader that dies is a channel that went deaf silently
                _log.exception("the companion channel's reader raised")
                self._stop.wait(self._settings.retry_seconds)
                continue
            if self._stop.is_set():
                # A poll that was already in flight when `aclose` returned still
                # comes back with a batch. Handing it up would mean an adapter
                # that said it had stopped listening putting control-plane text
                # into Bridge Core afterwards, which is worse than losing it:
                # Telegram never had these acknowledged and re-serves them to
                # whatever listens next.
                return
            for update in updates or ():
                self._heard(update)

    def _skip_backlog(self) -> None:
        """Throw away whatever accumulated while this engine was not running.

        One call, not a drain loop: asking for the last update alone gives the
        far end of the backlog, and starting the cursor past it confirms every
        update before it in the same motion Telegram already uses for that.

        This happens once per process, at the first *successful* contact. A
        network blip mid-run does not bring it back: the engine was alive
        throughout and its state never reset, so what arrives afterwards is not
        stale in the way a message from before the start is.
        """
        pending = self._transport(
            "getUpdates",
            {"offset": LAST_UPDATE, "timeout": 0, "allowed_updates": ALLOWED_UPDATES},
            timeout_seconds=self._settings.request_timeout_seconds,
        )
        for update in pending or ():
            self._advance(update)
        if pending:
            _log.info(
                "discarded %d message(s) that arrived before this engine started", len(pending)
            )

    def _poll(self) -> dict[str, object]:
        """One long poll's request, carrying the cursor when there is one."""
        asked: dict[str, object] = {
            "timeout": int(self._settings.poll_timeout_seconds),
            "allowed_updates": ALLOWED_UPDATES,
        }
        if self._offset is not None:
            asked["offset"] = self._offset
        return asked

    def _patience(self) -> float:
        """How long the *transport* waits: the poll's own hang, plus one request's."""
        return self._settings.poll_timeout_seconds + self._settings.request_timeout_seconds

    def _heard(self, update: dict) -> None:
        """One update: move the cursor, then decide whether it is the user's."""
        self._advance(update)
        message = update.get("message")
        if not isinstance(message, dict):
            return
        text = message.get("text")
        chat = message.get("chat")
        origin = str(chat.get("id", "")) if isinstance(chat, dict) else ""
        if not isinstance(text, str) or not text:
            return
        if origin != self._settings.chat_id:
            # Silence, deliberately: a refusal sent back would tell whoever is
            # probing that this bot is alive and attended.
            _log.warning("dropped inbound text from %s, which is not this channel's chat", origin)
            return
        self._hand_up(InboundText(text=text, origin=origin))

    def _hand_up(self, event: InboundText) -> None:
        """The one thing that crosses from the reader to the engine, the one legal way.

        A loop that has already closed is not an error worth raising on a
        thread nobody is watching: it means the engine went away while this poll
        was in flight, which is exactly the case the daemon flag exists for.
        """
        if self._sink is None or self._loop is None or self._stop.is_set():
            return
        try:
            self._loop.call_soon_threadsafe(self._surface, event)
        except RuntimeError:
            _log.debug("inbound text arrived after the engine stopped listening")

    def _surface(self, event: InboundText) -> None:
        """Emit, on the loop — and read the stop signal *there*, which is what makes it hold.

        The reader's own check above is an early exit and nothing more: between
        a check on one thread and a hand-off it schedules, `aclose` can run to
        completion, and the event would then reach Bridge Core after this
        adapter had said it stopped listening. Read here, the check is ordered
        against `aclose` itself — both run on this loop, so one of them is
        first and there is no gap between them to lose.
        """
        if self._sink is None or self._stop.is_set():
            return
        self._sink.emit(event)

    def _advance(self, update: dict) -> None:
        """Move the cursor past one update, whatever this adapter did with it."""
        update_id = update.get("update_id")
        if isinstance(update_id, int) and not isinstance(update_id, bool):
            self._offset = max(self._offset or 0, update_id + 1)

    async def _ask(self, method: str, payload: dict, *, timeout_seconds: float) -> object:
        """One Bot API call, on a worker thread so the engine's loop keeps turning."""
        return await asyncio.to_thread(
            self._transport, method, payload, timeout_seconds=timeout_seconds
        )
