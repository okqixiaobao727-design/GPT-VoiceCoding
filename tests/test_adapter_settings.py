"""`[adapters.settings.<seam>]`: read at the root, understood only by the adapter.

Two halves, and the split is the point. Configuration checks the *name on the
table* — settings addressed to a seam this engine does not fill would silently
never be applied — and nothing else. What is inside is forwarded whole, because
only the adapter knows what its own keys mean, and it is the adapter that
refuses the ones it does not have.

The backward-compatibility case is a test rather than a claim: an adapter whose
factory takes only the sink must keep working untouched, which is what every
existing configuration and every existing fake relies on.
"""

from __future__ import annotations

from typing import Any

import pytest

from fakes import FakeAgent, FakeCall, FakeCompanionChannel
from gpt_voicecoding.config import ConfigError, of
from gpt_voicecoding.engine.composition import Engine, EngineAssemblyError

#: What a factory that accepts a settings table was handed, per seam.
handed: dict[str, Any] = {}


def channel_taking_settings(*, sink: Any = None, settings: Any = None) -> FakeCompanionChannel:
    handed["companion_channel"] = settings
    return FakeCompanionChannel(sink=sink)


def agent_taking_settings(*, sink: Any = None, settings: Any = None) -> FakeAgent:
    handed["agent.codex"] = settings
    return FakeAgent(sink=sink)


def channel_taking_only_the_sink(*, sink: Any = None) -> FakeCompanionChannel:
    """The shape every adapter had before this table existed."""
    return FakeCompanionChannel(sink=sink)


def document(**adapters: Any) -> dict[str, Any]:
    """One whole configuration document, with the seams filled by fakes."""
    chosen: dict[str, Any] = {
        "call": "fakes:FakeCall",
        "companion_channel": "test_adapter_settings:channel_taking_only_the_sink",
        "agents": {"codex": "fakes:FakeAgent"},
    }
    chosen.update(adapters)
    return {
        "engine": {},
        "adapters": chosen,
        "delegate": {"model": "a-model"},
        "log": {"max_bytes": 1, "retained_files": 0, "stripped_environment_prefixes": []},
    }


class TestReadingTheTable:
    def test_a_configuration_with_no_settings_has_none(self) -> None:
        config = of(document())
        assert config.adapters.settings == {}
        assert config.adapters.settings_for("call") is None

    def test_a_table_is_kept_whole_and_uninspected(self) -> None:
        """Nonsense inside is the adapter's to refuse, not configuration's."""
        config = of(document(settings={"call": {"anything": [1, 2], "at": {"all": True}}}))
        assert config.adapters.settings_for("call") == {"anything": [1, 2], "at": {"all": True}}

    def test_an_agent_seam_is_addressed_by_its_dotted_name(self) -> None:
        config = of(document(settings={"agent.codex": {"executable": "/opt/codex"}}))
        assert config.adapters.settings_for("agent.codex") == {"executable": "/opt/codex"}

    def test_settings_for_a_seam_this_engine_does_not_fill_are_refused(self) -> None:
        """Otherwise they are settings that quietly never apply to anything."""
        with pytest.raises(ConfigError, match="names no seam this engine fills"):
            of(document(settings={"agent.claude": {"executable": "claude"}}))

    def test_a_settings_entry_that_is_not_a_table_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="must be a table"):
            of(document(settings={"call": "not-a-table"}))

    def test_the_settings_key_itself_must_be_a_table(self) -> None:
        with pytest.raises(ConfigError, match=r"\[adapters.settings\] must be a table"):
            of(document(settings="nope"))


class TestForwardingTheTable:
    def test_a_seam_with_a_table_is_handed_it(self) -> None:
        handed.clear()
        Engine.assemble(
            of(
                document(
                    companion_channel="test_adapter_settings:channel_taking_settings",
                    settings={"companion_channel": {"chat_id": "left"}},
                )
            )
        )
        assert handed["companion_channel"] == {"chat_id": "left"}

    def test_an_agent_seam_is_handed_its_own_table(self) -> None:
        handed.clear()
        Engine.assemble(
            of(
                document(
                    agents={"codex": "test_adapter_settings:agent_taking_settings"},
                    settings={"agent.codex": {"receipt_timeout_seconds": 2}},
                )
            )
        )
        assert handed["agent.codex"] == {"receipt_timeout_seconds": 2}

    def test_a_seam_with_no_table_is_called_exactly_as_before(self) -> None:
        """The compatibility guarantee: an adapter taking only the sink still builds."""
        engine = Engine.assemble(of(document()))
        assert isinstance(engine.adapters.call, FakeCall)
        assert isinstance(engine.adapters.channel, FakeCompanionChannel)

    def test_a_table_for_an_adapter_that_takes_none_says_so_plainly(self) -> None:
        """Naming the settings in the refusal is what makes the mistake findable."""
        with pytest.raises(EngineAssemblyError, match="and its settings table"):
            Engine.assemble(of(document(settings={"call": {"model": "a-model"}})))
