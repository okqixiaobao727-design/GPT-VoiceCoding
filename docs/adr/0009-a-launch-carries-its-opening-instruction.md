# 9. A launch carries its Opening Instruction, and a bare launch is refused

Date: 2026-08-25

Status: Accepted

Taken in: [The engine never attaches to a launched Codex Session's app-server, so relays deadlock and delivery fails](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/39)

## Context

The Session Launcher seam was built with a Session Label and no words. A launch
brought a Session into existence; whatever the user wanted it to do arrived
afterwards, as an Answer Relay. That reads as the clean separation — launching and
speaking are different capabilities — and it is what the rewrite implemented.

It does not survive contact with either agent.

**On the Codex lane it deadlocks, and this was measured rather than reasoned.**
Against codex 0.149.1, during the triage of #39:

- `thread/resume` against a thread that has done no work is refused with
  `no rollout found`. The rollout is a file, and a thread that has never worked
  has not got one.
- An **unsubscribed** client never receives `thread/status/changed`. Two clients
  were attached and neither had resumed; neither heard it. What does reach an
  unsubscribed client is `thread/started` and `remoteControl/status/changed`, and
  neither of those is a status transition.

The Codex Agent adapter had exactly one retry trigger for a blocked subscription,
and it was `thread/status/changed`. So: subscribing needs a rollout, a rollout
needs the thread to have worked, working needs a Relay to land, a Relay needs the
Reply Window open, the window needs a status event, and the status event needs
the subscription. A closed loop, entered by every Session the product launches.

The module carried a comment asserting that lifecycle notifications "arrive
whether or not this client is subscribed, which is what makes the retry possible
at all". That assertion is false, and it is why the defect was invisible for as
long as it was: the fake app-server in the suite answers `thread/resume`
unconditionally, so no test could ever reach the branch, and the design note in
the tests recorded the same false assumption as fact.

**On the Claude lane it does not deadlock — it simply never happens.** #37 records
the same absence from the other side: a launched Claude Session "can never be
given any instruction at all — not a follow-up, and not even a first one". Two
issues, opened independently, describing one missing concept.

**The first generation did not have this problem.** The gen-1 bridge carried an
initial instruction on its launch request and appended it, shell-quoted, to the
launch command line, with a validator that checked Unicode categories, trimmed
outer whitespace, enforced a byte ceiling, and refused rather than repaired. The
capability was not rejected during the rewrite; it was dropped without being
noticed, which makes this a regression rather than a design change.

## Decision

**A `LaunchRequest` carries an Opening Instruction, and it is required.** Each
Session Launcher passes it to its agent as the trailing positional prompt
argument. Both CLIs already take one — `codex [options] [prompt]`,
`claude [options] [prompt]` — so this is one argument per lane and no new
mechanism.

**A bare launch is refused at the seam.** This is the part that is a decision
rather than a restoration: gen-1 made the instruction optional, and this repo
makes it required. An optional field would leave the deadlock live behind it —
reachable by anyone who launched without words, failing in the one direction
this product exists to serve. Required makes the path unreachable, which is a
stronger guarantee than fixing the loop and a much smaller change than fixing it
properly.

**The deadlock itself is therefore not fixed, and that is deliberate.** It stays
in the code, unreachable. Anything that later re-introduces a bare launch
re-introduces it, which is the cost this decision accepts in exchange for not
redesigning the Reply Window gate, the relay queue and the subscription trigger
in a repair release.

**Subscription retries over a bounded window.** Measured: a `thread/resume`
issued at the same moment as the first turn is refused, and one issued 160 ms
later succeeds — the rollout appears when the turn *starts*, not when it
finishes. So an Opening Instruction alone does not fix this; without a retry the
adapter loses the race every time. The bound is a named constant with headroom
over the measured figure, in the style of the launcher's other bounded waits. A
subscription that never succeeds inside it is a failed launch: registering a
Session whose Reply Window can never open is what produced #39's symptom in the
first place.

**It is called an Opening Instruction, not an initial task.** `task` is a word
`CONTEXT.md` already reserves against for a Session. Restoring a gen-1 capability
does not restore a name the glossary has since ruled out.

## Consequences

**"Open a Session and leave it waiting" is no longer a supported use.** The
product's launch verb now means "start a Session on these words". Anyone wanting
an idle Session starts one themselves; the product does not offer it. This is a
narrowing of the seam and is meant to be read as one.

**The Session Launcher can no longer be described as label-only.** #37 called
that "by design"; it was, and the design is hereby changed. Documentation and
tests that pin label-only behaviour are wrong rather than stale.

**One change closes two issues, and that is the honest shape.** #39 and #37 are
the same missing concept on two lanes. Splitting the fix would mean one lane
receiving the Opening Instruction and dropping it, which is precisely what #37
reports.

**The validator is ported by behaviour, not by copy.** Unicode-category
allowlist, outer-whitespace trim, UTF-8 byte ceiling, refuse rather than repair,
and shell-quote at the command line. It is a value that crosses a process
boundary into a shell, so the rule that it is never repaired matters more than
where the code lives: a repaired instruction is a different instruction, and the
user said what they said.

**The fake app-server has to be able to lie the way the real one does.** It
answered `thread/resume` unconditionally, which is why a green suite coexisted
with a lane that could not deliver anything. Any fake that cannot produce
`no rollout found` cannot pin this regression.

**The `lsof` reading in #39's body is not a criterion anyone should reuse.**
`lsof` prints the socket path for a listener and for the server side of an
accepted connection, but for the *client* side it prints only the peer address.
The engine is the client on that hop, so an empty
`lsof -p <engine> | grep <socket dir>` is exactly what a connected engine looks
like. Assert against the adapter's own roster instead.
