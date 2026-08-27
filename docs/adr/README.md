# Architecture decisions

| ADR | Decision |
| --- | --- |
| [0001](0001-hub-and-spoke-bridge-core-with-seams.md) | Bridge Core is a hub; everything else is a deep module behind a seam |
| [0002](0002-the-control-plane-is-never-gated-by-switches.md) | The control plane is never gated by switches |
| [0003](0003-the-engine-reports-what-it-loaded.md) | The engine reports what it loaded, and liveness checks read that answer |
| [0004](0004-the-engine-owns-its-log.md) | The engine owns its log, so rotation can rename rather than truncate |
| [0005](0005-the-engine-lives-inside-the-app-bundle.md) | The engine lives inside the app bundle, because that is what earns the microphone grant |
| [0006](0006-the-claude-channel-server-is-python-and-stdlib-only.md) | The Claude Session Channel server is Python, and speaks MCP with the standard library alone — **superseded by 0013** |
| [0007](0007-the-approval-hook-is-a-session-scoped-plugin.md) | The Claude Approval Relay's hook is registered as a session-scoped plugin — **superseded by 0011** |
| [0010](0010-legacy-is-the-behaviour-spec.md) | The seam architecture stays, and the first generation is the behaviour spec it must satisfy |
| [0011](0011-the-claude-hooks-are-a-fingerprinted-block-in-the-user-settings-file.md) | The Claude hooks are a fingerprinted block in the user's settings file |
| [0012](0012-installation-runs-at-first-launch.md) | Installation runs at first launch, from a package that needs no engine |
| [0013](0013-the-answer-relay-rides-the-sessions-own-inbox-socket.md) | The Claude Answer Relay rides the Session's own inbox socket, and an accepted write is not a receipt |
| [0014](0014-question-answers-ride-the-approval-hook.md) | A question answer rides the Approval Relay as a typed verdict |

0008 (headless direct-child launcher) and 0009 (a launch carries its Opening Instruction) were removed with the launcher when v1.0 became a bridge over Sessions the user starts ([#67](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/67), [#68](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/68)); they return with the launch map, from git history.

## Provenance

The decisions above were taken in the reference implementation and locked by its [wayfinding map](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/issues/11); each ADR links its source. Legacy ADRs deliberately not carried: [0001 launchd supervision](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/blob/main/docs/adr/0001-launchd-supervision-for-the-bridge-daemon.md) (superseded by ADR 0005), [0002 session retention](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/blob/main/docs/adr/0002-session-retention-and-indexed-session-reads.md) (a schema lesson for a store not yet built), [0005 Live Driver on the GUI](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/blob/main/docs/adr/0005-live-driver-stays-on-the-gui-hotkey-quarantined-as-an-adapter.md) (superseded by the bridge-owned call, ADR 0001).
