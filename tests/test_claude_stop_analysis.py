"""What a Claude Session stopped on, read out of transcript fragments.

The fragments are **re-expressed** from the cases the reference implementation
proved (`legacy@1d32845:test_bridge.py:16381-16600,23618-24450,29450-29650`), not
copied: legacy asserted on a JSON-RPC reply built by a daemon with a store behind
it, and this asserts on the one value the pure module returns. Each case keeps
the fact the legacy test was written to pin, and the docstring says which.

Every record here is the shape Claude Code really writes — `isSidechain`,
`userType`, `promptSource` and the nested `message.role` included — because the
visibility rules are the tail boundary, and a fragment that omits them would test
a parser that never sees a real transcript.
"""

from __future__ import annotations

from typing import Any

import pytest

from gpt_voicecoding.adapters.agent.claude.stop_analysis import (
    QUESTION_TOOL,
    SUMMARY_MAX_CHARS,
    analyse,
    summarise,
)
from gpt_voicecoding.seams.agent import WaitingKind

# --- the shapes Claude Code writes -------------------------------------------


def said(text: str, *, role: str = "assistant", **extra: Any) -> dict[str, Any]:
    """One record of the visible conversation — the thing that moves the tail."""
    return {
        "type": role,
        "isSidechain": False,
        "userType": "external",
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
        **extra,
    }


def called(tool: str, identifier: str, tool_input: Any) -> dict[str, Any]:
    """An assistant record whose content is one `tool_use`."""
    return {
        "type": "assistant",
        "isSidechain": False,
        "userType": "external",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": identifier, "name": tool, "input": tool_input}],
        },
    }


def answered(identifier: str) -> dict[str, Any]:
    """The `tool_result` that closes one call. Carries no text of its own."""
    return {
        "type": "user",
        "isSidechain": False,
        "userType": "external",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": identifier, "content": "ok"}],
        },
    }


def asked(identifier: str, *groups: tuple[str, list[str]]) -> dict[str, Any]:
    """An `AskUserQuestion` call, one entry per question group."""
    return called(
        QUESTION_TOOL,
        identifier,
        {
            "questions": [
                {
                    "question": prompt,
                    "options": [{"label": label, "description": ""} for label in labels],
                }
                for prompt, labels in groups
            ]
        },
    )


#: One finished exchange, so every fragment below starts from a real turn rather
#: than from an empty file.
def turn(said_by_user: str = "do the thing", said_back: str = "done") -> list[dict[str, Any]]:
    return [said(said_by_user, role="user"), said(said_back)]


