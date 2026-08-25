# 7. The Claude Approval Relay's hook is registered as a session-scoped plugin

Date: 2026-08-21 · Status: Superseded by [ADR 0011](0011-the-claude-hooks-are-a-fingerprinted-block-in-the-user-settings-file.md) · Source: [#14](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/14)

The Approval Relay's route into a Claude Session is the `PermissionRequest` hook. Three ways to register it differ by blast radius: `~/.claude/settings.json` (the engine writes a user-owned file), an installed plugin (fires in every Claude session on the machine), or `claude --plugin-dir <path>` (that session only — verified live on 2.1.238).

## Decision

**The hook is rendered as its own plugin directory and loaded per Session with `--plugin-dir`.** Separate from the channel plugin so either route can be loaded or absent independently. One name, `gpt-voicecoding-approval-hook`, chosen once because Claude Code caches plugins by name; its version carries a fingerprint of the hook command. Install renders two files; uninstall takes exactly those back; no settings file is touched. Scope is structural: a Session launched without the flag has no hook to fire.

A 2026-08-25 probe on 2.1.241 with an isolated `HOME` showed both plugins load inline as repeated `--plugin-dir` arguments plus `--channels`, with no marketplace or install — but the administrator-owned `managed-settings.json` `allowedChannelPlugins` entry remains a deployment precondition.

## Consequences

Every launched Claude Session must pass `--plugin-dir` for the rendered hook. `--plugin-dir` is undocumented and part of the Claude Code version pin; an upgrade re-verifies it with the hook contract. If Claude Code withdraws it, the fallback is the installed-plugin mechanism with the hook's bootstrap-variable check as scope — a weaker guarantee, to be reopened here.

This route depends on launch-time injection and serves the pre-#67 Session definition. [#71](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/71) proved the user-scope route live on 2.1.245 and it replaces this one: see [ADR 0011](0011-the-claude-hooks-are-a-fingerprinted-block-in-the-user-settings-file.md).
