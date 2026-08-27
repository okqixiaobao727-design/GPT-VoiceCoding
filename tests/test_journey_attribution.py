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

from pathlib import Path

import journey
import support

from gpt_voicecoding.control_plane.payloads import session_document
from gpt_voicecoding.core.approvals import announcement_for
from gpt_voicecoding.core.bridge import stop_notice_for
from gpt_voicecoding.core.sessions import Session, session_from
from gpt_voicecoding.seams.agent import (
    ApprovalRequest,
    ChildClassification,
    ChildKind,
    SessionInspection,
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


def session(target: SessionTarget = MINE, *, task: str | None = "port the log") -> Session:
    return Session(
        target=target,
        name=SessionName("workspace-claude", task) if task is not None else None,
        workspace=Path("/tmp/workspace"),
        first_seen=0.0,
    )


def row(one: Session) -> dict:
    """The roster row a surface reads, built the way the control plane builds it."""
    return session_document(one)


class TestWhatTheHarnessThinksNamesASession:
    def test_a_named_session_is_named_by_its_session_name(self) -> None:
        assert "workspace-claude · port the log" in journey._naming_forms(row(session()))

    def test_a_session_with_no_name_yet_falls_back_to_its_address(self) -> None:
        """Measured: a Codex Session has no name until its first turn."""
        assert journey._naming_forms(row(session(task=None))) == ("claude 6f723f5c",)

    def test_a_session_with_no_id_yet_falls_back_to_its_pid(self) -> None:
        """`spoken_target`'s own second fallback, mirrored — codex before its rollout."""
        bare = {"target": {"agent": "codex", "session_id": None, "pid": 95827}, "name": None}

        assert journey._naming_forms(bare) == ("codex pid 95827",)

    def test_a_row_that_names_nothing_yields_nothing_to_attribute_with(self) -> None:
        """Not an empty pass: `_await_message_naming` refuses on this rather than waiting."""
        assert journey._naming_forms({}) == ()
        assert journey._naming_forms({"target": "not a mapping", "name": ""}) == ()


class TestTheSwitchesWaitIsResolvable:
    """#80 must leave the journey Session usable for the following `child` step."""

    def test_claude_uses_the_real_question_and_ticket_answer(self, tmp_path: Path) -> None:
        instruction = journey.CLAUDE.actionable(tmp_path)

        assert instruction.path_in(tmp_path) is None
        assert "Tabs or spaces?" in instruction.words
        assert "spaces" in instruction.words and "tabs" in instruction.words
        assert journey.CLAUDE.actionable_kind == "question"
        assert journey.CLAUDE.actionable_answer == ("answer", "tabs")
        assert journey.CLAUDE.actionable_continuation == "You answered tabs."


class TestTheAcceptanceRunArrangesDistinctAndActionableGround:
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


class TestTheProductsOwnNoticesAreAttributable:
    """Both notices Bridge Core composes, matched by the forms the harness derives."""

    def test_a_stop_notice_names_the_session_it_is_about(self) -> None:
        mine = session()

        notice = stop_notice_for(mine, mine.target)

        assert journey._named_in(notice, journey._naming_forms(row(mine)))

    def test_an_approval_announcement_names_the_session_it_is_about(self) -> None:
        """#109's product half: until it, this sentence named only the tool."""
        mine = session()

        announcement = announcement_for(
            ApprovalRequest("a1", mine.target, "Write", WaitingKind.PERMISSION, detail="relay.txt"),
            spoken_as="workspace-claude · port the log",
        )

        assert journey._named_in(announcement, journey._naming_forms(row(mine)))

    def test_an_unnamed_session_is_still_attributable_by_its_address(self) -> None:
        anonymous = session(task=None)

        notice = stop_notice_for(anonymous, anonymous.target)

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

        notice = stop_notice_for(theirs, theirs.target)

        assert journey._named_in(notice, journey._naming_forms(row(theirs)))
        assert not journey._named_in(notice, journey._naming_forms(row(session())))

    def test_a_child_and_its_parent_are_told_apart(self) -> None:
        """#79's step asserts an absence about the child while the parent is announced."""
        parent = session()
        child = session(
            target=SessionTarget(agent=AgentKind.CLAUDE, session_id="9a11bd2e", pid=64399),
            task=None,
        )

        parents_notice = stop_notice_for(parent, parent.target)

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
        assert journey._naming_forms(row(child)) == ("claude 9a11bd2e",)
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
        assert journey._named_in(
            stop_notice_for(long, long.target), journey._naming_forms(row(short))
        )

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
        assert not journey._named_in(
            stop_notice_for(short, short.target), journey._naming_forms(row(long))
        )

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