class TestAQuestionInTheTail:
    """P3 — the decision only the user can supply."""

    def test_each_option_carries_its_description(self) -> None:
        waiting = analyse(
            [
                *turn(),
                called(
                    QUESTION_TOOL,
                    "q1",
                    {
                        "questions": [
                            {
                                "question": "Which base should the merge use?",
                                "options": [
                                    {
                                        "label": "main",
                                        "description": "Merge into the default branch",
                                    },
                                    {
                                        "label": "feature",
                                        "description": "Keep the work isolated",
                                    },
                                ],
                            }
                        ]
                    },
                ),
            ]
        )

        assert [option.description for option in waiting.options] == [
            "Merge into the default branch",
            "Keep the work isolated",
        ]

    def test_the_prompt_its_options_and_its_recommendation_are_fields(self) -> None:
        """Legacy extracted these once, so no consumer parses text back out."""
        waiting = analyse(
            [
                *turn(),
                asked(
                    "q1",
                    (
                        "Which base should the merge use?",
                        ["main (recommended)", "the feature branch"],
                    ),
                ),
            ]
        )
        assert waiting.kind is WaitingKind.QUESTION
        assert waiting.caught_up is True
        assert waiting.prompt == "Which base should the merge use?"
        assert [option.text for option in waiting.options] == ["main", "the feature branch"]
        assert [option.recommended for option in waiting.options] == [True, False]
        assert waiting.recommendation == "main"

    def test_a_question_with_no_marked_option_recommends_nothing(self) -> None:
        """A recommendation is the Session's, so an unmarked call has none."""
        waiting = analyse([*turn(), asked("q1", ("Which?", ["this", "that"]))])
        assert waiting.recommendation is None
        assert not any(option.recommended for option in waiting.options)

    def test_every_option_of_a_multi_group_call_is_named_once(self) -> None:
        """Flattened, because one flat list is what the user hears read out."""
        waiting = analyse(
            [
                *turn(),
                asked("q1", ("Which base?", ["main", "feature"]), ("Squash?", ["yes", "no"])),
            ]
        )
        assert [option.text for option in waiting.options] == ["main", "feature", "yes", "no"]
        assert waiting.prompt == "Which base?\nSquash?"

    def test_a_recommendation_in_each_group_is_not_one_recommendation(self) -> None:
        """Picking one would credit the Session with a conclusion it never reached.

        The marks survive on the options; only the single whole-call
        recommendation is withheld
        (`legacy@1d32845:bridge/transcript.py:1736-1741`).
        """
        waiting = analyse(
            [
                *turn(),
                asked(
                    "q1",
                    ("Which base?", ["main (recommended)", "feature"]),
                    ("Squash?", ["yes", "no (recommended)"]),
                ),
            ]
        )
        assert waiting.recommendation is None
        assert [option.recommended for option in waiting.options] == [True, False, False, True]

    def test_a_question_the_user_already_answered_is_not_this_stop(self) -> None:
        """The criterion is an *unanswered* question, not any question.

        Announcing an answered one would send the user back to a decision they
        have already made.
        """
        waiting = analyse(
            [*turn(), asked("q1", ("Which base?", ["main", "feature"])), answered("q1")]
        )
        assert waiting.kind is WaitingKind.NONE

    def test_an_older_unanswered_question_is_not_this_stop(self) -> None:
        """A question answered at the keyboard writes no result and stays open.

        The tail is the boundary: the Session spoke afterwards, so whatever it is
        waiting on now, it is not that.
        """
        waiting = analyse(
            [*turn(), asked("q1", ("Which base?", ["main", "feature"])), *turn("and now this")]
        )
        assert waiting.kind is WaitingKind.NONE

    def test_a_question_still_being_written_is_not_readable_yet(self) -> None:
        """`__unparsedToolInput` is exactly the record the caller is waiting for.

        Counting it would declare the Session caught up on a question nobody can
        read, so the call is not entered at all and the tail says nothing.
        """
        waiting = analyse(
            [
                *turn(),
                called(QUESTION_TOOL, "q1", {"__unparsedToolInput": '{"questions":[{"quest'}),
            ]
        )
        assert waiting.kind is WaitingKind.NONE

    def test_a_question_beats_a_permission_call_beside_it(self) -> None:
        """When a turn ends on both, the decision is the thing only the user has."""
        waiting = analyse(
            [
                *turn(),
                called("Bash", "b1", {"description": "push the branch"}),
                asked("q1", ("Which base?", ["main", "feature"])),
            ]
        )
        assert waiting.kind is WaitingKind.QUESTION

    def test_a_question_written_before_the_permission_still_beats_it(self) -> None:
        """Precedence is by kind, not by which call came last in the tail."""
        waiting = analyse(
            [
                *turn(),
                asked("q1", ("Which base?", ["main", "feature"])),
                called("Bash", "b1", {"description": "push the branch"}),
            ]
        )
        assert waiting.kind is WaitingKind.QUESTION

    def test_the_newest_of_two_open_questions_is_the_one_asked(self) -> None:
        """An older question the Session moved past is not what it is asking now."""
        waiting = analyse(
            [
                *turn(),
                asked("q1", ("Which base?", ["main", "feature"])),
                asked("q2", ("Squash?", ["yes", "no"])),
            ]
        )
        assert waiting.prompt == "Squash?"


