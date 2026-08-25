# 1. Bridge Core is a hub; everything else is a deep module behind a seam

Date: 2026-08-20 · Status: Accepted · Source: [module map and seams](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/issues/18)

The reference implementation let policy leak into mechanism: Stop Notice delivery was hard-wired to one GUI socket ([#15](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/issues/15)), and nothing owned "exactly one call is up" ([#16](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/issues/16)).

## Decision

**`core` — Bridge Core — owns all policy**: Stop Notice escalation, Relay queueing against the Reply Window, the Approval budget and fallback, the one-call invariant, switch adjudication. Everything else is a deep module reached through a seam:

| Seam | Adapters |
| --- | --- |
| Call | bridge-owned realtime call — the only adapter shipped; the GUI Live Driver is not migrated |
| Agent | Codex, Claude — one interface, two adapters |
| Session Launcher | direct child, tmux — parked in v1.0 (#72); launching and conversing stay orthogonal seams |
| Companion Channel | Telegram (generic, public); a deployment's own wiring is not |
| Menu-bar shell | Swift, thin — a control-plane surface plus process parenthood (spawn, health, restart), not a module |

There is no Session module: the registry is Bridge Core state, conversing is the Agent seam.

Splitting principles:

1. **Policy in the hub, mechanism in the spokes.** Bridge Core imports no protocol library; it speaks seam verbs only.
2. **Seams only where something varies** — each names two adapters, or one shipped plus one historical.
3. **The hub may have internal components** (escalation, relay queue, approval budget, persistence) but no new external seams.
4. **The hub is fully testable against fakes** — no network, no audio.

The engine is one Python asyncio process, a direct child of the menu-bar shell (ADR 0005). Bridge Core memory is the single source of truth; the durable subset is written by one internal storage component and read by nothing else.

Seam responsibilities the verbs encode: Reply-Window queueing is Bridge Core policy — adapters deliver, never queue. Classifying inbound Companion Channel text (control command / Answer Relay / delegation) is Bridge Core's job. The Reply Window level is *asked for* synchronously at registration and only its changes are reported afterwards, because an event raised before the roster holds the Session is dropped and never repeated ([#27](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/27)). Seam verbs extend only through a ruling with a use case behind it.

## Consequences

`core` may not import `adapters`; no protocol library may be imported from `core`; `seams` imports neither. `tests/test_architecture.py` enforces all three.

The hub is named **Bridge Core**; "Bridge Control Center" was rejected as a third `Control`-prefixed term.
