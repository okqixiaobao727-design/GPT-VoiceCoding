"""The acceptance harness's attribution rule, held against the product's own words.

`tests/acceptance/journey.py` states one rule for every step that reads the chat:
**a step only ever attributes what names its own target.** The rule exists because
the engine bridges every Session on the machine, so the chat is a shared surface —
on run `20260826T213402Z` the `stop notice` step passed on a permission prompt
belonging to a stale `/tmp/vcprobe` thread (#109).

The rule rests on the harness knowing what the product would call a Session, and
`_naming_forms` mirrors `core/sessions.py:spoken_name`/`spoken_target` rather than
importing them — a harness that asked the product what it had said would agree
with the product by construction. Mirrors drift, and an acceptance run is an
expensive place to find out. So the tests below compose the product's **real**
notices, off a **real** roster row, and assert the harness attributes them.

The acceptance run itself never reaches CI. This does, and it is what makes the
mirror a thing that breaks loudly rather than an acceptance step that goes quiet.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import hand_started
import journey
import pytest
import support

from gpt_voicecoding.control_plane.payloads import session_document
from gpt_voicecoding.core import briefing
from gpt_voicecoding.core.bridge import stop_brief
from gpt_voicecoding.core.sessions import Session, session_from
from gpt_voicecoding.seams.agent import (
    ChildClassification,
    ChildKind,
    SessionInspection,
    WaitingFor,
    WaitingKind,
)
from gpt_voicecoding.seams.identity import AgentKind, SessionName, SessionTarget

MINE = SessionTarget(agent=AgentKind.CLAUDE, session_id="6f723f5c", pid=64312)
A_STRANGER = SessionTarget(agent=AgentKind.CODEX, session_id="01a04001", pid=95827)

#: The message that made #109: a permission prompt from somebody else's Session,
#: taken verbatim off run `20260826T213402Z`'s journal.
THE_STRANGERS_PROMPT = (
    "a session is waiting for your permission to use a shell command — Do you want to "
    "allow me to run exactly this one write command to create /tmp/vcprobe/approval-probe.txt?"
)


@pytest.fixture
def isolated_codex_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A Codex config stack that cannot inherit this machine's writable roots."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setattr(
        journey,
        "CODEX_SYSTEM_CONFIG",
        tmp_path / "system-config.toml",
        raising=False,
    )
    return {
        "CODEX_HOME": str(codex_home),
        "TMPDIR": "/private/var/folders/example/T/",
    }


def session(target: SessionTarget = MINE, *, task: str | None = "port the log") -> Session:
    return Session(
        target=target,
        name=SessionName("workspace-claude", task) if task is not None else None,
        workspace=Path("/tmp/workspace"),
        first_seen=0.0,
    )


def notice_for(one: Session) -> str:
    """The product's real Stop Notice for that Session — a Session Brief, as text.

    Composed through the product rather than quoted, which is the whole point of
    this module: the harness's mirror has to break when the product's own words
    move, and since #189 those words are `Briefing`'s.
    """
    return briefing.text(stop_brief(one, WaitingFor()))


def row(one: Session) -> dict:
    """The roster row a surface reads, built the way the control plane builds it."""
    return session_document(
        one,
        progress={
            "availability": "not_read",
            "has_history": None,
            "omission": "none",
            "read_at": None,
            "recent": [],
        },
    )


class TestWhatTheHarnessThinksNamesASession:
    def test_a_named_session_is_named_by_its_session_name(self) -> None:
        assert "workspace-claude · port the log" in journey._naming_forms(row(session()))

    def test_a_session_with_no_name_yet_falls_back_to_its_address(self) -> None:
        """Measured: a Codex Session has no name until its first turn."""
        assert journey._naming_forms(row(session(task=None))) == (
            "claude 6f723f5c",
            "claude:6f723f5c:64312",
        )

    def test_a_session_with_no_id_yet_falls_back_to_its_pid(self) -> None:
        """`spoken_target`'s own second fallback, mirrored — codex before its rollout."""
        bare = {"target": {"agent": "codex", "session_id": None, "pid": 95827}, "name": None}

        # `codex::95827` is `address_of` on a target with no id yet: the id half
        # is written as nothing at all rather than the word `None` (#73), and the
        # brief's header prints exactly that.
        assert journey._naming_forms(bare) == ("codex pid 95827", "codex::95827")

    def test_a_row_that_names_nothing_yields_nothing_to_attribute_with(self) -> None:
        """Not an empty pass: `_await_message_naming` refuses on this rather than waiting."""
        assert journey._naming_forms({}) == ()
        assert journey._naming_forms({"target": "not a mapping", "name": ""}) == ()


