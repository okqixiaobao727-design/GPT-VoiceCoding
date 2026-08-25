# 6. The Claude Session Channel server is Python, and speaks MCP with the standard library alone

Date: 2026-08-21 · Status: Accepted · Source: [#8](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/8)

The Answer Relay's route into a Claude Session is an MCP channel server that Claude Code spawns from a plugin manifest; the bridge only dials its socket. Legacy wrote it in Node (`claude-channel/channel.mjs`), which would put a second runtime in the bundle (ADR 0005) and outside the test suite.

## Decision

**The channel server is Python and imports nothing outside the standard library.** `channel.mjs` is the protocol oracle — wire shapes are transcribed into `protocol.py`, not invented. A pip dependency in a process spawned by Claude Code under an unknown interpreter is a deployment failure waiting to happen. The manual live test against a real Session is the closing gate; the manifest names no interpreter — that is the launch contract's to supply.

## Consequences

One runtime in the bundle; the handshake is covered by `pytest`. The only deliberate byte-level difference from `channel.mjs` is the bridge–channel wire carrying an explicit Relay kind. Handshake drift surfaces as a classified non-delivery, never as silence.

This route depends on launch-time injection and serves the pre-#67 Session definition; the inbox-socket route replaces it once [#71](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/71) proves it.
