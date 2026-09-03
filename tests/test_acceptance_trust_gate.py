"""Fast regression coverage for which Claude state file the trust gate writes.

Run `20260903T050619Z` granted trust and still met the full-screen "Is this a
project you created or one you trust?" dialog: `support.TrustGate` wrote
`~/.claude.json` while the Session it was arranging for had
`CLAUDE_CONFIG_DIR=~/.claude-b` in its environment and read
`~/.claude-b/.claude.json`. The grant landed and the journal said so; the file
was one nobody opened, `roster` failed ungraded and every step behind it was
SKIPPED (#217).

**The measurement this module encodes**, taken on 2026-09-03 against `claude`
2.1.259 on this machine — a `git init` workspace under the acceptance root,
launched in a pty the way `hand_started` launches one:

| `CLAUDE_CONFIG_DIR` | entry written into      | trust dialog |
| ------------------- | ----------------------- | ------------ |
| unset               | `~/.claude.json`        | no           |
| unset               | (no entry)              | yes          |
| `~/.claude-b`       | `~/.claude.json`        | **yes**      |
| `~/.claude-b`       | `~/.claude-b/.claude.json` | no        |

Two facts fall out of it, and both are asserted below. The state file follows
`CLAUDE_CONFIG_DIR`, which the product's own installation module already
resolves; and the *unset* default is `~/.claude.json` in the home directory,
**not** `.claude.json` inside the default config directory — so the rule is its
own resolver rather than `default_config_directory(environ) / ".claude.json"`.

Nothing here starts an agent or touches the operator's real files: a temporary
home throughout, and the Codex half of the gate pointed at a temporary file,
because a test that read `~/.codex/config.toml` would be a test that edits it.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
import support

from gpt_voicecoding.installation import claude_hooks

STATE_NAME = ".claude.json"


@pytest.fixture
def journal(tmp_path: Path) -> support.Journal:
    return support.Journal(tmp_path / "journal.jsonl")


@pytest.fixture
def run_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "run"
    directory.mkdir()
    return directory


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    directory = tmp_path / "workspace"
    directory.mkdir()
    return directory


@pytest.fixture(autouse=True)
def codex_elsewhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The Codex half of the gate, pointed away from the operator's own file."""
    config = tmp_path / "codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("# a codex config\n")
    monkeypatch.setattr(support, "CODEX_CONFIG", config)
    return config


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home directory this test owns, with a Claude state file in it."""
    directory = tmp_path / "home"
    (directory / ".claude").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: directory))
    return directory


def _state(path: Path, projects: dict[str, Any] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"numStartups": 7, "projects": projects or {}}, indent=2))
    return path


def _projects(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())["projects"]


def _gate(
    workspace: Path,
    *,
    run_directory: Path,
    journal: support.Journal,
    environment: dict[str, str],
) -> support.TrustGate:
    return support.TrustGate(
        workspace,
        run_directory=run_directory,
        journal=journal,
        label="claude",
        environment=environment,
    )


class TestWhichStateFile:
    """The resolver, alone: one rule, both readers."""

    def test_a_named_config_directory_carries_the_state_file(self, tmp_path: Path) -> None:
        named = tmp_path / "claude-b"
        assert support.claude_state_path({"CLAUDE_CONFIG_DIR": str(named)}) == named / STATE_NAME

    def test_no_named_directory_means_the_home_file_not_the_default_directory(
        self, home: Path
    ) -> None:
        resolved = support.claude_state_path({})
        assert resolved == home / STATE_NAME
        assert resolved != claude_hooks.default_config_directory({}, home) / STATE_NAME

    def test_a_blank_name_is_no_name(self, home: Path) -> None:
        assert support.claude_state_path({"CLAUDE_CONFIG_DIR": "   "}) == home / STATE_NAME

    def test_the_variable_is_the_one_the_product_reads(self) -> None:
        assert claude_hooks.CONFIG_DIRECTORY_VARIABLE == "CLAUDE_CONFIG_DIR"


class TestTheFileTheSessionWillRead:
    def test_grants_where_a_named_config_directory_puts_it(
        self, tmp_path: Path, home: Path, workspace: Path, run_directory: Path, journal
    ) -> None:
        named = _state(tmp_path / "claude-b" / STATE_NAME)
        at_home = _state(home / STATE_NAME)
        environment = {"CLAUDE_CONFIG_DIR": str(named.parent)}

        gate = _gate(
            workspace, run_directory=run_directory, journal=journal, environment=environment
        )
        with gate:
            assert _projects(named)[str(workspace)][support.CLAUDE_TRUST_KEY] is True
            assert _projects(at_home) == {}

        assert _projects(named) == {}

    def test_grants_in_the_home_file_when_nothing_names_a_directory(
        self, home: Path, workspace: Path, run_directory: Path, journal
    ) -> None:
        at_home = _state(home / STATE_NAME)
        inside_the_default_directory = _state(home / ".claude" / STATE_NAME)

        with _gate(workspace, run_directory=run_directory, journal=journal, environment={}):
            assert _projects(at_home)[str(workspace)][support.CLAUDE_TRUST_KEY] is True
            assert _projects(inside_the_default_directory) == {}

    def test_the_journal_names_the_file_it_wrote(
        self, tmp_path: Path, home: Path, workspace: Path, run_directory: Path, journal
    ) -> None:
        named = _state(tmp_path / "claude-b" / STATE_NAME)
        environment = {"CLAUDE_CONFIG_DIR": str(named.parent)}

        gate = _gate(
            workspace, run_directory=run_directory, journal=journal, environment=environment
        )
        with gate:
            pass

        claude = [
            record
            for record in journal.read()
            if record["event"].startswith("trust.") and record["agent"] == "claude"
        ]
        assert claude, "the gate journalled nothing about Claude"
        assert all(record["state"] == str(named) for record in claude)

    def test_backs_up_the_file_it_is_about_to_write(
        self, tmp_path: Path, home: Path, workspace: Path, run_directory: Path, journal
    ) -> None:
        named = _state(tmp_path / "claude-b" / STATE_NAME)
        pristine = named.read_text()
        environment = {"CLAUDE_CONFIG_DIR": str(named.parent)}

        gate = _gate(
            workspace, run_directory=run_directory, journal=journal, environment=environment
        )
        with gate:
            pass

        backups = list(run_directory.glob(f"{STATE_NAME}.before-trust-*"))
        assert [backup.read_text() for backup in backups] == [pristine]

    def test_an_absent_state_file_is_reported_and_nothing_is_written(
        self, tmp_path: Path, home: Path, workspace: Path, run_directory: Path, journal
    ) -> None:
        missing = tmp_path / "claude-b" / STATE_NAME
        environment = {"CLAUDE_CONFIG_DIR": str(missing.parent)}

        gate = _gate(
            workspace, run_directory=run_directory, journal=journal, environment=environment
        )
        with gate:
            assert not missing.exists()

        absent = [record for record in journal.read() if record["event"] == "trust.absent"]
        claude = [record["path"] for record in absent if record["agent"] == "claude"]
        assert claude == [str(missing)]


class TestAnEntryThatIsNotTrusted:
    """`hasTrustDialogAccepted: false` is a state the gate has to grant through.

    The failing run's workspace was fresh, so this is not what #217 measured —
    but "there is no entry" and "there is an entry that says no" are different
    facts, and only the first was being asked. A machine carrying either one
    would have the gate report `trust.already` and grant nothing, which is the
    same silent arrangement failure by another route.
    """

    def test_grants_over_an_entry_that_says_no(
        self, home: Path, workspace: Path, run_directory: Path, journal
    ) -> None:
        entry = {support.CLAUDE_TRUST_KEY: False, "allowedTools": ["Read"], "lastCost": 0.5}
        at_home = _state(home / STATE_NAME, {str(workspace): dict(entry)})

        with _gate(workspace, run_directory=run_directory, journal=journal, environment={}):
            granted = _projects(at_home)[str(workspace)]
            assert granted[support.CLAUDE_TRUST_KEY] is True
            assert granted["allowedTools"] == ["Read"]
            assert granted["lastCost"] == 0.5

        assert _projects(at_home) == {str(workspace): entry}

    def test_leaves_an_entry_that_already_says_yes_exactly_as_it_found_it(
        self, home: Path, workspace: Path, run_directory: Path, journal
    ) -> None:
        entry = {support.CLAUDE_TRUST_KEY: True, "lastCost": 0.5}
        at_home = _state(home / STATE_NAME, {str(workspace): dict(entry)})

        with _gate(workspace, run_directory=run_directory, journal=journal, environment={}):
            assert _projects(at_home) == {str(workspace): entry}

        assert _projects(at_home) == {str(workspace): entry}
        assert any(record["event"] == "trust.already" for record in journal.read())


class TestPuttingItBack:
    def test_restores_exactly_what_it_found(
        self, home: Path, workspace: Path, run_directory: Path, journal
    ) -> None:
        neighbour = {support.CLAUDE_TRUST_KEY: True, "lastCost": 1.0}
        at_home = _state(home / STATE_NAME, {"/elsewhere": dict(neighbour)})
        pristine = json.loads(at_home.read_text())

        with _gate(workspace, run_directory=run_directory, journal=journal, environment={}):
            pass

        assert json.loads(at_home.read_text()) == pristine

    def test_grants_and_revokes_both_spellings_of_the_workspace(
        self, tmp_path: Path, home: Path, run_directory: Path, journal
    ) -> None:
        real = tmp_path / "real"
        real.mkdir()
        seen_as = tmp_path / "seen-as"
        seen_as.symlink_to(real)
        at_home = _state(home / STATE_NAME)

        with _gate(seen_as, run_directory=run_directory, journal=journal, environment={}):
            assert set(_projects(at_home)) == {str(seen_as), str(real)}

        assert _projects(at_home) == {}


#: The two halves the lock has to span whole. Named once, so the watcher below
#: cannot watch one of them and let the other drift out of the guarded region.
GUARDED = ("_trust_claude", "_untrust_claude")


class _WatchedLock:
    """`_TRUST_LOCK` with the three moments that matter written down.

    The lock is where the claim lives, so it is where the test listens. A lane
    records `acquiring` **before** it asks — which is what makes "the second lane
    is at the door" an observation rather than an assumption — then `acquired`
    when it gets in and `released` on the way out. `Thread.start()` promises
    nothing about when the target runs; this promises that by the time the test
    asserts anything, the second lane has reached the lock and the only thing
    that can be holding it is the lock.
    """

    def __init__(
        self,
        inner,
        order: list[str],
        asking: dict[str, threading.Event],
        entered: dict[str, threading.Event],
    ) -> None:
        self._inner = inner
        self._order = order
        self._asking = asking
        self._entered = entered

    def _mark(self, moment: str) -> str:
        lane = threading.current_thread().name
        self._order.append(f"{lane}:{moment}")
        return lane

    def __enter__(self):
        lane = self._mark("acquiring")
        self._asking[lane].set()
        self._inner.__enter__()
        self._mark("acquired")
        self._entered[lane].set()
        return self

    def __exit__(self, *unused: object) -> None:
        self._mark("released")
        self._inner.__exit__(*unused)


class TestOneLaneAtATime:
    """`_TRUST_LOCK` spans a **whole** grant and a **whole** revoke, not each write.

    Two lanes run at once (#182) and both read-modify-write the one state file. A
    lock taken per write would let the second lane read between the first's read
    and its write: the loser then runs untrusted, and the revoke that wrote last
    drops an entry the other lane is still standing on. The claim is about the
    *span*, so this holds one lane inside the guarded region and proves the other
    cannot get in — an implementation that locked more narrowly would interleave
    here and pass every other test in this module.

    **No sleep decides anything.** The test waits for the second lane to record
    that it is asking for the lock, and only then asserts it does not get in; the
    window assertion afterwards reads the recorded order rather than a clock. A
    busy machine makes the waits longer, never the verdict weaker. Checked by
    mutation on 2026-09-03: with `_TRUST_LOCK` swapped for
    `contextlib.nullcontext()`, both parameters fail.
    """

    @pytest.mark.parametrize("phase", GUARDED)
    def test_a_second_lane_cannot_get_in_while_one_is_inside(
        self, phase: str, tmp_path: Path, home: Path, run_directory: Path, journal
    ) -> None:
        at_home = _state(home / STATE_NAME)
        order: list[str] = []
        lanes_named = ("first", "second")
        asking = {label: threading.Event() for label in lanes_named}
        entered = {label: threading.Event() for label in lanes_named}
        held = threading.Event()
        let_go = threading.Event()
        guarded = {name: getattr(support.TrustGate, name) for name in GUARDED}

        def watching(name: str):
            def call(gate: support.TrustGate) -> None:
                order.append(f"{gate._label}:{name}-in")
                if gate._label == "first" and name == phase:
                    held.set()
                    assert let_go.wait(20), "the held lane was never released"
                guarded[name](gate)
                order.append(f"{gate._label}:{name}-out")

            return call

        def walk(label: str) -> None:
            workspace = tmp_path / label
            workspace.mkdir()
            with support.TrustGate(
                workspace,
                run_directory=run_directory,
                journal=journal,
                label=label,
                environment={},
            ):
                pass

        watched_lock = _WatchedLock(support._TRUST_LOCK, order, asking, entered)
        lanes = [threading.Thread(target=walk, args=(label,), name=label) for label in lanes_named]
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(support, "_TRUST_LOCK", watched_lock)
            for name in GUARDED:
                patched.setattr(support.TrustGate, name, watching(name))
            try:
                lanes[0].start()
                assert held.wait(20), "the first lane never reached the phase under test"
                lanes[1].start()
                assert asking["second"].wait(20), "the second lane never reached the lock"
                assert not entered["second"].wait(1.0), (
                    f"the second lane took the lock while the first was inside {phase}"
                )
            finally:
                let_go.set()
                for lane in lanes:
                    lane.join(30)

        assert [lane.is_alive() for lane in lanes] == [False, False]
        opened = order.index(f"first:{phase}-in")
        closed = order.index(f"first:{phase}-out")
        assert "second:acquired" not in order[opened:closed], order
        # Each lane grants then revokes. How the two lanes' phases interleave once
        # neither is inside one is not something this lock promises, so it is not
        # something this test asserts.
        for label in lanes_named:
            mine = [record for record in order if record.startswith(f"{label}:_")]
            assert mine == [f"{label}:{name}-{end}" for name in GUARDED for end in ("in", "out")]
        assert _projects(at_home) == {}