class TestTheSwitchesWaitIsResolvable:
    """#80 must leave the journey Session usable for the following `child` step."""

    def test_claude_keeps_the_switch_permission_and_adds_the_question(self, tmp_path: Path) -> None:
        permission = journey.CLAUDE.actionable(tmp_path)
        assert permission.path_in(tmp_path) == tmp_path / journey.SWITCH_FILE

        assert journey.CLAUDE.question is not None
        question = journey.CLAUDE.question(tmp_path)
        assert question.path_in(tmp_path) == tmp_path / journey.QUESTION_FILE
        assert journey.CLAUDE_QUESTION in question.words
        assert all(option in question.words for option in journey.CLAUDE_OPTIONS)
        assert journey.CLAUDE.question_answer == journey.CLAUDE_ANSWER
        assert journey.CODEX.question is None

    def test_the_question_result_is_proved_before_continuation_flushes(
        self, tmp_path: Path
    ) -> None:
        record = tmp_path / "session.jsonl"
        result = {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "content": journey.CLAUDE_ANSWER_FRAME,
                        "is_error": True,
                    }
                ]
            },
        }
        record.write_text(json.dumps(result) + "\n", encoding="utf-8")
        walk = object.__new__(journey.Walk)
        walk._record_now = lambda: record

        assert walk._question_tool_result_proof(journey.CLAUDE_ANSWER) is not None
        assert walk._question_transcript_proof(journey.CLAUDE_ANSWER) is None

        result["message"]["content"][0]["content"] = journey.CLAUDE_ANSWER_FRAME + " extra"
        record.write_text(json.dumps(result) + "\n", encoding="utf-8")
        assert walk._question_tool_result_proof(journey.CLAUDE_ANSWER) is None

        result["message"]["content"][0]["content"] = journey.CLAUDE_ANSWER_FRAME
        assistant = {"type": "assistant", "message": {"content": []}}
        record.write_text(
            json.dumps(result) + "\n" + json.dumps(assistant) + "\n",
            encoding="utf-8",
        )

        assert walk._question_transcript_proof(journey.CLAUDE_ANSWER) is not None


