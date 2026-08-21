"""The optional Session Launcher: a window a human can attach to and take over.

This is the **only** module in this system that knows tmux exists, and an
architecture test holds it to that. Bridge Core sees a workspace going in and an
outcome coming back; a tmux server, session, window or pane is this file's
business and never crosses the seam. `ChildOutcome.ref` is an opaque
adapter-owned string for exactly that reason.

**What tmux buys, and it is only one thing: visibility.** The direct-child
adapter is headless by design (ADR 0008). This adapter puts the same Session in a
window, so a human can attach, watch, and answer anything that stops for them —
including the first-run dialogs a headless Session would stall on invisibly.
Nothing else about launching changes: the same argv, the same environment, the
same readback.

**Two things follow from the child not being ours.**

*The environment.* A tmux Session is not forked by this engine at all — an
already-running tmux server forks it, and what it inherits is the environment
that server was started with, possibly days ago, in a shell this engine never
saw. That is precisely how ADR 0004's outage happened: `MallocStackLogging` was
set by nobody in the repository and inherited from the installing shell. So the
pane command is `env -i` and the environment is stated in full — default-deny,
which is stronger than removing names somebody had to know to list. See
`environment`.

*The log.* Obligation 2 of ADR 0004 — give the child a pipe rather than a
descriptor on the engine's log — has nothing to bite on here, and this is a
ruling rather than an omission: the problem it solves is a child *this engine
forked* inheriting the engine's redirected stdout. A tmux pane never had one. Its
output belongs to the tmux server's pane buffer, which is this adapter's own and
is never the engine's log. Bridge Core enumerates no adapter's log and neither
does the engine.

**And the app-server outlives this engine, on purpose.** A visible Session is one
a human is using, so an engine restart must not take it down. Handing the
per-TUI app-server to the tmux server rather than detaching it keeps the original
ownership shape — the terminal owns it, the engine never does — and keeps it
*visible*: an orphan in the tmux window list is one somebody can find and clean
up, which a `setsid` orphan in the process table is not.
"""

from __future__ import annotations

import asyncio
import contextlib
import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path

from gpt_voicecoding.adapters.session_launcher.claude import ClaudeEngineFacts, ClaudePreparation
from gpt_voicecoding.adapters.session_launcher.codex import CodexEngineFacts, CodexPreparation
from gpt_voicecoding.adapters.session_launcher.lifecycle import LaunchRegistry
from gpt_voicecoding.adapters.session_launcher.plan import (
    CONFIRM_TIMEOUT_SECONDS,
    Preparation,
    PreparationError,
)
from gpt_voicecoding.adapters.session_launcher.settings import LauncherSettings, SettingsError
from gpt_voicecoding.seams.events import EventSink
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget
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

#: The program that runs the pane command with an environment this launcher
#: states in full. `-i` starts from nothing at all, so the tmux server's own
#: environment — whatever shell started it, whenever — reaches the child through
#: no path whatsoever.
ENV_COMMAND = "/usr/bin/env"

#: How long one tmux command is given. tmux answers locally and immediately; a
#: bound exists so a wedged server cannot hold a launch open forever.
TMUX_TIMEOUT_SECONDS = 15.0

#: How much of a window's screen is quoted back when a launch fails.
QUOTED_TAIL_CHARACTERS = 2000


class TmuxError(Exception):
    """A tmux command could not be run, or refused."""


