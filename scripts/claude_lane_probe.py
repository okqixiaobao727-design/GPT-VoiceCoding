"""Claude-lane discovery probe for #71 — read-only, and deliberately so.

Ticket #71 asks whether the product can find every Session the user starts on the
Claude lane, and reach it. This script answers the *finding* half and refuses to
touch the reaching half: it never opens a socket, so it can never deliver a
message into a real Session by accident. #70's probe crossed exactly that line
and the incident is in its research note; this one cannot.

What it does:

1. reads the official roster, `claude agents --json`
   (https://code.claude.com/docs/en/agent-view);
2. reads `~/.claude/sessions/<pid>.json`, Claude Code's own per-Session registry,
   which carries sessionId, cwd, version, name, status and `messagingSocketPath`;
3. sweeps the inbox socket directory for sockets neither surface accounts for;
4. checks each pid for liveness, its binary version, and whether it is a bare
   `claude` or a wrapped one;
5. prints one row per live Session with the verdict the domain calls for:
   `rostered` or `discovered, unattached` (CONTEXT.md, ticket #68).

**What this run established: discovery is scoped by `CLAUDE_CONFIG_DIR`.** The
roster and the registry first appeared to disagree — four live Sessions with bound
sockets were missing from `claude agents --json`, including one freshly started,
bare and on the same version. They were not missing. This machine runs two config
directories, `~/.claude` and `~/.claude-b`, and each holds its own `sessions/`
registry; the roster had listed exactly the Sessions of whichever directory the
probe inherited. Run once per directory, roster and registry agree completely.

That makes the coverage rule explicit, and it is a real one for a bridge that
claims every Session the user starts: the two directories are disjoint universes.
Neither roster sees the other's Sessions, and their inbox keys live in separate
`sessions/` directories, so a sender in one cannot even authenticate to a receiver
in the other. The product must be told which config directories to cover and sweep
each, or it will silently miss half the machine. This probe takes `--config-dir`
for exactly that reason, and defaults to every directory it can find.

    python3 scripts/claude_lane_probe.py
    python3 scripts/claude_lane_probe.py --json

Version pin: written against Claude Code 2.1.245 on macOS, 2026-08-25.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


#: Config directories to cover. `CLAUDE_CODE_CONFIG_DIR`/`CLAUDE_CONFIG_DIR`
#: selects one; a machine may run several, and a Session is only ever visible
#: within its own.
def config_dirs(explicit: list[str] | None) -> list[Path]:
    if explicit:
        return [Path(d).expanduser() for d in explicit]
    found = set()
    if "CLAUDE_CONFIG_DIR" in os.environ:
        found.add(Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser())
    found |= {d for d in Path.home().glob(".claude*") if (d / "sessions").is_dir()}
    return sorted(found) or [Path.home() / ".claude"]


def socket_dir() -> Path:
    """Where inbox sockets live — a default, not a constant.

    2.1.245 derives this from `CLAUDE_CODE_TMPDIR` or `$XDG_RUNTIME_DIR` and also
    accepts `--messaging-socket-path`, so a Session's socket is only reliably
    known by reading the `messagingSocketPath` it registered. This function is
    for the sweep alone: finding sockets that no registry entry accounts for.
    """
    base = os.environ.get("CLAUDE_CODE_TMPDIR") or os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(base) / "cc-socks"


#: Argv fragments that mean this Session was started through something other
#: than a bare `claude`. The gen-1 GPT-VoiceCoding shell function is the one on
#: this machine (`~/.zshrc:174`); #71 must be proven against Sessions without it.
WRAPPER_MARKERS = ("--channels", "--plugin-dir", "--dangerously-skip-permissions")


@dataclass
class Row:
    pid: int
    rostered: bool
    socket: str | None = None
    alive: bool = False
    session_id: str | None = None
    name: str | None = None
    cwd: str | None = None
    status: str | None = None
    version: str | None = None
    argv: str | None = None
    wrapped: bool = False
    registry_heartbeat_age_s: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if not self.alive:
            return "stale socket, no process"
        return "rostered" if self.rostered else "discovered, unattached"


def official_roster(config_dir: Path) -> list[dict]:
    """The documented discovery surface, asked once per config directory."""
    out = subprocess.run(
        ["claude", "agents", "--json"],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "CLAUDE_CONFIG_DIR": str(config_dir)},
    )
    if out.returncode != 0:
        raise SystemExit(f"claude agents --json failed: {out.stderr.strip()}")
    return json.loads(out.stdout)


def process_facts(pid: int) -> tuple[bool, str | None]:
    """Liveness and full argv, so a wrapped Session can be told from a bare one."""
    out = subprocess.run(["ps", "-p", str(pid), "-o", "args="], capture_output=True, text=True)
    argv = out.stdout.strip()
    return bool(argv), argv or None


def binary_version(pid: int) -> str | None:
    """Which installed version this process is actually running.

    An auto-update leaves already-running Sessions on the old binary, which is
    the first thing to suspect when the roster disagrees with the socket sweep.
    """
    out = subprocess.run(["lsof", "-p", str(pid)], capture_output=True, text=True)
    for line in out.stdout.splitlines():
        marker = "/.local/share/claude/versions/"
        if marker in line:
            tail = line.split(marker, 1)[1]
            return tail.split("/", 1)[0].strip()
    return None


def registry_entry(config_dir: Path, pid: int) -> dict | None:
    path = config_dir / "sessions" / f"{pid}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def collect(config_dir: Path) -> list[Row]:
    rows: dict[int, Row] = {}

    for entry in sorted((config_dir / "sessions").glob("*.json")):
        try:
            registered = json.loads(entry.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        pid = int(registered["pid"])
        rows[pid] = Row(
            pid=pid,
            rostered=False,
            session_id=registered.get("sessionId"),
            name=registered.get("name"),
            cwd=registered.get("cwd"),
            status=registered.get("status"),
            socket=registered.get("messagingSocketPath"),
        )

    for agent in official_roster(config_dir):
        pid = int(agent["pid"])
        row = rows.setdefault(
            pid,
            Row(
                pid=pid,
                rostered=True,
                session_id=agent.get("sessionId"),
                name=agent.get("name"),
                cwd=agent.get("cwd"),
                status=agent.get("status"),
            ),
        )
        row.rostered = True

    socks = socket_dir()
    for sock in sorted(socks.glob("*.sock")) if socks.is_dir() else []:
        try:
            pid = int(sock.stem)
        except ValueError:
            continue
        row = rows.setdefault(pid, Row(pid=pid, rostered=False))
        row.socket = str(sock)

    now = time.time()
    for row in rows.values():
        row.alive, row.argv = process_facts(row.pid)
        if not row.alive:
            continue
        row.wrapped = any(m in (row.argv or "") for m in WRAPPER_MARKERS)
        row.version = binary_version(row.pid)
        entry = registry_entry(config_dir, row.pid)
        if entry:
            row.session_id = row.session_id or entry.get("sessionId")
            row.name = row.name or entry.get("name")
            row.cwd = row.cwd or entry.get("cwd")
            row.status = row.status or entry.get("status")
            row.socket = row.socket or entry.get("messagingSocketPath")
            if isinstance(entry.get("updatedAt"), int):
                row.registry_heartbeat_age_s = round(now - entry["updatedAt"] / 1000, 1)
        elif row.rostered:
            row.notes.append("in the official roster with no registry entry")
        else:
            row.notes.append("socket only: this Session belongs to a different config directory")
        if row.socket is None:
            row.notes.append("no inbox socket — nothing to Relay into")
        if row.wrapped:
            row.notes.append(
                "not a bare claude: started with "
                + " ".join(m for m in WRAPPER_MARKERS if m in (row.argv or ""))
            )

    return [rows[pid] for pid in sorted(rows)]


def render(config_dir: Path, rows: list[Row]) -> None:
    print(f"=== {config_dir} ===")
    live = [r for r in rows if r.alive]
    rostered = [r for r in live if r.rostered]
    print(f"live claude processes seen from here: {len(live)}")
    print(f"of those, listed by `claude agents --json`: {len(rostered)}\n")
    for row in rows:
        print(f"pid {row.pid} — {row.verdict}")
        print(f"  name      {row.name or '-'}   status {row.status or '-'}")
        print(f"  cwd       {row.cwd or '-'}")
        print(f"  version   {row.version or '-'}   socket {row.socket or '-'}")
        if row.registry_heartbeat_age_s is not None:
            print(f"  registry  heartbeat {row.registry_heartbeat_age_s / 60:.1f} min old")
        for note in row.notes:
            print(f"  ! {note}")
        print()
    foreign = [r for r in live if not r.rostered and r.session_id is None]
    disagreeing = [r for r in live if not r.rostered and r.session_id is not None]
    if disagreeing:
        print(
            f"roster and registry disagree inside {config_dir} for "
            f"{', '.join(str(r.pid) for r in disagreeing)} — explain that before trusting either."
        )
    else:
        print(f"roster and registry agree inside {config_dir}.")
    if foreign:
        print(
            f"{len(foreign)} more live Session(s) hold a socket in the shared socket directory "
            f"({', '.join(str(r.pid) for r in foreign)}) and belong to another config directory. "
            "They are invisible here and unauthenticatable from here: covering every Session the "
            "user starts means sweeping every config directory, not just this one."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable rows")
    parser.add_argument(
        "--config-dir",
        action="append",
        help="a config directory to cover; repeatable, defaults to every one found",
    )
    args = parser.parse_args()
    covered = {str(d): collect(d) for d in config_dirs(args.config_dir)}
    if args.json:
        print(
            json.dumps(
                {
                    d: [asdict(r) | {"verdict": r.verdict} for r in rows]
                    for d, rows in covered.items()
                },
                indent=2,
            )
        )
    else:
        for directory, rows in covered.items():
            render(Path(directory), rows)


if __name__ == "__main__":
    main()
