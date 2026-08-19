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
outsiders see one Bridge Core.

**Hard constraint (ADR 0001, principle 1):** no protocol library — WebRTC,
Telegram, JSON-RPC framing, tmux — may ever be imported from this package, and
this package never imports ``gpt_voicecoding.adapters``. ``tests/test_architecture.py``
enforces both.
"""