class TestAToolAwaitingPermission:
    """P4 and P5 — a Session waiting to be *allowed* to act."""

    def test_a_tail_ending_on_an_ordinary_call_is_waiting_for_permission(self) -> None:
        waiting = analyse([*turn(), called("Bash", "b1", {"description": "push the branch"})])
        assert waiting.kind is WaitingKind.PERMISSION
        assert waiting.tool_name == "Bash"
        assert waiting.detail == "push the branch"
        assert waiting.approval_id is None

    def test_a_call_that_describes_nothing_names_the_tool_alone(self) -> None:
        """A call this scan cannot describe is still a call it is waiting on."""
        waiting = analyse([*turn(), called("WebFetch", "w1", {"prompt": "summarise"})])
        assert waiting.kind is WaitingKind.PERMISSION
        assert waiting.tool_name == "WebFetch"
        assert waiting.detail is None

    def test_the_newest_outstanding_call_is_the_one_it_is_held_up_on(self) -> None:
        """An older call it wrote first is already waiting behind this one."""
        waiting = analyse(
            [
                *turn(),
                called("Read", "r1", {"file_path": "/tmp/first"}),
                called("Edit", "e1", {"file_path": "/tmp/second"}),
            ]
        )
        assert waiting.tool_name == "Edit"
        assert waiting.detail == "/tmp/second"

    def test_a_closed_call_leaves_nothing_outstanding(self) -> None:
        waiting = analyse([*turn(), called("Read", "r1", {"file_path": "/tmp/x"}), answered("r1")])
        assert waiting.kind is WaitingKind.NONE

    def test_a_finished_turn_is_waiting_on_nothing(self) -> None:
        """`NONE` is the transcript's whole answer; the roster decides the rest."""
        waiting = analyse(turn())
        assert waiting.kind is WaitingKind.NONE
        assert waiting.caught_up is True
        assert waiting.tool_name is None

    def test_an_empty_transcript_is_waiting_on_nothing(self) -> None:
        """A Session whose first turn has not written a record yet (#73)."""
        assert analyse([]).kind is WaitingKind.NONE


class TestTheSummaryTheUserHears:
    """P5 — the bounded, human-facing projection, and what it refuses to carry."""

    @pytest.mark.parametrize(
        ("tool_input", "expected"),
        [
            ({"description": "run the tests"}, "run the tests"),
            ({"file_path": "/tmp/notes.md"}, "/tmp/notes.md"),
            ({"path": "/tmp/dir"}, "/tmp/dir"),
            ({"notebook_path": "/tmp/book.ipynb"}, "/tmp/book.ipynb"),
            # Preference order: the tool's own sentence about itself first.
            ({"description": "run the tests", "file_path": "/tmp/x"}, "run the tests"),
            ({"file_path": "/tmp/x", "path": "/tmp/y"}, "/tmp/x"),
            # Nothing readable, so nothing said.
            ({}, ""),
            ({"prompt": "summarise this"}, ""),
            (None, ""),
            ("not a mapping", ""),
            ({"description": "   "}, ""),
            ({"description": 17}, ""),
        ],
    )
    def test_the_fields_it_reads_and_the_order_it_prefers_them(
        self, tool_input: Any, expected: str
    ) -> None:
        assert summarise(tool_input) == expected

    @pytest.mark.parametrize("secret_field", ["command", "content", "old_string", "new_string"])
    def test_it_never_carries_the_arguments_proper(self, secret_field: str) -> None:
        """The rule this extractor exists for.

        A command, a file's contents and an edit string are the code and shell
        text the reference implementation always excluded
        (`legacy@1d32845:bridge/transcript.py:1779-1790`). v1.0 reads this field
        into a Live Call and pushes it to a phone, so the exclusion holds harder
        here than it did there.
        """
        leak = "curl -H 'Authorization: Bearer sk-live-secret' https://example.test"
        assert summarise({secret_field: leak}) == ""
        assert leak not in summarise({secret_field: leak, "description": "call the API"})

    def test_something_over_the_bound_is_passed_over_whole(self) -> None:
        """Not truncated: half a sentence says less than the tool's name does.

        A cut lands mid-secret as readily as mid-word, which is the second reason
        the bound rejects rather than shortens.
        """
        assert summarise({"description": "x" * (SUMMARY_MAX_CHARS + 1)}) == ""
        assert summarise({"description": "x" * SUMMARY_MAX_CHARS}) == "x" * SUMMARY_MAX_CHARS

    def test_a_long_description_falls_through_to_the_next_field(self) -> None:
        """Over-long is unreadable, not fatal — the path is still worth saying."""
        assert (
            summarise({"description": "x" * 500, "file_path": "/tmp/notes.md"}) == "/tmp/notes.md"
        )

    def test_whitespace_is_stripped_but_the_text_is_not_reflowed(self) -> None:
        assert summarise({"description": "  run the tests  "}) == "run the tests"

    def test_the_permission_detail_comes_through_the_same_extractor(self) -> None:
        """One rule, one implementation — the parser does not have its own."""
        waiting = analyse(
            [*turn(), called("Bash", "b1", {"command": "rm -rf /", "description": "clean up"})]
        )
        assert waiting.detail == "clean up"


