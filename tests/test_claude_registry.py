"""What the Claude Session registry is allowed to tell this engine.

The registry is a file Claude Code writes and this engine only reads. Every test
here is about failing closed on it: a record that is absent, malformed, stale, or
speaking a protocol this adapter was not proven against must produce a refusal
naming what is wrong, never a plausible-looking record.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from gpt_voicecoding.adapters.agent.claude.registry import (
    PEER_PROTOCOL,
    RegistryError,
    SessionRecord,
    pid_is_live,
    read_record,
    records,
)

LIVE_PID = os.getpid()


def entry(pid: int = LIVE_PID, **overrides: object) -> dict[str, object]:
    """One `~/.claude/sessions/<pid>.json` record, in the shape 2.1.238 really writes.

    Transcribed from a live record rather than invented, so a test that passes
    here is a test about the file Claude Code actually produces.
    """
    document: dict[str, object] = {
        "pid": pid,
        "sessionId": "430b0def-38ef-4783-8d57-d800710d83bd",
        "cwd": "/Users/someone/work",
        "startedAt": 1787275795615,
        "version": "2.1.238",
        "peerProtocol": PEER_PROTOCOL,
        "peerFeatures": ["notify_idle"],
        "kind": "interactive",
        "entrypoint": "cli",
        "messagingSocketPath": f"/tmp/cc-socks/{pid}.sock",
        "name": "a-session",
        "status": "idle",
    }
    document.update(overrides)
    return document


def write(directory: Path, document: dict[str, object], *, named: int | None = None) -> Path:
    """Write one record. `named` is the filename's pid, which a record may contradict."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{named if named is not None else document['pid']}.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_a_record_is_read_into_exactly_what_the_adapter_addresses(tmp_path: Path) -> None:
    write(tmp_path, entry())

    found = read_record(tmp_path, LIVE_PID)

    assert found == SessionRecord(
        pid=LIVE_PID,
        session_id="430b0def-38ef-4783-8d57-d800710d83bd",
        cwd=Path("/Users/someone/work"),
        version="2.1.238",
        peer_protocol=PEER_PROTOCOL,
        socket_path=Path(f"/tmp/cc-socks/{LIVE_PID}.sock"),
        status="idle",
        name="a-session",
    )


def test_an_absent_record_is_refused_by_the_path_it_looked_at(tmp_path: Path) -> None:
    with pytest.raises(RegistryError) as refused:
        read_record(tmp_path, 4242)

    assert "4242" in str(refused.value)


def test_an_unreadable_record_is_refused_rather_than_half_read(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / f"{LIVE_PID}.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(RegistryError):
        read_record(tmp_path, LIVE_PID)


@pytest.mark.parametrize("missing", ["sessionId", "messagingSocketPath", "peerProtocol", "pid"])
def test_a_record_missing_anything_load_bearing_is_refused(tmp_path: Path, missing: str) -> None:
    document = entry()
    del document[missing]
    write(tmp_path, document, named=LIVE_PID)

    with pytest.raises(RegistryError) as refused:
        read_record(tmp_path, LIVE_PID)

    assert missing in str(refused.value)


def test_a_record_whose_pid_disagrees_with_its_filename_is_refused(tmp_path: Path) -> None:
    write(tmp_path, entry(pid=LIVE_PID + 1), named=LIVE_PID)

    with pytest.raises(RegistryError) as refused:
        read_record(tmp_path, LIVE_PID)

    assert "pid" in str(refused.value)


def test_another_peer_protocol_is_refused_by_number(tmp_path: Path) -> None:
    """The protocol field governs, not the version string. This is the real pin."""
    write(tmp_path, entry(peerProtocol=2))

    with pytest.raises(RegistryError) as refused:
        read_record(tmp_path, LIVE_PID)

    assert "2" in str(refused.value)
    assert str(PEER_PROTOCOL) in str(refused.value)


def test_a_record_is_read_even_when_its_process_is_gone(tmp_path: Path) -> None:
    """Liveness is a separate question, asked separately: a stale record still parses."""
    write(tmp_path, entry(pid=4242))

    found = read_record(tmp_path, 4242)

    assert found.pid == 4242
    assert not pid_is_live(found.pid)


def test_this_process_is_live(tmp_path: Path) -> None:
    assert pid_is_live(LIVE_PID)


def test_a_pid_that_is_not_a_pid_is_never_live() -> None:
    assert not pid_is_live(0)
    assert not pid_is_live(-1)


def test_every_readable_record_is_listed_and_unreadable_ones_are_skipped(tmp_path: Path) -> None:
    """A registry with one broken file is still a registry — the rest must be usable."""
    write(tmp_path, entry(pid=4242))
    write(tmp_path, entry(pid=4243))
    (tmp_path / "4244.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "4245.key").write_text("not a record at all", encoding="utf-8")

    listed = records(tmp_path)

    assert sorted(record.pid for record in listed) == [4242, 4243]


def test_a_registry_directory_that_does_not_exist_lists_nothing(tmp_path: Path) -> None:
    assert records(tmp_path / "absent") == ()


def test_a_fork_is_two_records_under_one_session_id(tmp_path: Path) -> None:
    """`--resume` forks under the same session id, which is why the pid addresses."""
    write(tmp_path, entry(pid=4242))
    write(tmp_path, entry(pid=4243))

    listed = records(tmp_path)

    assert len({record.session_id for record in listed}) == 1
    assert read_record(tmp_path, 4243).socket_path == Path("/tmp/cc-socks/4243.sock")
