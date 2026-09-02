"""Bridge Core — the hub. Owns every policy and the system's single source of truth.

Bridge Core decides; the modules around it do. Policy that lives here and nowhere
else: the Stop Notice escalation pipeline, Relay queueing against the Reply
Window, the one-call-at-a-time invariant, and switch adjudication. **The Approval
Relay is not on that list** (#191): it carries a verdict to the dialog the roster
says is open and decides nothing about how long that dialog lives, because the
wire that holds the hook is what bounds it (ADR 0015, amended).

State held here and nowhere else: switch state, the Session registry, the
undelivered Relay queue. Modules keep no copies; every surface queries the hub.
The durable subset is persisted by an internal storage component only Bridge Core
touches — nothing else may read those files, or the disk becomes a second truth.

Bridge Core may grow internal components (escalation pipeline, relay queue,
persistence), separately testable but *not* new external seams: outsiders see one
Bridge Core.

The ones that hold state:

- ``switches`` — the Duty / Voice / Message hierarchy and the Feature Switches
  configuration declares beneath it.
- ``sessions`` — the Session registry: exact targets, stale ones refused, Session
  Names that disambiguate or ask.
- ``relay_queue`` — undelivered Answer Relays waiting on a Reply Window.
- ``persistence`` — the durable subset, and the only component that touches disk.
- ``state`` — the three of them assembled, and the single persistence path.
- ``events`` — the one queue every seam's events arrive on.

The ones that decide — the policy, all of it:

- ``adjudication`` — what the switches permit the *system* to do. Never consulted
  by the control plane (ADR 0002).
- ``interlock`` — one call at a time, above the Call seam. The only door to
  opening one.
- ``escalation`` — the Stop Notice route matrix for one delivery attempt.
- ``relays`` — queueing the user's own words against the Reply Window, and the
  ceiling on how long they may wait.
- ``router`` — what inbound text means. Unknown or ambiguous fails closed.
- ``verification`` — what configuration named against what the engine loaded
  (ADR 0003); only the hub knows the configured side.
- ``bridge`` — the five of them assembled, and the one dispatch that feeds them.

And the substrate all of them share: ``lifecycle`` (the five state names for
anything pending), ``policy`` (every configurable duration), ``clock`` (where a
duration is measured from), and ``errors`` (what a refusal looks like).

**Hard constraint (ADR 0001, principle 1):** no protocol library — WebRTC,
Telegram, JSON-RPC framing, tmux — may ever be imported from this package, and
this package never imports ``gpt_voicecoding.adapters``. ``tests/test_architecture.py``
enforces both.
"""
