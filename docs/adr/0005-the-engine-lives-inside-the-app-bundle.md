# 5. The engine lives inside the app bundle, because that is what earns the microphone grant

Date: 2026-08-20 · Status: Accepted · Source: [packaging research](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/issues/14), [TCC attribution probe](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/issues/24)

macOS TCC attaches the microphone grant to whoever owns the process. The legacy LaunchAgent daemon put it on the bare `python3.12` path. The probe showed the grant lands on the bundle id whenever the executable is *inside* the `.app`, and collapses to the binary path outside it — launchd versus direct spawn makes no difference.

## Decision

**The engine executable lives inside the app bundle**: a thin Swift menu-bar shell spawns a bundled python-build-standalone engine as a direct child, everything signed under one identity, no library-validation exceptions. Direct spawn is the default topology (parenthood, health and restart in one place), not what earns the grant.

## Consequences

The shell's only relationship to the engine beyond the control plane is process parenthood (ADR 0001). Fallback if containment ever stops holding: single-process packaging via Briefcase. v0 is ad-hoc signed and not notarized, so an update may re-prompt for the microphone — accepted; signing, notarization and auto-update fit route (a) later without architectural change.
