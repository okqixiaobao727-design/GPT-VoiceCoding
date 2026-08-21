# 6. The Claude Session Channel server is Python, and speaks MCP with the standard library alone

Date: 2026-08-21

Status: Accepted

Taken in: [Build: Claude Agent adapter — Answer Relay (MCP channel)](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/8)

## Context

The Answer Relay's route into a Claude Session is an MCP channel: a small server
that Claude Code itself spawns from a plugin manifest, which declares the
`claude/channel` experimental capability, exposes one `acknowledge_answer` tool,
and pushes `notifications/claude/channel` into the session. The bridge never
starts it — it only dials the private Unix socket that server binds.

The reference implementation wrote that server in Node
(`claude-channel/channel.mjs`, on `@modelcontextprotocol/sdk` and `zod`) and it
is proven in production against Claude Code 2.1.235. The issue left the language
open.

Keeping Node would mean the app bundle (ADR 0005) carries a second runtime and a
`node_modules` tree forever, and the server could never be exercised by this
repository's own test suite. Porting it re-opens a risk that was settled: the
exact shape of the MCP handshake is cheap to get subtly wrong, and CI cannot
catch it because no real Claude Code runs there.

## Decision

**The channel server is Python, and imports nothing outside the standard
library.**

Three things make that safe rather than merely cheaper:

- **`channel.mjs` is the protocol oracle.** The wire shapes — the experimental
  capability key, the notification method, the tool schema, the initialize
  handshake including protocol-version negotiation — are transcribed from the
  working implementation and from the SDK version it pins, not invented here.
  `protocol.py` is the one place they are written down.
- **Standard library only.** This process is spawned by Claude Code from a
  plugin manifest, under whatever interpreter the deployment provides. A
  pip-installed dependency in that position is a deployment failure waiting for
  a machine that lacks it — and this repository already hand-rolls the wires it
  needs (ADR 0001's spokes, and the Codex adapter's own WebSocket).
- **The manual live test is the closing gate.** The re-opened risk is closed by
  running a real answer through a real Claude Code session, not by a green CI.

The manifest names no interpreter. Which Python runs the server is part of the
launch contract the Session Launcher and the bundle own, not a constant here.

## Consequences

The bundle carries one runtime. The channel server is importable, so its
handshake, its socket privacy and its acknowledgement bookkeeping are covered by
the same `pytest` run as everything else.

Any place this Python emits a different byte shape than `channel.mjs` did is a
deliberate, recorded difference — the only one v0 makes is the wire between the
bridge and the channel, which carries an explicit Relay kind instead of
hard-coding one (the defect the migration inventory named).

If the handshake ever drifts, the failure surfaces at the live test and as a
classified non-delivery at runtime — never as silence.