class TestTheAcceptanceRunArrangesDistinctAndActionableGround:
    def test_the_documented_root_keeps_codex_permission_targets_outside_writable_ground(
        self, isolated_codex_environment: dict[str, str]
    ) -> None:
        run_directory = support.ACCEPTANCE_ROOT / "20260829T090000Z"

        refusal = journey.codex_permission_ground_refusal(
            run_directory,
            environment=isolated_codex_environment,
        )

        assert refusal is None

    def test_a_slash_tmp_root_refuses_both_codex_permission_consumers(
        self, isolated_codex_environment: dict[str, str]
    ) -> None:
        configured_root = Path("/tmp/gpt-voicecoding-acceptance")
        run_directory = configured_root / "20260829T090100Z"

        refusal = journey.codex_permission_ground_refusal(
            run_directory,
            environment=isolated_codex_environment,
        )

        assert refusal == (
            "configured acceptance root /tmp/gpt-voicecoding-acceptance puts Codex "
            "permission targets inside writable ground for pinned `--sandbox workspace-write`, "
            "so Codex can write them without approval: approval target "
            "/tmp/gpt-voicecoding-acceptance/20260829T090100Z/outside-the-sandbox/relay.txt "
            "is under /tmp; switches target /tmp/gpt-voicecoding-acceptance/"
            "20260829T090100Z/outside-the-sandbox/switches.txt is under /tmp"
        )

    def test_a_tmpdir_root_is_also_writable_without_codex_approval(
        self, isolated_codex_environment: dict[str, str]
    ) -> None:
        configured_root = Path("/private/var/folders/example/T/gpt-voicecoding-acceptance")
        run_directory = configured_root / "20260829T090200Z"

        refusal = journey.codex_permission_ground_refusal(
            run_directory,
            environment=isolated_codex_environment,
        )

        assert refusal == (
            "configured acceptance root /private/var/folders/example/T/"
            "gpt-voicecoding-acceptance puts Codex permission targets inside writable ground "
            "for pinned `--sandbox workspace-write`, so Codex can write them without approval: "
            "approval target /private/var/folders/example/T/gpt-voicecoding-acceptance/"
            "20260829T090200Z/outside-the-sandbox/relay.txt is under TMPDIR "
            "(/private/var/folders/example/T); switches target /private/var/folders/example/T/"
            "gpt-voicecoding-acceptance/20260829T090200Z/outside-the-sandbox/switches.txt "
            "is under TMPDIR (/private/var/folders/example/T)"
        )

    def test_a_realpath_alias_cannot_bypass_the_slash_tmp_rule(
        self, isolated_codex_environment: dict[str, str]
    ) -> None:
        configured_root = Path("/private/tmp/gpt-voicecoding-acceptance")
        run_directory = configured_root / "20260829T090250Z"

        refusal = journey.codex_permission_ground_refusal(
            run_directory,
            environment=isolated_codex_environment,
        )

        assert refusal == (
            "configured acceptance root /private/tmp/gpt-voicecoding-acceptance puts Codex "
            "permission targets inside writable ground for pinned `--sandbox workspace-write`, "
            "so Codex can write them without approval: approval target /private/tmp/"
            "gpt-voicecoding-acceptance/20260829T090250Z/outside-the-sandbox/relay.txt is under "
            "/tmp; switches target /private/tmp/gpt-voicecoding-acceptance/"
            "20260829T090250Z/outside-the-sandbox/switches.txt is under /tmp"
        )

    def test_a_permission_consumer_cannot_move_back_inside_the_workspace(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_codex_environment: dict[str, str],
    ) -> None:
        run_directory = support.ACCEPTANCE_ROOT / "20260829T090300Z"
        workspace = support.workspace_path(run_directory, journey.CODEX.name)
        codex_with_inside_relay = replace(
            journey.CODEX,
            relayed=lambda _: journey.writing_at(
                workspace / journey.RELAY_FILE, journey.RELAY_WORD
            ),
        )
        monkeypatch.setattr(journey, "CODEX", codex_with_inside_relay)

        refusal = journey.codex_permission_ground_refusal(
            run_directory,
            environment=isolated_codex_environment,
        )

        assert refusal == (
            f"configured acceptance root {support.ACCEPTANCE_ROOT} puts Codex permission targets "
            "inside writable ground for pinned `--sandbox workspace-write`, so Codex can write "
            f"them without approval: approval target {workspace / journey.RELAY_FILE} is under "
            f"Session workspace ({workspace})"
        )

    def test_a_permission_consumer_with_no_target_is_refused_closed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_codex_environment: dict[str, str],
    ) -> None:
        run_directory = support.ACCEPTANCE_ROOT / "20260829T090400Z"
        codex_with_unverifiable_switch = replace(
            journey.CODEX,
            actionable=lambda _: journey.Instruction(words="wait for permission"),
        )
        monkeypatch.setattr(journey, "CODEX", codex_with_unverifiable_switch)

        refusal = journey.codex_permission_ground_refusal(
            run_directory,
            environment=isolated_codex_environment,
        )

        assert refusal == (
            f"configured acceptance root {support.ACCEPTANCE_ROOT} cannot establish that every "
            "Codex permission target is outside writable ground for pinned `--sandbox "
            "workspace-write`: switches instruction has no filesystem target to validate"
        )

    def test_a_configured_writable_root_refuses_both_permission_consumers(
        self, isolated_codex_environment: dict[str, str]
    ) -> None:
        codex_home = Path(isolated_codex_environment["CODEX_HOME"])
        codex_home.joinpath("config.toml").write_text(
            f'[sandbox_workspace_write]\nwritable_roots = ["{support.ACCEPTANCE_ROOT}"]\n',
            encoding="utf-8",
        )
        run_directory = support.ACCEPTANCE_ROOT / "20260829T090500Z"
        workspace = support.workspace_path(run_directory, journey.CODEX.name)

        refusal = journey.codex_permission_ground_refusal(
            run_directory,
            environment=isolated_codex_environment,
        )

        assert refusal is not None
        assert f"Codex configured writable root ({support.ACCEPTANCE_ROOT})" in refusal
        assert f"approval target {journey.CODEX.relayed(workspace).path_in(workspace)}" in refusal
        assert (
            f"switches target {journey.CODEX.actionable(workspace).path_in(workspace)}" in refusal
        )

    def test_an_invalid_writable_root_config_is_refused_closed(
        self, isolated_codex_environment: dict[str, str]
    ) -> None:
        codex_home = Path(isolated_codex_environment["CODEX_HOME"])
        codex_home.joinpath("config.toml").write_text(
            'sandbox_workspace_write = "not a table"\n',
            encoding="utf-8",
        )
        run_directory = support.ACCEPTANCE_ROOT / "20260829T090600Z"

        refusal = journey.codex_permission_ground_refusal(
            run_directory,
            environment=isolated_codex_environment,
        )

        assert refusal is not None
        assert (
            "cannot establish that every Codex permission target is outside writable ground"
            in refusal
        )
        assert "sandbox_workspace_write" in refusal

    def test_user_writable_roots_override_the_system_config_value(
        self, isolated_codex_environment: dict[str, str]
    ) -> None:
        Path(journey.CODEX_SYSTEM_CONFIG).write_text(
            f'[sandbox_workspace_write]\nwritable_roots = ["{support.ACCEPTANCE_ROOT}"]\n',
            encoding="utf-8",
        )
        codex_home = Path(isolated_codex_environment["CODEX_HOME"])
        codex_home.joinpath("config.toml").write_text(
            '[sandbox_workspace_write]\nwritable_roots = ["/opt/codex-extra-root"]\n',
            encoding="utf-8",
        )

        refusal = journey.codex_permission_ground_refusal(
            support.ACCEPTANCE_ROOT / "20260829T090700Z",
            environment=isolated_codex_environment,
        )

        assert refusal is None

    def test_each_run_gets_a_distinct_workspace_basename(self, tmp_path: Path) -> None:
        run = tmp_path / "20260827T091500Z"
        run.mkdir()

        workspace = support.fresh_workspace(run, "codex", "/usr/bin:/bin")

        assert workspace.name == f"workspace-codex-{run.name}"

    def test_an_absolute_write_explicitly_attempts_apply_patch_without_a_bypass(
        self, tmp_path: Path
    ) -> None:
        instruction = journey.writing_at(tmp_path / "outside.txt", "DELTA")
        words = instruction.words.casefold()

        assert "`apply_patch`" in instruction.words
        assert "leave any approval request pending" in words
        assert "sandbox" not in words
        assert "danger-full-access" not in words

    def test_codex_uses_a_fresh_write_outside_its_sandbox(self, tmp_path: Path) -> None:
        instruction = journey.CODEX.actionable(tmp_path)

        assert instruction.path_in(tmp_path) == (
            tmp_path.parent / journey.OUTSIDE_THE_SANDBOX / journey.SWITCH_FILE
        )
        assert instruction.content == journey.SWITCH_WORD


