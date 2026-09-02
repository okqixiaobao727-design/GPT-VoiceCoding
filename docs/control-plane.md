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
- Reply capacity is measured and written with one canonical UTF-8 JSON-line
  encoder, including JSON escaping and the terminating newline. The server
  performs a final outbound check. If an honest response skeleton cannot fit,
  it sends a bounded `refused` reply; it never drops Session rows silently.
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
{"ok": true, "action": "switch", "protocol": 6, "data": {"name": "duty", "on": true, "previous": false}}
```

```json
{"ok": false, "action": "switch", "protocol": 6, "error": {"code": "unknown_switch", "message": "unknown switch: 'sound'"}}
```

`action` is `null` when the line never named a usable one. `protocol` is the
numeric protocol version, currently `6`. A missing field or JSON `null` means the
reply did not declare a usable version. The Swift shell refuses to interpret any
reply whose version is missing or differs from the version it supports, and shows
that protocol mismatch separately from an engine refusal or an unreachable engine.

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
| `refused` | Any other Bridge Core refusal. Still carries its own words. |
| `engine_unreachable` | Raised by a **surface**, never sent by the engine: nothing answered. |

## The actions

Eight, and the set is closed. Adding one is a contract change. Protocol 6
retires `sessions` and adds `brief`, the one verb Session state is fetched
through. A protocol-5 surface must report a mismatch: it would otherwise send
`sessions` to an engine that answers `unknown_action`.

`launch` and `close` were the eighth and ninth until protocol 4. They are parked
with the code behind them ([#72](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/72)):
v1.0 is a bridge over the Sessions the user starts, so nothing here brings one
into existence or ends one. Their two error codes, `launch_failed` and
`close_failed`, went with them. A surface still sending either action is
answered `unknown_action`.

### `status`

Payload: none. Data:

```json
{
  "switches": {"duty": false, "voice": false, "message": false, "auto_hangup": true},
  "sessions": [ /* see below */ ],
  "call_id": null,
  "pending_relays": [{"request_id": "…", "target": {…}, "kind": "answer", "text": "…",
                      "route": "deliver", "queued_at": 0.0, "expires_at": 600.0,
                      "outcome": "unknown" /* or null: nothing has been attempted */}],
  "pending_approvals": [{"approval_id": "…", "target": {…}, "tool_name": "Bash",
                         "detail": "", "options": [], "opened_at": 0.0, "expires_at": 600.0}]
}
```

A session — every field of one roster row, because a surface that had to ask a
second question to render one line would be a second reader of the same Session:

```json
{"target": {"agent": "claude", "session_id": "abc", "pid": 1234},
 "label": "gpt-voicecoding · build the control plane",
 "name": "workspace-claude-ed",
 "workspace": "/Users/…", "first_seen": 1787222000.0,
 "lifecycle": "live", "state": "idle",
 "waiting_for": {"kind": "question", "caught_up": true, "prompt": "Which base?",
                 "options": [{"text": "main", "description": "Use the default branch",
                              "recommended": true}],
                 "recommendation": "main", "tool_name": null, "detail": null,
                 "approval_id": null},
 "progress": {"availability": "readable", "has_history": true,
              "omission": "status_summary",
              "read_at": "2026-08-26T02:44:39+00:00", "recent": []},
 "last_activity": "2026-08-26T02:44:39+00:00",
 "child": {"kind": "main", "parent": null},
 "reply_window": "open"}
