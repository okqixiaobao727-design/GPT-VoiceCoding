"""Bridge Core — the hub. Owns every policy and the system's single source of truth.

Bridge Core decides; the modules around it do. Policy that lives here and nowhere
else: the Stop Notice escalation pipeline, Relay queueing against the Reply
Window, the Approval Relay budget and its fallback, the one-call-at-a-time
invariant, and switch adjudication.

State held here and nowhere else: switch state, the Session registry, the
undelivered Relay queue. Modules keep no copies; every surface queries the hub.
The durable subset is persisted by an internal storage component only Bridge Core
touches — nothing else may read those files, or the disk becomes a second truth.

Bridge Core may grow internal components (escalation pipeline, relay queue,
approval budget, persistence), separately testable but *not* new external seams:
outsiders see one Bridge Core. The ones that hold state are here now:

- ``switches`` — the Duty / Voice / Message hierarchy and the Feature Switches
  configuration declares beneath it.
- ``sessions`` — the Session registry: exact targets, stale ones refused, labels
  that disambiguate or ask.
- ``relay_queue`` — the *one* ledger of everything undelivered, retained Stop
  Notices included.
- ``persistence`` — the durable subset, and the only component that touches disk.
- ``state`` — the three of them assembled, and the single persistence path.
- ``events`` — the one queue every seam's events arrive on.

**Hard constraint (ADR 0001, principle 1):** no protocol library — WebRTC,
Telegram, JSON-RPC framing, tmux — may ever be imported from this package, and
this package never imports ``gpt_voicecoding.adapters``. ``tests/test_architecture.py``
enforces both.
"""
