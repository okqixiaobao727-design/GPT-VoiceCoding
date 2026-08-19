# The menu-bar shell

A thin Swift menu-bar app. Not built yet.

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