```

`target` is the **address**; `label` is for speech and for matching. A label
never crosses the wire as an address — resolving one to a target is Bridge
Core's router, on the way in from the Companion Channel.

`first_seen` is wall-clock seconds, and it is when *this engine* first saw the
Session — no agent knows it.

`waiting_for` is what a stopped Session stopped on, as structure rather than as a
rendered sentence. `kind` is one of `none`, `question`, `permission`, `unknown`;
`unknown` always comes with `caught_up: false`, and means *ask again*, never
*nothing is happening*. A question option always carries `text` and `recommended`;
its `description` is the Agent's optional explanation of that choice, or `null`
when the Agent supplied none.

`progress` always carries the same five fields:

- `availability`: `not_read`, `unreadable`, or `readable`;
- `has_history`: a boolean only when readable, otherwise `null`;
- `omission`: `none`, `older`, `status_summary`, or `newest_oversize`;
- `read_at`: the readable observation's timestamp, otherwise `null`; and
- `recent`: ordered whole entries, each with `role` and `text`.

Only `readable` plus `has_history=false` and `omission=none` means nothing has
been said. A roster publication carries no chat body: readable history becomes
`has_history=true`, `recent=[]`, `omission=status_summary`. `not_read` and
`unreadable` are never inferred from an empty array. The source failure behind
an unreadable discovery is reported as lane degradation; an exact progress read
maps it to a typed refusal.

`last_activity` is separate from `progress` on purpose. A Session can have moved
without saying anything a reader would show, and this is the field that says so.
It is `null` when nothing read one.

It is deliberately **wider than `progress`**: it counts any work in the Session,
its own subagents included, where `progress` carries only what the user would be
shown. On the Claude lane that means a sidechain record advances it even though
such a record never becomes an entry; on the Codex lane it is the thread's own
`updatedAt`, which moves for any work at all. One meaning on both lanes — and a
Session four minutes into a subagent is not a Session nobody has heard from. A
subagent is not a Child Process: that is a separate agent process, and it has a
row of its own.

`reply_window` is derived on the row and rendered here, so no surface re-derives
it and no two surfaces can disagree about one Session.

### `brief`

Payload: none, or `{"target": {"agent": "codex", "session_id": "abc", "pid": null}}`.
Answered by Briefing (`core/briefing.py`), which is the **only** thing in this
engine that puts words to what a Session is doing. An omitted `target`, or one
written as JSON `null`, means the whole roster — the same reading `route` gives
an absent value. A `target` that is **present and unusable** is refused rather
than widened: a surface that asked about one Session is never answered with all
of them.

Every reply carries `kind`, `text`, and the structure `text` was rendered from:

```json
{"kind": "roster",
 "text": "the others: 1 running\n  …",
 "roster": {"counts": {"running": 1},
            "focus": {"agent": "codex", "session_id": "abc", "pid": null},
            "rows": [{"target": {…}, "name": "gpt-voicecoding · port the log",
                      "agent": "codex", "state": "decision", "focus": true}]}}
```

```json
{"kind": "session",
 "text": "gpt-voicecoding · port the log — codex:abc — waiting for your decision\n  …",
 "session": {"target": {…}, "name": "gpt-voicecoding · port the log", "agent": "codex",
             "state": "decision",
             "newest": {"state": "said", "text": "I rebuilt the index."},
             "decision": {"prompt": "Which base?",
                          "options": [{"text": "main", "description": "the default branch",
                                       "recommended": true}],
                          "recommendation": "main", "tool": null, "summary": null},
             "answerable_here": true,
             "last_activity_at": "2026-09-02T03:04:05+00:00"}}