class TestTheCodexPermissionPolicyReadbackRemainsExact:
    def test_the_lane_pins_the_sandbox_and_nothing_the_product_asserts(self) -> None:
        """The lane may pin the sandbox and its own cost. It may not pin a policy.

        The sandbox pin is what #105 asks this lane to name, and the model pin is
        cost — neither is a field `policy_at` grades. The approval family *is*:
        `turn/start` asserts `approvalPolicy` and `approvalsReviewer` on every
        relayed turn, and a lane that pinned either at the keyboard would
        pre-arrange the assertion #77's approval route has to make for itself. So
        this names the pins that are allowed and refuses the rest by exhaustion,
        rather than freezing a tuple that now also carries values no assertion
        depends on.
        """
        arguments = journey.CODEX.arguments

        assert arguments[:2] == ("--sandbox", "workspace-write")
        assert set(arguments[2:]) == {"-m", support.CODEX_LANE_MODEL}
        # Every approval-touching spelling `codex --help` carries on 0.153.0.
        assert not set(arguments) & {
            "-a",
            "--ask-for-approval",
            "--approve-for-me",
            "--dangerously-bypass-approvals-and-sandbox",
        }

    def test_the_lane_carries_no_config_override(self) -> None:
        """#232: a `-c` here costs the lane every step, not one setting.

        A `-c` override makes codex-tui run its own core instead of joining the
        shared daemon, and the Codex roster composes a row from a daemon-held user
        thread plus a live terminal in its workspace — so a lane launched with one
        has no row for any step to read. Measured 2026-09-05 on codex-cli 0.153.0
        against a shared app-server 0.149.1: `--sandbox workspace-write` joins,
        `… -m gpt-5.6-luna` joins, and either of those plus
        `-c model_reasoning_effort="high"` does not.

        Named as its own test rather than folded into the tuple assertion above,
        because it is a different claim: that one is "these are the pins this lane
        is allowed", this one is "this *spelling* is not available to this lane at
        any value". The reason and the table live beside the pin block in
        `support.py`; the run-time guard is `settle_daemon_membership`.
        """
        assert not [
            argument for argument in journey.CODEX.arguments if support.is_config_override(argument)
        ]
        assert not hasattr(support, "CODEX_LANE_REASONING_EFFORT")

    @pytest.mark.parametrize(
        "flag",
        [
            "-c",
            "-cmodel_reasoning_effort=high",
            "-c=model_reasoning_effort=high",
            "-config",
            "--config",
            "--config=sandbox_mode=danger",
        ],
    )
    def test_every_spelling_of_a_config_override_is_one(self, flag: str) -> None:
        """A short flag that takes a value swallows the rest of its own argument.

        All of these were accepted by codex-cli 0.153.0 on this machine, `-config`
        included — `clap` reads that one as `-c` with the value `onfig`. Every one
        of them fills `cli_kv_overrides`, and `can_reuse_implicit_local_daemon`
        requires it to be empty (`tui/src/lib.rs:919-921`), so every one of them
        keeps this lane's TUI out of the daemon.
        """
        assert support.is_config_override(flag)

    @pytest.mark.parametrize("flag", ["--sandbox", "workspace-write", "-m", "--cd", "gpt-5.6-luna"])
    def test_nothing_else_is_mistaken_for_one(self, flag: str) -> None:
        """A pin refused by a prefix nobody checked is the same bug pointed the other way."""
        assert not support.is_config_override(flag)

    def test_the_codex_lane_is_the_one_that_answers_for_its_daemon(self) -> None:
        """Membership is load-bearing on this lane and does not arise on the other.

        A Claude Session is discovered from its own registration and its
        transcript, with no daemon in the path; a Codex Session *is* a daemon
        thread a terminal vouches for (ADR 0020). `None` on the Claude lane is
        that difference written down, not a check nobody got round to.
        """
        assert journey.CODEX.daemon_membership is not None
        assert journey.CLAUDE.daemon_membership is None

    def test_the_exact_product_policy_is_sound(self, tmp_path: Path) -> None:
        rollout = self._rollout(
            tmp_path,
            {
                "sandbox_policy": {"type": "workspace-write"},
                "approval_policy": "on-request",
                "approvals_reviewer": "user",
            },
        )

        policy = journey.CODEX.policy_at(rollout)

        assert (policy.named, policy.unsound) == (
            "sandbox 'workspace-write', approval_policy 'on-request', approvals_reviewer 'user' "
            "(codex's own `turn_context`, codex.jsonl)",
            "",
        )

    @pytest.mark.parametrize(
        ("field", "value", "reported"),
        (
            ("sandbox_policy", {"type": "danger-full-access"}, "sandbox is 'danger-full-access'"),
            ("approval_policy", "never", "approval_policy is 'never'"),
            ("approvals_reviewer", "auto_review", "approvals_reviewer is 'auto_review'"),
        ),
    )
    def test_any_nonexact_product_policy_is_unsound(
        self,
        tmp_path: Path,
        field: str,
        value: object,
        reported: str,
    ) -> None:
        payload = {
            "sandbox_policy": {"type": "workspace-write"},
            "approval_policy": "on-request",
            "approvals_reviewer": "user",
            field: value,
        }
        rollout = self._rollout(tmp_path, payload)

        policy = journey.CODEX.policy_at(rollout)

        assert reported in policy.unsound

    @staticmethod
    def _rollout(tmp_path: Path, payload: dict[str, object]) -> Path:
        rollout = tmp_path / "codex.jsonl"
        rollout.write_text(json.dumps({"type": "turn_context", "payload": payload}) + "\n")
        return rollout


