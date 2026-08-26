"""The nine steps of the bridge journey, written once and walked by both lanes.

## What changed, and why

The journey this module used to hold walked the **launch** journey: `bridgectl
launch` started a Session, the steps watched what the product had started, and
`bridgectl close` ended it. Map #67 redrew the destination — v1.0 is a *bridge*
over the Sessions the user starts — so a harness that starts its own Sessions
through the product is measuring the wrong thing, and #72 has since parked the
launcher out of the protocol entirely (`launch` and `close` are not actions on
`main`). The harness now starts each Session **the way the user does**: the
ordinary `claude` / `codex` binary, in a pty, no wrapper (`hand_started.py`).

Steps `0c`, `1a`, `1b` and `8` are gone with the launcher. `0b` (the realtime
contract probe) and the provenance compare stay where they were.

## The nine names are a contract

Every one of the build tickets #74–#80 cites a step name from `STEPS` verbatim in
its "Red first" line. Renaming one here silently moves seven tickets' exit
criteria, so the names are fixed and their spelling is the interface.

## What the steps rest on, and what they never rest on

Observations come from the agent's own roster or rollout, the filesystem, the
engine's reply and log, and the real Telegram chat. **Never from the screen.**
Measured on 2026-08-26: both TUIs redraw with cursor addressing, and `codex` in
a pty interleaves to roughly one glyph per line once escapes are stripped. The
raw stream is kept as an artifact for a human; nothing parses it.

## What a step may attribute to itself

The engine this run spawns bridges **every** Session on the machine, so the chat
is a shared surface: at any moment it may carry a notice about somebody's open
work that has nothing to do with the lane being walked. The rule that follows
from that is one line, and it is stated here so no step has to relearn it —

> **A step only ever attributes what names its own target.**

Every chat read in this module goes through `Walk._await_message_naming`, which
is where the rule is enforced; `_naming_forms` is what "names" means. That
includes the two reads that assert *absence*, where an unattributed message is a
false red rather than a false green, and `drain_boot_notice`, which is neither —
a stranger's notice accepted there ends the drain early and lets the real boot
notice land where `stop notice` is looking. Learned on run `20260826T213402Z`, where
`stop notice` passed on a permission prompt belonging to a stale `/tmp/vcprobe`
thread, and on a quieter machine would have failed for a reason equally
unrelated to the lane (#109). The sibling lesson had already been learned once,
one method over, for `pending_approvals` (`_answer_pending_approval`).

## The turns, and why there are five

A step that needs a turn drives its own, because a turn shared between steps
makes one step's failure look like another's. The one exception is stated where
it happens: `relay` and `approval` observe the *same* turn from two ends — the
words arriving and the permission that turn raises — because a relayed
instruction that needs a permission is exactly the shape the product has to
survive, and running it twice would prove less at twice the cost.

The Codex lane runs a sixth that no step drives: it is *launched* with a prompt,
because that is what carries it past Codex's update gate (#110), and a prompt on
the command line is a turn. No step observes it and `Walk.settle_boot_turn` waits
it out before the walk begins — a turn still running when the first step types is
a turn whose Stop lands where a later step is looking for a different one.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import hand_started
import support
from support import LaneBlocked, StepFailed

if TYPE_CHECKING:
    # Named for a type and never imported at runtime: telethon lives behind this
    # module (`telegram_person.py`, "Telethon lives here and nowhere else"), and
    # the fast suite imports this file to test its attribution rule without it.
    import telegram_person

#: The nine steps, in the order #73 fixed. Cited verbatim by #74–#80.
STEPS = (
    "roster",
    "stable name",
    "progress",
    "stop notice",
    "relay",
    "approval",
    "companion inbound",
    "switches",
    "child",
)

#: How many times `stable name` reads the roster. #73: "identical across three
#: `status` calls and across a Stop".
NAME_READS = 3

#: How long the engine gets to notice a Session that is already running before an
#: empty roster is taken as the answer. Not derived from the product, because on
#: `main` there is no discovery to derive it from — #74 builds it. Chosen with the
#: reason stated: long enough that any polling discovery has ticked at least once
#: and a slow `codex` boot (MCP servers; measured at tens of seconds on this
#: machine) is not read as a missing Session, short enough that a roster which is
#: simply empty is an answer rather than a wait. Re-derive it from #74's own
#: cadence once there is one.
DISCOVERY_SECONDS = 30.0

#: How long a boot turn gets, as a multiple of the far side's turn figure.
#: Derived rather than guessed: `settle_boot_turn` waits out a TUI's **boot and**
#: its first turn, and boot alone has been measured at the whole turn figure on
#: this machine — a `codex` sat in `starting MCP servers` for a full 180s
#: ground-truth wait on 2026-08-26 (`hand_started.codex_ground_truth`). So two of
#: them, one for each half, and a lane still unsettled after that is blocked
#: rather than typed into. Blocked, not merely slow: everything after it would be
#: measuring a Session with a turn still running underneath.
BOOT_TURN_ALLOWANCE = 2.0

#: The engine's own line for a Stop it announced (`core/bridge.py:_session_stopped`).
#: `stop notice` matches a looser pattern for its own purposes; `drain_boot_notice`
#: wants the announcement itself, because "was a notice raised for the boot turn"
#: is exactly the question it is asking.
ENGINE_STOP_LINE = r"(?i)Session stopped:"

#: What the escalated permission announcement says (`core/approvals.py:52-76`).
#: Matched rather than quoted whole: the tool name and the detail are the agent's,
#: and this run does not get to predict them. **Which Session it is about is not
#: this pattern's business either** — the sentence opens with the Session's name
#: since #109, and the attribution rule is what reads it.
APPROVAL_ANNOUNCEMENT = re.compile(r"waiting for your permission to use", re.IGNORECASE)


# --- what the Sessions are asked to do --------------------------------------


@dataclass(frozen=True)
class Instruction:
    """One small, deterministic action, and the effect to read back.

    The shape `docs/acceptance-design.md` prescribes: an effect the harness reads
    off the filesystem, so "the Session acted on the words" is a fact from the far
    side rather than a claim from the engine.
    """

    words: str
    #: Where the effect lands: relative to the workspace, or absolute. Both are
    #: real cases — the Codex lane asks for a file *outside* the workspace,
    #: because that is what its sandbox will not let it write (`writing_at`) —
    #: and `path_in` resolves the one against the other.
    target: Path | None = None
    content: str | None = None

    def path_in(self, workspace: Path) -> Path | None:
        if self.target is None:
            return None
        return self.target if self.target.is_absolute() else workspace / self.target

    def effect_in(self, workspace: Path) -> str | None:
        target = self.path_in(workspace)
        if target is None:
            return None
        text = support.read_if_exists(target)
        return text.strip() if text is not None else None

    def performed_in(self, workspace: Path) -> bool:
        return self.content is not None and self.effect_in(workspace) == self.content


def writing(filename: str, content: str) -> Instruction:
    """The wording, in one place, so both lanes ask for the same shape of thing."""
    return Instruction(
        words=(
            f"Create a file named {filename} in the current directory whose entire "
            f"contents are the single word {content}. Do nothing else, and do not "
            f"ask any questions."
        ),
        target=Path(filename),
        content=content,
    )


def writing_at(path: Path, content: str) -> Instruction:
    """The same action, named by absolute path, so the sandbox is what decides.

    Identical to `writing` in everything the steps read back — one file, one word,
    read off the filesystem. The only difference is *where*, and on the Codex lane
    that is the whole point: the path is outside the Session's writable roots, so
    the action cannot be taken without asking.
    """
    return Instruction(
        words=(
            f"Create a file at the absolute path {path} whose entire contents are the "
            f"single word {content}. Do nothing else, and do not ask any questions."
        ),
        target=path,
        content=content,
    )


#: Turn 1 — `stable name`'s Stop. **No tool use**, on purpose: a turn that raises
#: a permission would sit in `waiting` until something answered it, and nothing is
#: supposed to answer one until `approval`. So the first turn is words only, it
#: ends on its own, and the Stop it ends with is what `stop notice` observes.
ACKNOWLEDGE = Instruction(
    words="Reply with the single word READY. Do not use any tools, and do not ask anything."
)

#: Turn 2 — arrives by Relay and raises a permission on the way. The file and the
#: word are one shape for both lanes; **where** it is written is the lane's, and
#: that is what `Lane.relayed` holds. One instruction for both lanes is what left
#: the codex `approval` step silent (#105): at its own sandbox a Codex writes
#: inside its workspace without asking anybody, so there was nothing to
#: round-trip. See `CLAUDE` and `CODEX` for each lane's measurement.
RELAY_FILE = "relay.txt"
RELAY_WORD = "BRAVO"

#: Where the Codex lane's relayed instruction writes: beside the workspace, under
#: the same run directory, and outside the Session's writable roots. Kept inside
#: the run directory so the design's rule still holds — nothing outside it is
#: written by the agents — and kept out of the workspace because being outside is
#: the entire reason Codex has to ask before writing there.
OUTSIDE_THE_SANDBOX = "outside-the-sandbox"

#: Turn 3 — arrives from Telegram.
INBOUND = writing("inbound.txt", "CHARLIE")

#: Turn 4 — `switches`. It has to end **waiting on the user**, because #80's rule
#: is about Sessions that are still actionable when Duty comes back on.
ASK_SOMETHING = Instruction(
    words=(
        "Ask me one short question about what to do next, then stop and wait for my "
        "answer. Do not use any tools."
    )
)

#: Turn 5 — `child`. The main Session is asked to do the one thing that produces a
#: second agent process under it. What each lane calls that differs, so the words
#: live on the lane.
CHILD_FILE = "child.txt"


# --- lanes ------------------------------------------------------------------


@dataclass(frozen=True)
class Lane:
    """Everything about a lane that is not the journey itself.

    The two things that genuinely differ between lanes are *where the agent's own
    record of a Session lives* and *how to find out it exists at all*, and both
    are held here as functions. They used to be two `if self.agent == "claude"`
    branches in this class, which is the shape that grows a third branch in a
    third method the first time a lane needs one — and the lanes are the one axis
    this harness is certain to keep adding to. A lane is now a value that carries
    its own answers, and `Walk` never asks which lane it is walking.
    """

    name: str
    agent: str
    binary: str
    #: Arguments the *person* would not normally type, and why each is here.
    arguments: tuple[str, ...]
    #: What the lane's TUI is **launched with**, or None when it is launched
    #: silent. Not one of `arguments`, because it is not a flag: it starts a turn
    #: nobody drove, before the walk has asked for anything. `hand_started.
    #: launch_arguments` puts its words last on the command line and refuses an
    #: empty one; `Walk.settle_boot_turn` waits the turn out, through the reading
    #: the value carries, before a word is typed.
    boot: hand_started.BootPrompt | None
    #: The words that make this lane's agent spawn a Child Process. "subagent"
    #: and "sub-agent" appear here on purpose: this string is spoken *to* the
    #: agent, where it is the agent's own mechanism word and the thing that makes
    #: the instruction work. Everywhere the harness speaks about the concept, it
    #: is a Child Process (`CONTEXT.md`).
    child_words: str
    #: The instruction `relay` carries and `approval` grades, given the lane's
    #: workspace. It is the lane's because *what a permission is* is the lane's:
    #: the two agents' policies refuse different actions, and an instruction that
    #: asks one of them for permission asks the other for nothing (#105).
    relayed: Callable[[Path], Instruction]
    #: The ground the permission was measured on, given the agent's own record of
    #: the Session. `approval` says its `named` half in the evidence line, so a
    #: green step states the ground it stood on rather than implying some
    #: default — and fails on the `unsound` half, because a step that cannot
    #: stand on its own ground has not proved what it claims.
    policy_at: Callable[[Path | None], hand_started.Policy]
    #: What that policy is measured to ask about. Said by the step that finds no
    #: permission at all, so a silent lane reports the measurement it contradicts
    #: instead of the other lane's.
    asks_about: str
    #: What the agent itself says about a Session the harness started, or None
    #: when it says nothing yet. Takes the pid, the workspace, the environment to
    #: read a roster with, and the moment the harness started looking.
    ground_truth: Callable[[int, Path, dict[str, str], float], hand_started.GroundTruth | None]
    #: Where that agent's own record is **at this moment**, or None when there is
    #: not one yet. Re-located on every call, never cached: measured 2026-08-26,
    #: **neither agent has a record until it has taken a turn** — a Claude Session
    #: that has not been typed into has no transcript file, and `codex` writes its
    #: rollout when the first turn starts, not when the Session does (a full run
    #: watched it sit in `starting MCP servers` with an empty workspace for 180s).
    #: Caching the `None` that resolves at Session start would make every later
    #: turn look like a turn that never grew the record, which is exactly how
    #: `_drive_turn` decides a turn is over.
    record_now: Callable[[hand_started.GroundTruth, float], Path | None]


#: `--permission-mode default` is not the person's own flag, and it is the one
#: place this harness overrides what the machine would do. It has to: measured
#: 2026-08-26, `~/.claude/settings.json` on this machine sets
#: `permissions.defaultMode = "auto"` at user scope, so a bare hand-started
#: `claude` auto-approves the Write and **no permission is ever raised**. The
#: `approval` step would then have nothing to observe, and its silence would look
#: like a pass. #60 ruled that neither lane may set a permission mode, on the
#: grounds that overriding it would *pre-approve* the thing the step exists to
#: observe; here the user's own setting is what pre-approves it, and the flag is
#: what restores the observation. The rule is kept, its direction reversed, and
#: the reason is recorded on the verdict rather than left in a diff.
CLAUDE = Lane(
    name="claude",
    agent="claude",
    binary="claude",
    arguments=("--permission-mode", "default"),
    # Launched silent. No boot gate of the Codex kind has been measured here —
    # `claude` boots into an empty composer — and a Session nobody has typed into
    # is what `roster` and `stable name`'s three reads want to find.
    boot=None,
    relayed=lambda workspace: writing(RELAY_FILE, RELAY_WORD),
    # The flag the harness passes *is* the whole policy on this lane, and Claude
    # publishes no per-turn readback of it, so there is nothing to read back and
    # nothing that can disagree. Sound by construction, and said out loud here so
    # the asymmetry with the Codex lane is a measurement rather than an oversight.
    policy_at=lambda record: hand_started.Policy("`--permission-mode default`"),
    asks_about=(
        "a Write of a new file asks `Do you want to create <name>?` and the roster's "
        "`status` goes to `waiting` (measured 2026-08-26 on claude 2.1.246)"
    ),
    ground_truth=lambda pid, workspace, environment, since: hand_started.claude_ground_truth(
        pid, environment
    ),
    record_now=lambda truth, since: hand_started.claude_transcript(truth.session_id),
    child_words=(
        "Use the Task tool to start one subagent that writes a file named "
        f"{CHILD_FILE} containing the single word DELTA in the current directory. "
        "Do nothing else yourself."
    ),
)

#: `--sandbox workspace-write` pins the **sandbox**, and nothing else. It is the
#: Codex config surface #105 asks this lane to name, and it is chosen because it
#: is the one thing here the product never asserts: `turn/start` pins
#: `approvalPolicy` and `approvalsReviewer` on every relayed turn
#: (`agent/codex/threads.py:36-40`), so pinning those at the keyboard too would
#: pre-arrange the very assertion #77's approval route has to make for itself.
#: What the sandbox *allows* is nobody's assertion, and until this flag it came
#: from `~/.codex/config.toml` — a file the user owns, where one
#: `sandbox_mode = "danger-full-access"` would silence this step exactly as
#: `permissions.defaultMode = "auto"` silenced the Claude one. The value is what
#: a trusted workspace already gives (measured 2026-08-26 on the failing run's
#: own rollout, `turn_context.sandbox_policy = workspace-write`), so the flag
#: fixes the ground rather than moving it.
#:
#: The **boot prompt** is not a flag and is not here to ask for anything: it is
#: what gets this lane past the update gate (#110; the measurement is in
#: `hand_started`'s module docstring). Three things follow, and each is stated
#: because a reader will otherwise meet it as a surprise:
#:
#: * **It is an extra turn, not a replacement.** `stable name` still types
#:   `ACKNOWLEDGE` itself, because it requires a name held across a Stop it
#:   drives, and `stop notice` marks the chat immediately before that turn — a
#:   Stop crossed at launch would predate the mark and the notice would be
#:   unfindable. The words are `ACKNOWLEDGE`'s so the boot turn asks the Session
#:   nothing the run does not already ask, and it uses no tools, so it cannot
#:   raise a permission before `approval` is there to answer it, and it leaves
#:   `codex_turn_policy` reading the same ground: the sandbox is this lane's pin
#:   and the `turn_context` that step grades is `relay`'s, the last one written.
#: * **The walk waits it out first** (`Walk.settle_boot_turn`). Two turns of the
#:   same words are not two turns the harness can tell apart: a boot turn still
#:   running when `stop notice`'s mark is taken puts *its* Stop Notice after the
#:   mark, and the step would pass on the notice for a turn nobody drove.
#: * **The rollout now exists before `roster` runs.** Codex writes its rollout
#:   when the first *turn* starts, so this lane's `ground_truth` carries a real
#:   `session_id` at the first read instead of the `""` it used to carry — the
#:   evidence line `roster` prints changes shape, and the pid join it rests on
#:   does not.
CODEX = Lane(
    name="codex",
    agent="codex",
    binary="codex",
    arguments=("--sandbox", "workspace-write"),
    boot=hand_started.BootPrompt(words=ACKNOWLEDGE.words, turn_over=hand_started.codex_turn_over),
    # Measured 2026-08-27 through the shared daemon with the product's own pin
    # and no sandbox override, on codex-cli 0.149.1 and again on 0.150.0 over a
    # 0.149.1 app-server: a write to a path outside the workspace raises
    # `item/fileChange/requestApproval`, the thread goes to `waitingOnApproval`,
    # and **the file does not appear until the approval is answered**. The same
    # instruction aimed *inside* the workspace raised nothing, both times, which
    # is what `approval.none` was reporting on run `20260826T122216Z`. The
    # directory is made by the harness, so the one refused action is the write.
    relayed=lambda workspace: writing_at(
        workspace.parent / OUTSIDE_THE_SANDBOX / RELAY_FILE, RELAY_WORD
    ),
    policy_at=lambda record: hand_started.codex_turn_policy(record),
    asks_about=(
        "a write to a path outside the Session's writable roots raises "
        "`item/fileChange/requestApproval` and the thread goes to `waitingOnApproval` "
        "(measured 2026-08-27 on codex-cli 0.149.1 and 0.150.0); a write *inside* the "
        "workspace raises nothing, which is the silence #105 was opened for"
    ),
    ground_truth=lambda pid, workspace, environment, since: hand_started.codex_ground_truth(
        pid, workspace, since
    ),
    record_now=lambda truth, since: hand_started.codex_rollout(truth.workspace, since),
    child_words=(
        "Start one sub-agent to write a file named "
        f"{CHILD_FILE} containing the single word DELTA in the current directory. "
        "Do nothing else yourself."
    ),
)


#: Both lanes, in the order they are walked. Named here so the run can declare up
#: front what it promised to observe — see `Verdict.expected_lanes`.
LANES = (CLAUDE, CODEX)


# --- the walk ---------------------------------------------------------------


@dataclass
class Turn:
    """One turn, timed. `docs/acceptance-design.md` § Still to measure wanted this."""

    what: str
    seconds: float
    ended: bool


class Walk:
    """One lane's journey. Every method is one step; each returns its evidence."""

    def __init__(
        self,
        *,
        lane: Lane,
        session: hand_started.HandStartedSession,
        engine: support.Engine,
        config: support.DerivedConfig,
        bridgectl: support.Bridgectl,
        person,  # telegram_person.TelegramPerson
        journal: support.Journal,
        verdict: support.Verdict,
        far_side: support.FarSideDeadlines,
        environment: dict[str, str],
        started_at: float,
    ) -> None:
        self.lane = lane
        self.session = session
        self.engine = engine
        self.config = config
        self.bridgectl = bridgectl
        self.person = person
        self.journal = journal
        self.far_side = far_side
        self.environment = environment
        self.started_at = started_at
        self.journey = support.Journey(
            lane=lane.name, verdict=verdict, journal=journal, steps=STEPS
        )
        self.truth: hand_started.GroundTruth | None = None
        self.address: str | None = None
        #: Held for `stop notice`: the chat's high-water mark from *before* the
        #: first turn started, so the notice it looks for cannot predate the Stop.
        self.before_first_turn: int | None = None
        #: Held for `approval`: what `relay`'s turn raised and how it was answered.
        self.approval_evidence: str | None = None
        #: Held for `drain_boot_notice`, which writes the one `boot turn`
        #: observation once both halves of the arrangement are done.
        self.boot_turn: Turn | None = None
        self.turns: list[Turn] = []

    # --- the walk ---------------------------------------------------------

    def walk(self) -> None:
        self.journey.observe(
            "workspace trust",
            "arranged by the harness, not observed: both agents stop a run in a directory they "
            "have never seen with a full-screen trust dialog and the Session never registers "
            "(re-measured on claude 2.1.246, 2026-08-26). `journal.jsonl` carries the grant and "
            "the revoke. It is not a step: the run cannot both arrange this and judge it.",
        )
        try:
            boot_mark = self.settle_boot_turn()
            self.arm_switches()
            self.drain_boot_notice(boot_mark)
        except LaneBlocked as unarmed:
            self.journey.skip_rest(str(unarmed))
            return
        self.journey.run("roster", self.roster)
        self.journey.run("stable name", self.stable_name)
        self.journey.run("progress", self.progress)
        self.journey.run("stop notice", self.stop_notice)
        self.journey.run("relay", self.relay)
        self.journey.run("approval", self.approval)
        self.journey.run("companion inbound", self.companion_inbound)
        self.journey.run("switches", self.switches)
        self.journey.run("child", self.child)
        self.journey.observe(
            "turns measured",
            "; ".join(f"{turn.what} {turn.seconds:.1f}s ended={turn.ended}" for turn in self.turns)
            or "no turn ran",
        )

    def settle_boot_turn(self) -> int | None:
        """Wait out the turn the *launch* started, and mark the chat behind it.

        Not a step, for `arm_switches`' reason: it is how this lane is put in the
        state the walk assumes, not a claim about the product. But it is not
        optional either, and what it prevents is a **false green** rather than a
        red.

        A lane with a `boot` prompt is running a turn from the moment it starts
        (#110 — a non-empty prompt is what carries it past the update gate).
        Nothing may be typed into a Session that is mid-turn, and no chat mark may
        be taken while one is in flight: `stable name` drives the walk's first
        turn and hands `stop notice` the mark from just before it, so a boot turn
        that ends *after* that mark puts its own Stop Notice on the far side of it
        — and `stop notice` passes on a notice for a turn nobody drove, having
        proved nothing. The two turns carry the same words, so no reader of the
        chat could tell them apart afterwards either.

        This waits on the agent's **own** turn boundary rather than on the record
        going quiet. `_drive_turn` settles for nine seconds of silence, which for
        a graded turn costs a slow reading and here would cost the run its
        meaning, because a turn waiting on the model appends nothing either.
        Codex says which it is (`hand_started.codex_turn_over`), so this asks
        Codex.

        The mark it returns is the second half, and `drain_boot_notice` spends
        it. Nothing is typed and nothing is asked — this only watches.
        """
        boot = self.lane.boot
        if boot is None:
            return None
        started = time.monotonic()
        # Resolves the record this waits on. The ordinary first call: every step
        # reads ground truth through here, and it is cached after the first.
        truth = self._ground_truth()
        allowed = self.far_side.agent_turn_seconds * BOOT_TURN_ALLOWANCE
        over = support.wait_for(
            lambda: boot.turn_over(self._record_now()),
            deadline_seconds=allowed,
            poll_seconds=2.0,
        )
        self.boot_turn = self._measured("boot prompt", started, bool(over))
        if not self.boot_turn.ended:
            raise LaneBlocked(
                f"the turn this lane was launched with had not ended after "
                f"{self.boot_turn.seconds:.0f}s, so the walk cannot type into this Session "
                f"without racing it. The agent reports {truth.describe()}; its record is "
                f"{self._record_size()} bytes. Screen tail: {self.session.screen_tail()[-600:]!r}"
            )
        return self.person.latest_message_id()

    def drain_boot_notice(self, mark: int | None) -> None:
        """Let the boot turn's Stop Notice land before the walk marks the chat for a later one.

        **Turning an outlet on is what delivers what was held.** A notice with no
        route open is retained rather than dropped (`core/escalation.py:230-232`),
        and `flip_switch(on=True)` sweeps the retained ones
        (`core/bridge.py:361-372`) — so `arm_switches`, which runs between
        `settle_boot_turn` and this, is where a held boot notice goes out. That
        much is already before `stable name`'s mark and needs nothing from here.

        What needs this is the other path. The rollout's `task_complete` and the
        engine's own observation of the Stop are not synchronised, so an engine
        that observes it after the arming escalates it straight out, and a green
        `stop notice` would then rest on that message losing a race — which is
        the one thing this harness may not do. So the walk waits here, on a mark
        taken behind the boot turn, until either the notice arrives or the window
        `stop notice` itself trusts has passed.

        **It records and never asserts**, because *no notice* is a legitimate
        answer: a Stop is only raised on a transition out of `active`, and the
        first `idle` a thread reports is it sitting there having done nothing
        (`adapters/agent/codex/adapter.py:965-985`). An engine that first saw this
        thread already idle raised nothing for the boot turn, and there is nothing
        to drain — which is also the case that pays the full window.

        **It reads the chat, so it obeys the attribution rule** (#109), and here
        that is load-bearing rather than tidy. A stranger's notice taken as the
        boot turn's would end this wait early and leave the *real* boot notice
        still in flight, to land after `stable name`'s mark — re-creating exactly
        the false green this drain exists to prevent, and writing a sentence about
        someone else's Session into the verdict on the way. This is the one chat
        read that runs before `roster`, so the naming forms may come from the
        agent's own record rather than the roster row (`_own_row`).
        """
        if mark is None or self.boot_turn is None:
            return
        try:
            arrived = self._await_own_message(
                mark, deadline_seconds=self.far_side.telegram_round_trip_seconds
            )
        except StepFailed as unattributable:
            # Arrangement, not judgement: this method has no step to fail. A lane
            # whose messages cannot be told from another Session's is a lane none
            # of the nine steps could read either, so it is blocked here rather
            # than walked into nine reds with one cause.
            raise LaneBlocked(
                f"the boot turn's notice could not be attributed, and neither could anything "
                f"a later step reads: {unattributable}"
            ) from unattributable
        announced = support.matching_lines(self.engine.log_lines(), ENGINE_STOP_LINE)
        boot = self.lane.boot
        assert boot is not None  # there is no mark to spend without one
        self.journey.observe(
            "boot turn",
            f"the Session was launched with {boot.words!r} — the words "
            f"`stable name` types anyway — because a non-empty prompt is what skips Codex's "
            f"update gate (#110). Waited out on Codex's own `task_started`/`task_complete` "
            f"bracketing, with every outlet still off: {self.boot_turn.seconds:.1f}s. Its Stop "
            f"Notice was then drained behind chat mark {mark} — only a message naming this "
            f"Session counting as it (#109) — so that nothing after "
            f"`stable name`'s later mark can be it: "
            + (
                f"chat message {arrived.id} ({arrived.text[:80]!r})"
                if arrived is not None
                else f"nothing arrived in "
                f"{self.far_side.telegram_round_trip_seconds:.0f}s, which is what an engine "
                f"that first saw this thread already idle would do"
            )
            + f"; engine.log Stop lines: {announced[-2:] or 'none'}. Arranged by the harness, "
            f"not judged by it.",
        )

    def arm_switches(self) -> None:
        """Voice off, Message on, Duty on — the text-only mode this whole run exercises.

        Not a step, because it is the run's mode rather than a claim about the
        product. Measured at build time on #60 and unchanged: a fresh engine
        answers `switches: duty off, message off, voice off`, so an unarmed run
        would see no push anywhere and read one cause as four failures.
        """
        for name, position in (("voice", "off"), ("message", "on"), ("duty", "on")):
            answer = self.bridgectl("switch", name, position)
            self.journal(
                "switch.armed", lane=self.lane.name, switch=name, to=position, reply=answer.text
            )
            if not answer.ok:
                raise LaneBlocked(f"`switch {name} {position}` refused: {answer.text}")

    # --- roster -----------------------------------------------------------

    def roster(self) -> str:
        """Every main Session the user starts is in the roster, and is a target like any other.

        **This step was rewritten after the fact, and the reason belongs here.**
        #73's own wording asked for "provenance and separate Relay/Approval reach
        grades, and an unattached row refused as a target" — the vocabulary
        #74's *body* still locks. #68 removed it, and Simon said so on #74 in as
        many words: *the product has no Reach / Attached / Unattached /
        Provenance vocabulary — every listed Session is one the bridge talks to,
        and a route that fails surfaces as a delivery failure with a reason
        through the existing delivery grades. Simplify the locked `Reach`,
        `ReachGrade` and `Provenance` types … before starting.* #82 says the same
        of the Codex fallback: it "adds no Reach/Provenance state and returns
        existing `FAILED` before the wire". A step asserting the old shape would
        have been a red line #74 could only clear by building types Simon had
        already deleted.

        So there is **no second class of row**. What this step claims:

        1. the Session the harness started by hand — identified from the agent's
           own record, never from the engine — has a row (blocking: nothing
           after it is observable without one);
        2. that row is a *target*: its `target` writes out as an address
           `bridgectl` accepts, and its workspace is the one the harness made,
           which is the join that makes it this Session rather than a coincidence.

        Unreachability is not this step's business and has no row of its own —
        the Codex lane with no shared daemon is exactly such a Session, and the
        proof it is still listed is that *this same step runs on that lane*.
        Where its unreachability does surface is `relay`, as a graded failure
        carrying a reason (`seams/delivery.py:40-49` — a non-delivered receipt
        cannot be built without one).
        """
        truth = self._ground_truth()
        rows = self._roster_rows()
        mine = self._row_for(rows, truth)
        if mine is None:
            # Give a polling discovery its tick before calling the roster empty.
            deadline = time.monotonic() + DISCOVERY_SECONDS
            while mine is None and time.monotonic() < deadline:
                time.sleep(5.0)
                rows = self._roster_rows()
                mine = self._row_for(rows, truth)
        if mine is None:
            raise LaneBlocked(
                f"the hand-started Session is not in the engine's roster. The agent itself "
                f"reports {truth.describe()}; the engine reports "
                f"{[support.flatten([row.get('target')]) for row in rows] or 'no sessions'}"
            )
        self.address = _address_of(mine)
        if "<no target>" in self.address or self.address.endswith(":None"):
            raise StepFailed(
                f"the roster row carries no address a surface could name it by: "
                f"target is {mine.get('target')!r}"
            )

        listed = mine.get("workspace")
        if not listed or os.path.realpath(str(listed)) != os.path.realpath(self.config.workspace):
            raise StepFailed(
                f"{self.address} is listed against workspace {listed!r}, not the one the "
                f"harness started it in ({self.config.workspace}) — the join that makes this "
                f"row this Session rather than a coincidence"
            )
        return (
            f"{self.address} present in the roster against its own workspace; "
            f"agent's own record {truth.describe()}; the engine lists {len(rows)} session(s)"
        )

    # --- stable name ------------------------------------------------------

    def stable_name(self) -> str:
        """One name, unchanged across three reads and across a Stop (#78).

        This is also the walk's **first turn**, and it is words-only by design —
        see `ACKNOWLEDGE`. The chat is marked before it starts and the mark is
        handed to `stop notice`, so what that step waits for cannot be a message
        that predates the Stop it is about.
        """
        if self.address is None:
            raise LaneBlocked("no Session in the roster to name")
        before = [self._name_now() for _ in range(NAME_READS)]
        if len(set(before)) != 1:
            raise StepFailed(f"three consecutive reads gave {before!r}, not one name")
        if before[0] is None:
            official = self.truth.name if self.truth else None
            raise StepFailed(
                f"{self.address} has no name in the roster after {NAME_READS} reads — #78 "
                f"requires the official one, and the agent's own record calls it {official!r}"
            )

        self.before_first_turn = self.person.latest_message_id()
        turn = self._drive_turn("acknowledge", ACKNOWLEDGE)
        after = self._name_now()
        if after != before[0]:
            raise StepFailed(f"the name was {before[0]!r} before the Stop and {after!r} after it")
        if not turn.ended:
            raise StepFailed(
                f"the name held at {after!r}, but the turn never ended within "
                f"{self.far_side.agent_turn_seconds:.0f}s so no Stop was crossed"
            )
        return f"{after!r} across {NAME_READS} reads and across a Stop ({turn.seconds:.1f}s turn)"

    # --- progress ---------------------------------------------------------

    def progress(self) -> str:
        """Progress is readable without costing a turn (#76).

        Two things have to hold at once: there is something to read, and reading
        it does not make the Session work. The second is checked the only way it
        can be from outside — the agent's own record does not grow across the
        read, and the roster's state does not leave `idle`.

        **Both surfaces are exercised, because #76 built both and they are one
        reading.** `bridgectl progress <target>` is the verb the ticket names,
        and it reads the Session *now*; the roster row carries the same fields so
        a Control Panel can render them without asking a second question. The
        step fails if either is absent, and fails if they disagree.
        """
        if self.address is None:
            raise LaneBlocked("no Session to read progress from")
        before_size = self._record_size()
        before_state = self._roster_field("state")

        answer = self.bridgectl("progress", self.address)
        if not answer.ok:
            raise StepFailed(f"`bridgectl progress {self.address}` refused: {answer.text}")

        row = self._roster_row()
        if row is None:
            raise StepFailed(f"{self.address} left the roster before progress could be read")
        if "progress" not in row:
            raise StepFailed(
                f"the roster row carries no `progress`: #74 locks "
                f"`Progress(recent, truncated, read_at)` on the inspection and #76 is the verb "
                f"that fills it; the row has keys {sorted(row)}"
            )
        reported = row["progress"]
        if not reported or not (reported.get("recent") if isinstance(reported, dict) else None):
            raise StepFailed(f"progress for {self.address} is {reported!r} after a turn that ran")
        said = support.flatten(
            f"{entry.get('role')}: {entry.get('text')}"
            for entry in reported["recent"]
            if isinstance(entry, dict)
        )
        newest = reported["recent"][-1]
        if isinstance(newest, dict) and str(newest.get("text", ""))[:40] not in answer.text:
            raise StepFailed(
                f"the verb and the roster describe {self.address} differently — "
                f"`bridgectl progress` said {answer.text[:200]!r} and the row says {said[:200]!r}"
            )

        time.sleep(2.0)
        after_size = self._record_size()
        after_state = self._roster_field("state")
        if after_size != before_size:
            raise StepFailed(
                f"reading progress grew the Session's own record from {before_size} to "
                f"{after_size} bytes — that is a turn, and #76 forbids one"
            )
        return (
            f"progress read without a turn, through `bridgectl progress` and the roster row: "
            f"{said[:160]!r}; record steady at {after_size} bytes; "
            f"state {before_state!r} → {after_state!r}"
        )

    # --- stop notice ------------------------------------------------------

    def stop_notice(self) -> str:
        """The Stop `stable name` crossed reached the chat, and it says what it stopped on.

        #75's shape: the notice carries the question or the permission, not a
        flattened sentence. What can be checked from the chat is that a message
        arrived for that Stop and that it is not empty; whether it carries the
        typed `WaitingFor` is #75's own exit and is read off the payload.

        **The message has to name this Session**, which is the module's
        attribution rule and not a nicety of this step: until #109 this took the
        next bot message after its mark, and on run `20260826T213402Z` that was a
        stranger's permission prompt.
        """
        if self.before_first_turn is None:
            raise LaneBlocked("no turn was driven, so no Stop was crossed to be announced")
        message = self._await_own_message(
            self.before_first_turn, deadline_seconds=self.far_side.telegram_round_trip_seconds
        )
        stop_lines = support.matching_lines(self.engine.log_lines(), r"(?i)stop|SessionStopped")
        if message is None:
            raise StepFailed(
                f"no message naming {self.address} reached the chat within "
                f"{self.far_side.telegram_round_trip_seconds:.0f}s of the turn ending. The bot "
                f"said {self._other_traffic(self.before_first_turn)} in that window, none of it "
                f"about this Session; engine.log stop lines: {stop_lines[-3:] or 'none'}"
            )
        if not message.text.strip():
            raise StepFailed(f"the Stop reached the chat as an empty message ({message.id})")
        waiting = self._roster_field("waiting_for")
        kind = waiting.get("kind") if isinstance(waiting, dict) else None
        if not kind:
            raise StepFailed(
                f"a message arrived for the Stop ({message.id}: {message.text!r}) but the roster "
                f"does not say what the Session stopped on: waiting_for is {waiting!r}. #75 "
                f"replaces `SessionStopped.detail` free text with the typed `WaitingFor`, and a "
                f"notice the roster cannot corroborate is a sentence, not a state."
            )
        if not stop_lines:
            raise StepFailed(
                f"message {message.id} reached the chat and the roster says {kind!r}, but "
                f"engine.log carries no Stop line — the run cannot attribute the message to "
                f"this engine's own Stop"
            )
        return (
            f"bot message {message.id}, naming this Session: {message.text!r}; roster "
            f"waiting_for kind {kind!r}; engine.log: {stop_lines[-1]!r}"
        )

    # --- relay ------------------------------------------------------------

    def relay(self) -> str:
        """Words go in through `bridgectl relay`, come out as a receipt and an effect.

        **DELIVERED is never inferred from a write** (#71, carried into #77), so
        this step wants two things the engine cannot fake: a reply that says
        `delivered` rather than retained or unknown, and the file the words asked
        for. The permission this turn raises is answered here — through
        `bridgectl approve`, so the *bridge* answers it — and the evidence is
        handed to `approval`, which is the step that grades it.

        **The words are the lane's** (`Lane.relayed`). Both lanes ask for one
        file containing one word; only the path differs, because only the path
        decides whether the agent has to ask permission first (#105).
        """
        if self.address is None:
            raise LaneBlocked("no Session to relay to")
        relayed = self.lane.relayed(self.config.workspace)
        # The directory the effect lands in is the harness's to make. On the
        # Codex lane it is outside the sandbox, and an agent that had to create
        # it as well would be asking permission twice for one instruction — two
        # permissions where the step grades one.
        target = relayed.path_in(self.config.workspace)
        if target is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
        mark = self.person.latest_message_id()
        started = time.monotonic()
        answer = self.bridgectl(
            "relay", self.address, relayed.words, timeout=support.RELAY_DEADLINE_SECONDS
        )
        if not answer.ok:
            raise StepFailed(f"relay refused: {answer.text}")

        self.approval_evidence = self._answer_pending_approval(mark)
        performed = support.wait_for(
            lambda: relayed.performed_in(self.config.workspace),
            deadline_seconds=self.far_side.workspace_effect_seconds,
        )
        self.turns.append(Turn("relay", time.monotonic() - started, performed))
        if "delivered" not in answer.text:
            # #68's rule, and the one place it is observable: a route that cannot
            # be taken surfaces **as a graded delivery failure carrying a
            # reason**, never as silence and never as a bare refusal.
            # `seams/delivery.py:47-49` will not let a non-delivered receipt be
            # built without one, so an answer with no reason is a defect in what
            # reaches the surface rather than in the delivery itself.
            reason = answer.text.partition("—")[2].strip()
            raise StepFailed(
                f"relay answered {answer.text!r}, not `delivered`"
                + (
                    f"; reason given: {reason!r}"
                    if reason
                    else "; AND no reason travelled with it — #68 requires a delivery failure "
                    "to carry one, and `seams/delivery.py:47-49` cannot construct one without"
                )
                + f". {target} is {relayed.effect_in(self.config.workspace)!r}"
            )
        if not performed:
            raise StepFailed(
                f"relay answered {answer.text!r} but {target} never appeared within "
                f"{self.far_side.workspace_effect_seconds:.0f}s — a receipt without an effect"
            )
        return f"{answer.text}; {target} contains {relayed.content}"

    # --- approval ---------------------------------------------------------

    def approval(self) -> str:
        """A permission raised inside a Session round-trips through the bridge.

        Graded here, observed during `relay` — the same turn, from the other end.
        Splitting them into two turns would prove less: the shape the product has
        to survive is a *relayed* instruction that needs a permission, and that is
        one turn by definition.

        The evidence names the policy the permission was measured at, read from
        the agent's own record where the agent records one. A green step that did
        not say which ground it stood on is a step that would read the same on
        ground where the permission could not have been raised at all — which is
        the run #105 was opened on.
        """
        policy = self.lane.policy_at(self._record_now())
        if self.approval_evidence is None:
            # Each lane reports the measurement its own silence contradicts. One
            # shared sentence here is how the codex step spent a run explaining
            # what a Claude at `--permission-mode default` would have done
            # (#105): true, and about the other lane.
            raise StepFailed(
                f"no permission was raised by the relayed instruction, so nothing "
                f"round-tripped. Measured at {policy.named}: {self.lane.asks_about}"
            )
        if policy.unsound:
            # A round trip that happened is not the whole claim. #105 asks this
            # step to *name* the policy it was measured at, and a green line
            # reading `no policy` — or naming ground on which no permission could
            # have been raised — is the same silent pass in a new costume.
            raise StepFailed(
                f"a permission did round-trip ({self.approval_evidence}), but the ground it was "
                f"measured on is not the ground this lane stands on: {policy.unsound}"
            )
        return f"{self.approval_evidence}; measured at {policy.named}"

    # --- companion inbound ------------------------------------------------

    def companion_inbound(self) -> str:
        """A typed `@<name>: words` becomes a delivered relay, with the line #48 requires."""
        name = self._name_now()
        if not name:
            raise StepFailed("no Session name to address an inbound message to")
        mark = self.person.latest_message_id()
        sent = self.person.send(f"@{name}: {INBOUND.words}")
        self._answer_pending_approval(mark)
        performed = support.wait_for(
            lambda: INBOUND.performed_in(self.config.workspace),
            deadline_seconds=self.far_side.workspace_effect_seconds,
        )
        inbound_lines = support.matching_lines(self.engine.log_lines(), r"(?i)inbound")
        if not performed:
            raise StepFailed(
                f"message {sent.id} addressed to @{name} was sent to the bot but "
                f"{INBOUND.target} never appeared in {self.config.workspace}; engine.log "
                f"inbound lines: {inbound_lines[-3:] or 'none'}"
            )
        if not inbound_lines:
            raise StepFailed(
                f"{INBOUND.target} was written, so the words arrived, but engine.log carries "
                f"no inbound line — #48's requirement"
            )
        return (
            f"message {sent.id} → @{name} → {INBOUND.target} contains {INBOUND.content}; "
            f"engine.log: {inbound_lines[-1]!r}"
        )

    # --- switches ---------------------------------------------------------

    def switches(self) -> str:
        """Duty off pushes nothing; Duty on reports only what is still actionable (#80).

        The turn here ends **waiting on the user** rather than done, because that
        is what makes a Session still actionable when Duty comes back on. Two
        observations: silence over a derived window with Duty off, and — with
        Duty back on — a notice naming this Session.

        Both observations are about *this* Session, under the module's
        attribution rule. The silence one is where that matters most and reads
        least obviously: Duty is a global switch, so a stranger's notice arriving
        with Duty off would be a real product bug — but it would be a bug about
        somebody else's Session, and failing this lane on it is the mirror image
        of the #109 pass.
        """
        if self.address is None:
            raise LaneBlocked("no Session to watch the switches over")
        off = self.bridgectl("switch", "duty", "off")
        if not off.ok:
            raise StepFailed(f"`switch duty off` refused: {off.text}")

        mark = self.person.latest_message_id()
        turn = self._drive_turn("ask something", ASK_SOMETHING, expect_waiting=True)
        status = self.bridgectl("status")
        if not status.ok:
            raise StepFailed(f"with Duty off, `status` refused: {status.text}")
        intruder = self._await_own_message(
            mark, deadline_seconds=self.far_side.absence_window_seconds
        )
        if intruder is not None:
            self.bridgectl("switch", "duty", "on")
            raise StepFailed(
                f"with Duty off a message about this Session still reached the chat: "
                f"{intruder.id} {intruder.text!r}"
            )

        mark = self.person.latest_message_id()
        back_on = self.bridgectl("switch", "duty", "on")
        if not back_on.ok:
            raise StepFailed(f"`switch duty on` refused: {back_on.text}")
        reconciled = self._await_own_message(
            mark, deadline_seconds=self.far_side.telegram_round_trip_seconds
        )
        if reconciled is None:
            waiting = self._roster_field("waiting_for")
            raise StepFailed(
                f"Duty off held silence for {self.far_side.absence_window_seconds:.0f}s "
                f"(correct), but turning Duty back on reported nothing about a Session that "
                f"stopped waiting on the user (turn ended={turn.ended}, "
                f"roster waiting_for {waiting!r}). The bot said "
                f"{self._other_traffic(mark)} in that window, none of it about this Session"
            )
        return (
            f"Duty off: nothing pushed in {self.far_side.absence_window_seconds:.0f}s, `status` "
            f"still answered ({status.text.splitlines()[0]!r}); Duty on: message "
            f"{reconciled.id} {reconciled.text!r}"
        )

    # --- child ------------------------------------------------------------

    def child(self) -> str:
        """A child process is seen, never announced, and never spoken to (#79).

        Measured 2026-08-26 and the reason this step can exist at all: a `claude`
        started with `CLAUDE_CODE_CHILD_SESSION` set is absent from `claude
        agents --json` altogether. So "the child appears under its parent" is a
        claim about a source the official roster does not serve, and the product
        has to find it another way — which is what makes this #79's work rather
        than a formality.

        **"Never announced" is a claim about the child**, so the module's
        attribution rule is applied to the *child's* names here, not the parent's
        — the one place in this walk where the target of a read is not the
        Session under test. It has to be: the parent's own turn ends inside this
        step's window, and the parent's Stop Notice is the product working. A
        read that took the next bot message would have called that notice the
        child's and failed #79 for the one thing #79 does not forbid.
        """
        if self.address is None:
            raise LaneBlocked("no parent Session to hang a child from")
        mark = self.person.latest_message_id()
        before = {_address_of(row) for row in self._roster_rows()}
        turn = self._drive_turn("child", Instruction(words=self.lane.child_words))

        rows = self._roster_rows()
        children = [
            row
            for row in rows
            if _address_of(row) not in before
            and isinstance(row.get("child"), dict)
            and row["child"].get("kind") == "child"
        ]
        if not children:
            raise StepFailed(
                f"no child row appeared under {self.address} within "
                f"{self.far_side.agent_turn_seconds:.0f}s (turn ended={turn.ended}); the roster "
                f"gained {sorted({_address_of(row) for row in rows} - before) or 'nothing'}. "
                f"#74 locks `ChildClassification` and #79 fills it."
            )
        child_row = children[0]
        child_address = _address_of(child_row)
        parent = child_row["child"].get("parent")
        if not parent:
            raise StepFailed(f"the child row {child_address} names no parent")
        # "Listed **under its parent**" is the claim, and any non-empty parent
        # satisfied it before — including one naming some other Session, which is
        # precisely the bug a roster of several Sessions would produce and a
        # roster of one would hide.
        parent_address = _address_of({"target": parent})
        if parent_address != self.address:
            raise StepFailed(
                f"the child row {child_address} is listed under {parent_address}, not "
                f"under the Session that started it ({self.address})"
            )

        announced = self._await_message_naming(
            child_row,
            mark,
            deadline_seconds=self.far_side.absence_window_seconds,
            whose=f"the child {child_address}",
        )
        if announced is not None:
            raise StepFailed(
                f"a child raised a Stop Notice: message {announced.id} {announced.text!r} — "
                f"#79: children are seen, never announced"
            )
        refused = self.bridgectl("relay", child_address, "this must be refused")
        if refused.ok:
            raise StepFailed(
                f"the child {child_address} was accepted as a Relay target: {refused.text!r} — "
                f"#79: seen, never spoken to"
            )
        # A non-zero exit is not by itself a refusal: the surface exits non-zero
        # for an engine that never answered, a malformed address, a socket that
        # is not there. Only a refusal that *names this Session* proves the rule
        # was applied rather than the call merely failing.
        if child_address not in refused.text:
            raise StepFailed(
                f"the relay to the child {child_address} failed without refusing it — the answer "
                f"{refused.text!r} does not name it, so this is the call going wrong rather than "
                f"the child rule being applied"
            )
        return (
            f"{child_address} listed under {support.flatten([parent])}; no notice naming it in "
            f"{self.far_side.absence_window_seconds:.0f}s; relay refused: {refused.text!r}"
        )

    # --- plumbing ---------------------------------------------------------

    def _ground_truth(self) -> hand_started.GroundTruth:
        """Who the harness started, according to the agent — the oracle, not the product."""
        if self.truth is not None:
            return self.truth
        pid = self.session.pid
        if pid is None:
            raise LaneBlocked("the hand-started command never started")

        def found() -> bool:
            self.truth = self.lane.ground_truth(
                pid, self.config.workspace, self.environment, self.started_at
            )
            return self.truth is not None

        if not support.wait_for(found, deadline_seconds=self.far_side.agent_turn_seconds):
            raise LaneBlocked(
                f"the agent itself never recorded the Session the harness started (pid {pid}, "
                f"{self.config.workspace}). Screen tail: {self.session.screen_tail()[-600:]!r}"
            )
        assert self.truth is not None
        if not self.session.alive:
            raise LaneBlocked(
                f"the hand-started command exited before anything could be asked of it. "
                f"Screen tail: {self.session.screen_tail()[-600:]!r}"
            )
        self.journal("ground.truth", lane=self.lane.name, **vars(self.truth))
        return self.truth

    def _roster_rows(self) -> list[dict]:
        data = support.control_plane_payload(
            support.Action.SESSIONS,
            socket_path=self.config.socket_path,
            journal=self.journal,
            why="the roster payload carries fields `bridgectl sessions` does not render",
        )
        rows = data.get("sessions", [])
        return [row for row in rows if isinstance(row, dict)]

    def _row_for(self, rows: list[dict], truth: hand_started.GroundTruth) -> dict | None:
        """The roster row for the Session the harness started — by id, else by pid.

        The session id is the exact key and is used whenever there is one. There
        is not always one: `codex` writes the rollout that names it when the
        first *turn* starts (measured 2026-08-26), so before that the only thing
        either side can agree on is the process. `SessionTarget` carries the pid
        (`seams/identity.py:8-10`) and #74's Codex process fallback discovers
        rows by pid and cwd, so matching on it here is the same join the product
        has to make — not a weaker one the harness invented for itself.
        """
        for row in rows:
            target = row.get("target")
            if not isinstance(target, dict):
                continue
            if truth.session_id and str(target.get("session_id")) == truth.session_id:
                return row
            if not truth.session_id and target.get("pid") == truth.pid:
                return row
        return None

    def _roster_row(self) -> dict | None:
        if self.truth is None:
            return None
        return self._row_for(self._roster_rows(), self.truth)

    def _roster_field(self, field_name: str) -> object:
        row = self._roster_row()
        return row.get(field_name) if row else None

    def _name_now(self) -> str | None:
        row = self._roster_row()
        if row is None:
            return None
        name = row.get("name")
        return str(name) if name else None

    def _own_row(self) -> dict:
        """The roster's row for the Session under test, or the agent's own account of it.

        The engine is not the only thing that knows which Session this is, and
        the walk reads the chat before `roster` has established that it does:
        `drain_boot_notice` runs seconds after the launch turn (#110), where the
        engine's discovery may not have listed the Session yet — `roster` itself
        allows it `DISCOVERY_SECONDS` to.

        Ground truth is the fallback and it is not a weaker one for this
        question. A Session the engine holds no Session Name for is announced by
        its address (`core/sessions.py:529-546`), and the address is exactly what
        the agent's own record gives. `{}` — no roster row and no ground truth —
        is the honest empty answer, and `_await_message_naming` refuses on it.
        """
        row = self._roster_row()
        if row is not None:
            return row
        if self.truth is None:
            return {}
        return {
            "target": {
                "agent": self.lane.agent,
                "session_id": self.truth.session_id or None,
                "pid": self.truth.pid,
            },
            "name": None,
        }

    def _await_message_naming(
        self,
        row: dict | None,
        mark: int,
        *,
        deadline_seconds: float,
        whose: str,
        matching: Callable[..., bool] | None = None,
    ) -> telegram_person.PersonMessage | None:
        """Wait for a bot message that names *that* Session, and never any other.

        The one door every chat read in this walk goes through — the module's
        attribution rule, enforced. `None` stays a legitimate answer, because two
        of this walk's reads assert an absence.

        `matching` narrows what a caller will take **on top of** the rule, never
        instead of it: it is `and`-ed, so no caller can widen its way back to the
        message that made #109.

        The naming forms are resolved **once**, before the wait, and not inside
        the predicate: they come off the control plane, and re-deriving them per
        message per poll would ask the engine for the roster a hundred times over
        a two-minute absence window.
        """
        forms = _naming_forms(row or {})
        if not forms:
            # Nothing to attribute *with*. Louder than a silent `None`, which two
            # of the three callers would read as the absence they wanted.
            raise StepFailed(
                f"nothing in the roster names {whose}, so no message in the chat could be "
                f"attributed to it either way: the row is {row!r}"
            )
        shared = _indistinguishable_from(forms, self._roster_rows(), _address_of(row or {}))
        if shared is not None:
            raise StepFailed(
                f"{whose} is named {list(forms)}, and so is {shared} — a message naming one "
                f"names both, so this run cannot attribute anything in the chat to either"
            )
        self.journal("chat.attribution", lane=self.lane.name, whose=whose, names=list(forms))
        return self.person.await_message(
            mark,
            deadline_seconds=deadline_seconds,
            matching=lambda seen: (
                seen.from_bot
                and _named_in(seen.text, forms)
                and (matching is None or matching(seen))
            ),
        )

    def _await_own_message(
        self,
        mark: int,
        *,
        deadline_seconds: float,
        matching: Callable[..., bool] | None = None,
    ) -> telegram_person.PersonMessage | None:
        """The same, for the Session this walk is walking."""
        row = self._own_row()
        return self._await_message_naming(
            row,
            mark,
            deadline_seconds=deadline_seconds,
            # `roster` is what sets `self.address`, and one caller runs before it
            # (`drain_boot_notice`), so the row says who this is when it cannot.
            whose=f"the Session under test ({self.address or _address_of(row)})",
            matching=matching,
        )

    def _other_traffic(self, mark: int) -> str:
        """What else the bot said in the window, for a human reading a red step.

        A step that failed to find its own message is worth telling apart from a
        step that found nothing at all, and on a machine the bridge covers wholly
        those are different situations with different causes.
        """
        others = [
            f"{message.id}: {message.text!r}"
            for message in self.person.messages_after(mark)
            if message.from_bot
        ]
        return support.flatten(others) if others else "nothing"

    def _record_now(self) -> Path | None:
        """Where the agent's own record of this Session is, at this moment."""
        if self.truth is None:
            return None
        return self.lane.record_now(self.truth, self.started_at)

    def _record_size(self) -> int:
        """How big the agent's own record is — the far side's own measure of work."""
        record = self._record_now()
        return record.stat().st_size if record and record.exists() else 0

    def _drive_turn(
        self, what: str, instruction: Instruction, *, expect_waiting: bool = False
    ) -> Turn:
        """Type an instruction at the keyboard and wait for the turn to be over.

        Over means one of two things, and the caller says which: the agent's own
        record stopped growing (a turn that finished), or the roster says the
        Session is waiting on the user. Both are read from outside; neither is
        the screen.
        """
        started = time.monotonic()
        self.session.submit(instruction.words)
        settled_for = 0.0
        last = self._record_size()
        ended = False
        deadline = started + self.far_side.agent_turn_seconds
        while time.monotonic() < deadline:
            time.sleep(3.0)
            if expect_waiting and self._roster_field("state") == "waiting":
                ended = True
                break
            size = self._record_size()
            if size != last:
                last, settled_for = size, 0.0
                continue
            settled_for += 3.0
            # A record that has not grown for two polls after growing at all is a
            # turn that is over; before it grows at all there is nothing to settle.
            if size > 0 and settled_for >= 9.0:
                ended = True
                break
        return self._measured(what, started, ended)

    def _measured(self, what: str, started: float, ended: bool) -> Turn:
        """Record one turn on the walk, whoever asked for it."""
        turn = Turn(what, time.monotonic() - started, ended)
        self.turns.append(turn)
        self.journal("turn", lane=self.lane.name, what=what, seconds=turn.seconds, ended=turn.ended)
        return turn

    def _answer_pending_approval(self, mark: int) -> str | None:
        """Answer, through the bridge, whatever permission the current turn raises.

        Journaled rather than asserted here: a lane that stalled on an unanswered
        permission would report a missing file where the truth is a waiting
        dialog, and that is a worse lie than a longer journal. `approval` grades
        what this returns.

        **Only this lane's own dialog is answered, and that is not tidiness.**
        The engine this run spawns discovers every Session on the machine, and
        since #77 that includes every Codex Session on the shared daemon — so
        `pending_approvals` routinely holds dialogs belonging to somebody's open
        work. Answering the first of them, as this did until the 2026-08-26 run,
        grants `allow` to a stranger's permission and leaves this lane's own
        dialog unanswered behind it: measured, on that run, as a real Codex
        command approved by the harness and this lane's Write answered four
        minutes late, which failed `relay` and `companion inbound` downstream.
        The address is the filter, exactly as it is everywhere else here.

        **And the announcement it waits for is filtered the same way** (#109).
        Filtering the dialog and then taking any Session's announcement as proof
        that this one reached the Companion Channel is the same defect one clause
        later: on a machine bridging six Sessions the round trip could read green
        while this lane's own announcement never went out. That the check is
        possible at all is a product change #109 made — until then the
        announcement named the tool and never the Session
        (`core/approvals.py:51-56`).
        """
        deadline = time.monotonic() + self.far_side.agent_turn_seconds
        while time.monotonic() < deadline:
            data = support.control_plane_status(self.config.socket_path, self.journal)
            pending = [
                waiting
                for waiting in data.get("pending_approvals", [])
                if _address_of(waiting) == self.address
            ]
            if pending:
                announced = self._await_own_message(
                    mark,
                    deadline_seconds=self.far_side.telegram_round_trip_seconds,
                    matching=lambda seen: bool(APPROVAL_ANNOUNCEMENT.search(seen.text)),
                )
                approval_id = str(pending[0]["approval_id"])
                answer = self.bridgectl("approve", approval_id, "allow")
                self.journal(
                    "approval.answered",
                    lane=self.lane.name,
                    approval_id=approval_id,
                    announced=bool(announced),
                    reply=answer.text,
                )
                if not answer.ok:
                    raise StepFailed(f"`approve {approval_id} allow` refused: {answer.text}")
                if announced is None:
                    # The design says the permission "is escalated to every outlet
                    # — with Voice off, the Companion Channel — and the real bot's
                    # message is read by the user-account client". A run that
                    # answered an approval nobody was ever told about has proved
                    # the second half of the round trip and none of the first, and
                    # reporting that as a pass is how a silent escalation ships.
                    raise StepFailed(
                        f"approval {approval_id} was answered through `bridgectl approve` "
                        f"({answer.text!r}), but it never reached the Companion Channel within "
                        f"{self.far_side.telegram_round_trip_seconds:.0f}s — no chat message "
                        f"matched {APPROVAL_ANNOUNCEMENT.pattern!r}"
                    )
                return (
                    f"approval {approval_id}: announced as chat message {announced.id} "
                    f"({announced.text!r}); approve answered {answer.text!r}"
                )
            time.sleep(2.0)
        self.journal("approval.none", lane=self.lane.name)
        return None