```

**`text` is the engine's own rendering and surfaces print it unchanged.**
`bridgectl brief` prints exactly this string; so does the Companion Channel. One
renderer is the point: two would be two descriptions of one Session. The
structure travels beside it for a surface that reads fields rather than lines.

`state` is one of five, and they are what the user is told:

| State | When |
| --- | --- |
| `decision` | A question is waiting, **or** a Codex turn ended — Codex has no question hook, so the ambiguity is briefed as the answerable state until the heuristic that tells them apart lands. |
| `permission` | A permission dialog is open. |
| `finished` | This turn is done and the Session is idle for a new instruction. Exited Sessions appear nowhere. |
| `running` | Mid-turn. Nothing is being asked of the user. |
| `unreadable` | It stopped, and what it stopped on or what it said could not be read. **Never counted as a decision**, and the brief still carries whatever *was* read. |

`newest` is the newest assistant message **whole**, under ADR 0016's omission
rules — the engine never condenses, so the one-line conclusion a user hears and
the detail they may ask for are one field. Its `state` is `said`, `nothing_said`,
`not_read`, `unreadable` or `oversize`, and `text` is present only for `said`.

`decision` is `null` when nothing is being asked. A question carries `prompt`,
every `option` and any `recommendation`; a permission carries `tool` and a
one-line `summary`. Both shapes use the one object, and which one it is follows
from `state`.

`answerable_here` says whether the user's reply can reach this Session from
here. A question is answerable while its lane still holds the route; a
permission is answerable while a handle still holds the dialog open. Anything
else is answered at the terminal.

The **Roster Brief** lists one header row per live Session, the Focus Session
first, and children nowhere: every row is one `brief <address>` answers, and a
Child Process is seen and never spoken to. `counts` is **the others** whenever
there is a Focus Session — that Session is named in full, so counting it again
would be one Session told twice.

**The Focus Session is never set here.** It moves when the user *replies* to a
Session — `relay` or `approve` — and is cleared when that Session ends. Asking
about a Session is not replying to one.

`newest` travels twice — as a field and inside `text` — so one whole message can
overflow the line that has to hold it. When it does, the reply still answers:
`newest` becomes `{"state": "oversize", "text": null}` and everything the user
acts on — the header, the state, the whole decision — stays. Text is never cut
(ADR 0016).

`brief <address>` is a **read**, read now, through exactly one `inspect`. Its
refusals are `progress`'s minus one: `unknown_session`, `stale_session`, and
`refused` for a Child Process or a lane that could not be read at all. A Session
whose *progress* could not be read is **not** a refusal here — it is the
`unreadable` state, or an `unreadable` `newest` on a Session that is still
running.

### `progress`

Payload: `{"target": {"agent": "codex", "session_id": "abc", "pid": null}}`. Data:
`{"session": { … }}` — one Session row whose progress is the exact-detail
publication. For example:

```json
{"availability": "readable", "has_history": true, "omission": "older",
 "read_at": "2026-08-26T02:44:39+00:00",
 "recent": [{"role": "user", "text": "do the thing"},
            {"role": "assistant", "text": "done"}]}
```

It is a **read**: it resolves one exact identity, asks that lane and no other,
and never starts a turn. What it adds over the same row in `status` is *when* —
the Session is read at the moment it was asked about, rather than at the last
discovery. The publisher keeps the newest complete entry and widens backwards
while the complete encoded Reply still fits. Entries are whole or omitted;
message text is never sliced. If the newest entry itself cannot fit, the action
still answers honestly with `has_history=true`, `recent=[]`, and
`omission=newest_oversize`. A Session mid-turn is answered here and is
deliberately not read deeply by the roster cadence: reading it is the expensive
half, and "how far along is it" is the question a user asks precisely while it
works.

**It refuses rather than answering emptily**, and the four refusals are four
different facts:

| Code | When |
| --- | --- |
| `unknown_session` | No Session by that identity was ever registered here. |
| `stale_session` | The identity names a different process now, **or** the Session has ended. The row is not ended by this action: discovery is the whole-lane reading and is the only thing that ends one. |
| `refused` | The lane could not be read at all. The message is the lane's own words, and the row stands exactly as the roster last saw it: not being able to look is not a sighting. |
| `refused` | Nothing could read how far it has got — a Codex Session the shared daemon does not hold, or one whose first turn has written no record yet. |

The last one is the line this whole action is drawn around. A Session that *was*
read and had said nothing answers normally with `has_history=false`; a Session
nobody could read is a refusal. A surface handed the second as the first would
render a working Session as an idle one.

A Session the Codex daemon does not hold therefore never gets an invented
reading. Its rollout is on disk and reading it would be a second source answering
the same question with worse evidence.

### `switch`

Payload: `{"name": "duty", "on": true}`. `on` must be a JSON boolean; a string is
refused, because `"false"` is truthy and the switch it would turn on is the
master. Data: `{"name": …, "on": …, "previous": …}`.

Four names are registered — `duty`, `voice`, `message`, `auto_hangup` — plus
whatever Feature Switches configuration declares. Any other name is
`unknown_switch`. Every position is persisted, and a surface reads them back from
`status` under the same keys.

`auto_hangup` is the Auto Hang-up Switch, and it is the odd one: it starts **on**,
where the other three start off, and it hangs from nothing. The Silence Ceiling
is the call's own limit rather than an act toward the user, so it ends a silent
call with Duty off and on calls the user opened; only this switch stops it. How
long that silence runs is configuration, not a switch — `[policy]
silence_end_seconds`. Bridge Core asks `SwitchAdjudicator.may_auto_hangup()`,
the same way it asks `may_touch_call()` before it speaks.

Growing the set is additive on the wire and not a protocol change: the grammar
above is unchanged, and a surface renders `status`'s switches over its own known
order, so a key it has no row for is ignored rather than guessed at.

Turning an outlet on marks current-state reconciliation as owed. The next
ordinary discovery pass uses its fresh lane rows and announces each live main
Session still waiting on a question or permission. It never replays a historical
Stop Notice.

### `live` — the Live Toggle

Payload: none. Data: `{"state": "up" | "connecting" | "down", "call_id": "…" | null}`.

One action: it ends the call the system owns, or starts one if none is up. Every
surface calls this one — a surface holding its own call state is how two toggles
once opened two calls. It is bound only by the one-call-at-a-time invariant,
never by a switch.

### `relay` — an Answer Relay

Payload: `{"target": {…}, "text": "carry on", "route": "deliver" | "supplement"}`.
Data:

```json
{"request_id": "…", "target": {…}, "state": "pending" | "retained" | "delivered" | "reported_failed",
 "route": "deliver",
 "receipt": {"outcome": "delivered" | "failed" | "held" | "unknown", "reason": "…"} | null,
 "reason": "delivered" | "awaiting_reply_window" | "duplicate_risk" | "held_far_side"
         | "ceiling_passed" | "session_ended" | "question_unanswerable"}
