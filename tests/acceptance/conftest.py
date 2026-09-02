"""Preflight, and the fixtures a lane's journey is walked on.

**Preflight refuses rather than runs.** A run that starts against the wrong
environment produces a verdict that cannot be attributed to this engine
(`docs/acceptance-design.md` § Preflight refuses rather than runs), and a verdict
that cannot be attributed is worse than no verdict — it is a false one, kept.
So every check below either passes or ends the session with `REFUSED` written
down and the reason on the terminal.

Both lanes run **at once**, each against a fresh engine and a fresh workspace,
and never share either. The engine is spawned by the harness, not by the menu-bar
shell — repeatability over coverage, and the shell is out of scope.

**Why a thread per lane rather than a test per lane.** A lane is ten minutes of
real agent turns and real Telegram round trips, and two of them end to end is
most of the pre-merge wait (#180 §2 decision 3). What kept them sequential was
one bot: one bot serves one engine (`docs/app-bundle.md` § Cutover), so two
engines needed two bots before they could be two lanes at the same time. They
have two now — each lane binds its own `token_env` — and the concurrency lives
here rather than in a second pytest process so that the run keeps **one** run
directory, one journal and one verdict with a block per lane. Everything two
threads reach is either per-lane by construction (engine, workspace, socket root,
bot, chat) or locked where it is shared (`support.Journal`, `support.Verdict`,
`support.TrustGate` — that last one read-modify-writes the user's own
`~/.claude.json`, and puts back what it found).

Both engines bridge **every** Session on the machine, so each lane's roster shows
the other lane's Session. Nothing special is done about that: the journey's own
rule — a step only ever attributes what names its own target
(`journey.py`'s docstring) — is what already covers it.

## Three roots, and the harness derives two of them

"Socket roots are already per lane" was true of one root and not of three, which
run `20260902T012313Z` found the hard way — the second lane's engine died at
start. What one engine owns on this machine:

* **the control socket**, `[engine] socket_path`. Per lane, and was already:
  `/tmp/gvc-acceptance-<uid>-<run>-<lane>/control.sock`.
* **the Codex app-server socket**,
  `<socket_directory>/gpt-voicecoding-<uid>/codex-app-server.sock`
  (`adapters/agent/codex/adapter.py:143`). Per **machine** by default, and the
  product refuses rather than shadows a live one — so a second engine simply
  does not start. `[adapters.settings.agent.codex] socket_directory` is a real
  setting, so this run points each lane at its own lane root, and the engines get
  an app-server each. Both lanes' engines load both agent adapters, so this is
  not the Codex lane's problem alone.
* **the published approval address**, a fixed
  `~/Library/Application Support/GPT-VoiceCoding/engine/address.json`
  (`locations.py:56`). Per machine, and there is still no setting for it — but it
  is no longer a race. [#202](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/202)
  made publishing a **claim**: an engine dials whatever address is already there,
  takes over a socket nobody answers, and stands down from a socket that answers.
  A stood-down engine reaches no Claude Session at all — the `SessionStart`
  registration hook reads the same address the `PermissionRequest` hook does — so
  it says so in its log and goes **red** at `verify` rather than reporting an
  empty roster as healthy. Withdrawal removes only the engine's own address. So the route
  now belongs to one engine rather than to the last one to start, and this run
  decides *which* engine that is rather than leaving it to the clock: the Codex
  lane's derived config drops the Claude agent adapter (`support.derive_config`),
  because that lane's journey never walks the approval route. The Claude lane is
  the only claimant, and its config is the user's own.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Never

import pytest

from gpt_voicecoding.seams.identity import AgentKind

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
    Transport,
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


def _refuse(reason: str) -> Never:
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


# --- what this run walks ----------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    """The two selectors, and why they are options rather than `-k`.

    **Two layers, and these are how the first one is asked for** (see
    `test_lanes.py`'s docstring). A build ticket runs one step of one lane;
    the pre-merge run passes neither option and walks everything.

    A step is not a test — they share one engine, one Session and one
    chat — so `-k` cannot address one, and the prerequisite closure means asking
    for a step is asking for the steps beneath it too. A lane *is* a parametrised
    test, but the parameters are now chosen rather than fixed: the lanes run
    concurrently, and a lane nobody selected must not be started at all, which is
    a decision made before collection rather than filtered after it.

    Registered here, which pytest reaches as an initial conftest whenever the
    acceptance path is named — the documented way to run this suite
    (`docs/acceptance-design.md` § Running it).
    """
    group = parser.getgroup("acceptance")
    group.addoption(
        "--step",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "grade this acceptance step and walk its prerequisites as ungraded setup; "
            "repeatable. Default: every step. Names: " + ", ".join(journey_module.STEPS)
        ),
    )
    group.addoption(
        "--lane",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "walk this lane only; repeatable. Default: every lane, concurrently. "
            "Names: " + ", ".join(lane.name for lane in journey_module.LANES)
        ),
    )


def _selection(config: pytest.Config) -> journey_module.Selection:
    try:
        return journey_module.select(config.getoption("--step"))
    except journey_module.UnknownStep as unknown:
        raise pytest.UsageError(str(unknown)) from None


def _selected_lanes(config: pytest.Config) -> tuple[journey_module.Lane, ...]:
    asked = config.getoption("--lane")
    if not asked:
        return journey_module.LANES
    known = {lane.name for lane in journey_module.LANES}
    unknown = sorted(set(asked) - known)
    if unknown:
        raise pytest.UsageError(
            f"no such acceptance lane: {', '.join(repr(name) for name in unknown)}. "
            f"The lanes are: {', '.join(sorted(known))}."
        )
    return tuple(lane for lane in journey_module.LANES if lane.name in set(asked))


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Every test that walks a lane is parametrised over the lanes this run selected.

    The parametrisation stays pytest's own, so a failure still reads
    `test_the_lane[codex]` without a module per lane to say it — and `--lane`
    decides which ids exist rather than deselecting them after the fact.

    **The lanes see each other, and that is not a problem this has to solve.**
    Each lane's engine bridges *every* Session on the machine, so with both lanes
    walking at once each roster carries the other lane's Session and each engine
    could announce it. The journey's own rule already covers it — a step only ever
    attributes what names its own target (`journey.py`'s docstring, `#109`) — and
    the two lanes' chats are two different bots' chats, so nothing one lane says
    is even in the surface the other reads.
    """
    if "lane" in metafunc.fixturenames:
        metafunc.parametrize(
            "lane", _selected_lanes(metafunc.config), indirect=True, ids=lambda one: one.name
        )
    _selection(metafunc.config)


@pytest.fixture(scope="session")
def selection(request: pytest.FixtureRequest) -> journey_module.Selection:
    """Which steps this run grades, and which it walks only to reach them."""
    return _selection(request.config)


@pytest.fixture(scope="session")
def selected_lanes(request: pytest.FixtureRequest) -> tuple[journey_module.Lane, ...]:
    return _selected_lanes(request.config)


@pytest.fixture(scope="session")
def far_side() -> support.FarSideDeadlines:
    """The far-side waits this run used, so the journal and the verdict agree on them."""
    return FAR_SIDE


@pytest.fixture(scope="session")
def realtime_probe() -> Path:
    try:
        return support.realtime_probe_path(REPOSITORY)
    except support.RealtimeProbeUnavailable as missing:
        _refuse(str(missing))


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
def configured_channel() -> dict:
    """The engine's real Companion Channel table — read once, read by both lanes.

    `token_env` here is the **first** lane's variable and the name the second
    lane's is derived from (`journey.Lane.token_variable`); `chat_id` is the same
    person for both bots, because a chat id is the account's, not the bot's.
    """
    source = support.source_config_path()
    if not source.exists():
        _refuse(f"no engine configuration at {source} to derive this run's from")
    return dict(tomllib.loads(source.read_text())["adapters"]["settings"]["companion_channel"])


@pytest.fixture(scope="session")
def lane_tokens(
    selected_lanes: tuple[journey_module.Lane, ...], configured_channel: dict
) -> dict[str, str]:
    """Each lane's bot token, under the variable that lane's engine will be told to read."""
    configured = str(configured_channel["token_env"])
    tokens: dict[str, str] = {}
    for lane in selected_lanes:
        variable = lane.token_variable(configured)
        try:
            tokens[lane.name] = support.token_from_environment(variable)
        except LookupError as missing:
            _refuse(f"the {lane.name} lane's bot: {missing}")
            raise  # unreachable; for the type checker
    return tokens


@pytest.fixture(scope="session")
def bots(
    selected_lanes: tuple[journey_module.Lane, ...],
    lane_tokens: dict[str, str],
    configured_channel: dict,
    journal: support.Journal,
) -> dict[str, dict]:
    """Each lane's bot, asked two questions the run cannot assume the answers to.

    `getMe` — it answers, and says who it is. The username it returns is what the
    user-account client resolves as its peer, so no bot is named anywhere in this
    suite.

    `getChat` — it can reach the chat it is configured for. A bot cannot open a
    chat with a person, so a bot the account has never sent `/start` to is
    reachable, correct, and unable to say a word. That was one bot's one-time
    setup and is now a second bot's, which is exactly the kind of thing preflight
    refuses on rather than discovering three steps into a lane
    (`support.chat_open_refusal`).
    """
    configured = str(configured_channel["token_env"])
    chat_id = str(configured_channel["chat_id"])
    identities: dict[str, dict] = {}
    for lane in selected_lanes:
        transport: Transport = http_transport(
            token=lane_tokens[lane.name], api_root=DEFAULT_API_ROOT
        )
        try:
            identity = transport("getMe", {}, timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS)
        except TelegramError as unreachable:
            _refuse(
                f"the {lane.name} lane's Telegram bot did not answer getMe: {unreachable.detail}"
            )
            raise  # unreachable; for the type checker
        unreachable_chat = support.chat_open_refusal(
            transport, chat_id=chat_id, bot_username=str(identity["username"])
        )
        if unreachable_chat is not None:
            _refuse(f"the {lane.name} lane's bot: {unreachable_chat}")
        journal(
            "preflight.getMe",
            lane=lane.name,
            username=identity["username"],
            id=identity["id"],
            token_env=lane.token_variable(configured),
        )
        identities[lane.name] = identity

    # Two names in one `.env` are a copy-paste apart, and one token in both of
    # them answers `getMe` perfectly twice. Only the identity says otherwise.
    same_bot = support.duplicate_bot_refusal(
        identities,
        variables={lane.name: lane.token_variable(configured) for lane in selected_lanes},
    )
    if same_bot is not None:
        _refuse(same_bot)
    return identities


@pytest.fixture(scope="session")
def person_session_lock(run_directory: Path) -> Iterator[telegram_person.PersonSessionLock]:
    """One acceptance run per machine, refused rather than kept by a person (#203).

    `docs/acceptance-design.md` § Running it lists what two runs share and no
    `--lane` separates: the user-account session, which is SQLite backing one
    client, and `support.TrustGate`'s writes to the user's own `~/.claude.json`
    and `~/.codex/config.toml`, guarded by a thread lock that means nothing to a
    second pytest process. The rule was written down and kept by hand; this takes
    it.

    **It is listed before the bots for a reason.** A second run must refuse
    before it reads a token, opens the session file or touches the trust gate —
    a refusal that arrives after any of those has already had the collision it
    was supposed to prevent. So `preflight` names this fixture ahead of `bots`,
    and `person_connection` depends on it rather than the other way round.

    Held for the whole session and released here. A run killed outright leaves
    nothing to sweep: `flock` belongs to the open file description, so the kernel
    releases it when the process dies.
    """
    try:
        with telegram_person.PersonSessionLock(
            run_directory=run_directory,
            held_by=telegram_person.ACCEPTANCE_RUN_HOLDER,
        ) as lock:
            yield lock
    except telegram_person.SessionInUse as in_use:
        _refuse(str(in_use))


@pytest.fixture(scope="session")
def person_connection(
    person_session_lock: telegram_person.PersonSessionLock,  # noqa: ARG001 - held, not called
) -> Iterator[telegram_person.PersonConnection]:
    """The one account, connected once. One session file backs one client.

    It journals nothing itself: what a reader needs is *who* connected and to
    which peer, and each `TelegramPerson` writes that as it opens.
    """
    connection = telegram_person.PersonConnection()
    try:
        connection.open()
    except telegram_person.PersonError as unauthorised:
        connection.close()
        _refuse(str(unauthorised))
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture(scope="session")
def people(
    selected_lanes: tuple[journey_module.Lane, ...],
    bots: dict[str, dict],
    person_connection: telegram_person.PersonConnection,
    journal: support.Journal,
) -> Iterator[dict[str, telegram_person.TelegramPerson]]:
    """The one actor, once per lane's bot. Refuses here rather than mid-journey.

    One person, two chats: the same human account holds a chat with each bot, and
    a lane reads and writes only its own. That is what keeps two lanes' traffic
    apart on a surface the attribution rule would otherwise have to separate.
    """
    actors: dict[str, telegram_person.TelegramPerson] = {}
    try:
        for lane in selected_lanes:
            actor = telegram_person.TelegramPerson(
                f"@{bots[lane.name]['username']}",
                journal=journal,
                connection=person_connection,
            )
            try:
                actor.open()
            except telegram_person.PersonError as unauthorised:
                _refuse(f"the {lane.name} lane's bot: {unauthorised}")
            actors[lane.name] = actor
        yield actors
    finally:
        for actor in actors.values():
            actor.close()


@pytest.fixture(scope="session", autouse=True)
def preflight(
    realtime_probe: Path,
    bundle: Path,
    provenance: support.Provenance,
    engine_path: str,
    run_directory: Path,
    # Ahead of `bots`, `people` and every trust-gate write: a second run on this
    # machine refuses here, before it has touched anything the first run holds.
    person_session_lock: telegram_person.PersonSessionLock,
    journal: support.Journal,
    verdict: support.Verdict,
    selection: journey_module.Selection,
    selected_lanes: tuple[journey_module.Lane, ...],
    bots: dict[str, dict],
    people: dict[str, telegram_person.TelegramPerson],
) -> None:
    """Step 0. Everything here is a refusal, never a failure.

    **What it refuses about is what this run selected.** Every check below was
    written when a run was always both lanes and always every step, and each one
    was therefore about the run. With `--lane` and `--step` they are not: a Codex
    binary this run will never execute, or a Codex permission ground no selected
    step stands on, is a refusal about work nobody asked for — and a refusal that
    is not about the run is exactly the false verdict preflight exists to prevent,
    pointed the other way.
    """
    if journey_module.codex_permission_ground_matters(selected_lanes, selection.steps):
        permission_ground = journey_module.codex_permission_ground_refusal(
            run_directory,
            environment=os.environ,
        )
        if permission_ground is not None:
            _refuse(permission_ground)

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

    for lane in selected_lanes:
        if shutil.which(lane.binary, path=engine_path) is None:
            _refuse(
                f"the {lane.name} lane's `{lane.binary}` does not resolve on the PATH the "
                f"engine will be handed"
            )

    verdict.environment = support.environment_facts()
    journal("preflight.environment", **verdict.environment)
    journal(
        "preflight.passed",
        bundle=str(bundle),
        commit=provenance.commit,
        provenance=provenance.reason,
        engine_path=engine_path,
        bots={lane: identity["username"] for lane, identity in bots.items()},
        lanes=sorted(people),
        far_side_deadlines=vars(FAR_SIDE),
    )


@pytest.fixture(scope="session")
def verdict(
    run_directory: Path,
    bundle: Path,
    provenance: support.Provenance,
    engine_path: str,
    selection: journey_module.Selection,
    selected_lanes: tuple[journey_module.Lane, ...],
) -> Iterator[support.Verdict]:
    global _verdict
    record = support.Verdict(
        run_id=run_directory.name,
        bundle=str(bundle),
        commit=provenance.commit,
        provenance=provenance.reason,
        # What this run promised to observe. `Verdict.result` will not say PASS
        # while any of it is missing, so a lane that never ran cannot be silently
        # absent from a green verdict. On a `--step` run the promise is smaller —
        # the selected steps — and the prerequisites walked to reach them are
        # recorded beside it, ungraded, so a green step never reads as a green lane.
        expected_lanes=tuple(lane.name for lane in selected_lanes),
        expected_steps=selection.selected,
        setup_steps=selection.setup,
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
    """Which lane this test is. Parametrised by `pytest_generate_tests` from `--lane`."""
    return request.param


@pytest.fixture(scope="session")
def terminal_environment(engine_path: str) -> dict[str, str]:
    """The environment a terminal the user opened would carry — see `hand_started`."""
    return hand_started.terminal_environment(engine_path)


@dataclass(frozen=True)
class Arrangement:
    """Everything a lane's walk needs that belongs to the **run**, not the lane.

    One value rather than seven parameters passed through two functions: what the
    lanes share is a run, and naming it once is what keeps the next thing a run
    acquires from becoming an eighth parameter in three signatures.
    """

    selection: journey_module.Selection
    run_directory: Path
    journal: support.Journal
    verdict: support.Verdict
    engine_path: str
    far_side: support.FarSideDeadlines
    environment: dict[str, str]


@dataclass
class LaneRun:
    """One lane's whole journey, on its own thread, and how it ended.

    The three values beside the lane are what the run cannot share: its bot's
    token, the variable that engine will be told to read it from, and the person
    holding that bot's chat.

    An exception that escapes a thread is a traceback on stderr and a test that
    passes, so the thread keeps hold of it and the lane's own test re-raises it.
    """

    lane: journey_module.Lane
    person: telegram_person.TelegramPerson
    token: str
    token_variable: str
    thread: threading.Thread | None = None
    failure: BaseException | None = None


@pytest.fixture(scope="session")
def lane_runs(
    preflight: None,  # noqa: ARG001 - the refusals happen before a thread is started
    selected_lanes: tuple[journey_module.Lane, ...],
    selection: journey_module.Selection,
    run_directory: Path,
    journal: support.Journal,
    verdict: support.Verdict,
    engine_path: str,
    far_side: support.FarSideDeadlines,
    terminal_environment: dict[str, str],
    people: dict[str, telegram_person.TelegramPerson],
    lane_tokens: dict[str, str],
    configured_channel: dict,
) -> Iterator[dict[str, LaneRun]]:
    """Start every selected lane at once, and hand each test its own lane's handle.

    Session-scoped, so both threads are running before the first test blocks on
    one of them — which is the whole point: the lanes overlap, and the run costs
    one lane's wall clock rather than two.
    """
    configured = str(configured_channel["token_env"])
    arrangement = Arrangement(
        selection=selection,
        run_directory=run_directory,
        journal=journal,
        verdict=verdict,
        engine_path=engine_path,
        far_side=far_side,
        environment=terminal_environment,
    )
    runs = {
        lane.name: LaneRun(
            lane=lane,
            person=people[lane.name],
            token=lane_tokens[lane.name],
            token_variable=lane.token_variable(configured),
        )
        for lane in selected_lanes
    }
    for run in runs.values():
        run.thread = threading.Thread(
            target=_walk_lane, args=(run, arrangement), name=f"lane-{run.lane.name}"
        )
        run.thread.start()
    try:
        yield runs
    finally:
        for run in runs.values():
            if run.thread is not None:
                run.thread.join()


def _walk_lane(run: LaneRun, arrangement: Arrangement) -> None:
    """The thread body: arrange this lane, walk it, and never raise into the thread."""
    try:
        _one_lane(run, arrangement)
    except Exception as unfinished:  # noqa: BLE001 - the lane's test re-raises it
        run.failure = unfinished
        arrangement.verdict.refuse(
            run.lane.name,
            f"the lane ended in {type(unfinished).__name__}: {unfinished}",
        )


def _one_lane(run: LaneRun, arrangement: Arrangement) -> None:
    """A fresh engine, a fresh workspace and a hand-started Session, then the walk.

    This was three fixtures, and it is one function because the two lanes now run
    on two threads: a fixture is set up on the thread that *requests* it, which is
    pytest's, and two lanes' engines built there would be two lanes built one
    after the other. What the fixtures said is kept, and said here.

    The socket lives under `/tmp` rather than in the run directory because Darwin
    caps an AF_UNIX path at 103 bytes and the run directory is most of that
    already — the same reason `config.RUNTIME_ROOT` exists.

    The trust gate is arranged around **both** the engine and the Session: a fresh
    workspace is what the design requires, and a hand-started agent stops in one
    it has never seen with a full-screen dialog and never registers (re-measured
    on `claude` 2.1.246, 2026-08-26). `support.TrustGate` grants it, backs up both
    user files into the run directory and revokes on the way out.

    **The Session is started after the engine, and that is a choice with a
    reason.** #71 proved both of the Claude lane's routes are hot — the built-in
    inbox socket and the user-scope hooks reach a Session that is already running
    — so the harder order (Session first, engine second) is the one the product
    claims to survive, and a later ticket may well want it. It is not this run's
    order because a Session started before the engine has no `SessionStart` for
    the engine to have heard, and every red would then have the same single cause.
    The engine-first order is stated here so nobody reads it as an accident.
    """
    lane, journal, verdict = run.lane, arrangement.journal, arrangement.verdict
    workspace = support.fresh_workspace(
        arrangement.run_directory, lane.name, arrangement.engine_path
    )
    socket_root = support.SOCKET_ROOT / (
        f"gvc-acceptance-{os.getuid()}-{arrangement.run_directory.name}-{lane.name}"
    )
    socket_root.mkdir(parents=True, exist_ok=True)
    socket_root.chmod(0o700)

    config = support.derive_config(
        source=support.source_config_path(),
        run_directory=arrangement.run_directory / f"engine-{lane.name}",
        workspace=workspace,
        socket_path=socket_root / "control.sock",
        project_name=f"acceptance-{lane.name}",
        token_variable=run.token_variable,
        codex_socket_directory=socket_root,
        # #202: one engine per machine holds the Claude approval address, and the
        # Codex lane's journey never walks that route. Dropping the adapter is
        # what leaves exactly one claimant when both lanes are up.
        dropped_agents=() if lane.agent == str(AgentKind.CLAUDE) else (AgentKind.CLAUDE,),
        # #183: only a run that walks a step that dials gets the harness's own
        # Call adapter and the `bridgectl` wrapper. Conditional rather than
        # always, because every other step is accepting the Call adapter the
        # *user* configured, and swapping it on a run that never dials would mean
        # those steps were graded against an engine nobody runs.
        harness_live_call=any(
            step in arrangement.selection.steps for step in journey_module.LIVE_CALL_STEPS
        ),
    )
    engine = support.Engine(
        config=config,
        bundle=support.bundle_path(),
        journal=journal,
        token=run.token,
        path_value=arrangement.engine_path,
    )
    bridgectl = support.Bridgectl(
        bundle=support.bundle_path(), socket_path=config.socket_path, journal=journal
    )

    with support.TrustGate(
        workspace, run_directory=arrangement.run_directory, journal=journal, label=lane.name
    ):
        engine.start()
        session: hand_started.HandStartedSession | None = None
        try:
            binary = hand_started.resolve(lane.binary, arrangement.engine_path)
            if binary is None:
                verdict.refuse(
                    lane.name,
                    f"`{lane.binary}` does not resolve on the PATH the engine was handed",
                )
                return
            started_at = time.time()
            session = hand_started.HandStartedSession(
                lane=lane.name,
                binary=binary,
                arguments=hand_started.launch_arguments(lane.arguments, lane.boot),
                workspace=config.workspace,
                environment=arrangement.environment,
                journal=journal,
                transcript=arrangement.run_directory / f"pty-{lane.name}.log",
            )
            session.start()

            verified = bridgectl("verify")
            if not verified.ok:
                # A refusal, not a skip: the design says preflight refuses with
                # `REFUSED` and the reason, and a skipped lane that left no row
                # would be a lane the verdict could not tell apart from a lane
                # that passed.
                verdict.refuse(
                    lane.name,
                    f"`bridgectl verify` refused against this run's config: {verified.text}",
                )
                return

            journey_module.Walk(
                lane=lane,
                session=session,
                engine=engine,
                config=config,
                bridgectl=bridgectl,
                person=run.person,
                journal=journal,
                verdict=verdict,
                far_side=arrangement.far_side,
                environment=arrangement.environment,
                started_at=started_at,
                selection=arrangement.selection,
            ).walk()
        finally:
            # #44: the engine unlinked its socket but left its approval directory
            # behind. Recorded rather than graded — a real open bug and a real
            # detector, but not one of the step names the build tickets cite.
            # Checked after the engine is down, because that is when the listener
            # stops, and before the Session, which is the order the sequential
            # harness observed it in.
            engine.stop()
            leftovers = sorted(config.socket_path.parent.glob("vc-approvals-*"))
            verdict.observe(
                lane.name,
                "approval directory removed (#44)",
                f"{config.socket_path.parent} holds "
                f"{[str(path) for path in leftovers] or 'nothing'}",
            )
            if session is not None:
                session.stop()
            shutil.rmtree(socket_root, ignore_errors=True)