def _address_of(row: dict) -> str:
    """`agent:session_id[:pid]`, the way `control_plane/commands.py:116` writes it."""
    target = row.get("target")
    if not isinstance(target, dict):
        return "<no target>"
    pid = target.get("pid")
    tail = f":{pid}" if pid else ""
    return f"{target.get('agent')}:{target.get('session_id')}{tail}"


def _naming_forms(row: dict) -> tuple[str, ...]:
    """Every string the product would name one Session by, from its roster row.

    Mirrors `core/sessions.py:529-546` rather than importing it: a harness that
    asked the product what it had said would agree with the product by
    construction, and the whole point of reading the chat is that it might not.

    Both forms are kept because the product chooses between them by what it holds
    *at the moment it speaks* — `spoken_name` where the Session has a Session
    Name, `spoken_target` where it does not — and the roster read that answers
    this question is a different moment from the one the message was composed in.
    A Codex Session, in particular, has no name until its first turn.
    """
    target = row.get("target")
    target = target if isinstance(target, dict) else {}
    forms: list[str] = []
    name = row.get("name")
    if name:
        forms.append(str(name))
    agent = target.get("agent")
    session_id = target.get("session_id")
    pid = target.get("pid")
    if agent and session_id:
        forms.append(f"{agent} {session_id}")
    elif agent and pid:
        forms.append(f"{agent} pid {pid}")
    return tuple(forms)