class Tmux:
    """Every tmux command this adapter runs, and the only place one is spelled.

    A class rather than loose functions so a contract test can put a fake tmux
    behind the adapter and assert on what it was asked to do — including that the
    pane command carries no variable the tmux server happened to be holding.
    """

    def __init__(self, binary: Path, *, session: str) -> None:
        self._binary = binary
        self._session = session
        #: Held while the session is being ensured, so two launches starting at
        #: once do not both try to create it. See `ensure_session`.
        self._creating = asyncio.Lock()

    @property
    def session(self) -> str:
        return self._session

    async def run(self, *arguments: str) -> str:
        """One tmux command, or a refusal carrying what tmux itself said."""
        try:
            process = await asyncio.create_subprocess_exec(
                str(self._binary),
                *arguments,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(process.communicate(), TMUX_TIMEOUT_SECONDS)
        except (OSError, TimeoutError) as unrunnable:
            raise TmuxError(f"tmux {' '.join(arguments)}: {unrunnable}") from None
        if process.returncode != 0:
            said = err.decode("utf-8", errors="replace").strip() or "no reason given"
            raise TmuxError(f"tmux {' '.join(arguments)} failed: {said}")
        return out.decode("utf-8", errors="replace").strip()

    async def is_available(self) -> bool:
        """Whether there is a runnable tmux here at all."""
        try:
            await self.run("-V")
        except TmuxError:
            return False
        return True

    async def ensure_session(self) -> None:
        """Make the session if it is not there, without ever trying to attach.

        Asking first and creating only if absent, rather than the shorter
        `new-session -A`. `-A` means "attach if it already exists", and attaching
        needs a terminal — so it fails with `open terminal failed: not a
        terminal` in exactly the situation this adapter always runs in: an engine
        with no tty of its own. It works when run by hand from inside tmux, which
        is what makes it the kind of bug that reaches production; a real launch
        from a daemon found it.

        **Asking and creating are two commands, so they are a race**, and the
        losing side of it must not fail a perfectly good launch: two launches
        starting at once can both find the session missing, and the second
        `new-session` then refuses with `duplicate session`. What was wanted was
        the session existing, and it does — so the refusal is re-checked rather
        than propagated, and only a session that is still not there is an error.
        The in-process lock makes that collision rare; the re-check is what makes
        it harmless, including against a tmux somebody else is also using.
        """
        async with self._creating:
            try:
                await self.run("has-session", "-t", self._session)
                return
            except TmuxError:
                pass
            try:
                await self.run("new-session", "-d", "-s", self._session)
            except TmuxError as refused:
                try:
                    await self.run("has-session", "-t", self._session)
                except TmuxError:
                    raise refused from None

    async def open_window(self, name: str, command: str, *, cwd: Path) -> str:
        """Make the session if it is not there, then a window, and answer with its id."""
        await self.ensure_session()
        return await self.run(
            "new-window",
            "-d",
            "-t",
            f"{self._session}:",
            "-n",
            name,
            "-c",
            str(cwd),
            "-P",
            "-F",
            "#{window_id}",
            command,
        )

    async def pane_pid(self, window: str) -> int:
        """The pid at the top of a window's pane — a launch's ancestry root."""
        answer = await self.run("display-message", "-p", "-t", window, "#{pane_pid}")
        if not answer.isdigit():
            raise TmuxError(f"tmux gave no pane pid for {window}: {answer!r}")
        return int(answer)

    async def is_live(self, window: str) -> bool:
        """Whether that window is still there — and a refusal when that cannot be told.

        The window is looked for in a **list**, rather than addressed directly and
        the error swallowed. Addressing it conflates two answers that must stay
        apart: "tmux says there is no such window" is a Session that exited, and
        "tmux could not be reached" is a question with no answer at all. Treating
        the second as the first is fail-*open* — it would let a close report a
        live Session as gone on the strength of a transport error, and then
        forget it. So a listing that fails raises, and the caller reports that it
        could not tell.
        """
        listed = await self.run("list-windows", "-a", "-F", "#{window_id}")
        return window in listed.split()

    async def screen(self, window: str) -> str:
        """What is on that window's screen, for quoting back when a launch fails."""
        try:
            return await self.run("capture-pane", "-p", "-t", window)
        except TmuxError:
            return ""

    async def kill_window(self, window: str) -> None:
        await self.run("kill-window", "-t", window)


def pane_command(argv: Sequence[str], env: Mapping[str, str]) -> str:
    """One shell-safe line that runs `argv` with exactly `env` and nothing else.

    Every assignment and every argument is quoted. This is not defensive style:
    an interpreter path under "Application Support" contains a space, and the one
    time that went unquoted the launch failed with 127 and the only symptom was a
    permission dialog nobody ever answered.
    """
    assignments = [f"{name}={value}" for name, value in sorted(env.items())]
    return " ".join(shlex.quote(piece) for piece in [ENV_COMMAND, "-i", *assignments, *argv])


class _TmuxAppServer:
    """The per-TUI app-server, in a window of its own that outlives this engine."""

    def __init__(self, tmux: Tmux, *, name: str) -> None:
        self._tmux = tmux
        self._name = name
        self._window: str | None = None

    async def start(self, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path) -> None:
        self._window = await self._tmux.open_window(self._name, pane_command(argv, env), cwd=cwd)

    async def close(self) -> tuple[ChildOutcome, ...]:
        """Kill the window, and report truthfully — this adapter really owns it.

        **The window is let go of only once it is really gone.** Clearing it up
        front looks like ordinary take-once bookkeeping and is the opposite: a
        kill that fails would leave this host with nothing to name, so the *next*
        close would find no child to report, answer with an empty `children`, and
        let the Session be recorded as fully closed while the app-server was
        still running. Keeping the identity is what makes a retry possible and
        keeps the second outcome as truthful as the first.
        """
        window = self._window
        if window is None:
            return ()
        if not await self._tmux.is_live(window):
            self._window = None
            return (ChildOutcome(ref=f"app-server:{window}", closed=True, detail="already gone"),)
        try:
            await self._tmux.kill_window(window)
        except TmuxError as refused:
            return (ChildOutcome(ref=f"app-server:{window}", closed=False, detail=str(refused)),)
        self._window = None
        return (ChildOutcome(ref=f"app-server:{window}", closed=True),)


class _Held:
    """One live Session this adapter started, and the window it lives in."""

    def __init__(self, target: SessionTarget, window: str, preparation: Preparation) -> None:
        self.target = target
        self.window = window
        self.preparation = preparation


class TmuxLauncher:
    """A Session Launcher that puts a Session where a human can take it over."""

    def __init__(
        self,
        *,
        sink: EventSink | None = None,
        settings: LauncherSettings | None = None,
        confirm_timeout_seconds: float = CONFIRM_TIMEOUT_SECONDS,
        tmux: Tmux | None = None,
    ) -> None:
        self._sink = sink
        self._settings = settings or LauncherSettings()
        #: How long a launch waits for its Session to say who it is. Not a
        #: settings key — see `plan.CONFIRM_TIMEOUT_SECONDS` — but injectable,
        #: so a test can assert on the timeout path without spending it.
        self._confirm = confirm_timeout_seconds
        self._tmux = tmux
        self._claude: ClaudeEngineFacts | None = None
        self._codex: CodexEngineFacts | None = None
        self._launches = LaunchRegistry()
        self._live: dict[SessionTarget, _Held] = {}

    def use_claude(self, adapter: ClaudeEngineFacts) -> None:
        self._claude = adapter

    def use_codex(self, adapter: CodexEngineFacts) -> None:
        self._codex = adapter

    # -- the seam ---------------------------------------------------------

    async def launch(self, request: LaunchRequest) -> LaunchOutcome:
        return await self._launches.once(request, lambda: self._launching(request))

    async def _launching(self, request: LaunchRequest) -> LaunchOutcome:
        try:
            tmux = self._layer()
        except SettingsError as absent:
            # Not a failed launch: this adapter cannot run here at all, which is
            # a different thing and the seam has a different word for it.
            return LaunchOutcome(
                request_id=request.request_id,
                status=LaunchStatus.UNAVAILABLE,
                detail=str(absent),
            )
        if not await tmux.is_available():
            return LaunchOutcome(
                request_id=request.request_id,
                status=LaunchStatus.UNAVAILABLE,
                detail="tmux is not runnable on this machine, so this adapter cannot launch",
            )

        try:
            preparation = self._preparation(request, tmux)
        except PreparationError as unpreparable:
            return _failed(request, str(unpreparable))

        window: str | None = None
        try:
            plan = await preparation.prepare()
            window = await tmux.open_window(
                _window_name(request), pane_command(plan.argv, plan.env), cwd=plan.cwd
            )
            ancestor = await tmux.pane_pid(window)
            target = await preparation.confirm(ancestor=ancestor, still_running=None)
        except (PreparationError, TmuxError) as refused:
            screen = await tmux.screen(window) if window else ""
            await self._abandon(tmux, window, preparation)
            return _failed(request, str(refused), screen)
        except Exception as unexpected:
            screen = await tmux.screen(window) if window else ""
            await self._abandon(tmux, window, preparation)
            return _failed(request, f"{type(unexpected).__name__}: {unexpected}", screen)

        assert window is not None
        self._live[target] = _Held(target, window, preparation)
        return LaunchOutcome(
            request_id=request.request_id, status=LaunchStatus.LAUNCHED, target=target
        )

    async def close(self, request: CloseRequest) -> CloseOutcome:
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
        try:
            tmux = self._layer()
        except SettingsError as absent:
            return CloseOutcome(
                request_id=request.request_id,
                status=CloseStatus.UNAVAILABLE,
                detail=str(absent),
            )

        children: tuple[ChildOutcome, ...] = ()
        try:
            # A tmux that cannot be asked leaves this close with no answer, and
            # "no answer" is a failure rather than a Session presumed gone.
            already_gone = not await tmux.is_live(held.window)
            if not already_gone:
                await tmux.kill_window(held.window)
            children = await held.preparation.discard()
        except Exception as refused:
            return CloseOutcome(
                request_id=request.request_id,
                status=CloseStatus.FAILED,
                detail=f"{type(refused).__name__}: {refused}",
                children=children,
            )

        # A child this adapter could not take down keeps the close honest twice
        # over: the outcome says the app-server survived, **and** the Session
        # stays live here so a repeat tries again. Forgetting it would turn the
        # next close into a cheerful `already_closed` about a running process.
        if any(not child.closed for child in children):
            return CloseOutcome(
                request_id=request.request_id,
                status=CloseStatus.FAILED,
                detail="the Session's window is gone, but something it owned is not",
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
        """Whether tmux is really here — the one thing this adapter needs and may lack."""
        loaded = f"{type(self).__module__}:{type(self).__name__}"
        try:
            tmux = self._layer()
        except SettingsError as absent:
            return VerifyResult(outcome=VerifyOutcome.FAIL, loaded=loaded, detail=str(absent))
        if not await tmux.is_available():
            return VerifyResult(
                outcome=VerifyOutcome.FAIL,
                loaded=loaded,
                detail="tmux is named but does not run here",
            )
        return VerifyResult(outcome=VerifyOutcome.PASS, loaded=loaded)

    async def aclose(self) -> None:
        """Leave every Session running. That is what this adapter is *for*.

        A visible Session belongs to the human looking at it, and an engine
        shutting down is not a reason to close their editor. The windows stay,
        the app-servers stay, and a restarted engine attaches to them again.
        """
        return None

    # -- the parts the two agents differ in -------------------------------

    def _layer(self) -> Tmux:
        if self._tmux is not None:
            return self._tmux
        return Tmux(self._settings.tmux(), session=self._settings.tmux_session_name)

    def _preparation(self, request: LaunchRequest, tmux: Tmux) -> Preparation:
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
            host=_TmuxAppServer(tmux, name=f"app-server-{request.request_id[:8]}"),
            engine=self._codex,
            confirm_timeout_seconds=self._confirm,
        )

    async def _abandon(self, tmux: Tmux, window: str | None, preparation: Preparation) -> None:
        if window is not None:
            with contextlib.suppress(Exception):
                if await tmux.is_live(window):
                    await tmux.kill_window(window)
        with contextlib.suppress(Exception):
            await preparation.discard()


def _window_name(request: LaunchRequest) -> str:
    """A window name a human can recognise in a list, from the Session Label.

    tmux treats `.` and `:` as address separators, so a name carrying one would
    make the window awkward to address; the label's own text is otherwise kept.
    """
    return "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in f"{request.label.project}-{request.label.task}"
    )[:60]


def _failed(request: LaunchRequest, detail: str, screen: str = "") -> LaunchOutcome:
    said = screen.strip()[-QUOTED_TAIL_CHARACTERS:]
    return LaunchOutcome(
        request_id=request.request_id,
        status=LaunchStatus.FAILED,
        detail=f"{detail}\n--- what was on screen ---\n{said}" if said else detail,
    )
