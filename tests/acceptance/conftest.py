"""Preflight, and the fixtures a lane's journey is walked on.

**Preflight refuses rather than runs.** A run that starts against the wrong
environment produces a verdict that cannot be attributed to this engine
(`docs/acceptance-design.md` § Preflight refuses rather than runs), and a verdict
that cannot be attributed is worse than no verdict — it is a false one, kept.
So every check below either passes or ends the session with `REFUSED` written
down and the reason on the terminal.

Both lanes run sequentially against a **fresh engine and a fresh workspace**, and
never share either. The engine is spawned by the harness, not by the menu-bar
shell — repeatability over coverage, and the shell is out of scope.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip(
    "telethon",
    reason="the acceptance's one actor is a Telegram user account: pip install -e '.[acceptance]'",
)

import hand_started  # noqa: E402
import journey as journey_module  # noqa: E402
import support  # noqa: E402 - after the skip, so a venv without the extra collects cleanly
import telegram_person  # noqa: E402

from gpt_voicecoding.adapters.companion_channel.telegram.api import (  # noqa: E402
    TelegramError,
    http_transport,
)
from gpt_voicecoding.adapters.companion_channel.telegram.settings import (  # noqa: E402
    DEFAULT_API_ROOT,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
)
from gpt_voicecoding.config import default_socket_path  # noqa: E402

REPOSITORY = Path(__file__).resolve().parents[2]

#: The far-side waits. `docs/acceptance-design.md` § Deadlines forbids a guess, so
#: each one below says what it rests on — and one of them says, honestly, that it
#: could not be measured yet.
#:
#: Measured at build time on this machine (2026-08-25, ticket #60), against
#: `claude` 2.1.243 and `codex-cli` 0.149.1, with the bundle at `b55c454`:
#:
#:   * a relay's reply arrives at **45.1s**, which is the engine's own
#:     `DEFAULT_ACK_TIMEOUT_SECONDS` resolving, not a model thinking.
#:
#: Re-measured 2026-08-26 on ticket #73 against `claude` 2.1.246, this time with
#: **a real turn**, which #60 could not run because nothing on `main` got words
#: into a Session. A hand-started `claude` in a pty, given one file-writing
#: instruction at `--permission-mode default`, went `idle → busy → waiting` in
#: seconds, and `idle` again within seconds of the permission being answered —
#: the TUI's own figure for the turn was 4s. The number below is no longer a
#: derivation with nothing behind it; it is that measurement with room for a
#: model having a slow day. `Walk` journals every turn it drives as
#: `event: "turn"` with its seconds, so each run sharpens this rather than
#: re-deriving it.
FAR_SIDE = support.FarSideDeadlines(
    # Measured: seconds, not minutes. Kept at three times the engine's own 45s
    # proof wait so that a turn which is merely slow is never read as a turn that
    # never happened — the failure this number exists to keep apart.
    agent_turn_seconds=180.0,
    # A message crossing the real Bot API and coming back through MTProto. The
    # engine's own `getUpdates` long-poll is 25s (`telegram/settings.py:39`), so
    # anything under that would time the poll rather than the round trip.
    telegram_round_trip_seconds=90.0,
    # A file appearing in the workspace after the words that asked for it: the
    # turn figure, since that is what has to happen first.
    workspace_effect_seconds=180.0,
    # Step 7's negative observation: how long "not pushed" has to hold to mean it.
    # Derived, not chosen — one long-poll cycle (25s) with room for a retry
    # (`retry_seconds` 5s) and a round trip, so a Duty-off Notice that *was* going
    # to arrive has had every chance to.
    absence_window_seconds=120.0,
)


class PreflightRefused(Exception):
    """The environment is not one this run can be attributed to."""


#: The run's verdict, once there is one, so an ordinary refusal lands on the same
#: record every step lands on.
_verdict: support.Verdict | None = None

#: Where a refusal writes when there is **not** one yet. Made on first ask rather
#: than by a fixture: `preflight` is autouse and lists `engine_path` before
#: `run_directory`, so the fixture graph resolves the one that can refuse *first*
#: and a refusal that waited for the fixture would find nothing there. Memoised,
#: so the fixture and a refusal always name the same directory.
_run_directory: Path | None = None


def _ensure_run_directory() -> Path:
    global _run_directory
    if _run_directory is None:
        _run_directory = support.new_run_directory()
        print(f"\nacceptance run directory: {_run_directory}")
    return _run_directory


def _refuse(reason: str) -> None:
    """Refuse, and leave the reason somewhere that outlives the terminal.

    `docs/acceptance-design.md` § Preflight: a refusal produces "verdict
    `REFUSED` with the reason". Raising alone put the reason on stderr and
    nowhere else, so a run that refused left an artifact directory whose
    `verdict.json` did not say why — or no `verdict.json` at all.

    **The refusals that matter most happen before there is a `Verdict` to write
    on**, and that is not an edge case — it is the ordinary shape of this
    fixture graph. `verdict` is built from `bundle`, `provenance` and
    `engine_path`, so a PATH that cannot be read, or a bundle with no
    interpreter to ask for a version, refuses *while `verdict` is still being
    constructed*. Recording only when a `Verdict` already exists therefore
    silences exactly the environment failures step 0 exists to report.

    So a refusal with no verdict writes its own: the minimum a reader needs to
    know why this run directory has nothing else in it. The directory is made on
    first ask rather than taken from the fixture, because `preflight` is autouse
    and lists `engine_path` **before** `run_directory` — a refusal that waited
    for the fixture would find nothing there, which is exactly what the first
    attempt at this did.
    """
    if _verdict is not None:
        _verdict.refuse("preflight", reason)
    else:
        support.write_refusal(_ensure_run_directory(), reason)
    raise PreflightRefused(reason)


@pytest.fixture(scope="session")
def far_side() -> support.FarSideDeadlines:
    """The far-side waits this run used, so the journal and the verdict agree on them."""
    return FAR_SIDE


@pytest.fixture(scope="session")
def bundle() -> Path:
    return support.bundle_path()


@pytest.fixture(scope="session")
def engine_path() -> str:
    """The PATH the engine will be handed — the login shell's, as the shell reads it."""
    resolved = support.login_shell_path()
    if resolved is None:
        _refuse(
            "could not read a usable PATH from the login shell, so the engine would run on "
            "launchd's and find neither agent. `shell/Sources/ShellCore/LoginShellPath.swift` "
            "is the method being mirrored."
        )
    return resolved