class TestTheThreadTheDaemonWouldHaveToHold:
    """What the Session calls itself, read for `settle_daemon_membership` (#232).

    Read from the rollout at the moment it is asked rather than from the cached
    `GroundTruth`, because that is resolved once — before the boot turn, when
    `codex` has written no rollout and the id is `""`.
    """

    #: The id shape `codex` writes into `session_meta`, from a real rollout.
    THREAD = "01998f4c-0d5a-7c31-9f2b-6a0c1e77aa10"

    def test_the_id_comes_off_the_records_first_line(self, tmp_path: Path) -> None:
        rollout = self._meta(tmp_path, {"session_id": self.THREAD, "cwd": str(tmp_path)})

        assert hand_started.codex_thread_id(rollout) == self.THREAD

    def test_a_session_that_has_written_nothing_yet_names_no_thread(self) -> None:
        """The `None` a `record_now` answers before the first turn — not an absence."""
        assert hand_started.codex_thread_id(None) == ""

    def test_a_record_whose_first_line_is_not_session_meta_names_no_thread(
        self, tmp_path: Path
    ) -> None:
        rollout = tmp_path / "codex.jsonl"
        rollout.write_text(json.dumps({"type": "turn_context", "payload": {}}) + "\n")

        assert hand_started.codex_thread_id(rollout) == ""

    def test_a_record_that_cannot_be_read_names_no_thread(self, tmp_path: Path) -> None:
        rollout = tmp_path / "codex.jsonl"
        rollout.write_text("{not json\n")

        assert hand_started.codex_thread_id(rollout) == ""

    @staticmethod
    def _meta(tmp_path: Path, payload: dict[str, object]) -> Path:
        rollout = tmp_path / "codex.jsonl"
        rollout.write_text(json.dumps({"type": "session_meta", "payload": payload}) + "\n")
        return rollout


