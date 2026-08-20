"""The composition root: configuration in, one running engine out.

This is the only place in the system that knows how the parts fit together, and
the only place allowed to import an adapter — which it does by name, from
configuration, so a deployment's own wiring stays its own. Everything else is
handed what it needs: Bridge Core gets adapters as Protocols, adapters get the
event sink, the control plane gets the hub.

Assembled here and nowhere else:

- the adapters configuration named, constructed with the one sink they speak
  upward through;
- the one Bridge Core, over the one `BridgeState`, restored from the one state
  file before anything is served;
- the control plane, and the inbound-text grammar built from *its* command set,
  so `/status` in the Companion Channel and `bridgectl status` are one command;
- the Delegated Turn handler, carrying the model from configuration — the cost
  lever is a user-facing setting and nothing here may default it;
- the generation context for Bridge Core's two instruction sets: where the
  control-plane CLI really is on this machine, and which engine it reaches.
  Only this root can know either, and it states them rather than guessing —
  configuration first, then the console script beside this interpreter, and a
  refusal when neither is really there;
- the two loops the engine needs to be alive: one that drains events into the
  hub's dispatch, and one that advances the hub's two ceilings on a timer.

**Headless is the real shape.** Nothing here knows about the menu-bar shell; the
shell is another control-plane surface plus process parenthood (ADR 0005). An
engine started from a terminal is the same engine.

The engine never daemonises: the shell spawns it as a direct child and expects
it to stay one.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import shlex
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gpt_voicecoding import __version__
from gpt_voicecoding.config import EngineConfig
from gpt_voicecoding.control_plane.actions import ControlPlane
from gpt_voicecoding.control_plane.commands import CommandError, build_request, render
from gpt_voicecoding.control_plane.server import ControlPlaneServer
from gpt_voicecoding.core.bridge import BridgeCore
from gpt_voicecoding.core.events import EventQueue
from gpt_voicecoding.core.instructions import ControlPlaneCli, InstructionContext
from gpt_voicecoding.core.persistence import StateStore
from gpt_voicecoding.core.relay_queue import RelayQueue
from gpt_voicecoding.core.router import Classification, TextGrammar
from gpt_voicecoding.core.sessions import SessionRegistry
from gpt_voicecoding.core.state import BridgeState
from gpt_voicecoding.core.switches import Switchboard
from gpt_voicecoding.core.verification import SeamLoad
from gpt_voicecoding.seams.agent import AgentAdapter
from gpt_voicecoding.seams.call import CallAdapter
from gpt_voicecoding.seams.companion_channel import CompanionChannel
from gpt_voicecoding.seams.connection import Connectable
from gpt_voicecoding.seams.control_plane import Action
from gpt_voicecoding.seams.events import EventSink
from gpt_voicecoding.seams.identity import AgentKind, new_request_id
from gpt_voicecoding.seams.session_launcher import SessionLauncher

_log = logging.getLogger(__name__)

#: How often the hub's two ceilings are advanced. Mechanism, not policy: the
#: durations themselves are configuration, this is only how finely they are read.
DEFAULT_TICK_SECONDS = 1.0

#: The console script `pyproject.toml` installs beside the interpreter.
CONTROL_PLANE_CLI_NAME = "bridgectl"

#: How an adapter factory is called. One argument, because the sink is the one
#: thing every adapter needs and the only thing this root can honestly supply.
Factory = Callable[..., Any]


class EngineAssemblyError(Exception):
    """Configuration named something this engine cannot load or construct."""


def import_factory(reference: str) -> Factory:
    """Resolve one `module:attribute` reference. The only import of an adapter."""
    module_name, _, attribute = reference.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as error:
        raise EngineAssemblyError(f"cannot import {reference}: {error}") from None
    try:
        return getattr(module, attribute)
    except AttributeError:
        raise EngineAssemblyError(
            f"cannot load {reference}: {module_name} has no {attribute!r}"
        ) from None


@dataclass(frozen=True, slots=True)
class Adapters:
    """Everything behind a seam, as this engine actually constructed it."""

    call: CallAdapter
    channel: CompanionChannel
    launcher: SessionLauncher
    agents: dict[AgentKind, AgentAdapter]

    def all(self) -> tuple[object, ...]:
        """Everything behind a seam, in the order it is opened."""
        return (self.call, self.channel, self.launcher, *self.agents.values())

    def connectable(self) -> tuple[Connectable, ...]:
        """The ones with something of their own to open and close."""
        return tuple(held for held in self.all() if isinstance(held, Connectable))


class Engine:
    """One assembled engine: a hub, its adapters, and the socket it answers on."""

    def __init__(
        self,
        *,
        config: EngineConfig,
        core: BridgeCore,
        adapters: Adapters,
        plane: ControlPlane,
        server: ControlPlaneServer,
    ) -> None:
        self._config = config
        self.core = core
        self.adapters = adapters
        self.plane = plane
        self._server = server
        self._loops: list[asyncio.Task[None]] = []

    @property
    def socket_path(self) -> Path:
        return self._server.path

    @classmethod
    def assemble(
        cls, config: EngineConfig, *, factory_of: Callable[[str], Factory] = import_factory
    ) -> Engine:
        """Build one engine from configuration. Nothing is constructed twice."""
        events = EventQueue()
        adapters = _adapters(config, events, factory_of)

        state = BridgeState(
            switches=Switchboard(),
            sessions=SessionRegistry(),
            relays=RelayQueue(),
            store=StateStore(config.state_path),
        )
        state.restore()

        # The hub needs a control handler, and the control plane needs the hub.
        # The knot is tied with one late binding rather than by giving either of
        # them a way to be half-built.
        held: dict[str, ControlPlane] = {}

        async def control(found: Classification) -> str:
            return await _answer_text(held["plane"], found)

        async def delegate(found: Classification) -> str:
            reply = await adapters.call.delegate(
                found.text, model=config.delegated_turn_model, request_id=new_request_id()
            )
            return reply.text

        core = BridgeCore(
            state=state,
            call=adapters.call,
            channel=adapters.channel,
            agents=adapters.agents,
            launcher=adapters.launcher,
            events=events,
            policy=config.policy,
            # The grammar's command words are the action set itself, so the
            # channel cannot recognise a command the control plane lacks, nor
            # miss one it has.
            grammar=TextGrammar(control_commands=frozenset(str(name) for name in Action)),
            control=control,
            delegate=delegate,
            inventory=_inventory(config),
            instruction_context=_instruction_context(config),
        )
        plane = ControlPlane(core)
        held["plane"] = plane

        return cls(
            config=config,
            core=core,
            adapters=adapters,
            plane=plane,
            server=ControlPlaneServer(plane=plane, path=config.socket_path),
        )

    async def start(self, *, tick_seconds: float = DEFAULT_TICK_SECONDS) -> None:
        """Connect the adapters, serve the control plane, and start the two loops.

        Adapters open before the socket does, so a surface that reaches a
        serving engine reaches one whose seams are actually filled. An adapter
        that fails to open stops the start: an engine answering `status` over
        seams that never connected is the reference implementation's outage
        wearing a healthy face.

        **A start that fails closes whatever it already opened.** Otherwise the
        third adapter raising leaves the first two holding a socket and a reader
        task that nothing will ever close — the caller sees an exception and has
        no handle to clean up with, because the engine never finished being one.
        """
        opened: list[Connectable] = []
        try:
            for adapter in self.adapters.connectable():
                await adapter.connect()
                opened.append(adapter)
            await self._server.start()
        except BaseException:
            await self._closing(opened)
            await self._server.aclose()  # a no-op unless this engine bound it
            raise

        self._loops = [
            asyncio.create_task(self._dispatching(), name="bridge-core-dispatch"),
            asyncio.create_task(self._ticking(tick_seconds), name="bridge-core-tick"),
        ]

    async def run(self, *, tick_seconds: float = DEFAULT_TICK_SECONDS) -> None:
        """Serve until cancelled. What `python -m gpt_voicecoding.engine` runs."""
        await self.start(tick_seconds=tick_seconds)
        try:
            await asyncio.gather(*self._loops)
        except asyncio.CancelledError:
            raise
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        """Stop answering, stop looping, close the adapters, leave no socket behind.

        In the reverse order of opening, and every adapter is given its turn
        even if an earlier one objected: a shutdown that abandoned the rest on
        the first raise is how a reader task outlives the engine that owned it.
        """
        for loop in self._loops:
            loop.cancel()
        for loop in self._loops:
            try:
                await loop
            except asyncio.CancelledError:
                pass
        self._loops = []
        await self._server.aclose()
        await self._closing(self.adapters.connectable())

    async def _closing(self, opened: Sequence[Connectable]) -> None:
        """Close what is open, newest first, and give every one of them its turn."""
        for adapter in reversed(list(opened)):
            try:
                await adapter.aclose()
            except Exception:
                _log.exception("closing %s raised", type(adapter).__name__)

    async def _dispatching(self) -> None:
        """One event at a time, in arrival order. The hub decides what each means."""
        while True:
            event = await self.core.events.next_event()
            try:
                await self.core.dispatch(event)
            except Exception:  # one bad event must not stop the engine
                _log.exception("dispatching %s raised", type(event).__name__)

    async def _ticking(self, seconds: float) -> None:
        """The only time-driven thing here: the Relay ceiling and the approval budget."""
        while True:
            await asyncio.sleep(seconds)
            try:
                await self.core.tick()
            except Exception:
                _log.exception("advancing the ceilings raised")


async def _answer_text(plane: ControlPlane, found: Classification) -> str:
    """One inbound command, answered in words — the Companion Channel's surface."""
    try:
        request = build_request(found.command, shlex.split(found.text))
    except (CommandError, ValueError) as unreadable:
        return str(unreadable)
    return render(await plane.handle(request))


