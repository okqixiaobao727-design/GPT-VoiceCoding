"""Step selection, its prerequisite closure, and what the verdict says about both.

The acceptance run itself never reaches CI — it needs this machine, these
credentials and those two bots. The rules below are ordinary code, and #182 asks
for them at CI speed for the same reason #109 asked for the attribution rule:
the expensive walk is the worst place to discover that a pure decision was wrong.

Three of those rules live here.

* **A selected step brings its prerequisites as ungraded setup.** `--step "stable
  name"` is not a claim that `roster` passed; it is a claim about one step, walked
  on the state the whole lane would have given it.
* **A failed setup step blocks the lane.** The verdict must never carry a graded
  green for a step that stood on ground the run could not arrange.
* **The verdict names both kinds**, so a green single step is never read as a
  green lane.

The second bot is here too: which variable each lane's engine is told to read
(`token_env` in its derived config), and the chat-reachability preflight, because
the one thing that must not do — send a message to prove a chat is open — is
exactly what a test can pin.
"""

from __future__ import annotations

import json
from pathlib import Path

import journey
import pytest
import support

from gpt_voicecoding import config
from gpt_voicecoding.adapters.companion_channel.telegram.api import refused_by
from gpt_voicecoding.seams.identity import AgentKind

A_LANE = "claude"


def _nowhere(event: str, **fields: object) -> None:  # noqa: ARG001
    """A journal that writes nowhere: these tests grade the verdict, not the log."""


def _verdict(chosen: journey.Selection) -> support.Verdict:
    return support.Verdict(
        run_id="20260902T000000Z",
        bundle="/Applications/GPT-VoiceCoding.app",
        commit="0000000",
        provenance="identical",
        expected_lanes=(A_LANE,),
        expected_steps=chosen.selected,
        setup_steps=chosen.setup,
    )


def _walk(chosen: journey.Selection, record: support.Verdict) -> support.Journey:
    return support.Journey(
        lane=A_LANE,
        verdict=record,
        journal=_nowhere,
        steps=chosen.steps,
        setup=chosen.setup,
    )


# --- the prerequisite table -------------------------------------------------


def test_every_step_declares_what_it_needs_behind_it() -> None:
    """The table and the nine names are the same set, or a step has no answer."""
    assert tuple(journey.PREREQUISITES) == journey.STEPS


def test_no_step_needs_one_that_runs_after_it() -> None:
    """The closure is walked in `STEPS` order, so every edge has to point backwards."""
    order = {step: index for index, step in enumerate(journey.STEPS)}
    for step, needed in journey.PREREQUISITES.items():
        for one in needed:
            assert one in order, f"{step} needs {one!r}, which is not a step"
            assert order[one] < order[step], f"{step} needs {one!r}, which runs after it"


def test_every_step_name_is_bound_to_a_method() -> None:
    """`Walk.bound_steps` is where a name becomes code; it covers the nine exactly."""
    walk = object.__new__(journey.Walk)
    assert tuple(walk.bound_steps()) == journey.STEPS


# --- selection --------------------------------------------------------------


def test_no_selection_grades_the_whole_lane() -> None:
    chosen = journey.select(())
    assert chosen.selected == journey.STEPS
    assert chosen.setup == ()
    assert chosen.steps == journey.STEPS
    assert chosen.whole_lane


def test_a_selected_step_brings_its_prerequisite_as_ungraded_setup() -> None:
    chosen = journey.select(("stable name",))
    assert chosen.selected == ("stable name",)
    assert chosen.setup == ("roster",)
    assert chosen.steps == ("roster", "stable name")
    assert chosen.graded("stable name")
    assert not chosen.graded("roster")
    assert not chosen.whole_lane


def test_the_closure_is_transitive() -> None:
    """`approval` observes the turn `relay` drives, and `relay` needs the roster's address."""
    assert journey.select(("approval",)).setup == ("roster", "relay")


