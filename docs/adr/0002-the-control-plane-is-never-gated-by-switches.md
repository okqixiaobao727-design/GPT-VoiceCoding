# 2. The control plane is never gated by switches

Date: 2026-08-19

Status: Accepted

Carried over from: [ADR 0006 of the reference implementation](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/blob/main/docs/adr/0006-the-control-plane-is-never-gated-by-switches.md).
Carried **verbatim in force** — this decision was re-affirmed unchanged when the
module map was locked in [#18](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/issues/18).

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

## Consequences

The engine's socket actions are grouped into a control-plane set and a
business-plane set, and no control-plane action may ever consult switch state to
decide whether to answer.
