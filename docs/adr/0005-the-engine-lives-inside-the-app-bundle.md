# 5. The engine lives inside the app bundle, because that is what earns the microphone grant

Date: 2026-08-20

Status: Accepted

Carried over from: [Research: packaging the Python engine inside a notarized menu-bar app](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/issues/14)
and the probe that gated it, [Task: microphone TCC attribution probe](https://github.com/okqixiaobao727-design/GPT-VoiceCoding-legacy/issues/24).

## Context

The product needs a microphone grant, and macOS TCC decides *who* that grant
attaches to. The reference implementation is the documented-bad shape: its daemon
runs from a LaunchAgent, outside any `.app`, and the grant lands on the bare
interpreter path — which means it belongs to whatever `python3.12` happens to be,
not to the product.

A probe (`TCCProbe`) was built to settle the attribution empirically rather than
by reading documentation.

## Decision

**Route (a): a thin Swift menu-bar shell that spawns a bundled
python-build-standalone engine as a direct child** (`Process` / `posix_spawn` —
never `launchd`, never `open`), with everything re-signed under one identity and
no library-validation exceptions.

The probe confirmed the grant lands on the app bundle: `AUTHREQ_SUBJECT` is the
bundle id and the prompt reads *"TCCProbe" would like to access the Microphone*
with the app's own usage string. `python3.12` never appears. It survives a second
run, and the child outliving the shell.

**The load-bearing mechanism is bundle containment, not the responsibility chain.**
A LaunchAgent-launched interpreter *inside* the bundle still resolves to the app;
the same process tree *outside* any `.app` collapses to the bare binary path and
re-prompts. So the hard constraint this ADR fixes is exactly one sentence:

> The engine executable lives inside the app bundle.

Direct `Process` spawn stays the default — it is the simplest topology and keeps
process parenthood, health and restart in one place — but it is **not** what earns
the grant, and launchd is not what loses it.

Also settled by the probe: the two microphone services are granted together, and
rebundling under the same bundle id and signing identity does not re-prompt.

## Consequences

The menu-bar shell is not a module with a private protocol. Its only relationship
to the engine beyond the control plane is **process parenthood** — spawn, health,
restart (ADR 0001).

The fallback, if bundle containment ever stops holding, is single-process packaging
via Briefcase.

**v0 is not signed with a Developer ID and is not notarized.** v0 targets
developers: source build and GitHub Releases. Ad-hoc signatures change per build,
so an update may re-prompt for the microphone — a known, accepted v0 cost. Signing,
notarization and auto-update wait for adoption; route (a) accommodates all three
later without architectural change.
