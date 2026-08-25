# 10. The seam architecture stays, and the first generation is the behaviour spec it must satisfy

Date: 2026-08-25 · Status: Accepted · Source: [#58](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/58)

This repository was built in two days from an architecture spec. In the four days after, twenty-three bug tickets were found by a human against the real environment while the suite stayed green — most describing behaviour the first generation already had. The build issues were derived from the seams, and nothing pointed a builder at the code that proved what the product must do.

## Decision

**The first generation is the behaviour spec.** `GPT-VoiceCoding-legacy` at `1d32845` is the record of what the product must do. Every change that adds or repairs runtime behaviour cites the legacy implementation of the same behaviour and states *ported*, *adapted*, or *dropped, because …*; "legacy has no such behaviour" is a citation, silence is not. `CLAUDE.md` carries the rule.

**The seam architecture stays.** ADR 0001's shape, the seams and the vocabulary are the part of the rewrite that was right; reverting to the legacy monolith was rejected. Where a legacy behaviour cannot be expressed behind the existing seams, the brief says so and the seam question is raised on its own.

**The real-environment run is the exit criterion**: a phase ends when the automated acceptance is green on the maintainer's machine. Fake-far-side suites are regression nets.

## Consequences

The gen-1 remnants stay on disk until the port is done — they are what the citations point at.