class TestTheProductsOwnNoticesAreAttributable:
    """The one notice Bridge Core composes, matched by the forms the harness derives."""

    def test_a_stop_notice_names_the_session_it_is_about(self) -> None:
        mine = session()

        notice = notice_for(mine)

        assert journey._named_in(notice, journey._naming_forms(row(mine)))

    def test_a_permission_notice_names_the_session_it_is_about(self) -> None:
        """#109's product half, now carried by the Stop Notice alone (#191).

        Until #109 the permission sentence named only the tool, and it was its
        own renderer. It is the same brief as every other wait now, so the rule
        holds wherever the brief does — this case proves the permission shape of
        it, which is the one that cost the run.
        """
        mine = session()

        notice = briefing.text(
            stop_brief(
                mine,
                WaitingFor(
                    kind=WaitingKind.PERMISSION,
                    tool_name="Write",
                    detail="relay.txt",
                    approval_id="a1",
                ),
            )
        )

        assert "  permission: Write — relay.txt" in notice
        assert journey._named_in(notice, journey._naming_forms(row(mine)))

    def test_an_unnamed_session_is_still_attributable_by_its_address(self) -> None:
        anonymous = session(task=None)

        notice = notice_for(anonymous)

        assert journey._named_in(notice, journey._naming_forms(row(anonymous)))