@pytest.fixture(scope="session")
def run_directory() -> Path:
    return _ensure_run_directory()


@pytest.fixture(scope="session")
def journal(run_directory: Path) -> support.Journal:
    return support.Journal(run_directory / "journal.jsonl")


@pytest.fixture(scope="session")
def provenance(bundle: Path) -> support.Provenance:
    return support.compare_engine_to_tree(bundle, REPOSITORY)


@pytest.fixture(scope="session")
def bot_token() -> str:
    source = support.source_config_path()
    if not source.exists():
        _refuse(f"no engine configuration at {source} to derive this run's from")
    import tomllib

    channel = tomllib.loads(source.read_text())["adapters"]["settings"]["companion_channel"]
    try:
        return support.token_from_environment(str(channel["token_env"]))
    except LookupError as missing:
        _refuse(str(missing))
        raise  # unreachable; for the type checker


@pytest.fixture(scope="session")
def bot(bot_token: str, journal: support.Journal) -> dict:
    """`getMe` against the real Bot API: the bot answers, and says who it is.

    The username it returns is what the user-account client resolves as its peer,
    so no bot is named anywhere in this suite.
    """
    transport = http_transport(token=bot_token, api_root=DEFAULT_API_ROOT)
    try:
        identity = transport("getMe", {}, timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS)
    except TelegramError as unreachable:
        _refuse(f"the Telegram bot did not answer getMe: {unreachable.detail}")
    journal("preflight.getMe", username=identity["username"], id=identity["id"])
    return identity


@pytest.fixture(scope="session")
def person(bot: dict, journal: support.Journal) -> Iterator[telegram_person.TelegramPerson]:
    """The one actor. Refuses here rather than mid-journey if it is not authorised."""
    try:
        actor = telegram_person.TelegramPerson(f"@{bot['username']}", journal=journal)
        actor.open()
    except telegram_person.PersonError as unauthorised:
        _refuse(str(unauthorised))
    try:
        yield actor
    finally:
        actor.close()


@pytest.fixture(scope="session", autouse=True)
def preflight(
    bundle: Path,
    provenance: support.Provenance,
    engine_path: str,
    run_directory: Path,
    journal: support.Journal,
    verdict: support.Verdict,
    bot: dict,
    person: telegram_person.TelegramPerson,
) -> None:
    """Step 0. Everything here is a refusal, never a failure."""
    if not bundle.exists():
        _refuse(f"no bundle at {bundle}")
    if not support.bundled_python(bundle).exists():
        _refuse(f"the bundle at {bundle} carries no engine interpreter")
    if not provenance.matches:
        _refuse(provenance.reason)

    live = default_socket_path()
    if live.exists():
        _refuse(
            f"the shell's engine is answering at {live} — one bot, one engine "
            f"(`docs/app-bundle.md` § Cutover). Quit the menu-bar app and run again; "
            f"this run will not stop it for you."
        )

    for binary in ("claude", "codex"):
        import shutil

        if shutil.which(binary, path=engine_path) is None:
            _refuse(f"`{binary}` does not resolve on the PATH the engine will be handed")

    verdict.environment = support.environment_facts()
    journal("preflight.environment", **verdict.environment)
    journal(
        "preflight.passed",
        bundle=str(bundle),
        commit=provenance.commit,
        provenance=provenance.reason,
        engine_path=engine_path,
        bot=bot["username"],
        far_side_deadlines=vars(FAR_SIDE),
    )


