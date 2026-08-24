# 10. The seam architecture stays, and the first generation is the behaviour spec it must satisfy

Date: 2026-08-25

Status: Accepted

Taken in: [Map: v1.0 launch with legacy as the behaviour spec](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/58)

## Context

This repository was built in two days from an architecture spec — module map, seams,
vocabulary, repo layout — locked by the first wayfinding effort. In the four days that
followed, twenty-three bug tickets were opened in three waves. Every one was found by a
human running the product against the real environment; not one was found by the test
suite, which stayed green throughout.

Most of those tickets describe behaviour the first generation already had: launch
identity and retry, the Reply Window persisted rather than inferred, a launch that logs,
restore that re-establishes adapter registration, and a launch that carries the words it
was started for (ADR 0009). The rewrite did not reject these behaviours; it never saw
them. The migration inventory that fed the build issues classified the old modules by
*mechanism* — what could be transplanted — and the build issues were then derived from
the seams. Nothing derived them from what the product had been proved to do, and nothing
in this repository pointed a builder at the code that could have told them.

The repair phase that followed was scoped by a wiring audit, which asks whether what was
built is connected. It cannot ask whether what was built is what the product needs, so it
bounded the wrong defect class, and each batch it ordered was followed by another real
run and another batch.

## Decision

**The first generation is the behaviour spec.** `GPT-VoiceCoding-legacy` at `1d32845` is
the record of what the product must do. Any change that adds or repairs runtime behaviour
cites the legacy implementation of the same behaviour, and states whether it is ported,
adapted, or dropped with a reason. Proven legacy code is ported into the seams in
preference to being rewritten. `CLAUDE.md` carries the rule and the clone's location.

**The seam architecture stays.** ADR 0001's hub-and-spoke shape, the seams, and the
vocabulary are the part of the rewrite that was right. Reverting to the legacy monolith
as the code base and grafting the new pieces onto it was considered and rejected.

**The real-environment run is the exit criterion.** A repair phase ends when the
automated acceptance against the real far side is green on the maintainer's machine —
not when a list of tickets is closed. The fake-far-side suites are regression nets.

## Consequences

**Every repair brief gets longer by one line, and that line is the point.** A brief
without a legacy citation is incomplete. "Legacy has no such behaviour" is an acceptable
citation; silence is not.

**The gen-1 remnants stay on disk until the repair is done.** They are what the
citations point at.

**A parity audit precedes the next repair charter.** The scope of repair is the gap
table between the two generations, journey by journey, not the findings of any audit that
looks only at this repository.

**This decision does not reopen ADR 0001.** Where a legacy behaviour cannot be expressed
behind the existing seams, the brief says so and the seam question is raised on its own —
it is not settled by silently porting the old boundary along with the behaviour.
