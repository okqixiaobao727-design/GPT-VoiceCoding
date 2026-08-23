# The control plane

The interface Bridge Core exposes: JSON over a Unix domain socket, one object per
line. Every surface speaks it — the menu-bar shell, `bridgectl`, the Companion
Channel, and spoken commands inside a Live Call — and no surface has a private
protocol beside it.

**It is never gated by any switch** ([ADR 0002](adr/0002-the-control-plane-is-never-gated-by-switches.md),
absolute). Every action below answers with the Duty, Voice and Message Switches
all off. The reference implementation gated seven actions behind the Duty
Switch; that behaviour is dropped, not ported, and
`tests/test_control_plane_actions.py` is the regression test.

This document is the contract the Swift shell (#11) implements against. The
Python vocabulary is `gpt_voicecoding.seams.control_plane`; the mechanism is
`gpt_voicecoding.control_plane`.

## Where things live

| What | Where | Why |
| --- | --- | --- |
| The socket | `/tmp/gpt-voicecoding-<uid>/control.sock` | Darwin caps an `AF_UNIX` path at 103 bytes, and the application-support path is already 76 of them before a long home directory is considered. The runtime root is short, per-uid, created `0700` at engine start (`/tmp` is cleared on reboot, which is correct for a socket), ownership-checked with `lstat` before adoption, and the socket itself is `0600`. |
| The durable state | `~/Library/Application Support/GPT-VoiceCoding/engine/state.json` | It outlives reboots, and nothing but Bridge Core may read it. |
| The configuration | `~/Library/Application Support/GPT-VoiceCoding/engine/config.toml` | The user owns it; the engine only reads it. |

So the socket path is **not** derivable from the state path. A surface reads it
from configuration, or is told it directly (`bridgectl --socket`). Both paths are
overridable, and a configured socket path longer than 103 bytes is refused at
start with a named error rather than an `OSError` from inside asyncio.

## The wire

- One request per line, UTF-8, `\n`-terminated. One reply per request, on the
  same connection, in order.
- A line may not exceed **65536 bytes** in either direction. A request that
  overruns it is answered with `malformed_request` and that connection is closed
  — there is no honest way to resync inside a line.
- A connection may carry any number of requests. Connections are independent: two
  surfaces can neither wedge nor read each other.
- Malformed input costs one request, never the server.
- The socket is refused by both sides unless it is owned by the current user and
  private (`0600`). A live engine is never displaced; debris from a dead one is
  cleared.

### Request

```json
{"action": "switch", "payload": {"name": "duty", "on": true}}
```

`payload` may be omitted when an action takes nothing.

### Reply

```json
{"ok": true, "action": "switch", "protocol": 2, "data": {"name": "duty", "on": true, "previous": false}}
```

```json
{"ok": false, "action": "switch", "protocol": 2, "error": {"code": "unknown_switch", "message": "unknown switch: 'sound'"}}
```

`action` is `null` when the line never named a usable one. `protocol` is `2`;
a field being **absent** means an engine too old to have been asked, which is
distinct from a field being empty ([ADR 0003](adr/0003-the-engine-reports-what-it-loaded.md)).

**`error.message` is rendered verbatim.** It is the refusal's own words, from
Bridge Core. A surface that rephrased one would be a second voice deciding what
the user is told.

### Error codes

| Code | Means |
| --- | --- |
| `malformed_request` | Not one JSON object this protocol can represent, or past the byte bound. |
| `unknown_action` | Well-formed, naming an action this engine does not have. |
| `invalid_payload` | The action is known; what came with it is not usable. |
| `unknown_switch` | No switch by that name is registered. |
| `unknown_session` | No Session by that identity was ever registered here. |
| `stale_session` | Known session id, unreachable under that identity — a fork, or an end. |
| `unknown_pending` | Nothing is waiting under that id; it was answered or it expired. |
| `second_call_refused` | Something asked to open a call while the system owns one. |
| `launch_failed` / `close_failed` | Reserved for refusals; a Launcher that *tried* and failed answers `ok: true` — see below. |
| `seam_unavailable` | This engine has nothing loaded behind the seam that action needs. |
| `refused` | Any other Bridge Core refusal. Still carries its own words. |
| `engine_unreachable` | Raised by a **surface**, never sent by the engine: nothing answered. |

## The actions

Nine, and the set is closed. Adding one is a contract change.

### `status`

Payload: none. Data:

```json
{
  "switches": {"duty": false, "voice": false, "message": false},
  "sessions": [ /* see below */ ],
  "call_id": null,
  "pending_relays": [{"request_id": "…", "target": {…}, "kind": "answer", "text": "…",
                      "route": "deliver", "queued_at": 0.0, "expires_at": 600.0,
                      "outcome": "unknown"}],
  "pending_approvals": [{"approval_id": "…", "target": {…}, "tool_name": "Bash",
                         "detail": "", "options": [], "opened_at": 0.0, "expires_at": 600.0}]
}
```

A session:

```json
{"target": {"agent": "claude", "session_id": "abc", "pid": 1234},
 "label": "gpt-voicecoding · build the control plane",
 "workspace": "/Users/…", "registered_at": 1787222000.0,
 "state": "live", "reply_window": "closed"}
```

`target` is the **address**; `label` is for speech and for matching. A label
never crosses the wire as an address — resolving one to a target is Bridge
Core's router, on the way in from the Companion Channel.

`registered_at` is wall-clock seconds: it is written to disk and read back by the
next engine.

### `sessions`

Payload: none. Data: `{"sessions": [...]}` — the same rows `status` carries, for a
surface that renders only the roster.

### `switch`

Payload: `{"name": "duty", "on": true}`. `on` must be a JSON boolean; a string is
refused, because `"false"` is truthy and the switch it would turn on is the
master. Data: `{"name": …, "on": …, "previous": …}`.

Turning an outlet on is an outlet transition, so a retained Stop Notice may be
re-offered as a result.

### `live` — the Live Toggle

Payload: none. Data: `{"state": "up" | "connecting" | "down", "call_id": "…" | null}`.

One action: it ends the call the system owns, or starts one if none is up. Every
surface calls this one — a surface holding its own call state is how two toggles
once opened two calls. It is bound only by the one-call-at-a-time invariant,
never by a switch.

### `launch`

Payload:

```json
{"request_id": "21d73168-b1f0-4b18-977d-fba0d1f2cc13",
 "agent": "codex", "workspace": "/path/to/work",
 "label": {"project": "gpt-voicecoding", "task": "build the control plane"},
 "env": {"NAME": "value"}}
```

`request_id` is the sender-minted UUID for this distinct launch intent. A retry
carries the same UUID; an intentional second Session carries a new one. Reusing
one UUID with a different agent, workspace, label or environment is refused.
`env` is optional and is exactly the variables to set on the child. Data:

```json
{"request_id": "…", "status": "launched" | "failed" | "unavailable",
 "target": {…} | null, "detail": ""}
```

**A Launcher that tried and failed answers `ok: true`.** That is news the caller
asked for, carrying the real error in `detail`; a protocol refusal would say the
request was unusable, which is a different thing. Only a `launched` outcome
registers a Session. Sequential or concurrent repeats under one UUID return the
first complete outcome and neither launch nor register a second Session. This
in-process guarantee does not survive an engine restart.

### `close`

Payload: `{"target": {"agent": "codex", "session_id": "abc", "pid": null}}`. Data:

```json
{"request_id": "…", "status": "closed" | "already_closed" | "failed" | "unavailable",
 "detail": "", "children": [{"ref": "…", "closed": true, "detail": ""}]}
```

Three-way, and the distinction is load-bearing for adapters (#9):

- **Live** in the registry → the Launcher is asked to close it.
- **Known-ended** in the registry → `already_closed`, and the Launcher is *not*
  dialled. The caller asked for a state that already holds, which is what
  idempotent means; re-dialling risks reaping whatever now owns that pid or pane.
- **Unknown, or a wrong pid under a known session id** → a refusal
  (`unknown_session` / `stale_session`). A wrong pid is a fork, not a typo.

`children` is empty unless the adapter actually owns child destinations. Pane
semantics never cross this seam.

### `relay` — an Answer Relay

Payload: `{"target": {…}, "text": "carry on", "route": "deliver" | "supplement"}`.
Data:

```json
{"request_id": "…", "target": {…}, "state": "pending" | "retained" | "delivered" | "reported_failed",
 "route": "deliver", "outcome": "delivered" | "failed" | "held" | "unknown",
 "confirmation": "", "report": ""}
```

Queued is not delivered, and `state` says which it was.

Route follows the user's explicit intent and is never inferred from how busy a
Session is — the same "busy" carries both "add this now" and "this can wait".

**There is no Notice Relay action.** A Notice is words the system itself
originates, so a surface asking for one would be a surface claiming to be the
system.

### `approve`

Payload: `{"approval_id": "a1", "verdict": "allow" | "deny" | "ask"}`. Data:

```json
{"approval_id": "a1", "target": {…}, "verdict": "allow", "state": "delivered",
 "outcome": "delivered", "closing_notice": "…"}
```

A verdict for a request that already resolved is refused with `unknown_pending`:
Bridge Core discards it safely because its closing notice has already gone out,
and the user is owed the news that their verdict landed on nothing.

### `verify` — ADR 0003

Payload: none. Data:

```json
{"seams": [{"seam": "companion_channel", "outcome": "pass" | "fail" | "manual",
            "configured": "…", "loaded": "…", "detail": ""}]}
```

Seam names: `call`, `companion_channel`, `session_launcher`, and `agent.<kind>`
per configured agent.

The engine reports what it **actually loaded**, never what a configuration file
says it should have loaded. `configured` is what the file named; `loaded` is what
the adapter says about *itself* when asked — every seam here is pluggable and
every one of them has a `verify` verb, so all four are asked, and a Call adapter
whose far side is down reports that rather than the engine reciting the
configuration back and calling it an observation.

Three outcomes: `pass`, `fail`, and `manual` — nothing configured anywhere, which
is handed to the operator rather than passed or failed. What is compared is
*presence*, not spelling: configuration names a factory and an adapter names its
implementation, and demanding those match character for character would fail on
every correctly wired machine. So `fail` means one of three things — the adapter
itself reported a failure, configuration names an adapter and the engine loaded
nothing (or the null one), or something is loaded that nothing configured.

## The command line

`bridgectl` and the Companion Channel's `/` grammar are one command set, parsed
by one parser, so neither can grow a command the other lacks.

```
status
switch <name> on|off
sessions
live
launch --request-id <UUID> <agent> <workspace> <project · task>
close <agent>:<session id>[:<pid>]
relay <agent>:<session id>[:<pid>] [--supplement] <words>
approve <approval id> allow|deny|ask
verify
```

`<agent>:<session id>[:<pid>]` is how a `SessionTarget` is written on one line. A
Claude target without a pid is refused: `--resume` forks a second process under
the same session id.

`bridgectl` exits **0** when the engine answered, **1** when it refused, and **2**
when there was no engine to ask. Collapsing the last two would tell a user their
switch does not exist when nothing is running.

## Configuration

One TOML file, read once, by the composition root and nothing else.

```toml
[engine]
socket_path = "/tmp/gpt-voicecoding-501/control.sock"   # optional
state_path  = "~/Library/Application Support/GPT-VoiceCoding/engine/state.json"  # optional

[adapters]
call              = "gpt_voicecoding.adapters.call.realtime:realtime_call"
companion_channel = "gpt_voicecoding.adapters.companion_channel.telegram:build"
session_launcher  = "gpt_voicecoding.adapters.session_launcher.child:build"

[adapters.agents]
claude = "gpt_voicecoding.adapters.agent.claude:build"
codex  = "gpt_voicecoding.adapters.agent.codex:codex_agent"

[adapters.settings.call]            # optional; every key belongs to that adapter
workspace = "~/code"                # where the bridge's own threads run; default is ~

[policy]                            # optional; these are the locked defaults
relay_ceiling_seconds   = 600
approval_budget_seconds = 600

[delegate]
model = "the-model-you-chose"       # required: the cost lever has no default
cli   = "/Applications/GPT-VoiceCoding.app/Contents/MacOS/bridgectl"  # optional
```

Each adapter reference is `module:attribute`, resolved by the composition root —
the only thing in the system that imports an adapter. A factory is called as
`factory(sink=<event sink>)` and returns the adapter.

`[adapters.settings.<seam>]` is that seam's own table, and the composition root
**forwards it without reading a key**: only the adapter knows what its own keys
mean, and a root that parsed them would be the hub growing adapter-shaped
knowledge (ADR 0001). A seam given no table is called with the sink alone. Every
adapter that takes one refuses to start on a key it does not recognise, because a
misspelled setting that silently falls back to a default is the
configuration-shaped version of the silent fallback this project bans.

The shipped Call and Codex Agent adapters **share one `codex app-server`**. The
Codex Agent adapter spawns, owns and reaps it; the Call adapter's realtime route
rides it and starts none of its own. The composition root introduces them, which
is why naming the shipped Call adapter without also naming a Codex Agent adapter
in `[adapters.agents]` refuses to assemble rather than starting an engine whose
voice surface could never come up.

The shipped Call adapter also needs the voice extra —
`pip install 'gpt-voicecoding[voice]'` — and its factory says so at assembly
time rather than at the moment somebody tries to speak.

An adapter with a connection, a reader task or a child of its own may implement
the optional `Connectable` shape (`seams/connection.py`): `async connect()` and
`async aclose()`, both idempotent. The composition root opens every one of them
**before** it serves the socket — a surface that reaches a serving engine reaches
one whose seams are actually filled, and an adapter that cannot open stops the
start rather than being answered for. On shutdown they are closed in reverse
order, and every one gets its turn even if an earlier one raises. An adapter with
nothing to open implements neither verb; the contract is optional because not
every seam has a connection to hold.

**This file is executed with the privileges of the user who wrote it, and is
exactly as trusted as the engine itself.** That is the deliberate cost of naming
adapters by reference: a compiled-in table of allowed names could not name a
deployment's own private wiring, and keeping that wiring private is a charter
decision. The file lives in the user's own application-support directory for the
same reason.

`[delegate] cli` is where the control-plane CLI really is. Bridge Core names it
in the instructions it generates for the voice thread and for a Delegated Turn,
so it has to be true: an instruction naming a binary that is not there is exactly
the invented detail those instructions forbid. Left out — the ordinary case — the
engine uses the console script installed beside its own interpreter, and uses it
only after finding that it exists and can be run. A bundle moves that binary, so
a bundle states this key. If neither is really there, the engine **refuses to
start** rather than describing a CLI nobody can run.

An unconfigured Call, Companion Channel or Session Launcher seam **refuses to
start**, with a named error. An engine that silently loaded nothing behind a seam
looks exactly like a healthy one until it is needed — the outage ADR 0003 exists
to prevent. Running without a Companion Channel is legitimate, but the null
implementation ships with that adapter (#10) and is not built yet, so today it is
also a refusal that says so.

Keys this file does not carry yet belong to the tickets that own them: the log's
four numbers are ADR 0004's, and packaging keys arrive with the bundle.

## Running headless

```bash
python -m gpt_voicecoding.engine --config ~/Library/Application\ Support/GPT-VoiceCoding/engine/config.toml
```

The engine stays in the foreground and never daemonises: the menu-bar shell
spawns it as a direct child and expects it to remain one (ADR 0005). `SIGINT` and
`SIGTERM` both stop it in order — loops cancelled, socket removed, so the next
start is not left claiming its own debris. It exits **2** when it could not start,
naming what was missing on stderr; that output happens before the engine adopts
its own log (ADR 0004), which is why it goes to the terminal that started it.
