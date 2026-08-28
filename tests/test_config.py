"""The engine's configuration: what it must be told, and what it may assume.

Two rules shape every case here. The **cost lever is never hard-coded** — the
Delegated Turn's model is a user-facing setting, so the file must carry it and
there is no default to quietly overrule it. And an **unconfigured seam refuses
to start**: an engine with nothing loaded behind the Call seam that starts
anyway is ADR 0003's outage, where three guards said nothing for a day.

Paths are the exception to "defaults are fine": both have one, because they are
locations rather than decisions, and both are overridable so nothing about
this file is hard-coded either.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gpt_voicecoding.config import (
    NULL_COMPANION_CHANNEL,
    ConfigError,
    EngineConfig,
    default_log_path,
    default_socket_path,
    load,
)
from gpt_voicecoding.seams.identity import AgentKind

COMPLETE = """
[adapters]
call = "tests.fakes:FakeCall"
companion_channel = "tests.fakes:FakeCompanionChannel"

[adapters.agents]
claude = "tests.fakes:FakeAgent"
codex = "tests.fakes:FakeAgent"

[delegate]
model = "a-model-the-user-chose"

[log]
max_bytes = 8388608
retained_files = 3
stripped_environment_prefixes = ["Malloc"]
"""


def written(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


class TestACompleteConfiguration:
    def test_it_loads(self, tmp_path: Path) -> None:
        config = load(written(tmp_path, COMPLETE))

        assert isinstance(config, EngineConfig)
        assert config.adapters.call == "tests.fakes:FakeCall"
        assert config.adapters.agents == {
            AgentKind.CLAUDE: "tests.fakes:FakeAgent",
            AgentKind.CODEX: "tests.fakes:FakeAgent",
        }
        assert config.delegated_turn_model == "a-model-the-user-chose"

    def test_the_durations_are_the_locked_defaults_until_configured(self, tmp_path: Path) -> None:
        config = load(written(tmp_path, COMPLETE))

        assert config.policy.relay_ceiling_seconds == 600.0
        assert config.policy.approval_budget_seconds == 600.0
        # legacy@1d32845:config.plist:74-78 — one 60-second heartbeat.
        assert config.policy.silence_end_seconds == 60.0

    def test_a_duration_may_be_dialled(self, tmp_path: Path) -> None:
        config = load(
            written(
                tmp_path,
                COMPLETE + "\n[policy]\napproval_budget_seconds = 90\nsilence_end_seconds = 12.5\n",
            )
        )

        assert config.policy.approval_budget_seconds == 90.0
        assert config.policy.silence_end_seconds == 12.5

    def test_a_duration_that_would_expire_everything_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            load(written(tmp_path, COMPLETE + "\n[policy]\nrelay_ceiling_seconds = 0\n"))

        with pytest.raises(ConfigError):
            load(written(tmp_path, COMPLETE + "\n[policy]\nsilence_end_seconds = 0\n"))


class TestWhereThingsLive:
    def test_the_socket_defaults_to_a_short_runtime_root(self, tmp_path: Path) -> None:
        """Darwin caps an AF_UNIX path at 103 bytes, so it cannot live beside the state."""
        config = load(written(tmp_path, COMPLETE))

        assert config.socket_path == default_socket_path()
        assert str(config.socket_path).endswith(f"gpt-voicecoding-{os.geteuid()}/control.sock")
        assert len(str(config.socket_path).encode()) <= 103

    def test_the_state_defaults_beside_the_application_support_directory(
        self, tmp_path: Path
    ) -> None:
        config = load(written(tmp_path, COMPLETE))

        assert config.state_path.parent.name == "engine"
        assert config.state_path.name == "state.json"

    def test_both_may_be_moved(self, tmp_path: Path) -> None:
        config = load(
            written(
                tmp_path,
                COMPLETE
                + f'\n[engine]\nsocket_path = "/tmp/x.sock"\nstate_path = "{tmp_path}/s.json"\n',
            )
        )

        assert config.socket_path == Path("/tmp/x.sock")
        assert config.state_path == tmp_path / "s.json"

    def test_a_path_is_expanded_the_way_a_person_writes_it(self, tmp_path: Path) -> None:
        config = load(written(tmp_path, COMPLETE + '\n[engine]\nstate_path = "~/state.json"\n'))

        assert config.state_path == Path.home() / "state.json"


class TestTheLogItOwns:
    """ADR 0004's four values: one location with a default, three decisions without.

    The split is the point. A cap, a retention count and a list of noisy variable
    prefixes are what a 68 MB outage *measured*, so a fallback in code would
    quietly reinstate a number the measurement proved matters. The log's path is
    a location, like the state file's and the socket's, so it defaults by the
    same rule those two do.
    """

    def test_the_bounds_are_carried_as_written(self, tmp_path: Path) -> None:
        config = load(written(tmp_path, COMPLETE))

        assert config.log.max_bytes == 8388608
        assert config.log.retained_files == 3
        assert config.log.stripped_environment_prefixes == ("Malloc",)

    def test_the_path_defaults_beside_the_state_file(self, tmp_path: Path) -> None:
        config = load(written(tmp_path, COMPLETE))

        assert config.log.path == default_log_path()
        assert config.log.path.parent == config.state_path.parent
        assert config.log.path.name == "engine.log"

    def test_the_path_may_be_moved_and_is_expanded(self, tmp_path: Path) -> None:
        config = load(written(tmp_path, COMPLETE + '\npath = "~/somewhere/engine.log"\n'))

        assert config.log.path == Path.home() / "somewhere" / "engine.log"

    def test_stripping_nothing_is_a_legitimate_answer(self, tmp_path: Path) -> None:
        """An empty list is a decision; an absent key is a decision not taken."""
        text = COMPLETE.replace(
            'stripped_environment_prefixes = ["Malloc"]',
            "stripped_environment_prefixes = []",
        )

        config = load(written(tmp_path, text))

        assert config.log.stripped_environment_prefixes == ()

    @pytest.mark.parametrize(
        "key", ["max_bytes", "retained_files", "stripped_environment_prefixes"]
    )
    def test_a_decision_left_unsaid_refuses_to_start(self, tmp_path: Path, key: str) -> None:
        text = "\n".join(line for line in COMPLETE.splitlines() if not line.startswith(key))

        with pytest.raises(ConfigError) as refusal:
            load(written(tmp_path, text))

        assert key in str(refusal.value)

    def test_a_configuration_with_no_log_table_at_all_refuses_to_start(
        self, tmp_path: Path
    ) -> None:
        text = COMPLETE.split("[log]")[0]

        with pytest.raises(ConfigError) as refusal:
            load(written(tmp_path, text))

        assert "[log]" in str(refusal.value)

    @pytest.mark.parametrize(
        "written_as",
        [
            "max_bytes = 0",
            'max_bytes = "8388608"',
            "max_bytes = true",
            "max_bytes = 8388608.5",
            "retained_files = -1",
            'retained_files = "3"',
            'stripped_environment_prefixes = "Malloc"',
            "stripped_environment_prefixes = [1]",
            'stripped_environment_prefixes = ["  "]',
        ],
    )
    def test_a_bound_that_does_not_bind_anything(self, tmp_path: Path, written_as: str) -> None:
        key = written_as.split(" =")[0]
        text = "\n".join(line for line in COMPLETE.splitlines() if not line.startswith(key))

        with pytest.raises(ConfigError) as refusal:
            load(written(tmp_path, text + "\n" + written_as + "\n"))

        assert key in str(refusal.value)

    def test_a_log_path_that_is_not_a_path(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as refusal:
            load(written(tmp_path, COMPLETE + '\npath = ""\n'))

        assert "[log] path" in str(refusal.value)


class TestWhatItRefuses:
    def test_a_file_that_is_not_there(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as refusal:
            load(tmp_path / "absent.toml")

        assert str(tmp_path / "absent.toml") in str(refusal.value)

    def test_a_file_that_is_not_toml(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            load(written(tmp_path, "this is not = = toml"))

    def test_a_missing_delegated_turn_model(self, tmp_path: Path) -> None:
        """The cost lever is the user's, so there is no default to overrule it."""
        with pytest.raises(ConfigError) as refusal:
            load(written(tmp_path, COMPLETE.replace('model = "a-model-the-user-chose"', "")))

        assert "model" in str(refusal.value)

    @pytest.mark.parametrize("seam", ["call", "companion_channel"])
    def test_a_seam_with_nothing_behind_it(self, tmp_path: Path, seam: str) -> None:
        text = "\n".join(line for line in COMPLETE.splitlines() if not line.startswith(seam))

        with pytest.raises(ConfigError) as refusal:
            load(written(tmp_path, text))

        assert seam in str(refusal.value)

    def test_an_unconfigured_channel_names_the_null_one_to_write(self, tmp_path: Path) -> None:
        """Legitimate to run without text reach — and it is said out loud, not left blank.

        The refusal carries the exact reference an operator can paste, which is
        only useful for as long as it is the real one; `test_companion_channel`
        holds the spelling to the adapter that answers to it.
        """
        text = "\n".join(
            line for line in COMPLETE.splitlines() if not line.startswith("companion_channel")
        )

        with pytest.raises(ConfigError) as refusal:
            load(written(tmp_path, text))

        assert NULL_COMPANION_CHANNEL in str(refusal.value)

    def test_no_agent_at_all(self, tmp_path: Path) -> None:
        text = COMPLETE.replace('claude = "tests.fakes:FakeAgent"', "").replace(
            'codex = "tests.fakes:FakeAgent"', ""
        )

        with pytest.raises(ConfigError) as refusal:
            load(written(tmp_path, text))

        assert "agent" in str(refusal.value)

    def test_an_agent_this_system_does_not_run(self, tmp_path: Path) -> None:
        text = COMPLETE.replace("codex =", "emacs =")

        with pytest.raises(ConfigError) as refusal:
            load(written(tmp_path, text))

        assert "emacs" in str(refusal.value)

    def test_a_factory_reference_that_names_no_attribute(self, tmp_path: Path) -> None:
        text = COMPLETE.replace('"tests.fakes:FakeCall"', '"tests.fakes"')

        with pytest.raises(ConfigError) as refusal:
            load(written(tmp_path, text))

        assert "module:attribute" in str(refusal.value)