def test_a_step_that_reads_a_turn_brings_the_step_that_drove_one() -> None:
    """`progress` wants history and `stop notice` wants a Stop; `stable name` is the turn."""
    assert journey.select(("progress",)).setup == ("roster", "stable name")
    assert journey.select(("stop notice",)).setup == ("roster", "stable name")


def test_a_prerequisite_that_was_also_asked_for_is_graded() -> None:
    chosen = journey.select(("approval", "relay"))
    assert chosen.selected == ("relay", "approval")
    assert chosen.setup == ("roster",)
    assert chosen.graded("relay")


def test_the_selection_is_in_step_order_however_it_was_typed() -> None:
    chosen = journey.select(("child", "roster", "child"))
    assert chosen.selected == ("roster", "child")
    assert chosen.steps == ("roster", "child")


def test_an_unknown_step_refuses_and_lists_the_valid_ones() -> None:
    """A typo is a refusal with the answer in it, never a run that quietly walks nothing."""
    with pytest.raises(journey.UnknownStep) as refusal:
        journey.select(("rooster", "roster"))
    said = str(refusal.value)
    assert "rooster" in said
    for step in journey.STEPS:
        assert step in said


# --- what the verdict says about the two kinds ------------------------------


def test_the_verdict_names_the_selected_steps_and_the_setup_steps(tmp_path: Path) -> None:
    chosen = journey.select(("stable name",))
    record = _verdict(chosen)
    walked = _walk(chosen, record)
    walked.run("roster", lambda: "one row against its own workspace")
    walked.run("stable name", lambda: "'able-otter' across 3 reads")

    written = json.loads(record.write(tmp_path / "verdict.json").read_text())
    assert written["selection"] == {"selected": ["stable name"], "setup": ["roster"]}
    rows = {row["step"]: row for row in written["lanes"][A_LANE]}
    assert rows["roster"]["graded"] is False
    assert rows["stable name"]["graded"] is True
    assert written["result"] == "PASS"


def test_a_single_step_run_promises_only_the_step_it_selected() -> None:
    """The eight steps it never ran are not `missing`; the one it owes is."""
    chosen = journey.select(("stable name",))
    record = _verdict(chosen)
    walked = _walk(chosen, record)
    walked.run("roster", lambda: "one row")

    assert record.missing == (f"{A_LANE}/stable name",)
    assert record.result == support.FAIL


def test_a_failed_setup_step_blocks_the_lane() -> None:
    """The selected step is not graded on ground the run could not arrange."""
    chosen = journey.select(("stable name",))
    record = _verdict(chosen)
    walked = _walk(chosen, record)

    def refuse() -> str:
        raise support.StepFailed("no row for this Session in the roster")

    walked.run("roster", refuse)
    walked.run("stable name", lambda: "never reached")

    rows = {step.step: step for step in record.lanes[A_LANE]}
    assert rows["roster"].result == support.FAIL
    assert rows["roster"].graded is False
    assert rows["stable name"].result == support.SKIPPED
    assert rows["stable name"].graded is True
    assert record.result == support.FAIL


def test_the_run_is_decided_by_its_graded_rows_alone() -> None:
    """Stated on the `Verdict` itself, because `Journey` will not produce this shape.

    A failed setup step blocks the lane, so this pairing cannot arise from a walk.
    The rule is still the verdict's: an ungraded row is evidence about the
    arrangement, and only the steps the run *promised* decide what it says.
    """
    record = _verdict(journey.select(("stable name",)))
    record.record(A_LANE, "roster", support.FAIL, "arranged badly", graded=False)
    record.record(A_LANE, "stable name", support.PASS, "one name")
    assert record.result == support.PASS


def test_a_row_is_graded_unless_the_caller_says_otherwise() -> None:
    """`test_realtime_probe.py` records `0b` with no selection behind it."""
    record = _verdict(journey.select(()))
    recorded = record.record("probe", "0b realtime contract probe", support.PASS, "441 frames")
    assert recorded.graded is True


# --- the two lanes' bots ----------------------------------------------------


