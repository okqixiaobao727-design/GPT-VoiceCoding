# Architecture decisions

| ADR | Decision |
| --- | --- |
| [0001](0001-hub-and-spoke-bridge-core-with-seams.md) | Bridge Core is a hub; everything else is a deep module behind a seam |
| [0002](0002-the-control-plane-is-never-gated-by-switches.md) | The control plane is never gated by switches |
| [0003](0003-the-engine-reports-what-it-loaded.md) | The engine reports what it loaded, and liveness checks read that answer |
| [0004](0004-the-engine-owns-its-log.md) | The engine owns its log, so rotation can rename rather than truncate |
| [0005](0005-the-engine-lives-inside-the-app-bundle.md) | The engine lives inside the app bundle, because that is what earns the microphone grant |
| [0006](0006-the-claude-channel-server-is-python-and-stdlib-only.md) | The Claude Session Channel server is Python, and speaks MCP with the standard library alone |
| [0007](0007-the-approval-hook-is-a-session-scoped-plugin.md) | The Claude Approval Relay's hook is registered as a session-scoped plugin |
| [0008](0008-the-direct-child-launcher-is-headless.md) | The direct-child launcher is headless, and visibility is the tmux adapter's job |
| [0009](0009-a-launch-carries-its-opening-instruction.md) | A launch carries its Opening Instruction, and a bare launch is refused |
| [0010](0010-legacy-is-the-behaviour-spec.md) | The seam architecture stays, and the first generation is the behaviour spec it must satisfy |

## Provenance

This repository is the second generation of the product. The decisions above were
taken in the reference implementation,
[GPT-VoiceCoding-legacy](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy), and locked by a wayfinding effort whose
map and resolution comments are the full record:

- [Wayfinder map: open-source pivot to bridge-owned realtime voice](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/issues/11) — the map, its charter decisions, and what was ruled out of scope.

Resolutions that fed the ADRs above, and resolutions that are **not** yet ADRs
because they describe behaviour the build issues carry rather than structure:

| Resolution | Where it landed |
| --- | --- |
| [Module map and seams for the new core](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/issues/18) | ADR 0001 |
| [Packaging the Python engine inside a menu-bar app](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/issues/14) · [Microphone TCC attribution probe](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/issues/24) | ADR 0005 |
| [Term-by-term vocabulary audit](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/issues/20) | `CONTEXT.md` (verbatim) |
| [Name the product and the public repo](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/issues/17) | this repo's name |
| [What does terminal decoupling actually cost?](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/issues/12) | Session Launcher seam (ADR 0001) |
| [Claude peer socket — receipt, preamble, remote-control locality](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/issues/23) · [What carries user-authored mid-turn speech into a running Claude session?](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/issues/25) | peer route retired on [#41](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/41); Answer Relay remains Agent seam behaviour |
| [Does v0 answer permission prompts by voice?](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/issues/26) | Approval Relay behaviour — build issues |
| [Verify no unexpected billing after bridge calls](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/issues/15) | Delegated Turn as the cost lever — build issues |
| [Coexistence check — GUI Live Call alongside a bridge call](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/issues/16) | the one-call invariant (ADR 0001) |
| [Do skills survive into the new architecture?](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/issues/19) | voice house rules move in-core — build issues |
| [How deep can ChatGPT app remote reach into the new bridge?](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/issues/13) | out of scope for v0 |

Legacy ADRs deliberately **not** carried:

- [0001 — launchd supervises the Bridge daemon](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/blob/main/docs/adr/0001-launchd-supervision-for-the-bridge-daemon.md) — superseded by ADR 0005. Running outside a bundle is what loses the microphone grant; the menu-bar shell owns supervision now.
- [0002 — session retention and indexed session reads](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/blob/main/docs/adr/0002-session-retention-and-indexed-session-reads.md) — a schema lesson for a store this repo has not built yet. It belongs in the persistence build issue, not in an ADR of an empty repo.
- [0005 — the Live Driver stays on the GUI](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/blob/main/docs/adr/0005-live-driver-stays-on-the-gui-hotkey-quarantined-as-an-adapter.md) — superseded. Its prophecy came true: the seam it created is what let the bridge-owned call replace the GUI Live Driver, which is not migrated (ADR 0001).