class TestWhereTheTailBegins:
    """The visibility rules, which are here to give "the tail" a boundary."""

    def test_a_child_s_tool_call_is_not_this_session_s_stop(self) -> None:
        """A sidechain record is an Agent-created child's work (#68)."""
        child = called("Bash", "b1", {"description": "the child's own work"})
        child["isSidechain"] = True
        assert analyse([*turn(), child]).kind is WaitingKind.NONE

    def test_a_child_s_result_cannot_close_this_session_s_call(self) -> None:
        """The exclusion runs before the pairing, so it cannot leak either way."""
        result = answered("b1")
        result["isSidechain"] = True
        waiting = analyse([*turn(), called("Bash", "b1", {"description": "push"}), result])
        assert waiting.kind is WaitingKind.PERMISSION

    def test_slash_command_plumbing_does_not_move_the_tail(self) -> None:
        """Three records the pipeline writes as `user`, none of them a turn.

        Without this, the caveat or the stdout counts as the user having spoken
        and the boundary moves past the call the Session is held up on
        (`legacy@1d32845:bridge/transcript.py:1515-1540`).
        """
        for noise in (
            "<local-command-caveat>\nCaveat: the messages below…",
            "<local-command-stdout>Set model to Opus</local-command-stdout>",
        ):
            record = said(noise, role="user")
            record["message"]["content"] = noise
            waiting = analyse([*turn(), called("Bash", "b1", {"description": "push"}), record])
            assert waiting.kind is WaitingKind.PERMISSION, noise

    def test_an_expanded_skill_body_does_not_move_the_tail(self) -> None:
        skill = said("Base directory for this skill: /tmp/skills/x", role="user")
        skill["isMeta"] = True
        waiting = analyse([*turn(), called("Bash", "b1", {"description": "push"}), skill])
        assert waiting.kind is WaitingKind.PERMISSION

    def test_what_the_user_typed_into_a_slash_command_is_still_a_turn(self) -> None:
        """The command record proper carries `<command-args>` — their real intent."""
        typed = said(
            "<command-name>/review</command-name><command-args>#75</command-args>", role="user"
        )
        waiting = analyse([*turn(), called("Bash", "b1", {"description": "push"}), typed])
        assert waiting.kind is WaitingKind.NONE

    def test_a_marker_in_a_real_message_is_just_text(self) -> None:
        """The guard is the record shape: a real turn carries a `promptSource`.

        Without it, an assistant explaining this very format — or a user pasting
        a command's output — vanishes from its own conversation.
        """
        pasted = said("<local-command-stdout>look at this</local-command-stdout>", role="user")
        pasted["promptSource"] = "typed"
        pasted["message"]["content"] = "<local-command-stdout>look at this</local-command-stdout>"
        waiting = analyse([*turn(), called("Bash", "b1", {"description": "push"}), pasted])
        assert waiting.kind is WaitingKind.NONE

    def test_a_product_injected_record_is_not_the_user_speaking(self) -> None:
        injected = said("<system-reminder>…</system-reminder>", role="user")
        injected["promptSource"] = "system"
        waiting = analyse([*turn(), called("Bash", "b1", {"description": "push"}), injected])
        assert waiting.kind is WaitingKind.PERMISSION

    def test_a_record_that_is_not_the_user_s_own_turn_is_not_a_turn(self) -> None:
        internal = said("bookkeeping", role="user")
        internal["userType"] = "internal"
        waiting = analyse([*turn(), called("Bash", "b1", {"description": "push"}), internal])
        assert waiting.kind is WaitingKind.PERMISSION

    def test_the_question_call_itself_counts_as_something_the_user_was_shown(self) -> None:
        """A readable `AskUserQuestion` is part of the visible conversation.

        So an *older* question does not keep a newer permission call out of the
        tail — the boundary moved to the question, and the permission came after.
        """
        waiting = analyse(
            [
                *turn(),
                asked("q1", ("Which base?", ["main", "feature"])),
                answered("q1"),
                called("Bash", "b1", {"description": "push"}),
            ]
        )
        assert waiting.kind is WaitingKind.PERMISSION


