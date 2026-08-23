"""The durable subset, and the one path it travels.

Persistence is an internal component: only Bridge Core touches it, and nothing
else may read the file, or the disk becomes a second truth. What survives a
restart is switch state and the Session registry — nothing else, and in
particular not the undelivered Relay queue.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gpt_voicecoding.core.errors import StateFormatError
from gpt_voicecoding.core.persistence import (
    STATE_FILE_NAME,
    PersistedState,
    StateStore,
    default_state_path,
)
from gpt_voicecoding.core.relay_queue import PendingRelay, RelayKind, RelayQueue
from gpt_voicecoding.core.sessions import Session, SessionRegistry, SessionState
from gpt_voicecoding.core.state import BridgeState
from gpt_voicecoding.core.switches import FeatureSwitch, Switchboard, SwitchName
from gpt_voicecoding.seams.agent import ReplyWindow
from gpt_voicecoding.seams.identity import AgentKind, SessionLabel, SessionTarget, new_request_id

WORKSPACE = Path(__file__).resolve().parents[1]


def a_session(session_id: str = "abc", pid: int = 100) -> Session:
    return Session(
        target=SessionTarget(agent=AgentKind.CLAUDE, session_id=session_id, pid=pid),
        label=SessionLabel("GPT-VoiceCoding", "Implement the seam contracts"),
        workspace=WORKSPACE,
        registered_at=1_724_000_000.0,
    )


def features() -> list[FeatureSwitch]:
    return [FeatureSwitch(name="stop_notice", parent=SwitchName.VOICE, default=True)]


def a_bridge(path: Path) -> BridgeState:
    return BridgeState(
        switches=Switchboard(features=features()),
        sessions=SessionRegistry(),
        relays=RelayQueue(),
        store=StateStore(path),
    )


class TestWhereItLives:
    def test_the_default_path_sits_under_the_app_support_directory(self) -> None:
        path = default_state_path()
        assert path.name == STATE_FILE_NAME
        assert "Application Support/GPT-VoiceCoding" in str(path)

    def test_it_does_not_collide_with_the_reference_implementations_runtime(self) -> None:
        """The first-generation bridge already owns `.../GPT-VoiceCoding/runtime/`."""
        assert "runtime" not in default_state_path().parts

    def test_the_base_directory_is_injectable_so_nothing_is_hard_coded(
        self, tmp_path: Path
    ) -> None:
        assert default_state_path(tmp_path).is_relative_to(tmp_path)


class TestRoundTrip:
    def test_switch_state_survives_a_restart(self, tmp_path: Path) -> None:
        path = default_state_path(tmp_path)

        before = a_bridge(path)
        before.switches.flip(SwitchName.DUTY, True)
        before.switches.flip(SwitchName.MESSAGE, True)
        before.switches.flip("stop_notice", False)
        before.persist()

        after = a_bridge(path)
        assert after.restore() is True

        assert after.switches.is_effective(SwitchName.MESSAGE) is True
        assert after.switches.is_effective(SwitchName.VOICE) is False
        assert after.switches.is_set("stop_notice") is False

    def test_the_session_registry_survives_a_restart(self, tmp_path: Path) -> None:
        """The roster itself survives — every row, with everything it described."""
        path = default_state_path(tmp_path)
        session = a_session()

        before = a_bridge(path)
        before.sessions.register(session)
        before.sessions.set_reply_window(session.target, ReplyWindow.OPEN)
        before.persist()

        after = a_bridge(path)
        after.restore()

        (restored,) = after.sessions.all()
        assert restored.target == session.target
        assert restored.label == session.label
        assert restored.workspace == session.workspace
        assert restored.registered_at == session.registered_at

    def test_the_relay_queue_is_not_durable(self, tmp_path: Path) -> None:
        """Words whose moment has passed must not be re-delivered after a restart."""
        path = default_state_path(tmp_path)
        session = a_session()

        before = a_bridge(path)
        before.sessions.register(session)
        before.relays.enqueue(
            PendingRelay(
                request_id=new_request_id(),
                target=session.target,
                kind=RelayKind.ANSWER,
                text="yes, go ahead",
                queued_at=1.0,
                expires_at=601.0,
            )
        )
        before.persist()

        assert "yes, go ahead" not in path.read_text(encoding="utf-8")

        after = a_bridge(path)
        after.restore()
        assert after.relays.pending() == ()

    def test_restoring_when_nothing_was_ever_saved_is_a_first_run_not_a_failure(
        self, tmp_path: Path
    ) -> None:
        bridge = a_bridge(default_state_path(tmp_path))
        assert bridge.restore() is False
        assert bridge.sessions.all() == ()

    def test_a_bridge_with_no_store_neither_saves_nor_refuses(self) -> None:
        """Persistence is optional plumbing; the state components work without it."""
        bridge = BridgeState(
            switches=Switchboard(), sessions=SessionRegistry(), relays=RelayQueue()
        )
        bridge.persist()
        assert bridge.restore() is False


class TestTheFileItself:
    def test_the_file_is_written_atomically_leaving_no_partial_state(self, tmp_path: Path) -> None:
        path = default_state_path(tmp_path)
        bridge = a_bridge(path)
        bridge.switches.flip(SwitchName.DUTY, True)
        bridge.persist()
        bridge.persist()

        assert list(path.parent.iterdir()) == [path]

    def test_the_file_is_readable_by_a_human_during_an_outage(self, tmp_path: Path) -> None:
        path = default_state_path(tmp_path)
        bridge = a_bridge(path)
        bridge.switches.flip(SwitchName.DUTY, True)
        bridge.sessions.register(a_session())
        bridge.persist()

        written = json.loads(path.read_text(encoding="utf-8"))
        assert written["version"] == PersistedState.VERSION
        assert written["switches"]["duty"] is True
        assert written["sessions"][0]["target"]["agent"] == "claude"

    def test_a_corrupt_file_fails_closed_rather_than_starting_blank(self, tmp_path: Path) -> None:
        path = default_state_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")

        with pytest.raises(StateFormatError):
            a_bridge(path).restore()

    def test_a_file_from_a_future_version_fails_closed(self, tmp_path: Path) -> None:
        path = default_state_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": PersistedState.VERSION + 1, "switches": {}, "sessions": []}),
            encoding="utf-8",
        )

        with pytest.raises(StateFormatError):
            a_bridge(path).restore()

    def test_a_session_the_file_cannot_describe_fails_closed(self, tmp_path: Path) -> None:
        """A Claude row with no pid is unaddressable; guessing one would be worse."""
        path = default_state_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": PersistedState.VERSION,
                    "switches": {},
                    "sessions": [
                        {
                            "target": {"agent": "claude", "session_id": "abc", "pid": None},
                            "label": {"project": "p", "task": "t"},
                            "workspace": "/tmp",
                            "registered_at": 1.0,
                            "state": "live",
                            "reply_window": "closed",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(StateFormatError):
            a_bridge(path).restore()

    def test_a_switch_that_is_not_on_or_off_fails_closed(self, tmp_path: Path) -> None:
        """Read optimistically, a truthy string is how the master switch flips itself."""
        path = default_state_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": PersistedState.VERSION,
                    "switches": {"duty": "off"},
                    "sessions": [],
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(StateFormatError):
            a_bridge(path).restore()

    def test_a_switch_the_configuration_no_longer_declares_fails_closed(
        self, tmp_path: Path
    ) -> None:
        path = default_state_path(tmp_path)
        before = a_bridge(path)
        before.persist()

        without_features = BridgeState(
            switches=Switchboard(),
            sessions=SessionRegistry(),
            relays=RelayQueue(),
            store=StateStore(path),
        )
        with pytest.raises(StateFormatError):
            without_features.restore()


class TestOnlyCoreTouchesIt:
    def test_a_store_round_trips_state_on_its_own(self, tmp_path: Path) -> None:
        store = StateStore(default_state_path(tmp_path))
        state = PersistedState(
            switches=Switchboard(features=features()).snapshot(), sessions=(a_session(),)
        )
        store.save(state)
        assert store.load() == state


class TestARestoredSessionIsOneTheEngineCanHonour:
    """A restored row may not claim a capability the restart took away (#26).

    `register_session` is the sole place a Session's channel and its Reply Window
    watch are established, and the channel's address arrives from the launch that
    minted it. A restart loses both, so a row that came back LIVE — "whether this
    Session can still be Relayed into" — would be claiming, in the enum's own
    terms, exactly what it no longer has.
    """

    def test_a_live_row_comes_back_ended(self, tmp_path: Path) -> None:
        path = default_state_path(tmp_path)
        session = a_session()

        before = a_bridge(path)
        before.sessions.register(session)
        before.sessions.set_reply_window(session.target, ReplyWindow.OPEN)
        before.persist()

        after = a_bridge(path)
        after.restore()

        (restored,) = after.sessions.all()
        assert restored.state is SessionState.ENDED

    def test_its_reply_window_closes_with_it(self, tmp_path: Path) -> None:
        """An ended Session's window is closed, and a restored one is no exception."""
        path = default_state_path(tmp_path)
        session = a_session()

        before = a_bridge(path)
        before.sessions.register(session)
        before.sessions.set_reply_window(session.target, ReplyWindow.OPEN)
        before.persist()

        after = a_bridge(path)
        after.restore()

        (restored,) = after.sessions.all()
        assert restored.reply_window is ReplyWindow.CLOSED

    def test_no_restored_session_is_live_so_none_can_outlive_its_process(
        self, tmp_path: Path
    ) -> None:
        """#26's immortality, asked of the roster every surface actually reads."""
        path = default_state_path(tmp_path)

        before = a_bridge(path)
        before.sessions.register(a_session("abc", 100))
        before.sessions.register(a_session("def", 200))
        before.persist()

        after = a_bridge(path)
        after.restore()

        assert after.sessions.live() == ()

    def test_nothing_is_dropped_so_the_roster_keeps_its_history(self, tmp_path: Path) -> None:
        """Ending a row is the fix; forgetting it would be a second, quieter lie."""
        path = default_state_path(tmp_path)
        live, ended = a_session("abc", 100), a_session("def", 200)

        before = a_bridge(path)
        before.sessions.register(live)
        before.sessions.register(ended)
        before.sessions.mark_ended(ended.target)
        before.persist()

        after = a_bridge(path)
        after.restore()

        assert [held.target for held in after.sessions.all()] == [live.target, ended.target]

    def test_a_row_that_was_already_ended_restores_unchanged(self, tmp_path: Path) -> None:
        path = default_state_path(tmp_path)
        session = a_session()

        before = a_bridge(path)
        before.sessions.register(session)
        before.sessions.mark_ended(session.target)
        before.persist()

        after = a_bridge(path)
        after.restore()

        (restored,) = after.sessions.all()
        assert restored.state is SessionState.ENDED
        assert restored.reply_window is ReplyWindow.CLOSED

    def test_it_survives_repeated_restarts_without_changing_again(self, tmp_path: Path) -> None:
        """Idempotent: the second restart finds nothing left to end."""
        path = default_state_path(tmp_path)
        session = a_session()

        before = a_bridge(path)
        before.sessions.register(session)
        before.persist()

        first = a_bridge(path)
        first.restore()
        first.persist()
        once = json.loads(path.read_text(encoding="utf-8"))["sessions"]

        second = a_bridge(path)
        second.restore()
        second.persist()

        assert json.loads(path.read_text(encoding="utf-8"))["sessions"] == once