```

**The receipt is a grade and a reason, never a sentence.** Three facts and no
prose: `state` is where the words are, `receipt` is what the last attempt proved
— the delivery seam's own value, with the adapter's evidence in `reason` — and
the top-level `reason` is one code from the closed `RelayReason` set
(`core/relays.py`), which says why the Relay stands where it does:

| code | what it says |
| --- | --- |
| `delivered` | the attempt proved the words reached the model |
| `awaiting_reply_window` | they wait, and may go again when the Session next takes a turn |
| `duplicate_risk` | an attempt proved nothing either way, so they are kept and never re-sent on this system's authority (P9) |
| `held_far_side` | the far side parked them in front of a person |
| `ceiling_passed` | terminal: they waited past `relay_ceiling_seconds` |
| `session_ended` | terminal: the Session ended while they waited |
| `question_unanswerable` | terminal, before the wire: that question is no longer answerable from here (#68) |

`receipt` is `null` when nothing was attempted — **never** a grade of
`unknown`, which is a positive observation about an attempt that was made. The
two are the difference between "it may already have arrived" and "it never left
this process", which is the whole of the duplicate-safety rule.

Surfaces print the three codes and compose no sentence: `state=<state>
grade=<grade|none> reason=<code>` is the one format (`core/relays.py`,
`receipt_line`), and `bridgectl relay` and the Companion Channel's inbound relay
path both answer with exactly it. The words the user *hears* are the Voice's,
re-rendered from these facts.

`state` is `Lifecycle` (`core/lifecycle.py`), which is where the words stand
across every attempt they will get — deliberately not the per-attempt `Delivery`
grade, because reading one attempt's grade as the item's fate is the reference
implementation's worst delivery bug. Its literals are the four states this verb
can answer with: **queued** is `retained`, **delivered** is `delivered`, and
**terminal** is `reported_failed`. There is no `held` *state*: held is a grade
the far side returned, and it reaches a surface as `receipt.outcome = "held"`
with `reason = "held_far_side"`, still `retained` because the words are still
waiting.

Queued is not delivered, and `state` says which it was.

Route follows the user's explicit intent and is never inferred from how busy a
Session is — the same "busy" carries both "add this now" and "this can wait".

There is no action for system-authored words: a surface asking for one would be
a surface claiming to be the system.

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

Seam names: `call`, `companion_channel`, and `agent.<kind>` per configured agent.

The engine reports what it **actually loaded**, never what a configuration file
says it should have loaded. `configured` is what the file named; `loaded` is what
the adapter says about *itself* when asked — every seam here is pluggable and
every one of them has a `verify` verb, so all of them are asked, and a Call adapter
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
brief [<agent>:<session id>[:<pid>]]
progress <agent>:<session id>[:<pid>]
live
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

`--task` consumes every remaining word. Project names containing spaces are one
quoted argument; callers never quote an absolute workspace or compose a Session
Label. Omitting `--agent` selects the configured global default. The old
positional agent/workspace/label form is not accepted beside this one.

## Configuration

One TOML file, read once, by the composition root and nothing else.

`[adapters.settings.session_launcher]` and `[launch]` are no longer read. A
configuration carrying `[adapters.settings.session_launcher]` is refused rather
than silently ignored.

```toml
[engine]
socket_path = "/tmp/gpt-voicecoding-501/control.sock"   # optional
state_path  = "~/Library/Application Support/GPT-VoiceCoding/engine/state.json"  # optional

