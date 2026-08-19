# GPT-VoiceCoding

Voice-controlling terminal coding agents through a realtime voice call: the system
speaks agent progress to you, and carries your spoken instructions back to the
agents.

> **Status: skeleton.** The architecture is locked and this repository is its
> layout; the engine is not built yet. Nothing here runs. If you are looking for
> working software, the first-generation implementation lives at
> [GPT-VoiceCoding-legacy](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy).

## What it is

You are away from the keyboard. A Claude Code or Codex session stops and needs
you. GPT-VoiceCoding tells you — out loud, in a voice call it holds open — and
carries your spoken answer back into the session. It launches sessions, watches
them, relays your words into them, and answers their permission prompts with your
verdict.

The voice call is owned by this system directly, over `codex app-server`'s realtime
route. There is no API key to supply.

## Shape

A thin Swift menu-bar app spawns a single Python asyncio engine from inside its own
bundle. That engine is **Bridge Core**: it owns every policy and holds the single
source of truth, and reaches everything else — the call, the coding agents, session
launching, the Companion Channel — through seams with swappable adapters.

```
src/gpt_voicecoding/
├── core/        Bridge Core — the hub. All policy, all state.
├── seams/       The interfaces Bridge Core calls through.
├── adapters/    The implementations behind them. Protocol libraries live only here.
└── cli/         bridgectl — a control-plane surface.
shell/           The Swift menu-bar shell (see ADR 0005).
```

Start with [`docs/adr/0001`](docs/adr/0001-hub-and-spoke-bridge-core-with-seams.md).

## Platform

macOS only. The microphone grant, the menu-bar shell and the push path are all
macOS-shaped; cross-platform support is not planned.

## Reading order

1. [`CONTEXT.md`](CONTEXT.md) — the vocabulary. Every term in this repo means what
   it says there.
2. [`docs/adr/`](docs/adr/README.md) — the decisions, and where each one came from.

## Contributing

v0 targets developers: build from source, no signed release, no notarization
(ADR 0005). Issues and pull requests are welcome once there is something to build
on — the build issues land next.

## Licence

[MIT](LICENSE).
