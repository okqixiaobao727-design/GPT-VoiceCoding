"""The eight steps, written once and walked by both lanes.

`docs/acceptance-design.md` put the step assertions in the E2E suite's shared
support so they would be "written once". The E2E suite was never built, so this
module is where "once" lives; the two lane tests are the lane's own words and
nothing else.

## What today's `main` makes of this journey

The journey is written against the **specified** product, not the built one, and
two specified things are absent:

* **ADR 0009's Opening Instruction does not exist in the code.** `--task` is a
  *label* and nothing more — `control_plane/actions.py:140` reads it into
  `SessionLabel` (`seams/identity.py:62`) and no launcher ever sends it to an
  agent (`session_launcher/claude.py:139`, `session_launcher/codex.py:148`). ADR
  0009 is explicit that the instruction is a separate field, not `task`. So a
  launched Session today performs nothing unattended, and step 1b is red on both
  lanes before the run starts.
* **`approve <id>` cannot be driven.** The id reaches no surface; see
  `support.control_plane_status` for the trace and the legacy citation.

Neither is worked around. Step 1b attempts the launch surface ADR 0009 specifies
and records the product's own refusal; the lane then launches the way the product
today allows, so the six steps that do not depend on an Opening Instruction still
produce verdicts. One expensive run, every red.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import support
from support import LaneBlocked, StepFailed

#: Step names, in the design's order. Every lane records a verdict for each.
STEPS = (
    "1a launch answers",
    "1b opening instruction performed unattended",
    "2 approval round-trips",
    "3 turn runs",
    "4 stop notice fires",
    "5 relay delivered",
    "5b relay's surface deadline covers the engine's proof wait",
    "6 inbound (Companion Channel)",
    "7 the switches",
    "8 close",
)

#: The flag ADR 0009's Opening Instruction would arrive on. Attempted so the run
#: records the product's own words when it is not there, rather than the
#: harness's opinion that it is not.
OPENING_FLAG = "--opening-instruction"

#: What the escalated permission announcement says (`core/approvals.py:43-46`).
#: Matched rather than quoted whole, because the tool name and detail are the
#: agent's and this run does not get to predict them.
APPROVAL_ANNOUNCEMENT = re.compile(r"waiting for your permission to use", re.IGNORECASE)


@dataclass(frozen=True)
class Instruction:
    """One small, deterministic, file-producing action, and the effect to read back.

    The shape `docs/acceptance-design.md` prescribes: an effect the harness reads
    off the filesystem, so "the Session acted on the words" is a fact from the far
    side rather than a claim from the engine.
    """

    words: str
    filename: str
    content: str

    def effect_in(self, workspace: Path) -> str | None:
        text = support.read_if_exists(workspace / self.filename)
        return text.strip() if text is not None else None

    def performed_in(self, workspace: Path) -> bool:
        return self.effect_in(workspace) == self.content


def instruction(filename: str, content: str) -> Instruction:
    """The wording, in one place, so both lanes ask for the same shape of thing."""
    return Instruction(
        words=(
            f"Create a file named {filename} in the current directory whose entire "
            f"contents are the single word {content}. Do nothing else, and do not "
            f"ask any questions."
        ),
        filename=filename,
        content=content,
    )


#: Three instructions per lane: the Opening Instruction, the relayed one, and the
#: one that arrives from Telegram. Distinct filenames and distinct words so the
#: three effects can never be mistaken for one another.
OPENING = instruction("opening.txt", "ALPHA")
RELAYED = instruction("relay.txt", "BRAVO")
INBOUND = instruction("inbound.txt", "CHARLIE")


@dataclass
class Lane:
    """Everything about a lane that is not the journey itself."""

    name: str
    agent: str
    project: str

    def transcript_directory(self, workspace: Path) -> Path | None:
        """Where this agent keeps its transcript for a workspace, if the harness can know.

        Claude's is derivable: `~/.claude/projects/<path with separators flattened>`,
        which is what `ls ~/.claude/projects` shows. Codex keeps its rollouts under
        its own home in a shape this run does not assert on, so it answers None and
        the transcript half of step 1a is not claimed for that lane.
        """
        if self.agent != "claude":
            return None
        flattened = str(workspace).replace("/", "-").replace(".", "-")
        return Path.home() / ".claude" / "projects" / flattened


class Walk:
    """One lane's journey. Every method is one step; each returns its evidence."""

    def __init__(
        self,
        *,
        lane: Lane,
        engine: support.Engine,
        config: support.DerivedConfig,
        bridgectl: support.Bridgectl,
        person,  # telegram_person.TelegramPerson
        journal: support.Journal,
        verdict: support.Verdict,
        far_side: support.FarSideDeadlines,
    ) -> None:
        self.lane = lane
        self.engine = engine
        self.config = config
        self.bridgectl = bridgectl
        self.person = person
        self.journal = journal
        self.far_side = far_side
        self.journey = support.Journey(
            lane=lane.name, verdict=verdict, journal=journal, steps=STEPS
        )
        self.address: str | None = None
        self.label: str | None = None
        #: Set once an approval has been seen round-trip, wherever in the run it
        #: happened. Step 2's second half rests on this.
        self.approval_evidence: str | None = None

    # --- the walk ---------------------------------------------------------

    def walk(self) -> None:
        # Recorded, not asserted: the run *arranged* trust rather than observing
        # the gate, so claiming a verdict on it here would be claiming something
        # this run did not see. The measurement is on ticket #60 and the
        # arrangement is in the journal as `trust.granted`.
        self.journey.record_arranged(
            "0c workspace trust",
            "arranged by the harness, not observed: both agents stop a launch into a directory "
            "they have never seen with a full-screen trust dialog and the Session never "
            "registers (measured at build time; #18 reports the same gate for Codex). "
            "`journal.jsonl` carries the grant and the revoke.",
        )
        try:
            self.arm_switches()
        except LaneBlocked as unarmed:
            # Nothing after this can be observed for the reason it would appear
            # to fail — a run with the switches down sees no push anywhere.
            self.journey.skip_rest(str(unarmed))
            return
        self.journey.run(STEPS[0], self.launch_answers)
        self.journey.run(STEPS[1], self.opening_instruction_performed)
        self.journey.run(STEPS[2], self.approval_round_trips)
        self.journey.run(STEPS[3], self.turn_runs)
        self.journey.run(STEPS[4], self.stop_notice_fires)
        self.journey.run(STEPS[5], self.relay_delivered)
        self.journey.run(STEPS[6], self.relay_deadline_covers_the_wait)
        self.journey.run(STEPS[7], self.inbound_delivered)
        self.journey.run(STEPS[8], self.the_switches)
        self.journey.run(STEPS[9], self.close)

    def arm_switches(self) -> None:
        """Voice off, Message on, Duty on — the text-only mode this whole run exercises.

        Not a step, because it is the run's mode rather than a claim about the
        product. It is here rather than in the derived config because switches are
        runtime state and a fresh engine starts with **all three off** — measured,
        not assumed: `bridgectl status` on a freshly started engine answers
        `switches: duty off, message off, voice off`. An unarmed run would observe
        no push anywhere and read it as four separate failures.
        """
        for name, position in (("voice", "off"), ("message", "on"), ("duty", "on")):
            answer = self.bridgectl("switch", name, position)
            self.journal(
                "switch.armed", lane=self.lane.name, switch=name, to=position, reply=answer.text
            )
            if not answer.ok:
                raise LaneBlocked(f"`switch {name} {position}` refused: {answer.text}")

    # --- 1a ---------------------------------------------------------------

    def launch_answers(self) -> str:
        """`launch` answers `launched <address>`, and the Session is in the roster."""
        answer = self.bridgectl(
            "launch",
            "--request-id",
            str(uuid.uuid4()),
            "--project",
            self.lane.project,
            "--agent",
            self.lane.agent,
            "--task",
            f"acceptance {self.lane.name}",
        )
        if not answer.ok:
            raise LaneBlocked(f"launch refused: {answer.text}")
        if not answer.text.startswith("launched "):
            raise LaneBlocked(f"launch answered {answer.text!r}, not `launched <address>`")
        self.address = answer.text.removeprefix("launched ").strip()

        roster = self.bridgectl("sessions")
        line = next((row for row in roster.stdout.splitlines() if self.address in row), None)
        if line is None:
            raise LaneBlocked(f"{self.address} launched but does not appear in `sessions`")
        self.label = _label_of(line)

        transcripts = self.lane.transcript_directory(self.config.workspace)
        transcript_note = ""
        if transcripts is not None:
            found = sorted(transcripts.glob("*.jsonl")) if transcripts.exists() else []
            if not found:
                raise StepFailed(
                    f"{self.address} launched and is in the roster, but no transcript exists "
                    f"under {transcripts} for this workspace"
                )
            transcript_note = f"; transcript {found[0].name}"
        return f"{answer.text}; roster line {line.strip()!r}{transcript_note}"

    # --- 1b ---------------------------------------------------------------

    def opening_instruction_performed(self) -> str:
        """ADR 0009: the launch carries words, and the Session performs them unattended.

        Two observations, and today both are expected to fail. First the surface:
        a launch that names an Opening Instruction is offered to the product and
        the product's own answer is recorded. Then the effect: whatever the launch
        above started, did it do anything in the workspace on its own?
        """
        offered = self.bridgectl(
            "launch",
            "--request-id",
            str(uuid.uuid4()),
            "--project",
            self.lane.project,
            "--agent",
            self.lane.agent,
            OPENING_FLAG,
            OPENING.words,
            "--task",
            f"acceptance {self.lane.name} opening",
        )
        if offered.ok:
            performed = support.wait_for(
                lambda: OPENING.performed_in(self.config.workspace),
                deadline_seconds=self.far_side.agent_turn_seconds,
            )
            if not performed:
                raise StepFailed(
                    f"the launch accepted {OPENING_FLAG} but {OPENING.filename} never appeared "
                    f"in {self.config.workspace} within "
                    f"{self.far_side.agent_turn_seconds:.0f}s"
                )
            return f"{OPENING.filename} contains {OPENING.content}, written unattended"

        unattended = OPENING.effect_in(self.config.workspace)
        raise StepFailed(
            f"the product does not accept an Opening Instruction: `bridgectl launch "
            f"{OPENING_FLAG} …` answered {offered.text!r} (exit {offered.returncode}); the "
            f"Session started at 1a wrote nothing on its own "
            f"({OPENING.filename} is {unattended!r}). ADR 0009 is accepted and unimplemented: "
            f"`--task` is a label (seams/identity.py:62) and no launcher sends words to an "
            f"agent (session_launcher/claude.py:139, session_launcher/codex.py:148)."
        )

    # --- 2 ----------------------------------------------------------------

    def approval_round_trips(self) -> str:
        """The Opening Instruction needs one permission, and it round-trips.

        Its first half cannot happen without an Opening Instruction, so on today's
        `main` this fails here and the *second* half — that an approval raised by
        any turn round-trips at all — is observed around the relay in step 5 and
        recorded there. Nothing is claimed twice: this step's evidence names where
        the round trip was seen, if it was.
        """
        pending = self._pending_approvals()
        if not pending:
            raise StepFailed(
                "no permission request was waiting after the launch, because no first turn "
                "ran — there was no Opening Instruction to run one. The approval round trip "
                "is exercised around the step-5 relay instead and reported there."
            )
        return self._answer_one_approval(pending[0], why="raised by the Opening Instruction")

    # --- 3 ----------------------------------------------------------------

    def turn_runs(self) -> str:
        """The Reply Window closes and reopens around a turn, and `sessions` shows it."""
        if self.address is None:
            raise StepFailed("no Session to watch a Reply Window on")
        window = self._reply_window()
        if window is None:
            raise StepFailed(f"`sessions` carries no Reply Window for {self.address}")
        return f"Reply Window is {window!r} in `bridgectl sessions`"

    # --- 4 ----------------------------------------------------------------

    def stop_notice_fires(self) -> str:
        """A turn ending raises SessionStopped and one message reaches the chat."""
        mark = self.person.latest_message_id()
        message = self.person.await_message(
            mark, deadline_seconds=self.far_side.telegram_round_trip_seconds
        )
        log_lines = support.matching_lines(self.engine.log_lines(), r"(?i)stop|SessionStopped")
        if message is None:
            raise StepFailed(
                f"no message reached the chat within "
                f"{self.far_side.telegram_round_trip_seconds:.0f}s of the turn ending; "
                f"engine.log stop lines: {log_lines[-3:] or 'none'}"
            )
        return f"bot message {message.id}: {message.text!r}; engine.log: {log_lines[-1:] or 'none'}"

    # --- 5 ----------------------------------------------------------------

    def relay_delivered(self) -> str:
        """`relay` answers `delivered` and the Session acts on the words.

        This is also where an approval is expected today, because this is the
        first turn the Session runs. Any permission it raises is answered here and
        the evidence is handed back to step 2's second half.
        """
        if self.address is None:
            raise StepFailed("no Session to relay to")
        mark = self.person.latest_message_id()
        # An explicit deadline, because the surface's own is structurally too
        # short — see `support.RELAY_DEADLINE_SECONDS`. Step 5b records that as a
        # finding; this call is here to observe the relay rather than the CLI.
        answer = self.bridgectl(
            "relay", self.address, RELAYED.words, timeout=support.RELAY_DEADLINE_SECONDS
        )
        if not answer.ok:
            raise StepFailed(f"relay refused: {answer.text}")
        if "delivered" not in answer.text:
            raise StepFailed(f"relay answered {answer.text!r}, not `delivered`")

        approval = self._answer_any_approval(mark, why="raised by the relayed instruction")
        if approval:
            self.approval_evidence = approval
        if not support.wait_for(
            lambda: RELAYED.performed_in(self.config.workspace),
            deadline_seconds=self.far_side.workspace_effect_seconds,
        ):
            raise StepFailed(
                f"relay answered {answer.text!r} but {RELAYED.filename} never appeared in "
                f"{self.config.workspace} within "
                f"{self.far_side.workspace_effect_seconds:.0f}s (it is "
                f"{RELAYED.effect_in(self.config.workspace)!r})"
            )
        return f"{answer.text}; {RELAYED.filename} contains {RELAYED.content}{approval or ''}"

    # --- 5b ---------------------------------------------------------------

    def relay_deadline_covers_the_wait(self) -> str:
        """The surface waits at least as long as the engine takes to answer.

        Both numbers are read out of the bundle under test, so this is a fact
        about the artifact rather than a claim about the source. A surface whose
        deadline is shorter than the hub's own bounded wait reports a failure for
        an action that is succeeding — the defect #28 exists for, here pointed at
        `relay` instead of `launch`.
        """
        surface = support.SURFACE_RELAY_DEADLINE_SECONDS
        engine_wait = support.ENGINE_RELAY_PROOF_SECONDS
        if surface < engine_wait:
            raise StepFailed(
                f"`bridgectl relay` gives up after {surface:.0f}s "
                f"(control_plane/client.py:39) while the engine waits {engine_wait:.0f}s for the "
                f"Session to acknowledge (adapters/agent/claude/settings.py:34), so no relay "
                f"reply can reach the surface; measured: the CLI said "
                f"'did not answer within 10s' and engine.log then said 'not proven delivered "
                f"… within 45s; it waits'"
            )
        return f"surface {surface:.0f}s covers the engine's {engine_wait:.0f}s proof wait"

    # --- 6 ----------------------------------------------------------------

    def inbound_delivered(self) -> str:
        """A typed `@<label>: words` becomes a delivered relay, with a line in the log."""
        if self.label is None:
            raise StepFailed("no Session Label to address an inbound message to")
        mark = self.person.latest_message_id()
        sent = self.person.send(f"@{self.lane.project}: {INBOUND.words}")
        self._answer_any_approval(mark, why="raised by the inbound instruction")
        performed = support.wait_for(
            lambda: INBOUND.performed_in(self.config.workspace),
            deadline_seconds=self.far_side.workspace_effect_seconds,
        )
        inbound_lines = support.matching_lines(self.engine.log_lines(), r"(?i)inbound")
        if not performed:
            raise StepFailed(
                f"message {sent.id} was sent to the bot but {INBOUND.filename} never appeared "
                f"in {self.config.workspace}; engine.log inbound lines: "
                f"{inbound_lines[-3:] or 'none'}"
            )
        if not inbound_lines:
            raise StepFailed(
                f"{INBOUND.filename} was written, so the words arrived, but engine.log carries "
                f"no inbound line — #48's requirement"
            )
        return (
            f"message {sent.id} → {INBOUND.filename} contains {INBOUND.content}; "
            f"engine.log: {inbound_lines[-1]!r}"
        )

    # --- 7 ----------------------------------------------------------------

    def the_switches(self) -> str:
        """Duty off silences the push and leaves the control plane answering."""
        off = self.bridgectl("switch", "duty", "off")
        if not off.ok:
            raise StepFailed(f"`switch duty off` refused: {off.text}")
        mark = self.person.latest_message_id()

        status = self.bridgectl("status")
        if not status.ok:
            raise StepFailed(f"with Duty off, `status` refused: {status.text}")

        # A negative observation over a derived window: one long-poll cycle plus a
        # round trip, so a Notice that was going to arrive has had every chance to.
        intruder = self.person.await_message(
            mark, deadline_seconds=self.far_side.absence_window_seconds
        )
        back_on = self.bridgectl("switch", "duty", "on")
        if not back_on.ok:
            raise StepFailed(f"`switch duty on` refused: {back_on.text}")
        if intruder is not None:
            raise StepFailed(
                f"with Duty off a message still reached the chat: {intruder.id} {intruder.text!r}"
            )
        return (
            f"Duty off: nothing pushed in {self.far_side.absence_window_seconds:.0f}s, "
            f"`status` still answered ({status.text.splitlines()[0]!r}); Duty back on"
        )

    # --- 8 ----------------------------------------------------------------

    def close(self) -> str:
        """`close` answers, the agent is gone, and the approval directory with it."""
        if self.address is None:
            raise StepFailed("no Session to close")
        answer = self.bridgectl("close", self.address)
        if not answer.ok:
            raise StepFailed(f"close refused: {answer.text}")

        pid = _pid_of(self.address)
        gone = support.wait_for(
            lambda: pid is None or not _process_alive(pid), deadline_seconds=30.0
        )
        approvals = self.config.socket_path.parent / f"vc-approvals-{self.engine.pid}"
        if not gone:
            raise StepFailed(f"close answered {answer.text!r} but pid {pid} is still running")
        # The directory goes when the engine's listener stops, which is at engine
        # teardown rather than at close — so this is recorded, not asserted here;
        # `test_*_lane.py` checks it after the engine is down (#44).
        return (
            f"{answer.text}; pid {pid} gone; approval directory {approvals} (checked at teardown)"
        )

    # --- plumbing ---------------------------------------------------------

    def _pending_approvals(self) -> list[dict]:
        data = support.control_plane_status(self.config.socket_path, self.journal)
        return list(data.get("pending_approvals", []))

    def _answer_one_approval(self, pending: dict, *, why: str) -> str:
        """Observe the escalation in the chat, then allow it through `bridgectl`."""
        approval_id = str(pending["approval_id"])
        message = self.person.await_message(
            self.person.latest_message_id() - 1,
            deadline_seconds=self.far_side.telegram_round_trip_seconds,
            matching=lambda seen: seen.from_bot and bool(APPROVAL_ANNOUNCEMENT.search(seen.text)),
        )
        answer = self.bridgectl("approve", approval_id, "allow")
        if not answer.ok:
            raise StepFailed(f"`approve {approval_id} allow` refused: {answer.text}")
        seen = f"chat message {message.id}" if message else "NOT announced in the chat"
        return f"{why}: {seen}; approve answered {answer.text!r}"

    def _answer_any_approval(self, mark: int, *, why: str) -> str | None:
        """Allow whatever permission a turn raises, so its effect can be observed.

        Journaled rather than asserted: a lane that stalls on an unanswered
        permission would report a missing file where the truth is a waiting
        dialog, and that is a worse lie than a longer journal.
        """
        deadline = time.monotonic() + self.far_side.agent_turn_seconds
        while time.monotonic() < deadline:
            pending = self._pending_approvals()
            if pending:
                announced = self.person.await_message(
                    mark,
                    deadline_seconds=self.far_side.telegram_round_trip_seconds,
                    matching=lambda seen: (
                        seen.from_bot and bool(APPROVAL_ANNOUNCEMENT.search(seen.text))
                    ),
                )
                approval_id = str(pending[0]["approval_id"])
                answer = self.bridgectl("approve", approval_id, "allow")
                self.journal(
                    "approval.answered",
                    lane=self.lane.name,
                    why=why,
                    approval_id=approval_id,
                    announced=bool(announced),
                    reply=answer.text,
                )
                announcement = (
                    f"announced as chat message {announced.id}"
                    if announced
                    else "NOT announced in the chat"
                )
                return f"; approval {why}: {announcement}, approve answered {answer.text!r}"
            time.sleep(2.0)
        self.journal("approval.none", lane=self.lane.name, why=why)
        return None

    def _reply_window(self) -> str | None:
        roster = self.bridgectl("sessions")
        line = next(
            (row for row in roster.stdout.splitlines() if self.address and self.address in row),
            None,
        )
        if line is None:
            return None
        found = re.search(r"window ([^)]+)\)", line)
        return found.group(1) if found else None


def _label_of(roster_line: str) -> str | None:
    """`  <label> — <address> — <workspace> (<state>, window <w>)`."""
    head, separator, _ = roster_line.strip().partition(" — ")
    return head.strip() if separator else None


def _pid_of(address: str) -> int | None:
    parts = address.split(":")
    if len(parts) < 3 or not parts[2].isdigit():
        return None
    return int(parts[2])


def _process_alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
