# The menu-bar shell

A thin Swift menu-bar app.

Two responsibilities, and no third:

1. **Process parenthood.** It spawns the Python engine as a direct child, from
   inside its own `.app` bundle, and handles health and restart. Bundle
   containment is what earns the microphone grant — see
   [ADR 0005](../docs/adr/0005-the-engine-lives-inside-the-app-bundle.md).
2. **A control-plane surface.** Its dropdown *is* the Control Panel in v0. It
   speaks the same JSON-over-UDS control plane as `bridgectl` and the Companion
   Channel, and holds no policy and no state of its own — every value it shows is
   read from Bridge Core.

There is no private protocol between the shell and the engine. If you find
yourself adding one, read
[ADR 0001](../docs/adr/0001-hub-and-spoke-bridge-core-with-seams.md) first.

## Building

SwiftPM, not an Xcode project: this has to build from a checkout that has only
the Command Line Tools.

```bash
cd shell
swift build
swift test --disable-xctest
```

`--disable-xctest` is required, not cosmetic: XCTest ships with Xcode, and the
tests are written in swift-testing, which the package takes as an explicit
dependency for the same reason. With Xcode installed, a bare `swift test` also
works.

## Running it

`MenuBarExtra`, `LSUIElement`, `SMAppService.mainApp` and the microphone grant
all need a real bundle, so the shell cannot be shown to work as a bare
executable:

```bash
shell/scripts/dev-app.sh          # then: open shell/.build/GPT-VoiceCoding.app
```

That script assembles a **development** bundle and ad-hoc-signs it. It is not a
distribution pipeline — #12 owns that and may replace the script wholesale. The
bundle's identity lives in [`Resources/Info.plist`](Resources/Info.plist), which
is the one place #12 consumes or supersedes.

To exercise the path ADR 0005 is actually about — an engine spawned from *inside*
the bundle — hand it an interpreter tree you already have:

```bash
shell/scripts/dev-app.sh debug --engine .venv
```

It is copied to `Contents/Resources/engine/`, where the shell looks first. The
flag copies; it downloads nothing and vendors nothing. What it proves is that
the bundled branch really spawns and that the child is the shell's own. What it
does **not** prove is #12's part: python-build-standalone, the inside-out
enumerate-and-sign, the entitlement on the bundled interpreter, and the binary's
name. Those stay #12's acceptance.

One thing worth knowing before you pick a tree: a **framework** CPython (the
Homebrew one, and any virtual environment over it) re-executes
`Python.app/Contents/MacOS/Python` from its original location, so the process
that ends up running is outside the bundle even though the shell spawned the one
inside it. That is the interpreter's own behaviour, not the shell's — and it is
the reason #12's locked choice is python-build-standalone's relocatable
`install_only` build, which does not do it.

If nothing appears in the menu bar, the menu bar is full: macOS creates the
status item and places it off-screen rather than dropping it. `System Events`
will confirm it exists —

```bash
osascript -e 'tell application "System Events" to tell process "GPTVoiceCodingShell" \
  to return {description, position} of every menu bar item of menu bar 2'
```

— and hiding another menu-bar item makes room.

### Which engine it spawns

One resolver, in this order:

1. the interpreter bundled under `Contents/Resources/engine/` — the shipping
   shape, and the one ADR 0005 requires;
2. `GPTVOICECODING_ENGINE_PYTHON`, when it names one;
3. the first `python3` on `PATH`.

Two and three are the **developer path**, and it is a stated feature rather than
a stopgap: headless mode stays real, and the engine runs standalone with or
without this shell.

The socket is read from `[engine] socket_path` in the same configuration file
the engine is spawned with — never derived from the state path, because Darwin
caps a socket path at 103 bytes. The default is mirrored from
[`docs/control-plane.md`](../docs/control-plane.md), which is the canonical
statement of it.

## What it decides, and what it does not

The shell owns exactly one policy, because it is the only one that is process
parenthood rather than a seam concern:

- It restarts the engine on **every** exit, including a clean one. An exit-0
  crash class took the old Bridge down; `KeepAlive: true` is the lesson.
- Consecutive fast failures — died before 60 seconds up — back off 1s, 2s, 4s,
  8s, and after five of them it **stops and says so**, with the engine's own
  stderr and a Retry button. An endless silent retry loop looks exactly like a
  healthy system to the only person who could fix it.
- Exit 2 with something already listening on the socket is not a crash: a second
  engine refuses without touching the first one's socket. That state says so and
  offers Retry rather than spawning against a live engine.

Everything else it shows or flips is a control-plane action, including the Live
Toggle — Bridge Core decides whether that starts or ends a call, and no call
state is held here.

## Three failures, three sentences

They are kept apart on purpose:

| What happened | Who is speaking |
| --- | --- |
| Bridge Core refused | the engine, verbatim — `error.message` is never rephrased |
| Nothing answered (`engine_unreachable`) | this shell, about itself; the engine never sends this |
| There is no engine process | process parenthood, which the control plane cannot see |

## Reading

It reads `status` when the dropdown opens, about once a second while it stays
open, and again after every action. There is no background timer: a poll nobody
is looking at is a permanent metronome in a bounded log whose value is measured
in signal (ADR 0004). `verify` is asked only when a person asks for it.
