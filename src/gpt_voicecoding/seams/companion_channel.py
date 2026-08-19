"""The Companion Channel seam — reaching the user when no Live Call is up.

Verbs Bridge Core calls: ``send(message)`` and ``verify`` (liveness — ADR 0003).

Events raised upward: inbound user text, unclassified. Deciding whether inbound
text is a control-plane command, an Answer Relay, or a delegation is Bridge
Core's job and never the channel's.

Adapters: Telegram is the generic public one.
"""
