"""The inbound-text router — what arriving text *means*.

The Companion Channel hands text up and no opinion about it: the seam's event
has no field an adapter could use to volunteer one, because classifying it is
Bridge Core's job (ADR 0001). This is that job.

**Unknown or ambiguous input fails closed with an honest reply.** Never guessed
into a command, and never guessed into the wrong Session either — a wrong guess
delivers the user's own words, carrying the user's authority, somewhere they did
not mean. A refusal costs one round trip.

Four forms, and every marker and every command name is **injected**. The command
set belongs to the control-plane surface, so this module knows no command by
name and cannot grow one:

    /<command> …      a control-plane command
    ><prompt>         a Delegated Turn
    @<name>: words    the user's own words, for the Session that name names
    words             the user's own words, when exactly one Session is live

Bare text resolving to the single live Session is not a guess in the forbidden
sense: it classifies into the least dangerous class, and with exactly one
candidate nothing is being picked *between*. Zero or several fails closed and
asks, reusing the registry's locked "a Session Name disambiguates or asks" rule rather
than minting a second disambiguation mechanism.

The one collision worth spelling out is a bare word that is also a registered
command. It fails closed in **both** directions: bare `stop` is never promoted
into the command, and `/stop` is never injected into a Session as text. The
reply offers both readings and lets the user say which.

Asking for progress is not here as a class of its own. It is a read — a
control-plane status query — and it never touches a Session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from gpt_voicecoding.core.errors import AmbiguousNameError, NameMatchError
from gpt_voicecoding.core.sessions import Session, SessionRegistry, spoken_name
from gpt_voicecoding.seams.identity import SessionTarget


class InboundClass(StrEnum):
    """What arriving text turned out to be. Four, and the fourth is a refusal."""

    #: A status query or a switch flip. Never gated by any switch (ADR 0002).
    CONTROL = "control"
    #: The user's own words for a Session, carrying the user's authority.
    ANSWER_RELAY = "answer_relay"
    #: Work handed to a coding model on the user's behalf.
    DELEGATION = "delegation"
    #: Nothing could be said about it honestly. Carries the reply to send back.
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TextGrammar:
    """The markers and command names this router recognises. All configuration.

    Defaults are here so a test or a bare engine has a working grammar; the
    control-plane surface passes its real command set in. Nothing in this module
    may hard-code a command name — that would put half the command set here and
    half of it there.
    """

    control_prefix: str = "/"
    delegate_prefix: str = ">"
    relay_marker: str = "@"
    #: What the control plane will actually answer to. Empty means none.
    control_commands: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        markers = (self.control_prefix, self.delegate_prefix, self.relay_marker)
        for marker in markers:
            if not marker.strip():
                raise ValueError("a marker that is whitespace cannot mark anything")
        if len(set(markers)) != len(markers):
            raise ValueError(f"two forms cannot share one marker: {markers}")


@dataclass(frozen=True, slots=True)
class Classification:
    """What the text is, and what to do about it — including how to refuse."""

    kind: InboundClass
    #: The payload with its marker stripped: the command's arguments, the
    #: delegation prompt, or the words to Relay.
    text: str = ""
    #: Set for CONTROL only. The verb, already known to be registered.
    command: str = ""
    #: Set for ANSWER_RELAY only. The exact Session identity, never a name.
    target: SessionTarget | None = None
    #: Set for UNKNOWN only. What to say back — honest, and never a guess.
    reply: str = ""


class InboundRouter:
    """Classifies arriving text. Sends nothing, decides nothing else."""

    def __init__(self, *, sessions: SessionRegistry, grammar: TextGrammar | None = None) -> None:
        self._sessions = sessions
        self._grammar = grammar or TextGrammar()

    def classify(self, text: str) -> Classification:
        """Read one inbound line. Fails closed on anything it cannot place."""
        body = text.strip()
        if not body:
            return self._refuse("that arrived empty — say what you'd like me to do")

        grammar = self._grammar
        if body.startswith(grammar.control_prefix):
            return self._as_command(body[len(grammar.control_prefix) :])
        if body.startswith(grammar.delegate_prefix):
            return self._as_delegation(body[len(grammar.delegate_prefix) :])
        if body.startswith(grammar.relay_marker):
            return self._as_named_relay(body[len(grammar.relay_marker) :])
        return self._as_bare_text(body)

    def _as_command(self, rest: str) -> Classification:
        verb, _, arguments = rest.strip().partition(" ")
        if verb.casefold() not in self._grammar.control_commands:
            return self._refuse(f"I have no command called {verb!r}")
        return Classification(
            kind=InboundClass.CONTROL, command=verb.casefold(), text=arguments.strip()
        )

    def _as_delegation(self, rest: str) -> Classification:
        prompt = rest.strip()
        if not prompt:
            return self._refuse("that asked me to delegate, but did not say what")
        return Classification(kind=InboundClass.DELEGATION, text=prompt)

    def _as_named_relay(self, rest: str) -> Classification:
        name, separator, words = rest.partition(":")
        if not separator:
            return self._refuse(
                f"name the session and then the words, like "
                f"{self._grammar.relay_marker}<session>: your words"
            )
        try:
            session = self._sessions.match_name(name.strip())
        except AmbiguousNameError as ambiguous:
            return self._refuse(self._which_one(ambiguous.candidates))
        except NameMatchError:
            return self._refuse(f"nothing running matches {name.strip()!r}")

        if not words.strip():
            return self._refuse(f"that named {spoken_name(session)} but carried no words")
        return Classification(
            kind=InboundClass.ANSWER_RELAY, text=words.strip(), target=session.target
        )

    def _as_bare_text(self, body: str) -> Classification:
        live = self._sessions.live()
        if not live:
            return self._refuse("nothing is running for me to pass that to")

        collision = self._command_collision(body, live)
        if collision is not None:
            return collision

        if len(live) > 1:
            return self._refuse(self._which_one(live))
        return Classification(kind=InboundClass.ANSWER_RELAY, text=body, target=live[0].target)

    def _command_collision(self, body: str, live: tuple[Session, ...]) -> Classification | None:
        """A bare word that is also a registered command has two honest readings.

        Exact match only. Guarding a whole sentence that merely *starts* with a
        command word would swallow ordinary speech — "stop after the tests pass"
        is words for a Session and nothing else.
        """
        if body.casefold() not in self._grammar.control_commands:
            return None
        return self._refuse(
            f"{body!r} could be the command or words for a session — "
            f"say {self._grammar.control_prefix}{body.casefold()} for the command, or "
            f"{self._which_one(live)}"
        )

    def _which_one(self, candidates: tuple[Session, ...]) -> str:
        """Name every candidate and the form that picks one. Never picks itself."""
        marker = self._grammar.relay_marker
        named = ", ".join(f"{marker}{spoken_name(session)}" for session in candidates)
        return f"say which one: {named}"

    @staticmethod
    def _refuse(reply: str) -> Classification:
        return Classification(kind=InboundClass.UNKNOWN, reply=reply)