def test_the_second_lane_derives_its_token_variable_from_the_configured_one() -> None:
    """Nothing is hard-coded: the config names the first, and the second is `_2` of it."""
    configured = "GPTVOICECODING_TELEGRAM_TOKEN"
    assert journey.CLAUDE.token_variable(configured) == configured
    assert journey.CODEX.token_variable(configured) == f"{configured}_2"


def test_no_two_lanes_read_the_same_token_variable() -> None:
    """One bot, one engine — kept by giving each lane its own bot (#180 §2 decision 3)."""
    variables = {lane.token_variable("ANY") for lane in journey.LANES}
    assert len(variables) == len(journey.LANES)


A_CONFIG = """
[engine]
socket_path = "/tmp/never.sock"
state_path = "/tmp/never.json"

[log]
path = "/tmp/never.log"
max_bytes = 8388608
retained_files = 3
level = "INFO"
stripped_environment_prefixes = ["CLAUDE", "CODEX"]

[delegate]
model = "a-model"
cli = "/usr/bin/true"

[adapters]
call = "gpt_voicecoding.adapters.call.silent:silent_call"
companion_channel = "gpt_voicecoding.adapters.companion_channel.telegram:telegram_channel"

[adapters.agents]
claude = "gpt_voicecoding.adapters.agent.claude:claude_agent"
codex = "gpt_voicecoding.adapters.agent.codex:codex_agent"

[adapters.settings.companion_channel]
token_env = "GPTVOICECODING_TELEGRAM_TOKEN"
chat_id = "8675309"

[adapters.settings."agent.claude"]
request_timeout_seconds = 30.0
"""


def _derived(
    tmp_path: Path,
    lane: journey.Lane,
    *,
    codex_socket_directory: Path | None = None,
    dropped_agents: tuple[AgentKind, ...] = (),
) -> support.DerivedConfig:
    source = tmp_path / "config.toml"
    source.write_text(A_CONFIG)
    configured = "GPTVOICECODING_TELEGRAM_TOKEN"
    return support.derive_config(
        source=source,
        run_directory=tmp_path / f"engine-{lane.name}",
        workspace=tmp_path / f"workspace-{lane.name}",
        socket_path=tmp_path / f"{lane.name}.sock",
        project_name=f"acceptance-{lane.name}",
        token_variable=lane.token_variable(configured),
        codex_socket_directory=codex_socket_directory,
        dropped_agents=dropped_agents,
    )


def test_each_lane_binds_its_own_token_variable_in_the_derived_config(tmp_path: Path) -> None:
    """The engine is told which variable holds its bot's token — the shipped mechanism."""
    import tomllib

    variables = {}
    for lane in journey.LANES:
        derived = _derived(tmp_path, lane)
        written = tomllib.loads(derived.path.read_text())
        channel = written["adapters"]["settings"]["companion_channel"]
        assert channel["token_env"] == lane.token_variable("GPTVOICECODING_TELEGRAM_TOKEN")
        assert derived.token_variable == channel["token_env"]
        variables[lane.name] = channel["token_env"]

    assert len(set(variables.values())) == len(journey.LANES), variables


def test_each_lane_gets_its_own_codex_app_server_socket_directory(tmp_path: Path) -> None:
    """Two engines cannot share one app-server socket: the product refuses the second."""
    import tomllib

    directories = {}
    for lane in journey.LANES:
        derived = _derived(tmp_path, lane, codex_socket_directory=tmp_path / lane.name)
        written = tomllib.loads(derived.path.read_text())
        table = written["adapters"]["settings"][support.CODEX_SETTINGS_KEY]
        directories[lane.name] = table["socket_directory"]
        assert directories[lane.name] == str(tmp_path / lane.name)

    assert len(set(directories.values())) == len(journey.LANES), directories


