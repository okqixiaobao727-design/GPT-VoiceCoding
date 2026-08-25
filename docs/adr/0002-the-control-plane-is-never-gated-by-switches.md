# 2. The control plane is never gated by switches

Date: 2026-08-19 · Status: Accepted · Source: [legacy ADR 0006](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/blob/main/docs/adr/0006-the-control-plane-is-never-gated-by-switches.md), re-affirmed in [#18](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/issues/18)

A user who switched Duty off from the Companion Channel could never switch it back on remotely — locked out by the switch meant to protect them. Per-switch exemption lists were rejected as special-casing that rots.

## Decision

**Switches constrain the system's own reach; user-initiated control-plane actions are never adjudicated.** Duty, Voice and Message answer *may the system do this unbidden*. Status queries, switch flips and the Live Toggle are the user acting with the system as instrument, so they pass from every surface without consulting switch state. The Live Toggle remains bound by the one-call invariant — a physical constraint, not a permission.

## Consequences

Socket actions are grouped into a control-plane set and a business-plane set; no control-plane action may consult switch state to decide whether to answer.
