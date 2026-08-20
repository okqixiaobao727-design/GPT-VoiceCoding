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

from gpt_voicecoding.config import ConfigError, EngineConfig, default_socket_path, load
from gpt_voicecoding.seams.identity import AgentKind

COMPLETE = """
[adapters]
call = "tests.fakes:FakeCall"
companion_channel = "tests.fakes:FakeCompanionChannel"
session_launcher = "tests.fakes:FakeSessionLauncher"

[adapters.agents]
codex = "tests.fakes:FakeAgent"

[delegate]
model = "a-model-the-user-chose"
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
        assert config.adapters.agents == {AgentKind.CODEX: "tests.fakes:FakeAgent"}
        assert config.delegated_turn_model == "a-model-the-user-chose"

    def test_the_durations_are_the_locked_defaults_until_configured(
        self, tmp_path: Path
    ) -> None:
        config = load(written(tmp_path, COMPLETE))

        assert config.policy.relay_ceiling_seconds == 600.0
        assert config.policy.approval_budget_seconds == 600.0

    def test_a_duration_may_be_dialled(self, tmp_path: Path) -> None:
        config = load(written(tmp_path, COMPLETE + "\n[policy]\napproval_budget_seconds = 90\n"))

        assert config.policy.approval_budget_seconds == 90.0

    def test_a_duration_that_would_expire_everything_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            load(written(tmp_path, COMPLETE + "\n[policy]\nrelay_ceiling_seconds = 0\n"))


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

    @pytest.mark.parametrize(
        "seam", ["call", "companion_channel", "session_launcher"]
    )
    def test_a_seam_with_nothing_behind_it(self, tmp_path: Path, seam: str) -> None:
        text = "\n".join(line for line in COMPLETE.splitlines() if not line.startswith(seam))

        with pytest.raises(ConfigError) as refusal:
            load(written(tmp_path, text))

        assert seam in str(refusal.value)

    def test_an_unconfigured_channel_says_the_null_one_is_coming(self, tmp_path: Path) -> None:
        """Legitimate to run without a channel — but that adapter is not built yet."""
        text = "\n".join(
            line for line in COMPLETE.splitlines() if not line.startswith("companion_channel")
        )

        with pytest.raises(ConfigError) as refusal:
            load(written(tmp_path, text))

        assert "null" in str(refusal.value).lower()

    def test_no_agent_at_all(self, tmp_path: Path) -> None:
        text = COMPLETE.replace('codex = "tests.fakes:FakeAgent"', "")

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