def test_a_derived_config_is_one_the_engine_would_accept(tmp_path: Path) -> None:
    """Read back with the engine's **own** reader, which is the only opinion that counts.

    A derived config is only ever proved by an engine starting on it, and that
    costs a real run. This is the same judgement at CI speed — and it is not
    hypothetical: run `20260902T013222Z` lost both lanes in nine seconds to a
    settings table spelled as this harness imagined rather than as
    `config.py:132` builds it, with `verdict.json` REFUSED and no engine log to
    say why (ADR 0004: output before the log is adopted is discarded).
    """
    for lane in journey.LANES:
        dropped = () if lane.agent == str(AgentKind.CLAUDE) else (AgentKind.CLAUDE,)
        derived = _derived(
            tmp_path,
            lane,
            codex_socket_directory=tmp_path / lane.name,
            dropped_agents=dropped,
        )
        read = config.load(derived.path)
        assert read.adapters.settings[support.CODEX_SETTINGS_KEY]["socket_directory"] == str(
            tmp_path / lane.name
        )
        # The lane still has the agent it walks, and the engine's own reader is
        # what says the drop left a config it would start on (#202).
        assert AgentKind(lane.agent) in read.adapters.agents


def test_the_derived_socket_path_stays_inside_the_unix_domain_limit(tmp_path: Path) -> None:
    """Darwin caps an AF_UNIX path at 103 bytes, and this one is the longest.

    `<lane root>/gpt-voicecoding-<uid>/codex-app-server.sock` is deeper than the
    control socket beside it, so it is the one that would overflow first.
    """
    root = support.SOCKET_ROOT / f"gvc-acceptance-{99999}-20260902T012313Z-claude"
    longest = root / "gpt-voicecoding-99999" / "codex-app-server.sock"
    assert len(str(longest).encode()) < 104, longest


def test_a_derived_config_keeps_the_configured_variable_when_no_lane_asks(tmp_path: Path) -> None:
    source = tmp_path / "config.toml"
    source.write_text(A_CONFIG)
    derived = support.derive_config(
        source=source,
        run_directory=tmp_path / "engine",
        workspace=tmp_path / "workspace",
        socket_path=tmp_path / "one.sock",
        project_name="acceptance",
    )
    assert derived.token_variable == "GPTVOICECODING_TELEGRAM_TOKEN"


def test_the_engine_exports_the_token_under_its_own_lane_name(tmp_path: Path) -> None:
    """The binding has to reach the engine's environment, or the lane runs on no bot."""
    derived = _derived(tmp_path, journey.CODEX)
    engine = support.Engine(
        config=derived,
        bundle=tmp_path / "bundle",
        journal=_nowhere,
        token="second-bot-token",
        path_value="/usr/bin:/bin",
    )
    assert engine.environment[derived.token_variable] == "second-bot-token"
    assert derived.token_variable == "GPTVOICECODING_TELEGRAM_TOKEN_2"


def test_two_lanes_on_one_bot_is_a_refusal_that_names_both_variables() -> None:
    """One token in both variables answers `getMe` twice and breaks both lanes."""
    same = {"id": 42, "username": "only_one_bot"}
    refusal = support.duplicate_bot_refusal(
        {"claude": same, "codex": dict(same)},
        variables={"claude": "TOKEN", "codex": "TOKEN_2"},
    )
    assert refusal is not None
    assert "only_one_bot" in refusal
    assert "TOKEN" in refusal and "TOKEN_2" in refusal


def test_a_bot_each_is_no_refusal() -> None:
    assert (
        support.duplicate_bot_refusal(
            {"claude": {"id": 1, "username": "first"}, "codex": {"id": 2, "username": "second"}},
            variables={"claude": "TOKEN", "codex": "TOKEN_2"},
        )
        is None
    )


def test_one_lane_is_never_two_lanes_on_one_bot() -> None:
    """A `--lane` run of one has nothing to collide with, and must not refuse."""
    assert (
        support.duplicate_bot_refusal(
            {"codex": {"id": 2, "username": "second"}}, variables={"codex": "TOKEN_2"}
        )
        is None
    )


# --- what a selection makes preflight refuse about --------------------------


def test_the_codex_permission_ground_matters_to_the_steps_that_stand_on_it() -> None:
    """`relay` writes it, `approval` grades it, `switches` leaves one pending."""
    for step in journey.CODEX_PERMISSION_STEPS:
        chosen = journey.select((step,))
        assert journey.codex_permission_ground_matters(journey.LANES, chosen.steps), step


