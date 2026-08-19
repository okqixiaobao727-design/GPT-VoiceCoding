# 3. The engine reports what it loaded, and liveness checks read that answer

Date: 2026-08-19

Status: Accepted

Carried over from: [ADR 0003 of the reference implementation](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/blob/main/docs/adr/0003-companion-channel-liveness-is-verifiable.md),
whose evidence section holds the full failure account. The principle is carried;
its reference-implementation mechanics (`install.sh`, `preflight.sh`,
`bridgectl status`) are not.

## Context

In the reference implementation the Companion Channel was dead on the maintainer's
machine for a day while three separate guards said nothing. The decisive one:
`bridgectl status` printed `config.companion_channel.module` — the value the
*client* had just read from disk — and a reachable daemon proved only that some
daemon was answering. A daemon started before the wiring landed, or one whose
channel module failed to import, was indistinguishable from a healthy one. **The
line looked like an observation and was an echo.**

The install-time guardrail could not help either: it was edge-triggered, comparing
before and after an install, so once the loss had happened the steady state of
"should have a channel, has none" was invisible to it forever.

## Decision

**A liveness check reads what the engine actually loaded, never what a
configuration file says it should have loaded.** The engine's `ping` reply carries
the loaded Companion Channel adapter: the module string when a real adapter is
loaded, and the empty string when it loaded the null adapter. Empty string is a
*known* state, distinct from the field being absent, which means an engine too old
to have been asked.

Any surface reporting channel health reports the engine's answer, and **names the
disagreement** when configuration and engine differ rather than silently picking
one.

**The check is level-triggered and has three outcomes, not two:**

- **pass** — configuration names an adapter and the engine loaded that adapter.
- **fail** — they disagree, or the engine cannot be reached to answer.
- **manual** — nothing is configured anywhere.

The third state is the one that matters. A machine deliberately running without a
Companion Channel and a machine that silently lost one look identical from here.
A silent pass is what let the original outage survive; a fail would cry wolf on
every machine that never had a channel. **A manual check is the honest shape of a
question the check cannot answer**, and it is handed to the operator.

## Consequences

This generalises past the Companion Channel: it is the rule for every seam whose
adapter is pluggable. `verify` is a seam verb (ADR 0001) for exactly this reason.

What it still cannot detect is a machine that ought to have an adapter and has
never declared one — there is nothing on disk to compare against. That residual
case is precisely what the manual outcome hands to the operator.
