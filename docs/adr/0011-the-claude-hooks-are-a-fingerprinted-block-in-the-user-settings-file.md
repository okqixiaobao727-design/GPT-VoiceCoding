# 11. The Claude hooks are a fingerprinted block in the user's settings file

Date: 2026-08-25 · Status: Accepted · Source: [#71](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/71)

v1.0 is a bridge over every Session the user starts ([#67](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/67)), so nothing can be injected at launch and ADR 0007's `--plugin-dir` no longer reaches the Sessions that matter. Approval has no other route: upstream enforces that a peer message is never the user's approval, so the inbox socket that carries the Answer Relay can never carry a verdict. Two user-scope mechanisms remain — a plugin named in `enabledPlugins`, or a block in the config directory's `settings.json` — and both were probed live on Claude Code 2.1.245 in `~/.claude-b`, where #71 records the runs.

## Decision

**Two hooks, installed as one fingerprinted block in `<config dir>/settings.json`.** `SessionStart` registers the Session, because its payload carries the `transcript_path` that Claude Code's own registry does not and that cannot be derived without guessing at its directory naming. `PermissionRequest` is the approval wire: the hook process is held open, its return value is the verdict, and printing nothing hands the dialog back (`adapters/agent/claude/approval.py`).

**The fingerprint is the program the command runs, matched as a token and never as a substring** — ported from `legacy@1d32845:bridge/hookconfig.py:68-125` and adapted one token along, because here the program is an interpreter and the identity sits in a later argument. Install replaces only our handlers; uninstall keeps the other handlers in a matcher group; the render is `indent=2` with a trailing newline, which reproduces an untouched file byte for byte and so makes the round trip checkable.

**The plugin is rejected because it is cold.** Installed while a Session is running it never reaches that Session, which settles its plugin list at startup; the settings block is hot in both directions on the same running Session, with no restart and nothing typed into it. Uninstall was equally clean on both, so this was the only axis that separated them. An installation that asks the user to restart every terminal they already have open is not a bridge over the Sessions they already started.

**The engine publishes its address in a file; the hook reads it.** The launch wrapper's `GPT_VOICECODING_CLAUDE_CHANNEL_CONFIG` cannot exist in a Session the user started by hand. A missing or unreadable file is silence, which is the same fail-open the missing variable produced.

**Coverage is per `CLAUDE_CONFIG_DIR`.** The product is told which config directories to cover and installs into each; one machine can hold several, and they cannot see each other.

## Consequences

The engine read-modify-writes a file the user owns. **Concurrency is untested and is a real exposure in `~/.claude`**, where other tools rewrite the same file — ten of their backups sit beside it, which is why the probes ran in the clean directory instead. An install re-reads immediately before it writes, verifies what landed, and never holds the file open; two overlapping writers remain a way to lose a hook or somebody else's setting, and that failure is reported.

**ADR 0007 is superseded**, and its scope guarantee goes with it: the `--plugin-dir` route serves the pre-#67 Session definition, and its per-Session scope cannot be had at user scope. The hook now fires for **every** Session in the config directory, so "no phantom events" rests on the hook printing nothing when no engine holds that Session, and on the engine refusing what it does not hold. A Session the bridge does not hold pays ~33 ms, no socket opened and nothing written — so the guard stays the first thing the hook process does.
