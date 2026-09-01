# 17. A missed call is briefed from a fresh reading, never from replayed events

Date: 2026-09-01 · Status: Accepted · Source: [#165](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/165) Q5/Q10, [#167](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/167)

Two calls a minute is worse than one call late. The reference implementation had no
cool-down at all and recorded the incident that shape produces: a Stop Notice arriving
right after a dial opened a second call, and two notices were lost confirming the first
(`legacy@1d32845:bridge/livecall.py:561-581`, 2026-08-11). This system, until this
decision, could dial again the second after a hang-up (`core/interlock.py:124-128`
forgets when the call ended), and spoke every mid-call event into the call the moment it
arrived, whatever the assistant was saying.

Introducing a **Cool-down** raises the question every queue raises: what happens to the
events that arrive while the system may not sound? The obvious answer — hold them and
replay them when the interval ends — was rejected.

## Decision

**The Call Keeper remembers only that something is owed, never what.** A Session event
during Cool-down, or while the assistant is speaking, sets a flag: a dial is owed, or a
word to the Focus Session is owed. When the moment comes — Cool-down elapsed, both sides
silent — the Keeper asks Briefing for a **fresh reading** of the Sessions and acts on
that alone: it dials and briefs what needs the user now; it speaks the Focus Session's
brief as it stands now; or, if nothing needs the user any more, it does nothing and
clears the flag. Three events from one Session become one brief of its latest state.
The same one rule paces the ring for non-Focus events: at most one per interval, the
rest folded into the roster the model reads on request.

The same interval value serves all three moments — the wait before a dial, the gap
between mid-call announcements, the gap between rings — so the Keeper keeps a single
"last sounded at" and the user configures one number (`[policy]`, default 30 s).

Any end of a call starts Cool-down: a manual or voice hang-up, the Silence Ceiling, a
dropped transport, and a dial that failed. A failed dial also clears the owed flag; the
next event re-arms it, so one event buys at most one attempt and an outage cannot dial
every thirty seconds forever. The user's own Live Toggle ignores Cool-down entirely.

## Why not replay

Replay needs a ledger of what was announced and what was not. Legacy carried one
(`CurrentSessionStop`, `legacy@1d32845:bridge/store.py:869-897`) with supersession
rules to keep it honest (`store.py:2768-2814`), and #161 established that this system
cannot keep such a ledger truthful: the action that invalidates an entry — an adapter
handing an unanswered dialog back to the terminal — happens where Bridge Core cannot
see it. A fresh reading is correct by construction: it cannot announce a question the
user already answered, and it cannot lose a question the user has not, because the
Session still shows it. What is given up is narration of the past ("while you were
away, X asked and then withdrew"); the flow does not want it.

## Consequences

The Keeper's interface carries no content: `wake(focus)` says only that something
happened and whether it concerns the Focus Session. Briefing is consulted at the moment
of sounding, through one reading dependency. A Focus announcement that never finds a
gap waits indefinitely — it cannot go stale, because it is composed when it is spoken.
Bridge Core's reconciliation-on-transition path (`_owe_reconciliation`,
`_announce_current_stops`) is retired: a switch turning on is one more `wake`.

Legacy classification: the cool-down itself is **new** (legacy: none, cited above); the
"newer state replaces the older notice" principle is **adapted** from legacy
supersession into the fresh-reading rule; the durable ledger is **dropped, because**
#161 showed it cannot be kept truthful here.