def _named_in(text: str, forms: Sequence[str]) -> bool:
    """Does this message name one of these Sessions? The attribution rule's whole test.

    Substring, not equality: the product's own words wrap the name in a sentence
    it composes (`core/bridge.py:107-123`, `core/approvals.py:51-56`), and which
    sentence is the product's business rather than this harness's.
    """
    return any(form in text for form in forms)


def _indistinguishable_from(forms: Sequence[str], rows: Sequence[dict], mine: str) -> str | None:
    """Another Session in the roster that a message naming *this* one would also name.

    A Session Name is `<project> · <task>` and **nothing makes it unique**
    (`adapters/agent/_naming.py:39-62` composes it from a project and a task and
    checks neither against the other rows). So two Sessions on one machine can be
    called the same thing, and the product already knows it: `match_name` refuses
    with `AmbiguousNameError` rather than picking one of them
    (`core/sessions.py:456-463`). This is that same fact, met from the chat.

    **The pair it is not is a Child Process and its parent.** A child carries no
    Session Name at all — `core/sessions.py:225` keeps `name` for main Sessions
    and gives a child `None` — so its only naming form is its address, which is
    unique by construction. The `child` step is therefore safe from this by
    #78/#79's own design rather than by luck, which is worth saying because a
    parent announced *while* the child must not be is exactly the shape a
    collision would ruin.

    Where that happens the run cannot attribute either way, and the honest answer
    is to say so rather than pick: a message accepted would be a guess and a
    message rejected would be a guess too. That is the whole lesson of #109 —
    a step that passes for a reason it cannot name has not passed.
    """
    for other in rows:
        if _address_of(other) == mine:
            continue
        theirs = _naming_forms(other)
        if any(form in one for form in forms for one in theirs):
            return f"{_address_of(other)}, which the roster names {list(theirs)}"
    return None
