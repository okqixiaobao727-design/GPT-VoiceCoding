"""Fast regression coverage for the realtime-probe acceptance preflight."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import support


def initialise_repository(path: Path) -> None:
    subprocess.run(
        ["git", "init", "--quiet", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )


def add_linked_worktree(repository: Path, path: Path) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Acceptance Test",
            "-c",
            "user.email=acceptance@example.invalid",
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            "Initial test tree",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "worktree", "add", "--quiet", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_primary_checkout_discovers_the_canonical_sibling_probe(tmp_path: Path) -> None:
    repository = tmp_path / "GPT-VoiceCoding"
    initialise_repository(repository)
    expected = tmp_path / "GPT-VoiceCoding-legacy" / "scripts" / "rt_prototype.py"
    expected.parent.mkdir(parents=True)
    expected.write_text("# legacy realtime probe\n")

    assert support.realtime_probe_path(repository) == expected


def test_linked_worktree_discovers_the_primary_checkouts_sibling_probe(tmp_path: Path) -> None:
    repository = tmp_path / "GPT-VoiceCoding"
    initialise_repository(repository)
    worktree = tmp_path / "worktrees" / "ticket-142"
    add_linked_worktree(repository, worktree)
    expected = tmp_path / "GPT-VoiceCoding-legacy" / "scripts" / "rt_prototype.py"
    expected.parent.mkdir(parents=True)
    expected.write_text("# legacy realtime probe\n")

    assert support.realtime_probe_path(worktree) == expected


def test_explicit_probe_override_takes_precedence_from_a_linked_worktree(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "GPT-VoiceCoding"
    initialise_repository(repository)
    worktree = tmp_path / "worktrees" / "ticket-142"
    add_linked_worktree(repository, worktree)
    default = tmp_path / "GPT-VoiceCoding-legacy" / "scripts" / "rt_prototype.py"
    default.parent.mkdir(parents=True)
    default.write_text("# default legacy realtime probe\n")
    override = tmp_path / "maintained-elsewhere" / "rt_prototype.py"
    override.parent.mkdir()
    override.write_text("# explicit legacy realtime probe\n")

    resolved = support.realtime_probe_path(
        worktree,
        environment={"GPTVOICECODING_ACCEPTANCE_REALTIME_PROBE": str(override)},
    )

    assert resolved == override


def test_missing_probe_is_an_actionable_preflight_refusal(tmp_path: Path) -> None:
    repository = tmp_path / "GPT-VoiceCoding"
    initialise_repository(repository)
    missing = tmp_path / "GPT-VoiceCoding-legacy" / "scripts" / "rt_prototype.py"

    with pytest.raises(support.RealtimeProbeUnavailable) as refusal:
        support.realtime_probe_path(repository, environment={})

    assert str(refusal.value) == (
        f"no usable realtime probe at {missing}; set "
        "GPTVOICECODING_ACCEPTANCE_REALTIME_PROBE to the legacy checkout's "
        "scripts/rt_prototype.py"
    )


def test_unreadable_explicit_probe_is_an_actionable_preflight_refusal(tmp_path: Path) -> None:
    repository = tmp_path / "GPT-VoiceCoding"
    initialise_repository(repository)
    probe = tmp_path / "maintained-elsewhere" / "rt_prototype.py"
    probe.parent.mkdir()
    probe.write_text("# explicit legacy realtime probe\n")
    probe.chmod(0o000)

    with pytest.raises(support.RealtimeProbeUnavailable):
        support.realtime_probe_path(
            repository,
            environment={"GPTVOICECODING_ACCEPTANCE_REALTIME_PROBE": str(probe)},
        )
