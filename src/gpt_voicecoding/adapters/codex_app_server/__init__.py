"""The one component that owns talking to a ``codex app-server``.

Spoke-internal, and shared: the Agent seam's Codex adapter owns this, and the
Call seam's adapter consumes it rather than spawning a second app-server of its
own. It is not a seam — nothing about it varies, and ADR 0001's principle 2 says
a seam is named only where something does.

What lives here is mechanism and only mechanism: WebSocket framing over a Unix
socket, JSON-RPC correlation, deadlines, and process ownership. No policy, no
Relay vocabulary, no delivery grading — those are the adapter's and the hub's.
"""