class TestWhatTheHarnessMustNotAttributeToItself:
    def test_the_message_that_made_109_is_not_this_lanes_stop(self) -> None:
        assert not journey._named_in(THE_STRANGERS_PROMPT, journey._naming_forms(row(session())))

    def test_another_sessions_stop_notice_is_not_this_lanes_either(self) -> None:
        """The shape a quieter machine produces: a real notice, about someone else."""
        theirs = Session(
            target=A_STRANGER,
            name=SessionName("workspace-codex", "some other work"),
            workspace=Path("/tmp/elsewhere"),
            first_seen=0.0,
        )

        notice = notice_for(theirs)

        assert journey._named_in(notice, journey._naming_forms(row(theirs)))
        assert not journey._named_in(notice, journey._naming_forms(row(session())))

    def test_a_child_and_its_parent_are_told_apart(self) -> None:
        """#79's step asserts an absence about the child while the parent is announced."""
        parent = session()
        child = session(
            target=SessionTarget(agent=AgentKind.CLAUDE, session_id="9a11bd2e", pid=64399),
            task=None,
        )

        parents_notice = notice_for(parent)

        assert not journey._named_in(parents_notice, journey._naming_forms(row(child)))


class TestTwoSessionsTheChatCannotTellApart:
    """Nothing makes a Session Name unique, so the harness has to notice when one is not.

    `adapters/agent/_naming.py` composes `<project> · <task>` from a project and a
    task and checks neither against the other rows, so two Sessions on one machine
    can be called the same thing. The product already knows this: `match_name`
    refuses with `AmbiguousNameError` rather than picking one
    (`core/sessions.py:456-463`). These are the same fact met from the chat.

    **A Child Process and its parent are not such a pair**, and the test below
    says why: `core/sessions.py:225` keeps `name` for main Sessions and gives a
    child `None`, so a child's only naming form is its address.
    """

    def test_a_child_has_no_name_to_collide_with_its_parents(self) -> None:
        """#78/#79's design, taken through the product's own path rather than asserted.

        The lane offers the child a name; `session_from` refuses it because a
        Child Process is listed and never spoken to (`core/sessions.py:137-143`,
        `215-231`). So its only naming form is its address.
        """
        parent = session()
        seen_as_a_child = SessionInspection(
            target=SessionTarget(agent=AgentKind.CLAUDE, session_id="9a11bd2e", pid=64399),
            workspace=Path("/tmp/workspace"),
            # The lane offering exactly its parent's name is the collision this
            # would be, if the product let a child keep one.
            name=SessionName("workspace-claude", "port the log"),
            child=ChildClassification(kind=ChildKind.CHILD, parent=parent.target),
        )

        child = session_from(seen_as_a_child, first_seen=0.0)

        assert row(child)["name"] is None
        assert journey._naming_forms(row(child)) == (
            "claude 9a11bd2e",
            "claude:9a11bd2e:64399",
        )
        assert (
            journey._indistinguishable_from(
                journey._naming_forms(row(child)),
                [row(parent), row(child)],
                journey._address_of(row(child)),
            )
            is None
        )

    def test_a_name_two_sessions_share_is_refused_rather_than_guessed(self) -> None:
        parent = session()
        twin = session(
            target=SessionTarget(agent=AgentKind.CLAUDE, session_id="9a11bd2e", pid=64399)
        )
        rows = [row(parent), row(twin)]

        shared = journey._indistinguishable_from(
            journey._naming_forms(row(parent)), rows, journey._address_of(row(parent))
        )

        assert shared is not None and "claude:9a11bd2e" in shared

    def test_the_session_with_the_shorter_name_is_the_one_at_risk(self) -> None:
        """`· port` is inside `· port the log`, so the long Session's notice reads as short's."""
        short = session(task="port")
        long = session(target=SessionTarget(agent=AgentKind.CODEX, session_id="abc"))
        rows = [row(short), row(long)]

        shared = journey._indistinguishable_from(
            journey._naming_forms(row(short)), rows, journey._address_of(row(short))
        )

        assert shared is not None
        assert journey._named_in(notice_for(long), journey._naming_forms(row(short)))

    def test_the_session_with_the_longer_name_is_not_refused(self) -> None:
        """The direction matters, and refusing both ways would be a red for nothing.

        A notice about the short-named Session does not carry the long one's name,
        so the long one misreads nothing and has no reason to stop. The Session
        that *is* at risk in this pair asks the same question from its own side —
        the test above — and gets the refusal there.
        """
        short = session(task="port")
        long = session(target=SessionTarget(agent=AgentKind.CODEX, session_id="abc"))
        rows = [row(short), row(long)]

        shared = journey._indistinguishable_from(
            journey._naming_forms(row(long)), rows, journey._address_of(row(long))
        )

        assert shared is None
        assert not journey._named_in(notice_for(short), journey._naming_forms(row(long)))

    def test_distinct_sessions_are_not_refused(self) -> None:
        mine = session()
        theirs = Session(
            target=A_STRANGER,
            name=SessionName("workspace-codex", "some other work"),
            workspace=Path("/tmp/elsewhere"),
            first_seen=0.0,
        )
        rows = [row(mine), row(theirs)]

        assert (
            journey._indistinguishable_from(
                journey._naming_forms(row(mine)), rows, journey._address_of(row(mine))
            )
            is None
        )

    def test_a_session_is_never_indistinguishable_from_itself(self) -> None:
        mine = session()

        assert (
            journey._indistinguishable_from(
                journey._naming_forms(row(mine)), [row(mine)], journey._address_of(row(mine))
            )
            is None
        )