@pytest.fixture(scope="session")
def verdict(
    run_directory: Path, bundle: Path, provenance: support.Provenance, engine_path: str
) -> Iterator[support.Verdict]:
    global _verdict
    record = support.Verdict(
        run_id=run_directory.name,
        bundle=str(bundle),
        commit=provenance.commit,
        provenance=provenance.reason,
        # What this run promised to observe. `Verdict.result` will not say PASS
        # while any of it is missing, so a lane that never ran cannot be silently
        # absent from a green verdict.
        expected_lanes=tuple(lane.name for lane in journey_module.LANES),
        expected_steps=journey_module.STEPS,
        versions={
            "claude": support.binary_version("claude", engine_path),
            "codex": support.binary_version("codex", engine_path),
            "bundle_python": _version_of(support.bundled_python(bundle)),
        },
    )
    _verdict = record
    try:
        yield record
    finally:
        written = record.write(run_directory / "verdict.json")
        if record.missing:
            print(f"\nnot observed: {', '.join(record.missing)}")
        print(f"\nverdict: {record.result} — {written}")


def _version_of(interpreter: Path) -> str:
    finished = subprocess.run(
        [str(interpreter), "--version"], capture_output=True, text=True, timeout=30.0
    )
    return (finished.stdout or finished.stderr).strip()


@pytest.fixture
def lane(request) -> journey_module.Lane:  # noqa: ANN001
    """Which lane this test is, named once by the test and read by every fixture."""
    return request.param


@pytest.fixture
def lane_engine(lane, run_directory, journal, engine_path, bot_token):  # noqa: ANN001
    """A fresh engine and a fresh workspace for one lane, torn down after it.

    The socket lives under `/tmp` rather than in the run directory because Darwin
    caps an AF_UNIX path at 103 bytes and the run directory is most of that
    already — the same reason `config.RUNTIME_ROOT` exists.

    The trust gate is arranged around **both** the engine and the Session: a fresh
    workspace is what the design requires, and a hand-started agent stops in one
    it has never seen with a full-screen dialog and never registers (re-measured
    on `claude` 2.1.246, 2026-08-26). `support.TrustGate` grants it, backs up both
    user files into the run directory and revokes on the way out.
    """
    workspace = support.fresh_workspace(run_directory, lane.name, engine_path)
    socket_root = (
        support.SOCKET_ROOT / f"gvc-acceptance-{os.getuid()}-{run_directory.name}-{lane.name}"
    )
    socket_root.mkdir(parents=True, exist_ok=True)
    socket_root.chmod(0o700)

    config = support.derive_config(
        source=support.source_config_path(),
        run_directory=run_directory / f"engine-{lane.name}",
        workspace=workspace,
        socket_path=socket_root / "control.sock",
        project_name=f"acceptance-{lane.name}",
    )
    engine = support.Engine(
        config=config,
        bundle=support.bundle_path(),
        journal=journal,
        token=bot_token,
        path_value=engine_path,
    )
    with support.TrustGate(workspace, run_directory=run_directory, journal=journal):
        engine.start()
        try:
            yield (
                engine,
                config,
                support.Bridgectl(
                    bundle=support.bundle_path(), socket_path=config.socket_path, journal=journal
                ),
            )
        finally:
            engine.stop()
            import shutil as _shutil

            _shutil.rmtree(socket_root, ignore_errors=True)


@pytest.fixture
def terminal_environment(engine_path: str) -> dict[str, str]:
    """The environment a terminal the user opened would carry — see `hand_started`."""
    return hand_started.terminal_environment(engine_path)


@pytest.fixture
def hand_started_session(  # noqa: ANN201
    lane,  # noqa: ANN001
    lane_engine,  # noqa: ANN001 - ordered after the engine on purpose; see below
    run_directory: Path,
    journal: support.Journal,
    terminal_environment: dict[str, str],
    engine_path: str,
):
    """The Session the *user* starts: the ordinary command, in a pty, no wrapper.

    **Started after the engine, and that is a choice with a reason.** #71 proved
    both of the Claude lane's routes are hot — the built-in inbox socket and the
    user-scope hooks reach a Session that is already running — so the harder
    order (Session first, engine second) is the one the product claims to
    survive, and a later ticket may well want it. It is not this run's order
    because a Session started before the engine has no `SessionStart` for the
    engine to have heard, and every red would then have the same single cause.
    The engine-first order is stated here so nobody reads it as an accident.
    """
    _, config, _ = lane_engine
    binary = hand_started.resolve(lane.binary, engine_path)
    if binary is None:
        _refuse(f"`{lane.binary}` does not resolve on the PATH the engine was handed")
    started_at = time.time()
    session = hand_started.HandStartedSession(
        lane=lane.name,
        binary=binary,
        arguments=lane.arguments,
        workspace=config.workspace,
        environment=terminal_environment,
        journal=journal,
        transcript=run_directory / f"pty-{lane.name}.log",
    )
    session.start()
    try:
        yield session, started_at
    finally:
        session.stop()