def with_cli(text: str, stated: str) -> str:
    """State `cli` inside `[delegate]`, which is no longer the file's last table."""
    return text.replace(
        'model = "a-model-the-user-chose"', f'model = "a-model-the-user-chose"\ncli = "{stated}"'
    )


class TestWhereTheControlPlaneCliIs:
    """A location this file may state, and usually does not.

    The engine derives the CLI from its own installation; a bundle moves it, so
    the bundle can say where. Stating nothing is the ordinary case, and it is
    not the same as stating nothing useful.
    """

    def test_it_is_absent_until_an_installation_states_it(self, tmp_path: Path) -> None:
        assert load(written(tmp_path, COMPLETE)).control_plane_cli is None

    def test_a_stated_location_is_read_as_a_path(self, tmp_path: Path) -> None:
        config = load(written(tmp_path, with_cli(COMPLETE, "/Applications/GVC.app/bridgectl")))

        assert config.control_plane_cli == Path("/Applications/GVC.app/bridgectl")

    def test_a_home_relative_location_is_expanded(self, tmp_path: Path) -> None:
        config = load(written(tmp_path, with_cli(COMPLETE, "~/bin/bridgectl")))

        assert config.control_plane_cli == Path("~/bin/bridgectl").expanduser()

    def test_an_empty_location_is_refused_rather_than_treated_as_absent(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(ConfigError) as refusal:
            load(written(tmp_path, with_cli(COMPLETE, "   ")))

        assert "cli" in str(refusal.value)