class TestTheChildStepGradesItsOwnLanesChild:
    """The same rule as #109's, met on the roster instead of the chat.

    Two lanes run at once and each engine bridges every Session on the machine,
    so both lanes' children are on both lanes' rosters — correctly classified,
    correctly parented, and belonging to somebody else. Run `20260902T065340Z`
    is where that first cost a step: the Claude lane's `child` graded
    `codex:01a060e9-…` and failed it for being listed under a Codex parent, while
    the Codex lane's own step passed on that very row. Before #208 gave the Codex
    lane a roster row there was no second child to collide with, which is why the
    harness had run this way for four months without noticing.
    """

    def _child_of(self, parent: Session, session_id: str, pid: int) -> dict:
        return row(
            session_from(
                SessionInspection(
                    target=SessionTarget(agent=parent.target.agent, session_id=session_id, pid=pid),
                    workspace=Path("/tmp/workspace"),
                    name=None,
                    child=ChildClassification(kind=ChildKind.CHILD, parent=parent.target),
                ),
                first_seen=0.0,
            )
        )

    def _two_lane_roster(self) -> tuple[Session, list[dict]]:
        """A roster shaped like the one that failed: the other lane's child first."""
        mine = session()
        theirs = Session(
            target=A_STRANGER,
            name=SessionName("workspace-codex", "some other work"),
            workspace=Path("/tmp/elsewhere"),
            first_seen=0.0,
        )
        return mine, [
            row(mine),
            row(theirs),
            self._child_of(theirs, "01a060e9", 10200),
            self._child_of(mine, "9a11bd2e", 64399),
        ]

    def test_the_other_lanes_child_is_not_graded_however_early_it_appears(self) -> None:
        mine, rows = self._two_lane_roster()

        found = journey._first_child_of(rows, "claude", before=set())

        assert found is not None
        assert journey._address_of(found) == "claude:9a11bd2e:64399"
        assert found["child"]["parent"] == {
            "agent": "claude",
            "session_id": mine.target.session_id,
            "pid": mine.target.pid,
        }

    def test_a_lane_whose_own_child_has_not_appeared_yet_finds_nothing(self) -> None:
        """The step then fails with "no child row appeared", which is the truth."""
        _, rows = self._two_lane_roster()
        theirs_only = [one for one in rows if one["target"]["agent"] == "codex"]

        assert journey._first_child_of(theirs_only, "claude", before=set()) is None

    def test_the_codex_lane_still_finds_its_own(self) -> None:
        _, rows = self._two_lane_roster()

        found = journey._first_child_of(rows, "codex", before=set())

        assert found is not None
        assert journey._address_of(found) == "codex:01a060e9:10200"

    def test_a_child_the_roster_already_held_is_not_this_turns(self) -> None:
        """#79's first-new-sighting rule, kept: `before` is read after the agent."""
        _, rows = self._two_lane_roster()

        assert journey._first_child_of(rows, "claude", before={"claude:9a11bd2e:64399"}) is None

    def test_a_row_that_is_not_a_child_is_never_taken_for_one(self) -> None:
        mine, rows = self._two_lane_roster()
        parents_only = [one for one in rows if not one.get("child")]

        assert journey._first_child_of(parents_only, "claude", before=set()) is None
        assert journey._address_of(row(mine)) == "claude:6f723f5c:64312"
