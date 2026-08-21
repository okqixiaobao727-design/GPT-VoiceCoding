"""The default Session Launcher: a direct child on a pseudo-terminal this engine owns.

This is the adapter that needs nothing installed. It allocates a
pseudo-terminal, starts the agent on it as its own child, and keeps the master
end — so the Session is real and running on a real tty, and **nobody is looking
at it** (ADR 0008). Visibility is the tmux adapter's job, and when tmux is absent
the honest fallback is a human starting a Session themselves.

The three rules the seam fixes are structural here rather than remembered:

- **One launch per request id.** Outcomes are held by request id and a repeat
  answers with the one that was already produced. No second child, ever.
- **Truthful outcomes.** A launch is `LAUNCHED` only once the Session has
  registered itself and its readback says it is in the workspace that was asked
  for. Everything else is `FAILED`, carrying the real error and, where the child
  said something, what it actually printed.
- **Close is exact and idempotent.** A target this adapter never launched is a
  refusal, not a no-op: it is the stale-identity case, and answering "already
  closed" would tell Bridge Core a Session is gone when nothing was checked.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from pathlib import Path

from gpt_voicecoding.adapters.session_launcher.claude import ClaudeEngineFacts, ClaudePreparation
from gpt_voicecoding.adapters.session_launcher.codex import (
    CodexEngineFacts,
    CodexPreparation,
)
from gpt_voicecoding.adapters.session_launcher.console import Console, ConsoleError
from gpt_voicecoding.adapters.session_launcher.lifecycle import LaunchRegistry
from gpt_voicecoding.adapters.session_launcher.plan import (
    CONFIRM_TIMEOUT_SECONDS,
    Launch,
    Preparation,
    PreparationError,
)
from gpt_voicecoding.adapters.session_launcher.settings import LauncherSettings
from gpt_voicecoding.seams.events import EventSink
from gpt_voicecoding.seams.identity import AgentKind, RequestId, SessionTarget
from gpt_voicecoding.seams.session_launcher import (
    ChildOutcome,
    CloseOutcome,
    CloseRequest,
    CloseStatus,
    LaunchOutcome,
    LaunchRequest,
    LaunchStatus,
)
from gpt_voicecoding.seams.verify import VerifyOutcome, VerifyResult

#: How much of a failed child's output is quoted back in the outcome. The whole
#: tail would put a screen of escape sequences into a spoken sentence; this is
#: enough for the last thing it said to survive.
QUOTED_TAIL_CHARACTERS = 2000


class _OwnedAppServer:
    """The app-server hosting the direct-child adapter uses: an ordinary child.

    It dies with this engine, and that is the decision rather than an oversight
    (ADR 0008). The Session it serves runs on a pseudo-terminal the engine holds,
    so an engine that goes takes the TUI with it either way; keeping the
    app-server alive afterwards would leave a process serving a client that no
    longer exists and that nobody could ever have reached.
    """

    def __init__(self) -> None:
        self._console = Console()

    async def start(self, argv: Sequence[str], *, env, cwd: Path) -> None:
        await self._console.start(argv, env=env, cwd=cwd)

    async def close(self) -> tuple[ChildOutcome, ...]:
        if self._console.returncode is None and not self._console.is_running():
            return ()
        running = self._console.is_running()
        pid = self._console.pid if running else None
        await self._console.close()
        if pid is None:
            return ()
        return (ChildOutcome(ref=f"app-server:{pid}", closed=True),)


class _Held:
    """One live Session this adapter started, and everything it takes to end it."""

    def __init__(self, target: SessionTarget, console: Console, preparation: Preparation) -> None:
        self.target = target
        self.console = console
        self.preparation = preparation


class DirectChildLauncher:
    """A Session Launcher that needs nothing installed. The default."""

    def __init__(
        self,
        *,
        sink: EventSink | None = None,
        settings: LauncherSettings | None = None,
        confirm_timeout_seconds: float = CONFIRM_TIMEOUT_SECONDS,
    ) -> None:
        self._sink = sink
        self._settings = settings or LauncherSettings()
        #: How long a launch waits for its Session to say who it is. Not a
        #: settings key — see `plan.CONFIRM_TIMEOUT_SECONDS` — but injectable,
        #: so a test can assert on the timeout path without spending it.
        self._confirm = confirm_timeout_seconds
        #: The Agent adapters a launch has to tell where it put things. Filled by
        #: the composition root, which is the only thing allowed to know two
        #: adapters at once.
        self._claude: ClaudeEngineFacts | None = None
        self._codex: CodexEngineFacts | None = None
        #: One outcome per request id, so a repeat is answered rather than run.
        self._launches = LaunchRegistry()
        self._live: dict[SessionTarget, _Held] = {}

    # -- wiring -----------------------------------------------------------

    def use_claude(self, adapter: ClaudeEngineFacts) -> None:
        """Take the Claude Agent adapter this launch must bootstrap Sessions for."""
        self._claude = adapter

    def use_codex(self, adapter: CodexEngineFacts) -> None:
        """Take the Codex Agent adapter a launched Session must be registered with."""
        self._codex = adapter

    # -- the seam ---------------------------------------------------------

    async def launch(self, request: LaunchRequest) -> LaunchOutcome:
        """Bring exactly one Session into existence, and report what happened."""
        return await self._launches.once(request, lambda: self._launching(request))

    async def _launching(self, request: LaunchRequest) -> LaunchOutcome:
        try:
            preparation = self._preparation(request)
        except PreparationError as unpreparable:
            return _failed(request, str(unpreparable))

        console = Console()
        try:
            plan = await preparation.prepare()
            await self._start(console, plan)
            target = await preparation.confirm(
                ancestor=console.pid, still_running=console.is_running
            )
        except (PreparationError, ConsoleError) as refused:
            await self._abandon(console, preparation)
            return _failed(request, str(refused), console.tail())
        except Exception as unexpected:  # a launch must never raise into the hub
            await self._abandon(console, preparation)
            return _failed(request, f"{type(unexpected).__name__}: {unexpected}", console.tail())

        self._live[target] = _Held(target, console, preparation)
        return LaunchOutcome(
            request_id=request.request_id, status=LaunchStatus.LAUNCHED, target=target
        )

    async def close(self, request: CloseRequest) -> CloseOutcome:
        """Close exactly one Session. Idempotent; fails closed on a stale identity."""
        if self._launches.is_closed(request.target):
            return CloseOutcome(request_id=request.request_id, status=CloseStatus.ALREADY_CLOSED)
        held = self._live.get(request.target)
        if held is None:
            return CloseOutcome(
                request_id=request.request_id,
                status=CloseStatus.FAILED,
                detail=(
                    f"this launcher holds no Session {request.target}; a close it cannot "
                    "carry out must not be reported as one that happened"
                ),
            )

        already_gone = not held.console.is_running()
        children: tuple[ChildOutcome, ...] = ()
        try:
            await held.console.close()
            children = await held.preparation.discard()
        except Exception as refused:
            return CloseOutcome(
                request_id=request.request_id,
                status=CloseStatus.FAILED,
                detail=f"{type(refused).__name__}: {refused}",
                children=children,
            )

        del self._live[request.target]
        self._launches.forget(request.target)
        return CloseOutcome(
            request_id=request.request_id,
            status=CloseStatus.ALREADY_CLOSED if already_gone else CloseStatus.CLOSED,
            children=children,
        )

    async def verify(self) -> VerifyResult:
        """Report which implementation this is, and whether it can start anything.

        It reaches for the binaries rather than declaring itself well, because a
        launcher that cannot find `claude` or `codex` is exactly as useless as
        one that is not there, and ADR 0003 asks what was really loaded.
        """
        loaded = f"{type(self).__module__}:{type(self).__name__}"
        missing = []
        for agent in AgentKind:
            try:
                self._settings.binary_for(agent)
            except Exception as unrunnable:
                missing.append(str(unrunnable))
        if missing:
            return VerifyResult(
                outcome=VerifyOutcome.FAIL, loaded=loaded, detail="; ".join(missing)
            )
        return VerifyResult(outcome=VerifyOutcome.PASS, loaded=loaded)

    async def aclose(self) -> None:
        """End every Session this adapter still holds. Nothing outlives the engine."""
        for target in list(self._live):
            request = CloseRequest(request_id=RequestId(f"aclose:{target}"), target=target)
            with contextlib.suppress(Exception):
                await self.close(request)

    # -- the parts the two agents differ in -------------------------------

    def _preparation(self, request: LaunchRequest) -> Preparation:
        if request.agent is AgentKind.CLAUDE:
            if self._claude is None:
                raise PreparationError(
                    "this engine has no Claude Agent adapter, so a launched Claude Session "
                    "would have no Relay route at all"
                )
            return ClaudePreparation(
                request,
                settings=self._settings,
                engine=self._claude,
                confirm_timeout_seconds=self._confirm,
            )
        return CodexPreparation(
            request,
            settings=self._settings,
            host=_OwnedAppServer(),
            engine=self._codex,
            confirm_timeout_seconds=self._confirm,
        )

    async def _start(self, console: Console, plan: Launch) -> None:
        await console.start(plan.argv, env=plan.env, cwd=plan.cwd)

    async def _abandon(self, console: Console, preparation: Preparation) -> None:
        """Leave nothing running and nothing rendered after a launch that failed."""
        with contextlib.suppress(Exception):
            await console.close()
        with contextlib.suppress(Exception):
            await preparation.discard()


def _failed(request: LaunchRequest, detail: str, tail: str = "") -> LaunchOutcome:
    """A failure that carries the real error, and what the child said if it said anything."""
    said = tail.strip()[-QUOTED_TAIL_CHARACTERS:]
    return LaunchOutcome(
        request_id=request.request_id,
        status=LaunchStatus.FAILED,
        detail=f"{detail}\n--- what it printed ---\n{said}" if said else detail,
    )
