# 3. The engine reports what it loaded, and liveness checks read that answer

Date: 2026-08-19 · Status: Accepted · Source: [legacy ADR 0003](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/blob/main/docs/adr/0003-companion-channel-liveness-is-verifiable.md) (full failure account)

The Companion Channel was dead for a day while every guard said nothing: `status` echoed the config file the client had just read, and a reachable daemon proved only that some daemon answered.

## Decision

**A liveness check reads what the engine actually loaded, never what configuration says it should have.** `ping` carries the loaded Companion Channel adapter — the module string, or the empty string for the null adapter (a known state, distinct from the field being absent). A surface that finds configuration and engine disagreeing names the disagreement.

The check is level-triggered with three outcomes: **pass** (configured and loaded agree), **fail** (they disagree, or no answer), **manual** (nothing configured anywhere — the honest shape of a question the check cannot answer, handed to the operator).

## Consequences

This is the rule for every seam with a pluggable adapter; `verify` is a seam verb for this reason.
