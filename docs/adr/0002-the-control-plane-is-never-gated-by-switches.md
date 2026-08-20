# 2. The control plane is never gated by switches

Date: 2026-08-19

Status: Accepted

Amended: 2026-08-20 — the Live Toggle is a control-plane action, so it is not
gated either. See [The boundary](#the-boundary).

Carried over from: [ADR 0006 of the reference implementation](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/blob/main/docs/adr/0006-the-control-plane-is-never-gated-by-switches.md).
Carried **in force** — this decision was re-affirmed unchanged when the module
map was locked in [#18](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/issues/18). The decision itself has never been
narrowed or widened; the 2026-08-20 amendment below adds no exception and
removes none, it states the boundary the original enumeration was reaching for.

## Context

The forcing scenario: with the Duty Switch off suppressing Companion Channel
input, a user away from the computer who had switched Duty off could never switch
it back on remotely — locked out by the very switch meant to protect them.

The alternative considered and rejected was per-switch exemption lists, which is
the kind of special-casing that rots.

## Decision

The switch hierarchy — Duty Switch as master, Voice Switch and Message Switch
beneath it, flat Feature Switches under those — gates only **business** behaviour:
speaking into the Live Call, pushing Companion Channel messages, Relaying into
Sessions.

Status queries and switch flips — the **control plane** — are always accepted, from
every surface: the Control Panel, the Companion Channel, `bridgectl`, or spoken
commands in a Live Call.

One rule, absolute: **switches never gate the ability to control the switches
themselves.**

## The boundary

Amended 2026-08-20, while building the policy pipelines. The original wording
enumerated two control-plane actions — status queries and switch flips — and the
Live Toggle is neither, which made it look gateable by the Voice Switch. It is
not. The line the enumeration was reaching for is this:

**Switches constrain the system's own reach. User-initiated control-plane
actions are never adjudicated.**

Duty, Voice and Message answer *may the system do this unbidden* — escalate into
a call, push a notice, open a voice surface nobody asked for. They do not answer
*may the user do this*. The Live Toggle is the user touching the call with the
system as the instrument, exactly like flipping a switch, so it passes without
consulting switch state. Gating it produces the indefensible case the original
forcing scenario is made of: Voice is flipped off while a call is up, and the
user's explicit "end this call" is refused by the very switch that says the
system should be quiet.

This is a boundary, not an exemption list — the thing the original decision
rejected. Nothing is added to a list; the question "whose reach is this" is
asked once, and the answer decides.

The Live Toggle is still bound by the one-call-at-a-time invariant, which is a
different kind of constraint: not permission, but the physical fact that two
calls on shared speakers talk to each other.

## Consequences

The engine's socket actions are grouped into a control-plane set and a
business-plane set, and no control-plane action may ever consult switch state to
decide whether to answer.