def _adapters(
    config: EngineConfig, sink: EventSink, factory_of: Callable[[str], Factory]
) -> Adapters:
    def built(reference: str) -> Any:
        factory = factory_of(reference)
        try:
            return factory(sink=sink)
        except TypeError as error:
            raise EngineAssemblyError(
                f"{reference} could not be constructed with the event sink: {error}"
            ) from None

    return Adapters(
        call=built(config.adapters.call),
        channel=built(config.adapters.companion_channel),
        launcher=built(config.adapters.session_launcher),
        agents={
            agent: built(reference) for agent, reference in config.adapters.agents.items()
        },
    )


def _instruction_context(config: EngineConfig) -> InstructionContext:
    """Where the control-plane CLI is, so the generated instructions can name it.

    Stated, then derived, then refused. Configuration wins because the bundle
    moves the binary and is the only thing that knows where to; otherwise the
    console script beside this interpreter is the one this installation ships,
    and it is used only after it is found to be there and runnable. A generated
    instruction naming a CLI that does not exist is an invented detail, which is
    the first thing those instructions themselves forbid — so the last branch is
    a refusal, not a guess.
    """
    stated = config.control_plane_cli
    if stated is not None:
        if not _runnable(stated):
            raise EngineAssemblyError(
                f"[delegate] cli names {stated}, which is not there or cannot be run"
            )
        command = stated
    else:
        derived = Path(sys.executable).parent / CONTROL_PLANE_CLI_NAME
        if not _runnable(derived):
            raise EngineAssemblyError(
                f"no control-plane CLI to tell a generated thread about: {derived} is not "
                "there or cannot be run. Install this package so its console script exists, "
                "or name the one this installation ships in [delegate] cli"
            )
        command = derived

    return InstructionContext(
        cli=ControlPlaneCli(
            command=command,
            version=__version__,
            socket_path=config.socket_path,
        )
    )


def _runnable(command: Path) -> bool:
    """A file that is really there and really executable. Nothing weaker counts."""
    return command.is_file() and os.access(command, os.X_OK)


def _inventory(config: EngineConfig) -> tuple[SeamLoad, ...]:
    """The configured half of ADR 0003's question, which only this root knows.

    The loaded half is not recorded here on purpose. What this root constructed
    is what it was told to construct — recording that and calling it an
    observation is the echo the ADR exists to stop. The engine asks each adapter
    what it *is* instead, every time it is asked.
    """
    return tuple(
        SeamLoad(seam=seam, configured=reference)
        for seam, reference in config.adapters.as_mapping().items()
    )
