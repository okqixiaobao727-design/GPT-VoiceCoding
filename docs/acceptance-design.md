# The real-environment automated acceptance — design

Status: **design, decided on wayfinder ticket
[#56](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/56)** (Simon,
2026-08-25). It replaces the manual v0 acceptance in `docs/app-bundle.md` § The v0
acceptance for everything it covers. Under map
[#58](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/58) (ADR 0010) this
run is the **exit criterion** of the repair phase, and it is built **first** — before any
further repair — on ticket
[#60](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/60). This document
assumed the E2E suite (`docs/e2e-suite-design.md`) would exist beforehand and lend its
support code; it does not, and whether to build that support module here or first is #60's
own call. Nothing here is built yet.

## The one decision this design rests on

**The voice route is not proved by automation. Text stands in for speech.**

A spoken instruction reaches Bridge Core as a structured control-plane call the voice
thread makes (`core/bridge.py:29`); the voice layer's own work is speech↔text and a
realtime model choosing which `bridgectl` action to run. Automating that means audio
devices, a transcription step, and an LLM in the loop — three sources of flake guarding
one user's v1. Simon ruled it out: the acceptance drives the **same control-plane actions
the voice thread would**, from text, deterministically. What the voice layer adds on top
— hearing a Stop Notice, saying an instruction, saying a verdict — is proved by nothing
automated and is listed on the map as out of scope.

Two facts closed the alternative of feeding text *into* the Live Call: codex 0.149.1's
app-server exposes no method to append user text to a realtime session (the engine's
`appendSpeech` is the engine speaking *to* the user), and the websocket transport that
would take raw audio refuses ChatGPT auth (`docs/research/2026-08-24-realtime-session-model-rejection.md`).

## What it proves

The E2E suite's journey, **against the real far side**: the real `claude` and `codex`
binaries, the real Telegram bot, a real person's Telegram account typing the inbound
message. Every action goes through `bridgectl` against the installed bundle's engine.
No human step; a verdict file at the end.

| step | what must be observed | where the observation comes from |
| --- | --- | --- |
| 0 preflight | the bundle is installed and matches the tree that built it; `claude` and `codex` resolve on the engine's PATH; the Telegram bot answers `getMe`; the user-account session is valid; **no other consumer is polling this bot** (the shell's engine is not running); a fresh disposable workspace exists | provenance byte-compare (`docs/app-bundle.md`, the `diff -r` precedent); `bridgectl verify`; the user-account client; the real engine socket answering nobody |
| 0b realtime contract probe | an engine-free `codex app-server` client sends the v3 realtime start and receives audio frames back, with no microphone and no speaker | the bundle's own interpreter running the `--silent` probe `docs/app-bundle.md` prescribes before any voice attribution; the count of frames received |
| 1 launch | `bridgectl launch --request-id …` answers `status: launched`; the launched Session **performs its Opening Instruction unattended** — the thing the E2E suite's stand-ins cannot prove (noted on #51 from #39's triage) | control-plane reply; the effect of the Opening Instruction in the disposable workspace; Claude: a transcript under the Claude projects directory for that workspace |
| 2 approval round-trips | the Opening Instruction needs exactly one permission; the engine escalates it to every outlet — with Voice off, the Companion Channel — and the **real bot's message is read by the user-account client** in the private chat; `bridgectl approve <id> allow` answers `delivered`; the Session goes on to finish the task | user-account client (the far side of the real Telegram API); control-plane reply; the workspace |
| 3 turn runs | the Reply Window closes and reopens around that turn; `bridgectl sessions` reflects it | control-plane reply |
| 4 stop notice fires | the Session's turn ends; the engine raises SessionStopped; with Voice off the Stop Notice reaches the first outlet, the Companion Channel — one message the user-account client reads | user-account client; `engine.log` |
| 5 relay delivered | `bridgectl relay <session> "…"` answers `state: delivered` and the Session **acts on the words** — a second observable effect in the workspace | control-plane reply; the workspace |
| 6 inbound (Companion Channel) | the user-account client sends `@<label>: …` to the bot; the engine's `getUpdates` sees it; it becomes a delivered relay with a third effect in the workspace; `engine.log` carries the inbound line #48 requires | control-plane reply; the workspace; `engine.log` |
| 7 the switches | Duty off: a Stop Notice raised now is **not** pushed (no bot message inside the wait window) yet `bridgectl status` still answers; Duty on again, Voice off / Message on is the text-only mode this whole run exercises | user-account client (a negative observation over a derived window); control-plane reply |
| 8 close | `bridgectl close <session>` answers; the agent process is gone; `/tmp/vc-approvals-<pid>` is gone (#44) | control-plane reply; the process table; the filesystem |

Both lanes run the journey, sequentially, each against a fresh engine and a fresh
workspace; they never share either. Acceptance step 10 of the old checklist — headless
reachability — is proved by construction, as in the E2E suite.

Step order differs from the E2E suite's because a real agent's first turn is its Opening
Instruction: the approval comes *inside* the first turn, the Stop Notice at its end, and
the relays after. The six E2E observations are all present; they fall where a real agent
puts them.

## The far side is real; the one actor

There are **no stand-ins**. The only thing the harness *plays* is the person at the
Telegram keyboard: a **user-account MTProto client** (Telethon), on Simon's own account,
in the existing private chat with the bot — the `chat_id` the engine's config already
names. A bot cannot message a bot, so this is the only way inbound can be real.

The client is also the acceptance's eyes on outbound: what the bot sent is read back from
the chat by a real Telegram client, which is the far side of the Bot API, rather than
trusted from the API's `sendMessage` reply.

Messages cannot carry a run marker — the inbound grammar (`@<label>: words`) has no room
for one — so the harness distinguishes its traffic by message id and the run's time
window, and every message it sends or reads is journaled.

Telethon is a forbidden import for Bridge Core and the seams (`tests/test_architecture.py`);
it lives only under `tests/acceptance/` and is declared as an `acceptance` extra in
`pyproject.toml`, installed into the developer venv, never into the bundle.

## Shape of the harness

```
tests/acceptance/
  conftest.py            # preflight, engine-under-test fixture, run directory, deadlines
  telegram_person.py     # the user-account client: read the chat, send the inbound line
  test_claude_lane.py    # one test: the journey
  test_codex_lane.py     # one test: the journey
  test_realtime_probe.py # step 0b
```

- **Reuses** `tests/e2e/support.py` — the journal reader, the `bridgectl` runner, the
  derived deadlines. The acceptance is the E2E journey with the far side swapped for
  reality; the step assertions are written once.
- **Engine under test**: the installed bundle's own interpreter,
  `/Applications/GPT-VoiceCoding.app/Contents/Resources/engine/bin/python3 -m
  gpt_voicecoding.engine --config <run>/config.toml`, spawned by the harness with the
  real `HOME` (the real `claude` needs its own login and registry) and the login shell's
  PATH (the same resolution the shell performs, so the launcher finds the real binaries).
- **Config**: derived from the user's real `config.toml` — every value copied, then
  `[engine] socket_path` and `state_path` and `[log] path` pointed under the run directory,
  `[[launch.projects]]` replaced by the one disposable workspace, `[delegate] cli` left at
  the bundle's `bridgectl`. Voice **off**, Message **on**, Duty **on** for the whole run
  (flipped only in step 7). The engine therefore never opens a Live Call; step 0b is
  engine-free by design.
- **Marker**: `@pytest.mark.acceptance`, registered beside `e2e`, deselected by default,
  **never run in CI** — it needs this machine, these credentials, and this bot. Run as
  `.venv/bin/python -m pytest -m acceptance tests/acceptance -v`.
- **Deadlines**: derived, as in the E2E suite — `client.timeout_for(action)` for replies;
  for far-side events (a bot message appearing, a file appearing) a documented constant
  with headroom over a measured real-agent turn, never a guess.

## The disposable workspace and the Opening Instruction

Each lane gets a fresh `git init` directory under the run directory. The Opening
Instruction and the two relayed instructions are **small, deterministic, file-producing
actions** in that directory (the shape: "create a file named X containing the word Y"),
chosen so that:

- the first one raises **exactly one** permission request on that lane;
- each leaves an effect the harness can read off the filesystem, so "the Session acted
  on the words" is a fact from the far side rather than a claim from the engine.

The exact wording per lane, and each lane's permission mode, are **measured at build time
against the real binaries** and recorded in the harness beside the assertion they serve
— not taken from memory. A real agent that does not perform the action is a FAIL with the
workspace and transcript as evidence; that is the point of the run.

The workspace is kept with the run's artifacts. Nothing outside the run directory is
written by the agents; the harness never launches into a real project.

## Preflight refuses rather than runs

A run that starts against the wrong environment produces a verdict that cannot be
attributed to this engine (`docs/app-bundle.md` says the same of the manual run). So
step 0 **refuses** — exit non-zero, verdict `REFUSED` with the reason — on any of:

- the bundle absent, or its engine not byte-identical to the tree the run was asked to
  accept;
- the shell's engine (the real socket) answering — one bot, one engine
  (`docs/app-bundle.md` § Cutover);
- `bridgectl verify` failing against the run's config;
- the user-account session missing or not authorised;
- `claude` or `codex` not resolving on the PATH the engine will be handed.

## Credentials — what the run needs from Simon, once

| credential | how the run gets it | where it lives | never |
| --- | --- | --- | --- |
| Telegram **bot** token | the shipped mechanism: `token_env` in the config names the variable; the harness exports it into the engine's environment the way the shell does (#55) | wherever #55 puts it | in the repo, in the run directory |
| Telegram **user account** (Telethon) `api_id`, `api_hash`, and the authorised session | a one-time interactive login Simon performs (a HITL task on the map): `my.telegram.org` issues the id/hash, Telegram sends one code; Telethon writes a session file | a 0600 file under `~/Library/Application Support/GPT-VoiceCoding/acceptance/`, path given to the harness by environment variable | in the repo, in the journal |
| `claude`, `codex` logins | the real binaries' own | theirs | touched by the harness |

There is no OpenAI key: the realtime probe uses codex's own ChatGPT auth, as the product does.

## Artifacts and the verdict

Every run writes `~/Library/Application Support/GPT-VoiceCoding/acceptance/<run-id>/`
(`<run-id>` = UTC timestamp):

```
config.toml          # the derived config the engine ran on (token never in it)
engine.log           # the engine's own log, as it left it
journal.jsonl        # one JSON line per harness event: every bridgectl call and reply,
                     # every Telegram message sent or read (id, direction, text), every
                     # far-side observation with its timestamp
workspace-claude/    # the disposable workspaces, as the agents left them
workspace-codex/
verdict.json
```

`verdict.json` is the one artifact a reader needs: the run id, the bundle path and the
commit it was byte-compared against, the codex and claude versions seen, and per step per
lane a result from the closed set `PASS | FAIL | REFUSED | SKIPPED` with one line of
evidence (the journal line it rests on). A run's result is `PASS` only when every step on
every lane is `PASS`.

## Cost and blast radius, per run

- Two real agent Sessions (one per lane), each three short turns in a throwaway directory.
- Four to six real messages in Simon's private chat with the bot — read back and sent by
  his own account. They look like what the product sends; they are not deleted.
- One realtime connection of a few seconds, silent (step 0b). No Live Call is opened
  through the engine.
- The shell's engine must be **stopped** for the duration (one bot, one engine); the run
  refuses otherwise and never stops it itself.

## Ruled

- **The voice route is out.** Spoken Stop Notice, spoken instruction, spoken verdict —
  proved by nothing automated. On the map as out of scope; the closing summary says so.
- **The menu-bar shell is out.** Launching a Live Call from the menu bar, the shell's PATH
  hand-off and its #55 token injection are covered by `swift test`, not by this run. Shell
  end-to-end is a post-launch map.
- **No LLM in the loop as a driver.** The Delegated Turn (`>` from Telegram) — the text
  twin of the voice thread — was considered as an advisory step and dropped: the run is
  deterministic or it is not an acceptance.
- **Not #12, not the manual checklist.** `docs/app-bundle.md` § The v0 acceptance stays as
  history and as the source of the cutover checks step 0 automates; it is not run.
- **The engine is spawned by the harness, not by the shell.** Repeatability over coverage;
  the shell is out of scope above.

## Not yet decided (build-time, advisor-free)

- The exact Opening Instruction and relayed instructions per lane, and each lane's
  permission mode, measured against the real binaries.
- How the harness resolves the login shell's PATH for the engine: reuse of the shell's
  method versus `$SHELL -lc 'echo $PATH'`; whichever it is, it is one place.
- The far-side wait constants (a real agent turn, a Telegram round-trip), measured before
  they are written.
- Whether step 7's negative observation needs its own Stop Notice raised (a second short
  turn) or can reuse step 4's timing — decided from what the real agents do.
