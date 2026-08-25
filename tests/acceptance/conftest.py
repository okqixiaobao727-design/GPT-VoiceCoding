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
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip(
    "telethon",
    reason="the acceptance's one actor is a Telegram user account: pip install -e '.[acceptance]'",
)

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
#:   * a launch answers in **1.5s** once the workspace is trusted;
#:   * a relay's reply arrives at **45.1s**, which is the engine's own
#:     `DEFAULT_ACK_TIMEOUT_SECONDS` resolving, not a model thinking;
#:   * **a real agent turn could not be measured, because none ran.** Nothing on
#:     today's `main` gets words into a Session — the relay is retained rather
#:     than delivered and the Session acts on nothing — so the turn figure below
#:     is derived from the engine's bounded waits plus headroom for a model, and
#:     is marked to be **re-measured from the first run where a turn happens**.
FAR_SIDE = support.FarSideDeadlines(
    # Not measured: see above. The engine's own proof wait is 45s and a small
    # one-file turn on either agent is seconds to a minute of model time; three
    # times the proof wait is headroom over both without being a number that
    # makes an absent turn look like a slow one.
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


def _refuse(reason: str) -> None:
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
    directory = support.new_run_directory()
    print(f"\nacceptance run directory: {directory}")
    return directory


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
    record = support.Verdict(
        run_id=run_directory.name,
        bundle=str(bundle),
        commit=provenance.commit,
        provenance=provenance.reason,
        versions={
            "claude": support.binary_version("claude", engine_path),
            "codex": support.binary_version("codex", engine_path),
            "bundle_python": _version_of(support.bundled_python(bundle)),
        },
    )
    try:
        yield record
    finally:
        written = record.write(run_directory / "verdict.json")
        print(f"\nverdict: {record.result} — {written}")


def _version_of(interpreter: Path) -> str:
    finished = subprocess.run(
        [str(interpreter), "--version"], capture_output=True, text=True, timeout=30.0
    )
    return (finished.stdout or finished.stderr).strip()


@pytest.fixture
def lane_engine(request, run_directory, journal, engine_path, bot_token):  # noqa: ANN001
    """A fresh engine and a fresh workspace for one lane, torn down after it.

    The socket lives under `/tmp` rather than in the run directory because Darwin
    caps an AF_UNIX path at 103 bytes and the run directory is most of that
    already — the same reason `config.RUNTIME_ROOT` exists.
    """
    lane = request.param if hasattr(request, "param") else request.node.name
    workspace = support.fresh_workspace(run_directory, lane, engine_path)
    socket_root = support.SOCKET_ROOT / f"gvc-acceptance-{os.getuid()}-{run_directory.name}-{lane}"
    socket_root.mkdir(parents=True, exist_ok=True)
    socket_root.chmod(0o700)

    config = support.derive_config(
        source=support.source_config_path(),
        run_directory=run_directory / f"engine-{lane}",
        workspace=workspace,
        socket_path=socket_root / "control.sock",
        project_name=f"acceptance-{lane}",
    )
    engine = support.Engine(
        config=config,
        bundle=support.bundle_path(),
        journal=journal,
        token=bot_token,
        path_value=engine_path,
        stdio=run_directory / f"engine-{lane}.stdio",
    )
    # The workspace is fresh by design, and a launch into a directory an agent has
    # never seen stops at its full-screen trust dialog, so step 1a would be the
    # only thing this run ever measured. `support.TrustGate` grants trust for this
    # one workspace, backs both user files up into the run directory, and revokes
    # on the way out; `journey.walk` records the arrangement as `0c workspace
    # trust` and the journal carries the grant and the revoke.
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
