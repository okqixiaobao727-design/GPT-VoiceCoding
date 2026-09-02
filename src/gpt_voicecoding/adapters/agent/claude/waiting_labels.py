"""Which of `waiting`'s five causes this one is, read from Claude Code's own label.

**`status: "waiting"` is not the permission dialog** (#150). Five distinct
things write it, and one of them is a slash-command picker the user is sitting
in front of. Announcing every one of them as a Stop called the user about their
own keyboard; announcing it with no reason left the notice unanswerable.

**Claude Code already publishes which cause it is.** It writes a `waitingFor`
string beside `status`, in the same record write, and `claude agents --json`
copies it verbatim onto a `waiting` row. Both this lane's readers carry it, and
both ask this module what it means — one classification, so a Stop raised by the
Reply Window sweep and a row read off the roster cannot disagree.

**The rule is legacy's, ported onto a different source of truth** (ADR 0010).
`legacy@1d32845:bridge/daemon.py:130-142,1470-1479` raised a Claude Stop from
hook events behind a validated whitelist: `Stop` is a finished turn,
`Notification` counts only with subtype `permission_prompt`, and every subtype
that "was never observed … fails closed rather than being guessed into a Session
Stop". v2 swapped those events for a polled `status` and **dropped** the
whitelist with them, which turned a fail-closed rule into a fail-open one. The
three dispositions below are that whitelist re-expressed over `waitingFor`:

* `NEVER_A_STOP` — the user is driving their own TUI. No Stop, and no
  `needs_the_user`.
* `NAMED_NOW` — a wait only the user or something outside the Session can end,
  and the label is enough to say which. Announced as it always was.
* `CATCH_UP` — a wait this reader cannot name yet: the label proves a wait and
  the content lives in the transcript (`input needed`), or the label is one
  nobody has measured, or there is no label at all. The caller re-reads on its
  own cadence inside a budget rather than guessing either way — legacy's honest
  `unknown` behind a *confirmed* wait (`legacy@1d32845:bridge/daemon.py:191,
  194-211`), whose precondition v2 had lost.

**A label this build has never seen is `CATCH_UP`, never a guess.** A whitelist
rather than a blacklist, for the same reason `window.py` reads its statuses that
way: the set moves with the vendor's builds, and the harm of inventing a Stop is
one the user pays for.

**No version gate lives here.** The table is documentation for the next
re-probe, exactly as `registry.PROVEN_AGAINST_VERSION` and
`discovery.PROVEN_AGAINST_VERSION` are; the registry reader's gate is
`peerProtocol` and the roster reader has none by #71's decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from gpt_voicecoding.seams.agent import SANDBOX_TOOL_NAME, WaitingFor, WaitingKind

#: The Claude Code build every label below was read off, from the label function
#: at offset 178702064 of the 2.1.251 bundle. Documentation, not a gate.
PROVEN_AGAINST_VERSION: Final = "2.1.251"


class StopDisposition(StrEnum):
    """What a `waiting` record is worth as a Stop Notice, once its label is read.

    Named for the decision rather than for the label, because the decision is
    the thing both readers and the sweep act on: the label is an input to it and
    moves with the vendor's builds, while these three are this product's own
    vocabulary for *tell the user*, *say nothing*, and *not yet*.
    """

    #: The user is at their own keyboard driving their own dialog. Not a Stop.
    NEVER_A_STOP = "never_a_stop"
    #: A wait the label names well enough to announce on sight.
    NAMED_NOW = "named_now"
    #: A wait that is real and not yet nameable. Re-read, do not guess.
    CATCH_UP = "catch_up"


#: The seam's own words for "something is being waited on and this reader cannot
#: yet say what": `UNKNOWN` is only honest beside `caught_up=False`, and
#: `caught_up=False` is the documented instruction to *ask again, never guess*.
#: One value rather than one per use, so the pair cannot drift apart.
NOTHING_READ_YET: Final = WaitingFor(kind=WaitingKind.UNKNOWN, caught_up=False)


@dataclass(frozen=True, slots=True)
class LabelReading:
    """One label's reading: what to do about it, and what it says."""

    disposition: StopDisposition
    waiting_for: WaitingFor


#: Every label measured on `PROVEN_AGAINST_VERSION`, and what each one is.
#:
#: `permission prompt` is also **Claude Code's own default for a dialog kind
#: that is not in its label map**, which is why an unrecognised *dialog* still
#: arrives here as a permission while an unrecognised *label* does not: the
#: vendor made that fallback, deliberately, on the side of the person having to
#: answer something.
#:
#: `worker request` is a swarm worker forwarding its permission ask to the
#: lead's mailbox, and **no record either reader can see ever carries it**
#: (#156, measured live on 2.1.251 on 2026-08-31, `tmux` teammate backend).
#: An out-of-process teammate is its own process, launched with `--agent-id`
#: and `--team-name`, and it writes **no registry record at all**: while one sat
#: parked on "Waiting for team lead approval", `~/.claude/sessions` held no file
#: for its pid and `claude agents --json` did not list it. The lead's record
#: carried `permission prompt` for that same request, so the wait is announced
#: today, named, by the label above. The entry stays `CATCH_UP` because that is
#: the harmless reading of a label that cannot arrive — not because anything is
#: still owed. Unmeasured, and left that way deliberately: the `in-process` and
#: `iterm2` backends, and a teammate the vendor places elsewhere (its own
#: vocabulary has `where: "remote"`). An in-process teammate shares the lead's
#: app state, so if the forward path runs there at all the label would ride the
#: *lead's* record — the one case that could put it in front of a reader.
#:
#: `goal proposal` was measured: Claude itself
#: ignores one while busy, so nobody is blocked on it and it is never a Stop.
LABELS: Final[dict[str, LabelReading]] = {
    # An AskUserQuestion dialog, an MCP elicitation or teammate setup. The
    # question and its options are in the transcript, not in this word, and
    # `CONTEXT.md` names those as the notice's content — so the reader waits for
    # them rather than announcing a question it cannot repeat.
    "input needed": LabelReading(
        disposition=StopDisposition.CATCH_UP,
        waiting_for=NOTHING_READ_YET,
    ),
    "permission prompt": LabelReading(
        disposition=StopDisposition.NAMED_NOW,
        waiting_for=WaitingFor(kind=WaitingKind.PERMISSION),
    ),
    "sandbox request": LabelReading(
        disposition=StopDisposition.NAMED_NOW,
        waiting_for=WaitingFor(kind=WaitingKind.PERMISSION, tool_name=SANDBOX_TOOL_NAME),
    ),
    "worker request": LabelReading(
        disposition=StopDisposition.CATCH_UP,
        waiting_for=NOTHING_READ_YET,
    ),
    "dialog open": LabelReading(disposition=StopDisposition.NEVER_A_STOP, waiting_for=WaitingFor()),
    "goal proposal": LabelReading(
        disposition=StopDisposition.NEVER_A_STOP, waiting_for=WaitingFor()
    ),
}

#: What a record that said nothing, or said something nobody has measured, is
#: worth. The same answer for both, because they are the same fact: this reader
#: has not caught up with what the Session is waiting for.
UNMEASURED: Final = LabelReading(
    disposition=StopDisposition.CATCH_UP,
    waiting_for=NOTHING_READ_YET,
)


def classify(label: str | None) -> LabelReading:
    """What one `waitingFor` label means, or the honest *ask again* for one that is not read.

    Pure, and told its label rather than fetching one: the registry reader and
    the roster reader each have their own way of getting the string, and exactly
    one way of reading it.
    """
    return LABELS.get((label or "").strip(), UNMEASURED)
