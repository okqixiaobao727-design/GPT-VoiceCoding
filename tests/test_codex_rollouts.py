"""Locating a Codex thread's own record, and reading the two facts P13 wants.

The `session_meta` payloads here are taken verbatim off this machine on
2026-08-26 — one written by 0.149.1 and one by 0.130.0 — because the field that
names a thread is spelled differently between them and a reader that knew only
the newer spelling would find nothing in half the rollouts on disk.
"""

from __future__ import annotations

import json
from pathlib import Path

from gpt_voicecoding.adapters.agent.codex import rollouts

THREAD = "01a03b06-f995-7b60-bc9f-e2152ee4ed32"

#: 0.149.1: carries both `session_id` and `id`, plus `thread_source`.
CURRENT_META = {
    "session_id": THREAD,
    "id": THREAD,
    "cwd": "/tmp/workspace-codex",
    "originator": "codex-tui",
    "cli_version": "0.149.1",
    "source": "vscode",
    "thread_source": "user",
}

#: 0.130.0, still on disk here: `id` only, and no `thread_source` at all.
OLDER_META = {
    "id": "019ec0ec-a58a-7e61-84df-91af2f0600bd",
    "cwd": "/tmp",
    "originator": "spike",
    "cli_version": "0.130.0",
}


def write_rollout(home: Path, meta: dict, *, archived: bool = False, extra: str = "") -> Path:
    thread = rollouts.session_id_in(meta)
    directory = home / ("archived_sessions" if archived else "sessions/2026/08/26")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-08-26T10-25-08-{thread}.jsonl"
    line = json.dumps(
        {"timestamp": "2026-08-26T10:25:09Z", "type": "session_meta", "payload": meta}
    )
    path.write_text(line + "\n" + extra, encoding="utf-8")
    return path


class TestLocatingOneThread:
    def test_a_rollout_is_found_by_the_id_in_its_name(self, tmp_path: Path) -> None:
        written = write_rollout(tmp_path, CURRENT_META)
        assert rollouts.locate(THREAD, home=tmp_path) == written

    def test_an_archived_rollout_is_found_too(self, tmp_path: Path) -> None:
        """A thread archived while nobody was looking is still that thread."""
        written = write_rollout(tmp_path, CURRENT_META, archived=True)
        assert rollouts.locate(THREAD, home=tmp_path) == written

    def test_a_thread_with_no_rollout_is_not_yet_rather_than_missing(self, tmp_path: Path) -> None:
        """Measured (#73): `codex` writes the rollout at its first *turn*."""
        assert rollouts.locate(THREAD, home=tmp_path) == rollouts.NotYet(THREAD)

    def test_a_codex_home_that_does_not_exist_is_not_yet(self, tmp_path: Path) -> None:
        assert isinstance(rollouts.locate(THREAD, home=tmp_path / "nope"), rollouts.NotYet)

    def test_two_rollouts_claiming_one_thread_refuse_rather_than_pick(
        self, tmp_path: Path
    ) -> None:
        write_rollout(tmp_path, CURRENT_META)
        write_rollout(tmp_path, CURRENT_META, archived=True)
        assert rollouts.locate(THREAD, home=tmp_path) == rollouts.Ambiguous(THREAD, 2)

    def test_a_file_whose_name_carries_no_thread_id_is_not_a_candidate(
        self, tmp_path: Path
    ) -> None:
        directory = tmp_path / "sessions"
        directory.mkdir(parents=True)
        (directory / "rollout-2026-08-26T10-25-08-not-a-uuid.jsonl").write_text("{}\n")
        assert isinstance(rollouts.locate(THREAD, home=tmp_path), rollouts.NotYet)


class TestReadingTheFirstLine:
    def test_the_thread_id_is_read_under_either_spelling(self, tmp_path: Path) -> None:
        assert rollouts.session_id_in(CURRENT_META) == THREAD
        assert rollouts.session_id_in(OLDER_META) == OLDER_META["id"]

    def test_the_workspace_comes_from_the_record_itself(self, tmp_path: Path) -> None:
        written = write_rollout(tmp_path, CURRENT_META)
        meta = rollouts.session_meta(written)
        assert meta is not None
        assert rollouts.workspace_in(meta) == Path("/tmp/workspace-codex")

    def test_thread_source_is_p13s_child_evidence(self, tmp_path: Path) -> None:
        written = write_rollout(tmp_path, CURRENT_META)
        assert rollouts.thread_source(written) == "user"
        assert rollouts.started_by_the_user(written) is True

    def test_a_thread_something_else_started_says_so(self, tmp_path: Path) -> None:
        written = write_rollout(tmp_path, CURRENT_META | {"thread_source": "subagent"})
        assert rollouts.started_by_the_user(written) is False

    def test_a_record_that_cannot_say_answers_none_rather_than_guessing(
        self, tmp_path: Path
    ) -> None:
        """0.130.0 wrote no `thread_source`. Absent is not `user`."""
        written = write_rollout(tmp_path, OLDER_META)
        assert rollouts.thread_source(written) is None
        assert rollouts.started_by_the_user(written) is None

    def test_a_first_line_still_being_written_reads_as_nothing(self, tmp_path: Path) -> None:
        """The "not flushed yet" window, which closes by itself."""
        directory = tmp_path / "sessions"
        directory.mkdir(parents=True)
        half = directory / f"rollout-2026-08-26T10-25-08-{THREAD}.jsonl"
        half.write_text('{"type":"session_meta","payl', encoding="utf-8")
        assert rollouts.session_meta(half) is None

    def test_only_the_first_line_is_considered(self, tmp_path: Path) -> None:
        """A reader that scanned would answer differently depending on when it looked."""
        directory = tmp_path / "sessions"
        directory.mkdir(parents=True)
        path = directory / f"rollout-2026-08-26T10-25-08-{THREAD}.jsonl"
        path.write_text(
            json.dumps({"type": "turn_context", "payload": {}})
            + "\n"
            + json.dumps({"type": "session_meta", "payload": CURRENT_META})
            + "\n",
            encoding="utf-8",
        )
        assert rollouts.session_meta(path) is None

    def test_a_file_that_is_not_there_reads_as_nothing(self, tmp_path: Path) -> None:
        assert rollouts.session_meta(tmp_path / "gone.jsonl") is None


class TestTyingAWorkspaceBackToAThread:
    def test_the_newest_rollout_for_a_workspace_is_found_by_its_own_cwd(
        self, tmp_path: Path
    ) -> None:
        write_rollout(tmp_path, CURRENT_META)
        assert rollouts.newest_for(Path("/tmp/workspace-codex"), home=tmp_path) is not None

    def test_another_workspaces_rollout_is_not_this_ones(self, tmp_path: Path) -> None:
        write_rollout(tmp_path, CURRENT_META)
        assert rollouts.newest_for(Path("/tmp/somewhere-else"), home=tmp_path) is None

    def test_a_rollout_older_than_the_process_is_not_its_rollout(self, tmp_path: Path) -> None:
        written = write_rollout(tmp_path, CURRENT_META)
        after = written.stat().st_mtime + 60
        assert rollouts.newest_for(Path("/tmp/workspace-codex"), home=tmp_path, since=after) is None
