# The end-to-end integration suite — design

Status: **design, approved on wayfinder ticket
[#51](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/51)**. It was the
first repair charter's exit-criterion item 2; that charter is superseded. Under map
[#58](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/58) (ADR 0010) the
exit criterion is the real-environment acceptance (`docs/acceptance-design.md`), and this
suite is a regression net — whether it is built, and what standing it has in CI, is decided
on ticket [#62](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/62).
Nothing here is built yet.

## What it proves

For **both** agent lanes, against one real engine, in one journey:

| step | what must be observed | where the observation comes from |
| --- | --- | --- |
| 1 launch | `bridgectl launch --request-id …` answers `status: launched` and the far-side stand-in was exec'd with the arguments the product relies on | control-plane reply; the stand-in's journal |
| 2 relay delivered | `bridgectl relay … "your words"` answers `state: delivered`, and the stand-in holds the **exact `request_id`** — Claude: it called `acknowledge_answer` with it; Codex: `thread/read` readback shows one `userMessage` with that `clientId` | control-plane reply; journal |
| 3 turn runs | the Reply Window closes and reopens: Claude via the registry record's `status` (`busy` → `idle`); Codex via `thread/status/changed`; `bridgectl sessions` reflects it | control-plane reply |
| 4 approval round-trips | the stand-in raises one permission request; the engine escalates it to **every outlet** (Companion Channel push observed at the fake Telegram API); `bridgectl approve <id> allow` answers `delivered`; the stand-in received `allow` | fake Telegram journal; control-plane reply; stand-in journal |
| 5 stop notice fires | the stand-in exits; the engine raises SessionStopped; the Stop Notice reaches the **first outlet**, which with Voice off is the Companion Channel — one `sendMessage` at the fake Telegram API | fake Telegram journal; `engine.log` |
| 6 inbound (Companion Channel) | a message injected through the fake API's `getUpdates` (`@<label>: words`) becomes a second delivered relay, and `engine.log` carries the inbound line #48 requires | control-plane reply; `engine.log` |

Step 6 is not in the charter's sentence; it is on the same running engine, costs one
message, and is the only automated proof of acceptance step 8's inbound half. It stays.

## Where the fakes sit — the one rule

**The product side of every seam is real. A fake is a process on the far side of a socket,
pipe or HTTP connection, speaking the real protocol.** Nothing in `src/` is replaced,
monkeypatched or injected with a closure. The engine is a real subprocess started by the
real runner; the surface is the real `bridgectl`.

| far side | stand-in | speaks |
| --- | --- | --- |
| the `claude` binary | `tests/e2e/fake_claude_code.py` — named in config as the Claude binary | argv contract of `session_launcher/claude.py` (two `--plugin-dir`, one `--channels`); MCP client over stdio to the **real** `channel.py`; runs the **real** hook command from the rendered `hooks.json` with a `PermissionRequest` payload; writes `$HOME/.claude/sessions/<pid>.json` in the registry shape `registry.py` reads |
| the `codex` binary | `tests/e2e/fake_codex.py` — named in config as the Codex binary | `app-server --listen unix://…` → serves the existing `tests/codex_fake.FakeAppServer` (real RFC 6455 + JSON-RPC, payloads from codex 0.148.0 schema); `--remote unix://… -C …` → a placeholder TUI that connects and idles; also answers the engine's own `app-server --enable realtime_conversation` spawn |
| the Telegram Bot API | the existing `tests/test_companion_channel.FakeTelegram` (`ThreadingHTTPServer`), lifted into `tests/e2e/` shared support; `api_root` in config points at it | `getUpdates` (blocking, queue-fed), `sendMessage` (journaled) |
| the Live Call | **none** — `[adapters] call = "fakes:FakeCall"`, Voice **off**, Message **on** | a seam-level fake, permitted by ADR 0001 principle 4; see "Ruled" |

Both stand-ins write a **journal** (one JSON line per event, path given by environment)
so the test asserts on what the far side actually saw, not on what the engine says it sent.

Each stand-in is **scripted, not clever**: the journey above is its whole script; a
deviation (missing `--channels`, a relay with no `request_id`, a hook payload it cannot
parse) is journaled and the stand-in exits non-zero. That is what makes the suite red on
today's `main`: #37/#42 (no channel plugin / selector), #39 (no attach to the Session's
app-server), #40 (SessionStopped never raised), #48 (no inbound line) all fail step 1, 2,
5 or 6 outright.

## Shape of the suite

```
tests/e2e/
  conftest.py            # engine fixture, config writer, journals, deadlines
  support.py             # FakeTelegram (lifted), journal reader, bridgectl runner
  fake_claude_code.py    # executable stand-in
  fake_codex.py          # executable stand-in
  test_claude_lane.py    # one test: the six steps
  test_codex_lane.py     # one test: the six steps
```

- **Engine**: `python -m gpt_voicecoding.engine --config <tmp>/config.toml` as a subprocess,
  `HOME=<tmp>`, socket and state under a `/tmp/gvc-…` dir (Darwin's 103-byte AF_UNIX cap, the
  `tests/test_runner.py` idiom). The engine's stdout/stderr are captured and printed on
  failure; `engine.log` is read by the test.
- **Config**: written by the fixture from the documented example
  (`docs/control-plane.md` § Configuration); factories are the shipped ones for
  `session_launcher` (`child:build`), `companion_channel` (telegram), both agents; the
  launcher's binaries are the two stand-ins by absolute path; one `[[launch.projects]]`
  entry whose workspace is a tmp dir.
- **Surface**: every action goes through `bridgectl` as a subprocess (`--socket` at the tmp
  path). Acceptance step 10 — headless reachability — is thereby proved, and the CLI's
  parse/timeout rules are on the path.
- **Deadlines**: derived, not picked — each step waits at most `client.timeout_for(action)`
  for the reply and a documented constant for far-side journal events; the whole test has
  a pytest timeout twice the sum.
- **Marker**: `@pytest.mark.e2e`, registered in `pyproject.toml`, **deselected by default**
  (`-m "not e2e"` in `addopts`) so `pytest -q` stays fast and hermetic; the suite runs as
  `pytest -m e2e tests/e2e`.
- **Isolation**: nothing touches the real `~/Library/Application Support`, `~/.claude`, or
  the user's socket — everything is under the tmp HOME. Two lanes may run in one session
  sequentially; they never share an engine.

## CI

A fourth job, `e2e`, on `macos-latest`, one Python (3.13), `timeout-minutes` set,
`pip install -e '.[dev]'`, `pytest -m e2e tests/e2e -v`. It does not need tmux (the
default launcher is `direct_child`, a pty), Node, Swift, a microphone, or any credential.
Journals and `engine.log` are uploaded as artifacts on failure.

## Ruled

- **The Live Call is not exercised here.** CI has no OpenAI credential and no audio device,
  and the `check` job does not install the `voice` extra. `FakeCall` sits behind the Call
  seam; the Stop Notice and the approval announcement are proved on the Companion Channel
  route of the escalation matrix (Voice off). The voice route — the spoken Stop Notice, the
  spoken instruction, the spoken verdict — is proved by the **real-environment automated
  acceptance** (a separate map ticket; it replaces the manual acceptance run).
- **`tests/claude_fake.FakeChannel` is not used.** It stands in for the channel *server*,
  which is product code; here the real server runs and the stand-in is its MCP client.
- **No tmux lane.** `direct_child` is the default launcher and the one the shell uses; the
  tmux adapter keeps its own unit tests.
- **One journey per lane, not a matrix.** Failure cases (deny verdict, expired relay,
  refused launch) belong to the unit suites that already cover them against fakes.

## Not yet decided (build-time, advisor-free)

- Whether the placeholder Codex TUI needs to hold its socket open for the readback (depends
  on #39's fix); the stand-in follows the fix.
- The exact registry `status` transitions the Claude stand-in writes, read off `window.py`
  at build time rather than from memory.
