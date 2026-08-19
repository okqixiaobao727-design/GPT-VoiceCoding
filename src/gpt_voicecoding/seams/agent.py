"""The Agent seam — carrying words into a Session, and hearing back from it.

Verbs Bridge Core calls: ``answer_relay(session, text)``,
``notice_relay(session, text)``, ``approval_relay(session, request, verdict)``.

Events raised upward: Session stopped, Session awaiting approval, and delivery
receipts (delivered / held / expired).

Reply-Window queueing is Bridge Core policy. Adapters deliver; they never queue.

Adapters: Codex and Claude.
"""