class TestRecordsThisBuildHasNeverSeen:
    """Unknown is not hostile: a shape this build cannot read is skipped.

    Treating format drift as an error is what made the reference reader fail
    closed on ~99% of real transcripts (`legacy@1d32845:bridge/transcript.py:
    1213-1240`). Skipping can only ever omit; it can never invent.
    """

    @pytest.mark.parametrize(
        "record",
        [
            "not a mapping at all",
            {"type": "summary", "summary": "a compacted conversation"},
            {"type": "assistant", "isSidechain": False, "message": "not a mapping"},
            # `message.role` disagreeing with `type` is a record about somebody
            # else's turn, so its content is not followed.
            {
                "type": "assistant",
                "isSidechain": False,
                "userType": "external",
                "message": {"role": "user", "content": []},
            },
            {"type": "assistant", "isSidechain": False, "userType": "external", "message": {}},
        ],
    )
    def test_an_unreadable_record_costs_nothing_but_itself(self, record: Any) -> None:
        base = [*turn(), called("Bash", "b1", {"description": "push"})]
        assert analyse([*base, record]).kind is WaitingKind.PERMISSION

    @pytest.mark.parametrize(
        "item",
        [
            "not a mapping",
            {"type": "tool_use", "name": "Bash", "input": {}},  # no id to pair on
            {"type": "tool_use", "id": "", "name": "Bash", "input": {}},
            {"type": "tool_use", "id": 17, "name": "Bash", "input": {}},
            {"type": "thinking", "thinking": "…"},
        ],
    )
    def test_an_unreadable_content_item_opens_no_call(self, item: Any) -> None:
        record = called("Bash", "b1", {})
        record["message"]["content"] = [item]
        assert analyse([*turn(), record]).kind is WaitingKind.NONE

    def test_a_call_whose_name_is_not_a_string_still_says_it_is_waiting(self) -> None:
        """Saying so with no name beats saying nothing about a real dialog."""
        waiting = analyse([*turn(), called(17, "b1", {"description": "something"})])  # type: ignore[arg-type]
        assert waiting.kind is WaitingKind.PERMISSION
        assert waiting.tool_name is None
        assert waiting.detail == "something"

    def test_a_question_call_with_an_unreadable_group_keeps_the_readable_one(self) -> None:
        record = called(
            QUESTION_TOOL,
            "q1",
            {
                "questions": [
                    "not a mapping",
                    {"question": "Which?", "options": ["not a mapping", {"label": "  "}]},
                    {"question": "Really?", "options": [{"label": "yes"}]},
                ]
            },
        )
        waiting = analyse([*turn(), record])
        assert waiting.kind is WaitingKind.QUESTION
        assert [option.text for option in waiting.options] == ["yes"]
        assert waiting.prompt == "Which?\nReally?"

    def test_a_question_call_with_no_readable_option_is_not_a_question(self) -> None:
        """It falls through to the permission reading rather than vanishing.

        A call with a prompt and no options is still a call the Session is held
        up on; what it is not is a menu the user can be read.
        """
        record = called(QUESTION_TOOL, "q1", {"questions": [{"question": "Which?"}]})
        assert analyse([*turn(), record]).kind is WaitingKind.NONE
