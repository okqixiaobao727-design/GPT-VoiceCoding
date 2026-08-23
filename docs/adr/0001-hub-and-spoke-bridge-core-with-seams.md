# 1. Bridge Core is a hub; everything else is a deep module behind a seam

Date: 2026-08-20

Status: Accepted

Amended: 2026-08-20 — the Session Launcher's verb list gains `close`. See
[Seam verbs](#seam-verbs).

Carried over from: [Grilling: module map and seams for the new core](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/issues/18)

## Context

The reference implementation grew its structure by accretion, and it showed. Stop
Notice delivery was hard-wired to the GUI app's `ipc.sock`, so the moment the
voice route stopped going through that app, every notice failed with
`Connection refused` and the voice side fell back to polling `progress` — the
expensive failure mode measured in [#15](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/issues/15). Nothing owned "exactly one
call is up" either, so an escalation path pressed the GUI toggle while the system
already owned a call and the two assistants talked to each other in a loop
([#16](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/issues/16)).

Both failures have the same shape: policy had leaked into the thing that happened
to implement it.

## Decision

**One hub, `core` — Bridge Core — owns all policy.** The Stop Notice escalation
pipeline, Relay queueing against the Reply Window, the Approval Relay budget and
its fallback, the one-call-at-a-time invariant, and switch adjudication all live
there and nowhere else. Everything else is a deep module Bridge Core reaches
through a seam.

| Module / seam | Adapters | Notes |
| --- | --- | --- |
| Call | bridge-owned realtime call (aiortc) — the **only** adapter shipped | the GUI Live Driver is not migrated; the seam survives because it is what let the second adapter replace the first |
| Agent | Codex (app-server JSON-RPC), Claude (MCP server + peer socket + `PermissionRequest` hook) | one interface, two adapters |
| Session Launcher | tmux (optional), direct child process | launching and conversing are orthogonal seams |
| Companion Channel | capability public; the Telegram adapter is generic and public, a deployment's own wiring is not | see ADR 0003 |
| Menu-bar shell | Swift, thin | **not** a module with a private protocol — just another control-plane surface; its only extra relationship to the engine is process parenthood (spawn, health, restart) |

**There is no Session module.** The Session *registry* is Bridge Core state,
*launching* is the Session Launcher seam, and *conversing* is the Agent seam.

### The four splitting principles

1. **Policy in the hub, mechanism in the spokes.** Hard test: no protocol library
   (WebRTC, Telegram API, JSON-RPC framing, tmux) may ever be imported by Bridge
   Core. It speaks only seam verbs.
2. **Seams only where something varies.** Every seam above names at least two
   adapters, or one shipped plus one historical. No speculative seams.
3. **The hub may have internal components** — escalation pipeline, relay queue,
   approval budget, persistence — separately testable, but no new *external*
   seams. Outsiders see one Bridge Core.
4. **The hub must be fully testable against fakes.** All policy — interlock,
   queueing, budget fallback — exercisable with a fake call, fake agents and a
   fake channel: no network, no audio.

### Process topology and state

The engine is a **single Python asyncio process**, spawned as a direct child of
the menu-bar shell from inside the app bundle (ADR 0005).

Bridge Core memory is the **single source of truth**: switch state, the Session
registry, the undelivered Relay queue. No module keeps a copy; all surfaces query
the hub. The durable subset — switch state and the Session registry — is persisted
by an internal storage component only Bridge Core touches. Nothing else may read
those files, or the disk becomes a second truth.

### Seam verbs

- **Call**: `ensure_call` / `end_call` (the two halves of the Live Toggle) ·
  `call_state` · `speak(text)` · `delegate(text) -> reply`. Events up: user-speech
  transcript, call started / ended / dropped. The one-call invariant lives *above*
  this seam.
- **Agent**: `answer_relay(session, text)` · `notice_relay(session, text)` ·
  `approval_relay(session, request, verdict)` · `reply_window(session)`. Events
  up: Session stopped, awaiting approval, Reply Window changed, delivery
  receipts (delivered / held / expired). Reply-Window queueing is Bridge Core
  policy — adapters deliver, never queue.
- **Session Launcher**: `launch` a Session into a workspace and report the
  launch outcome; `close` a Session and report what closed. Pane semantics
  never cross this seam.
- **Companion Channel**: `send(message)` · `verify`. Events up: inbound user text.
  Classifying that text — control-plane command vs Answer Relay vs delegation — is
  Bridge Core's job, never the channel's.
- **Control plane**: an interface Bridge Core *exposes* (JSON over a Unix domain
  socket). Surfaces: menu-bar shell, `bridgectl`, Companion Channel, spoken
  commands in-call. Never gated by switches — see ADR 0002.

`close` was added to the Session Launcher on 2026-08-20. This list was written
before that verb was synced across the build issues, and the authority for it is
the [migration inventory](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/issues/27)'s `closing.md` dispositions: exactly one session
target, fail closed on a missing or stale identity, idempotent repeats, and
truthful per-child outcomes only where the adapter actually owns child
destinations. Closing was never missing from the product — the reference
implementation had a `close` action, wrongly gated behind the Duty Switch — it
was missing from this list, which is what an adapter implements against.

`reply_window` was added to the Agent seam on 2026-08-24, under [#27](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/27),
which is the adjudication this list's closed set requires: a seam's verbs extend
only through a ruling with a use case behind it, and that is the ruling.

The use case is that **an event cannot bootstrap a level.** An adapter is
registered before Bridge Core holds the Session, so a `ReplyWindowChanged`
raised at registration is dropped as belonging to a Session nobody knows — and
having been recorded by the adapter as reported, it is never repeated. A Session
that was already idle when it was launched therefore stayed at the fail-closed
default forever, unreachable while perfectly healthy. So the level is *asked
for*, once, the instant the roster holds the Session, and only its changes are
reported. The verb is synchronous alone with `supported_routes`, because an
await would reopen the very gap it exists to close.

This is the same seam as #26 seen from the other side. Registration is the one
point where a Session's reachability is settled: restore refuses to claim what it
cannot establish, and launch establishes what it can.

## Consequences

The repository layout is this decision made physical: `core` may not import
`adapters`, no protocol library may be imported from `core`, and `seams` — the
contract both sides implement against — imports neither of them, so the
dependency runs one way only. `tests/test_architecture.py` enforces all three, so
principle 1 fails CI rather than eroding quietly.

Naming: the hub is **Bridge Core**. "Bridge Control Center" was proposed and
rejected — a third `Control`-prefixed term would collide with Control Plane and
Control Panel. In v0 the Control Panel *is* the menu-bar dropdown plus
`bridgectl`; there is no separate UI.
