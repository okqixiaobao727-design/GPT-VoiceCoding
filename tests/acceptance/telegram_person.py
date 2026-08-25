#!/usr/bin/env python3
"""The acceptance's one actor: a real person at a real Telegram keyboard.

`docs/acceptance-design.md` allows the run exactly one stand-in, and this is it.
Everything else on the far side is real — the real bot, the real Bot API, the
real `claude` and `codex`. The person is played because a **bot cannot message a
bot**: the inbound half of the Companion Channel (`@<name>: words` arriving at
`getUpdates`) can only be produced by a user account, so the harness drives one
over MTProto with Telethon.

The same client is the run's **eyes on outbound**. What the bot sent is read back
out of the chat by a real Telegram client rather than trusted from the Bot API's
own `sendMessage` reply, because the reply proves the API accepted the call and
not that the message reached the far side.

Two boundaries this module keeps:

* **Telethon lives here and nowhere else.** `tests/test_architecture.py` lists it
  among the protocol libraries Bridge Core and the seams may not import, and the
  `acceptance` extra installs it into the developer venv only, never the bundle.
* **The peer is passed in, never guessed.** The harness resolves the bot from
  `getMe` on the Bot API and hands the username down, so this file names no bot,
  no chat and no account.

Run the one-time authorisation with:

    .venv/bin/python tests/acceptance/telegram_person.py login

and check it later with `… telegram_person.py status`.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import stat
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

#: Where the account's credentials and its authorised session live. A *location*
#: rather than a decision, so it has a default and an override — the same shape
#: the engine's own `state_path` uses. It sits beside the run directories rather
#: than among them: a run directory is named for a UTC timestamp, and this is not
#: one of those.
PERSON_DIRECTORY_VARIABLE = "GPTVOICECODING_ACCEPTANCE_PERSON_DIR"
DEFAULT_PERSON_DIRECTORY = (
    Path.home() / "Library" / "Application Support" / "GPT-VoiceCoding" / "acceptance" / "person"
)

#: Telethon appends `.session` to the name it is given, so the name is stated
#: without one and the file on disk carries it.
SESSION_STEM = "person"
CREDENTIALS_FILE = "credentials.json"

#: `my.telegram.org` issues these to a Telegram account, once. They are needed at
#: every connect and not only at login, so the login writes them down beside the
#: session; the environment overrides the file, for a machine that would rather
#: keep them somewhere else entirely.
API_ID_VARIABLE = "GPTVOICECODING_ACCEPTANCE_TG_API_ID"
API_HASH_VARIABLE = "GPTVOICECODING_ACCEPTANCE_TG_API_HASH"

#: Owner-only, on the directory and on both files it holds. The session file is a
#: bearer credential for a whole Telegram account.
PRIVATE_DIRECTORY = stat.S_IRWXU
PRIVATE_FILE = stat.S_IRUSR | stat.S_IWUSR


def _shut_down(client: TelegramClient, loop: asyncio.AbstractEventLoop) -> None:
    """Disconnect a client whose loop this code owns, then close the loop.

    `TelegramClient.disconnect` is a **dual-form** API: with the loop running it
    returns an awaitable, and with the loop stopped it runs the loop itself and
    returns `None`. Every call here is from outside the loop, so it takes the
    second path — and wrapping `None` in `run_until_complete` is a `TypeError`
    raised out of a `finally`, which is how a *successful* login came to end in a
    traceback with its session file left at 0644.
    """
    closing = client.disconnect()
    if inspect.isawaitable(closing):
        loop.run_until_complete(closing)
    if loop.is_closed():
        return
    # `disconnect` *requests* cancellation of Telethon's six background loops; a
    # loop closed in the same breath never gives them the turn they need to
    # finish, and asyncio prints "Task was destroyed but it is pending!" once per
    # task. Harmless, and six lines of noise on every acceptance run — so the
    # pending tasks are given that turn here.
    pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
    if pending:
        for task in pending:
            task.cancel()
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    loop.run_until_complete(loop.shutdown_asyncgens())
    loop.close()


class PersonError(RuntimeError):
    """The person cannot act — no credentials, no session, or no such peer."""


@dataclass(frozen=True)
class PersonMessage:
    """One message in the chat, as a real Telegram client sees it."""

    id: int
    text: str
    outgoing: bool
    date: datetime

    @property
    def from_bot(self) -> bool:
        """Sent by the bot rather than typed by the account the harness drives."""
        return not self.outgoing

    def as_journal_fields(self) -> dict[str, object]:
        return {
            "message_id": self.id,
            "direction": "sent" if self.outgoing else "received",
            "text": self.text,
            "date": self.date.isoformat(),
        }


def person_directory() -> Path:
    override = os.environ.get(PERSON_DIRECTORY_VARIABLE)
    return Path(override).expanduser() if override else DEFAULT_PERSON_DIRECTORY


def session_path(directory: Path | None = None) -> Path:
    return (directory or person_directory()) / f"{SESSION_STEM}.session"


def credentials_path(directory: Path | None = None) -> Path:
    return (directory or person_directory()) / CREDENTIALS_FILE


@dataclass(frozen=True)
class ApiCredentials:
    api_id: int
    api_hash: str


def load_credentials(directory: Path | None = None) -> ApiCredentials:
    """The account's `api_id`/`api_hash`: environment first, then the login's file.

    **Nothing here is hard-coded, and the environment always wins.**
    `GPTVOICECODING_ACCEPTANCE_TG_API_ID` and `…_TG_API_HASH` are consulted
    before the disk is touched, and `GPTVOICECODING_ACCEPTANCE_PERSON_DIR` moves
    the directory the fallback lives in.

    The fallback file exists because Telethon needs the pair on **every**
    `TelegramClient` construction while the pair is issued once, by a human, at
    `my.telegram.org` — so the alternative is not "no file" but "Simon exports
    two variables before every run, forever". `docs/acceptance-design.md`
    § Credentials chose the file for that reason: 0600, in the user's own
    application-support directory, written only by the explicit `login`
    subcommand, and never in the repository or the journal.
    """
    directory = directory or person_directory()
    from_environment = os.environ.get(API_ID_VARIABLE), os.environ.get(API_HASH_VARIABLE)
    if all(from_environment):
        api_id, api_hash = from_environment
        return ApiCredentials(int(api_id), str(api_hash))

    path = credentials_path(directory)
    if not path.exists():
        raise PersonError(
            f"no Telegram API credentials: neither {API_ID_VARIABLE}/{API_HASH_VARIABLE} in the "
            f"environment nor {path} on disk. Run `python tests/acceptance/telegram_person.py "
            f"login` once."
        )
    stored = json.loads(path.read_text())
    return ApiCredentials(int(stored["api_id"]), str(stored["api_hash"]))


def store_credentials(credentials: ApiCredentials, directory: Path | None = None) -> Path:
    directory = directory or person_directory()
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(PRIVATE_DIRECTORY)
    path = credentials_path(directory)
    path.write_text(json.dumps({"api_id": credentials.api_id, "api_hash": credentials.api_hash}))
    path.chmod(PRIVATE_FILE)
    return path


Journal = Callable[..., None]


def _no_journal(event: str, **fields: object) -> None:  # noqa: ARG001
    """The default sink: a person driven outside a run journals nowhere."""


class TelegramPerson:
    """A synchronous facade over one Telethon client, scoped to one peer.

    The harness is a pytest suite and pytest is synchronous, so the event loop is
    owned here — created, handed to Telethon, and closed with the client — rather
    than left to Telethon's implicit one. One object, one loop, one lifetime.
    """

    def __init__(
        self,
        peer: str,
        *,
        journal: Journal = _no_journal,
        directory: Path | None = None,
    ) -> None:
        self._peer_name = peer
        self._journal = journal
        self._directory = directory or person_directory()
        self._credentials = load_credentials(self._directory)
        self._loop = asyncio.new_event_loop()
        self._client = TelegramClient(
            str(session_path(self._directory).with_suffix("")),
            self._credentials.api_id,
            self._credentials.api_hash,
            loop=self._loop,
        )
        self._peer: object | None = None

    # --- lifetime ---------------------------------------------------------

    def __enter__(self) -> TelegramPerson:
        self.open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def open(self) -> None:
        """Connect, refuse if the session is not authorised, resolve the peer."""
        self._run(self._client.connect())
        if not self._run(self._client.is_user_authorized()):
            raise PersonError(
                f"the Telegram user-account session at {session_path(self._directory)} is not "
                f"authorised. Run `python tests/acceptance/telegram_person.py login` once."
            )
        self._peer = self._run(self._client.get_entity(self._peer_name))
        me = self._run(self._client.get_me())
        self._journal(
            "telegram.person.opened",
            account_id=me.id,
            account_username=me.username,
            peer=self._peer_name,
        )

    def close(self) -> None:
        _shut_down(self._client, self._loop)
        self._journal("telegram.person.closed", peer=self._peer_name)

    # --- reading and writing the chat -------------------------------------

    def latest_message_id(self) -> int:
        """The chat's high-water mark, or 0 for an empty chat.

        Every read the harness performs is *after* a mark taken before the action
        that should produce the message. Messages carry no run marker — the
        inbound grammar has no room for one — so a mark plus the run's time window
        is what separates this run's traffic from the chat's history.
        """
        latest = list(self._messages(limit=1))
        mark = latest[0].id if latest else 0
        self._journal("telegram.person.mark", message_id=mark)
        return mark

    def messages_after(self, message_id: int) -> list[PersonMessage]:
        """Every message in the chat newer than `message_id`, oldest first."""
        return sorted(self._messages(min_id=message_id), key=lambda message: message.id)

    def await_message(
        self,
        after: int,
        *,
        deadline_seconds: float,
        matching: Callable[[PersonMessage], bool] | None = None,
        poll_seconds: float = 1.0,
    ) -> PersonMessage | None:
        """Wait for one message the bot sent after `after`; None if the deadline passes.

        `None` is a legitimate answer, not an error: step 7 asserts the *absence*
        of a push over a derived window, and a raise there would be a fail dressed
        as a crash.
        """
        accept = matching or (lambda message: message.from_bot)
        expiry = time.monotonic() + deadline_seconds
        while True:
            for message in self.messages_after(after):
                if accept(message):
                    self._journal("telegram.person.read", **message.as_journal_fields())
                    return message
            if time.monotonic() >= expiry:
                self._journal(
                    "telegram.person.absent", after=after, waited_seconds=deadline_seconds
                )
                return None
            time.sleep(min(poll_seconds, max(0.0, expiry - time.monotonic())))

    def send(self, text: str) -> PersonMessage:
        """Type one line into the chat, as the person would."""
        sent = self._run(self._client.send_message(self._peer, text))
        message = _as_person_message(sent)
        self._journal("telegram.person.sent", **message.as_journal_fields())
        return message

    # --- plumbing ---------------------------------------------------------

    def _messages(self, **query: object) -> Iterator[PersonMessage]:
        raw = self._run(self._collect(**query))
        return (_as_person_message(message) for message in raw)

    async def _collect(self, **query: object) -> list[object]:
        return [message async for message in self._client.iter_messages(self._peer, **query)]

    def _run(self, coroutine):  # noqa: ANN001, ANN202 - Telethon's own return types
        return self._loop.run_until_complete(coroutine)


def _as_person_message(message: object) -> PersonMessage:
    return PersonMessage(
        id=int(message.id),
        text=str(message.message or ""),
        outgoing=bool(message.out),
        date=message.date,
    )


# --- the one-time login ---------------------------------------------------


def _prompt(question: str) -> str:
    answer = input(question).strip()
    if not answer:
        raise PersonError("nothing entered")
    return answer


def login(directory: Path | None = None) -> int:
    """Authorise the account once, interactively. Ticket #57 is this function's run."""
    directory = directory or person_directory()
    try:
        credentials = load_credentials(directory)
        print(f"Using the api_id already stored at {credentials_path(directory)}.")
    except PersonError:
        print(
            "This account needs an api_id and api_hash from https://my.telegram.org "
            "→ API development tools. They are issued once, per account."
        )
        credentials = ApiCredentials(int(_prompt("api_id: ")), _prompt("api_hash: "))
        stored = store_credentials(credentials, directory)
        print(f"Stored 0600 at {stored}.")

    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(PRIVATE_DIRECTORY)
    loop = asyncio.new_event_loop()
    client = TelegramClient(
        str(session_path(directory).with_suffix("")),
        credentials.api_id,
        credentials.api_hash,
        loop=loop,
    )
    try:
        loop.run_until_complete(client.connect())
        if loop.run_until_complete(client.is_user_authorized()):
            me = loop.run_until_complete(client.get_me())
            print(f"Already authorised as {me.first_name} (@{me.username}, id {me.id}).")
            return 0
        phone = _prompt("phone number, with country code (e.g. +64…): ")
        loop.run_until_complete(client.send_code_request(phone))
        code = _prompt("the code Telegram just sent: ")
        try:
            loop.run_until_complete(client.sign_in(phone, code))
        except SessionPasswordNeededError:
            password = _prompt("two-step verification password: ")
            loop.run_until_complete(client.sign_in(password=password))
        me = loop.run_until_complete(client.get_me())
        print(f"Authorised as {me.first_name} (@{me.username}, id {me.id}).")
    finally:
        # Before the disconnect, not after: the session file is a bearer
        # credential for a whole account, and a shutdown that raises must not be
        # what decides whether it is readable by everyone on this machine.
        written = session_path(directory)
        if written.exists():
            written.chmod(PRIVATE_FILE)
            print(f"Session written 0600 at {written}.")
        _shut_down(client, loop)
    return 0


def status(directory: Path | None = None) -> int:
    """Report whether a run could use this account. Preflight asks the same question."""
    directory = directory or person_directory()
    session = session_path(directory)
    if not session.exists():
        print(f"NOT AUTHORISED: no session file at {session}")
        return 1
    try:
        credentials = load_credentials(directory)
    except PersonError as refusal:
        print(f"NOT AUTHORISED: {refusal}")
        return 1
    loop = asyncio.new_event_loop()
    client = TelegramClient(
        str(session.with_suffix("")), credentials.api_id, credentials.api_hash, loop=loop
    )
    try:
        loop.run_until_complete(client.connect())
        if not loop.run_until_complete(client.is_user_authorized()):
            print(f"NOT AUTHORISED: the session at {session} has been revoked or never signed in")
            return 1
        me = loop.run_until_complete(client.get_me())
        print(f"AUTHORISED as {me.first_name} (@{me.username}, id {me.id}); session {session}")
        return 0
    finally:
        _shut_down(client, loop)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=("login", "status"))
    arguments = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return login() if arguments.command == "login" else status()
    except PersonError as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