def test_it_does_not_matter_to_a_lane_this_run_is_not_walking() -> None:
    """`--lane claude` must not be refused over a Codex ground nothing will stand on."""
    assert not journey.codex_permission_ground_matters((journey.CLAUDE,), journey.select(()).steps)


def test_it_does_not_matter_to_a_selection_that_provokes_no_permission() -> None:
    chosen = journey.select(("stable name",))
    assert chosen.steps == ("roster", "stable name")
    assert not journey.codex_permission_ground_matters(journey.LANES, chosen.steps)


def test_it_matters_to_the_full_run() -> None:
    assert journey.codex_permission_ground_matters(journey.LANES, journey.select(()).steps)


def test_a_prerequisite_can_be_what_makes_it_matter() -> None:
    """`approval` brings `relay`, and `relay` is a step the ground is about."""
    chosen = journey.select(("approval",))
    assert "relay" in chosen.setup
    assert journey.codex_permission_ground_matters(journey.LANES, chosen.steps)


def test_a_reachable_chat_is_no_refusal_and_costs_the_chat_nothing() -> None:
    """`getChat` is a read. Proving reachability by *sending* would write into the chat."""
    calls: list[tuple[str, dict]] = []

    def transport(method: str, payload: dict, *, timeout_seconds: float) -> dict:  # noqa: ARG001
        calls.append((method, payload))
        return {"id": 8675309, "type": "private"}

    assert support.chat_open_refusal(transport, chat_id="8675309", bot_username="second") is None
    assert calls == [("getChat", {"chat_id": "8675309"})]


def test_a_chat_the_account_never_opened_refuses_with_the_start_instruction() -> None:
    """A bot cannot open a chat with a person, so the refusal has to name the human step."""

    def transport(method: str, payload: dict, *, timeout_seconds: float) -> dict:  # noqa: ARG001
        raise refused_by(method, 400, "Bad Request: chat not found")

    refusal = support.chat_open_refusal(transport, chat_id="8675309", bot_username="second")
    assert refusal is not None
    assert "second" in refusal
    assert "/start" in refusal
    assert "chat not found" in refusal


def test_the_codex_lane_carries_no_claude_agent_adapter(tmp_path: Path) -> None:
    """#202: the published approval address is one file per user per machine, and
    only an engine that loads the Claude adapter ever claims it. The Codex lane's
    journey never needs that route, so dropping the adapter is what stops the two
    lanes racing for it — the harness's half of the fix, beside the product's."""
    import tomllib

    derived = _derived(tmp_path, journey.CODEX, dropped_agents=(AgentKind.CLAUDE,))
    agents = tomllib.loads(derived.path.read_text())["adapters"]["agents"]

    assert str(AgentKind.CLAUDE) not in agents
    assert str(AgentKind.CODEX) in agents, "the lane still needs the agent it walks"
    settings = tomllib.loads(derived.path.read_text())["adapters"]["settings"]
    assert f"agent.{AgentKind.CLAUDE}" not in settings, (
        "a settings table for an adapter the engine no longer builds names no seam it fills, "
        "and the engine refuses the whole config over it"
    )
    dropped = json.loads((derived.path.parent / "config-dropped.json").read_text())
    assert f"adapters.agents.{AgentKind.CLAUDE}" in dropped
    assert f'adapters.settings."agent.{AgentKind.CLAUDE}"' in dropped


def test_the_claude_lane_carries_every_agent_adapter_the_user_configured(tmp_path: Path) -> None:
    """Unchanged, and that is the asymmetry: the Claude lane is the one engine
    left claiming the address, so it keeps the config the user actually wrote."""
    import tomllib

    derived = _derived(tmp_path, journey.CLAUDE)
    agents = tomllib.loads(derived.path.read_text())["adapters"]["agents"]

    assert str(AgentKind.CLAUDE) in agents
    assert str(AgentKind.CODEX) in agents
