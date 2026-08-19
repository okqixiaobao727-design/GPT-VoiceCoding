"""The Call seam — the system's one voice surface.

Verbs Bridge Core calls: ``ensure_call`` and ``end_call`` (the two halves of the
Live Toggle), ``call_state``, ``speak(text)``, and ``delegate(text) -> reply``
(the Delegated Turn — the cost lever, whose model the caller selects).

Events raised upward: the user's speech transcript, and call started / ended /
dropped.

The one-call-at-a-time invariant lives *above* this seam, in Bridge Core, not in
any adapter (ADR 0001).

Adapters: the bridge-owned realtime call is the only one shipped. The GUI Live
Driver is historical — it is not migrated, and it is why this seam exists.
"""
