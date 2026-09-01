# 18. One dial, two audiences: the Voice hears prose, the Call Agent hears its rules

Date: 2026-09-01 · Status: Accepted · Source: [#177](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/177), evidence [#175](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/175) and [#179](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/179)

A codex v3 realtime call is two models: the **Voice** the user hears, and the **Call
Agent** behind it — the only half with tools. `thread/realtime/start` addresses them
through different slots. The live probe (`docs/research/2026-09-01-realtime-live-probe.md`,
Q4) proved by slot-swap that `realtimeStartInstructions` reaches the Call Agent only
(0/6 on the Voice), `prompt` reaches the Voice (6/6) and replaces codex's own default
voice prompt outright, and `initialItems` reaches the Voice silently (5/6). This engine
sent every rule it had — the whole voice catalogue — as `realtimeStartInstructions`. The
acting rules therefore landed where they belong; the speaking rules never landed
anywhere, and the ones Round 1 asked for (terse, third-person, slow) had no carrier at
all.

## Decision

**The dial carries three payloads to two audiences, and the seam names the audience,
not the slot.** `CallAdapter.ensure_call(dial: Dial)` replaces `ensure_call(instructions:
str)`. `Dial` is a frozen carrier of three fields: `voice` — prose for the Voice;
`agent` — prose for the Call Agent; `hand_over` — the Briefing's dial-time items (ADR
0017, [#166](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/166)). The
realtime adapter alone maps them to `prompt`, `realtimeStartInstructions` and
`initialItems` (`role: "developer"`); Bridge Core never learns a codex field name. A
`Dial` refuses an empty `voice` or `agent` text at construction — sending no `prompt`
would silently hand the Voice back to codex's stock prompt, which is exactly the state
this decision ends.

**The catalogue gains an audience.** `Audience.VOICE` now means the speaking half and
`Audience.AGENT` the acting half; `generate()` produces three sets (voice, agent,
delegated) and the coverage gate — every retained rule lands in exactly one set, or the
engine refuses to boot — runs over all three. Two budgets, for two reasons: the agent set
is capped at 8,192 bytes because codex caps that slot at 8,192 tokens and a byte is the
floor of a token; the voice set keeps this engine's own 8,000-byte cap, which codex does
not impose (the `prompt` slot is unbudgeted) and which stands as the measure of "terse".
Which rule belongs to which audience, and what each says, is
[#173](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/173)'s work,
rule by rule against the 0901 flow — nothing is carried over unread.

**The Voice is addressed in natural language.** Its set is rendered as plain prose
paragraphs — no headings, bullets, code or key-value text (Simon, 2026-09-01: 控制 voice
一定要用自然语言而不是代码语言). The agent set may keep its structure.

**Three dial-time switches are the adapter's constants, not `Dial` fields**, because no
caller varies them: `delegationAckFiller` off (the backend's own "好，等我看一下" filler
is the wordiness Round 1 Q9 removed), `codexResponsesAsItems` on with a prefix, and
`includeStartupContext` **off**.

*Amended 2026-09-01 after [#179](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/179).*
`includeStartupContext` was left at its default pending that probe; the default is **on**,
and what it includes is up to 5,300 tokens of the current thread's history, a scan of the
user's forty most recent local codex threads and a local workspace map, appended to the
**Voice's** prompt (`codex-rs/core/src/realtime_context.rs`,
`realtime_conversation.rs:1356-1377`). On a call whose premise is that the Voice speaks only
about what the Briefing handed it, that is noise and a disclosure surface both. Off.

`codexResponsesAsItems` keeps its `on`, but **not for the reason first given**. It was
turned on so "the Call Agent's answers become items the adapter can log"; with it on and a
prefix set, two live spoken calls produced **no item carrying an agent answer at all** —
every item in both records is a `handoff_request`, while the answers themselves came back
invisibly and were spoken. On this evidence the switch does not buy observability, and the
adapter must not be built expecting it to. What the Call Agent says is reachable, if at all,
through the `[BACKEND] `-prefixed `user` messages codex's own Voice prompt describes
(`prompts/templates/realtime/backend_prompt.md:40-41`) — a carrier no ticket has examined.
Sizing that is [#173](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/173)'s
to inherit.

**No `HandoffRequested` event.** The Voice's hand-off to the Call Agent
(`handoff_request`) is logged by the adapter and raised to nobody: the closed event set
stays as it is. A voice hang-up is the Call Agent running `bridgectl live` — the engine
executes off a request it can see, and the model's own claim to have hung up (observed
live, #175 run 3) is trusted by nothing.

*Confirmed 2026-09-01 by [#179](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/179).*
Told in `realtimeStartInstructions` that `bridgectl live` ends the call, the Call Agent ran
it on **3 of 3** spoken requests, four to five seconds after each hand-off. The rule reaches
the acting half and is obeyed; the Voice never needs to know the verb exists. So the event
does not return.

**But only the user's speech reaches the Call Agent.** The same probe put the same request
through `appendText` **30 times under codex's own Voice prompt and 16 under ours**, and it
routed twice and never, against **15 of 15** spoken. The engine's own mid-call append path
therefore **cannot trigger agent action** — a fact with no consequence for the Briefing,
which addresses the Voice, and a hard limit on anything that would drive the Call Agent from
the engine side. And when the Voice does not route, it does not fall silent: asked for the
system time it invented one eleven hours out and advanced it as the call went on. **A Voice
asked a question the engine did not hand it an answer to will produce one**, which is the
sharpest available argument for the Briefing owning every word about Session state.

## Considered and rejected

- *One set in every slot.* Hands the Voice eighteen rules about verbs it cannot run and
  the Agent three about pacing it cannot hear; doubles the byte cost; keeps the lie that
  "the voice thread" is one thing.
- *`Dial` fields named after codex slots.* Would put `prompt` / `initialItems` in Bridge
  Core; the seam exists so that only the adapter knows the wire.
- *Engine-side hang-up on `handoff_request`.* Requires the engine to classify the
  user's sentence as a hang-up — intent recognition moved from the model into code.

## Legacy (ADR 0010)

Legacy at `1d32845` never dialled codex realtime: `bridge/livecall.py:77-109` follows the
ChatGPT desktop app's own call by reading its log. **Legacy has no such behaviour.** The
untracked prototype in that clone, `scripts/rt_prototype.py:278-281,377`, put a single
instruction — "when asked to run a bridge command, delegate it to the coding agent" —
into `realtimeStartInstructions`, treating one slot as the whole voice. **Adapted**: that
slot stays the Call Agent's; the Voice gains its own.