[adapters]
call              = "gpt_voicecoding.adapters.call.realtime:realtime_call"
companion_channel = "gpt_voicecoding.adapters.companion_channel.telegram:telegram_channel"

[adapters.agents]
claude = "gpt_voicecoding.adapters.agent.claude:build"
codex  = "gpt_voicecoding.adapters.agent.codex:codex_agent"

[adapters.settings.call]            # optional; every key belongs to that adapter
workspace = "~/code"                # where the bridge's own threads run; default is ~

[policy]                            # optional; these are the locked defaults
relay_ceiling_seconds   = 600
approval_budget_seconds = 600
silence_end_seconds     = 60

[log]                               # required: three numbers with no default
max_bytes                     = 8388608
retained_files                = 3
stripped_environment_prefixes = ["Malloc"]

[delegate]
model = "the-model-you-chose"       # required: the cost lever has no default
cli   = "/Applications/GPT-VoiceCoding.app/Contents/Resources/engine/bin/bridgectl"
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

In the bundle that key is not optional, and it points into `Contents/Resources`
rather than `Contents/MacOS`: `pip` installs a console script beside the
interpreter that installed it, and the bundle's interpreter is under
`Contents/Resources/engine/`. There is no second copy in `Contents/MacOS/` — a
duplicate binary that shadowed the real one would be two things to sign and two
things to be wrong. The bundled `bridgectl` keeps `pip`'s console-script body,
but the pipeline replaces its *absolute* shebang with a shell/Python preamble
that resolves the script's real path and execs the interpreter sitting beside
it. The same rewrite covers every Python console script in `engine/bin/`; see
[`docs/app-bundle.md`](app-bundle.md).

An unconfigured Call or Companion Channel seam **refuses to start**, with a
named error. An engine that silently loaded nothing behind a seam
looks exactly like a healthy one until it is needed — the outage ADR 0003 exists
to prevent. Running without a Companion Channel is legitimate, but the null
implementation ships with that adapter (#10) and is not built yet, so today it is
also a refusal that says so.

`[log]`'s three numbers are **required, with no default in code**: they are what
ADR 0004's outage measured, and a compiled-in fallback would quietly reinstate a
value the measurement proved matters. The log's *path* is a location rather than
a decision, so it defaults beside the state file. `max_bytes` binds every
generation, so the disk one log can occupy is `max_bytes × (retained_files + 1)`.

## Running headless

```bash
python -m gpt_voicecoding.engine --config ~/Library/Application\ Support/GPT-VoiceCoding/engine/config.toml
```

The engine stays in the foreground and never daemonises: the menu-bar shell
spawns it as a direct child and expects it to remain one (ADR 0005). `SIGINT` and
`SIGTERM` both stop it in order — loops cancelled, socket removed, so the next
start is not left claiming its own debris. It exits **2** when it could not start,
naming what was missing on stderr. A configuration refusal before log takeover
goes there in full. After takeover, only the final
`the engine cannot start: …` sentence is mirrored to the inherited stderr; the
full diagnostic stays in `engine.log`, which the engine owns (ADR 0004).
