# 7. The Claude Approval Relay's hook is registered as a session-scoped plugin

Date: 2026-08-21

Status: Accepted

Taken in: [Build: Claude Agent adapter — Approval Relay (PermissionRequest hook)](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/14)

## Context

The Approval Relay's route into a Claude Session is the `PermissionRequest` hook:
a process Claude Code starts when a permission dialog is displayed, whose printed
decision resolves that dialog. The issue locked the route and left "registration
mechanics across Claude settings" open.

Registering a hook has three mechanisms, and the difference between them is not
convenience — it is blast radius.

- **`~/.claude/settings.json`.** The engine read-modify-writes a user-owned file
  it otherwise never touches. One bad merge takes somebody else's hooks with it,
  and the engine acquires a write to a file whose other contents are none of its
  business.
- **Inside the installed channel plugin** (ADR 0006's plugin). One install would
  cover both routes — but a plugin's hooks fire for **every** Claude Code session
  on the machine, including every session this engine never launched. The scope
  would then rest on the hook's own good manners rather than on structure. It
  also inherits the channel's dependency on an administrator-owned
  managed-settings entry.
- **`claude --plugin-dir <path>`**, which loads a plugin **for that session
  only**. Verified live against Claude Code 2.1.238 with a plugin containing
  nothing but `.claude-plugin/plugin.json` and `hooks/hooks.json`: the hook fired
  and received its stdin payload. No marketplace, no `claude plugin install`, no
  managed-settings entry, nothing written outside this engine's own runtime tree.

The issue's own "Done when" asks that the hook "installs, uninstalls, and
survives a settings round trip" — wording that assumes the first mechanism.

## Decision

**The hook is rendered as its own plugin directory and loaded per Session with
`--plugin-dir`.**

- **A separate plugin from the channel's.** This decision originally followed
  from the channel being marketplace-installed while the hook was inline. The
  Session Channel launch probe below later established that both can be inline.
  Their established identities and directories remain separate so either Relay
  route can be loaded or absent independently; this wiring repair does not merge
  two plugin identities into one.
- **One name, chosen once**: `gpt-voicecoding-approval-hook`. Claude Code caches a
  plugin by name and version and that cache outlives what it came from, so a
  planned rename is scheduled identity churn — the same reasoning ADR 0006's
  plugin name rests on. Its version carries a fingerprint of what the plugin
  actually says, so a changed hook command is a new directory by construction.
- **Install is rendering two files; uninstall is taking exactly those two back.**
  No settings file is read or written on either path.
- **The scope is structural, not disciplinary.** A Session launched without the
  flag has no hook to fire, so "no phantom events" is impossible rather than
  guarded. The hook's own check — no bootstrap variable, no engine to ask, print
  nothing — remains as a second line and not as the scope.

The manifest names no interpreter, for the reason ADR 0006 gives: which Python
runs it is a property of the deployment, so it is an argument the launcher and
the bundle supply.

### Session Channel launch probe

On 2026-08-25, Claude Code 2.1.241 was probed with the approval hook and the
Session Channel supplied as two repeated `--plugin-dir` arguments, plus
`--channels plugin:gpt-voicecoding-session-channel@gpt-voicecoding-channel`.
The probe used an isolated `HOME` and `CLAUDE_CONFIG_DIR` containing no installed
plugins or marketplaces. Claude Code loaded the Session Channel as an inline
plugin, connected its MCP server, and exposed `acknowledge_answer`; its debug log
also recorded that no marketplaces were declared. Authentication failed only
after plugin initialization, so it did not affect the observed loading and MCP
connection.

The isolation did not and cannot remove the administrator-owned
`/Library/Application Support/ClaudeCode/managed-settings.json`. The probe
machine had channels enabled and an `allowedChannelPlugins` entry matching
`gpt-voicecoding-session-channel@gpt-voicecoding-channel`. That managed policy
remains a deployment precondition: without it Claude Code does not admit the
channel. The probe establishes that marketplace registration and installation
are unnecessary; it does not establish that administrator admission is
unnecessary.

Therefore a launch renders the Session Channel plugin into its own per-launch
directory, passes that directory as a second `--plugin-dir`, and selects it with
`--channels`. Discarding the launch removes the per-launch directory and both
plugins with it. No marketplace registration, installation, cache entry, or
user-owned Claude configuration is part of this route; the managed policy above
is still required.

## Consequences

**The Session Launcher gains an obligation.** Every Claude Session it launches
must pass `--plugin-dir` naming the rendered hook plugin, exactly as it already
carries the channel bootstrap variable — which now also carries the approval
socket's address. That obligation is recorded as
[obligation 7 on the Session Launcher issue](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/9#issuecomment-5364831584).
Until it lands, the manual proof script passes the flag by hand, the same stand-in
the channel proof already makes for the launch wrapper.

**The `--plugin-dir` behaviour is part of the 2.1.238 pin**, alongside this
route's wire shapes. It is undocumented and was established by live probe, so a
Claude Code upgrade re-verifies it the way an upgrade re-probes the peer socket's
protocol number.

**"Survives a settings round trip" is satisfied by construction**, not by the
mechanism the issue imagined: there is no settings file in this story, so there is
nothing a round trip could lose. That is asserted as a test — nothing outside the
rendered directory is ever written — rather than left as a claim.

**Uninstall leaves a caller's own files alone.** The plugin directory is a
configured path, so it may not be this engine's alone; the uninstall removes the
files it wrote and takes the directories back only if they are empty.

If the `--plugin-dir` route is ever withdrawn by Claude Code, the fallback is the
installed-plugin mechanism plus the hook's bootstrap-variable check carrying the
scope — which is a weaker guarantee, and would be a decision to reopen here
rather than a change to make quietly.
