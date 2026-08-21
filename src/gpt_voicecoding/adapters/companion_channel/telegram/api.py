"""The only file in this repository that speaks HTTP to Telegram.

Confined on purpose, the way the audio stack is confined to one module: this
repository takes no third-party dependencies and hand-rolls the little it needs,
so "the Telegram library never reaches Bridge Core" is not a rule a dependency
list can enforce. What can be enforced is that the wire lives in exactly one
file, and `tests/test_architecture.py` asserts it — every other module in this
subpackage is ordinary Python that could not open a socket if it tried.

Two things live here and nothing else: how one Bot API method is called, and how
a refusal is **classified by the layer that produced it**. That classification is
what makes ADR 0003's liveness truthful — "Telegram did not answer" is not a
diagnosis, and an operator staring at a status line needs to know whether their
token is wrong, their network is down, or their chat id points nowhere.

The token never appears in an error message. It is in the URL every request is
built from, so an exception that quoted the URL would put a live credential into
the engine's log; failures are named by method instead.

Everything here is **synchronous and blocking**, which is what it is for: the
adapter calls it on a worker thread, so a long poll that hangs open for half a
minute never occupies the engine's event loop.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from enum import StrEnum
from typing import Any, Protocol


class FailureLayer(StrEnum):
    """Which layer of Telegram refused. A `verify` that cannot say this is not a diagnosis."""

    #: The token is wrong, revoked, or not a token at all.
    CREDENTIALS = "credentials"
    #: Nothing reached Telegram: DNS, routing, TLS, a timeout, a captive portal.
    NETWORK = "network"
    #: Telegram answered, and the chat this channel is configured for is not
    #: somewhere this bot can reach — the outage that looks healthiest.
    DESTINATION = "destination"
    #: Telegram answered and refused for a reason that is none of the above.
    API = "api"


class TelegramError(Exception):
    """One refused call, carrying the layer that refused it."""

    def __init__(self, layer: FailureLayer, message: str) -> None:
        super().__init__(message)
        self.layer = layer

    @property
    def detail(self) -> str:
        """What a `verify` result says out loud: the layer, then what happened."""
        return f"{self.layer}: {self}"


class Transport(Protocol):
    """One Bot API method, called and answered. Blocking, and called off the loop."""

    def __call__(self, method: str, payload: dict[str, Any], *, timeout_seconds: float) -> Any:
        """Return the method's `result`, or raise `TelegramError`."""
        ...


def http_transport(*, token: str, api_root: str) -> Transport:
    """The real wire, bound to one bot. The only place a URL is built."""

    root = api_root.rstrip("/")

    def call(method: str, payload: dict[str, Any], *, timeout_seconds: float) -> Any:
        request = urllib.request.Request(
            url=f"{root}/bot{token}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                body = response.read()
        except urllib.error.HTTPError as refused:
            # Read before the handle closes: the body is where Telegram says why.
            raise refused_by(method, refused.code, _description(refused.read())) from None
        except (urllib.error.URLError, TimeoutError, OSError) as unreachable:
            raise TelegramError(
                FailureLayer.NETWORK, f"{method} never reached Telegram: {unreachable}"
            ) from None
        return _answered(method, body)

    return call


def refused_by(method: str, code: int, description: str) -> TelegramError:
    """Name the layer behind one refusal, from the only two facts Telegram gives.

    `401`/`403` are the token: rejected, revoked, or blocked. `404` joins them
    because the token is part of the URL path, so a malformed one makes the
    method itself not exist. A `400` mentioning the chat is the destination —
    the bot is fine and the address is not. Everything else is the API refusing
    for its own reasons, which is a real answer and must not be dressed up as
    one of the three diagnoses above.
    """
    said = description or f"HTTP {code}"
    if code in (401, 403, 404):
        return TelegramError(FailureLayer.CREDENTIALS, f"{method} was refused: {said}")
    if code == 400 and "chat" in description.lower():
        return TelegramError(FailureLayer.DESTINATION, f"{method} could not reach the chat: {said}")
    return TelegramError(FailureLayer.API, f"{method} was refused: {said}")


def _answered(method: str, body: bytes) -> Any:
    """Read one 200 response. Telegram refuses inside a 200 as readily as with a code."""
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise TelegramError(
            FailureLayer.API, f"{method} answered with something that is not JSON"
        ) from None
    if not isinstance(document, dict):
        raise TelegramError(
            FailureLayer.API, f"{method} answered with something that is not a result"
        )
    if not document.get("ok"):
        raise refused_by(
            method, int(document.get("error_code") or 0), str(document.get("description") or "")
        )
    return document.get("result")


def _description(body: bytes) -> str:
    """Telegram's own words for a refusal, when the error body carries them."""
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    return str(document.get("description") or "") if isinstance(document, dict) else ""
