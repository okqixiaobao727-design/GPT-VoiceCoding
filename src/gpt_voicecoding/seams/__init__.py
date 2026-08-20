"""The seams Bridge Core calls through. One module per seam; no implementations.

A seam exists only where something genuinely varies — every seam here names at
least two adapters, or one shipped plus one historical (ADR 0001, principle 2).
Implementations live in ``gpt_voicecoding.adapters``.

This package is also where the vocabulary that crosses a seam lives, because it
is the one thing both sides share: ``identity`` (who a Relay is addressed to and
what correlates an attempt), ``delivery`` (the four-state classification),
``verify`` (ADR 0003 liveness) and ``events`` (how an adapter speaks upward). The
vocabulary is Bridge Core's — it is closed, and no adapter may extend or
reinterpret it — and it lives here so the dependency runs one way: Bridge Core
imports the seams, adapters import the seams, and the seams import neither.

The contracts are ``typing.Protocol`` classes rather than base classes to
inherit. An adapter is anything shaped right, so a fake is an ordinary class and
no implementation is coupled to a base here.

Seam verbs are ``async``: everything behind a seam is I/O. Bridge Core's own
state components are synchronous, so all of the policy this repository cares
about is testable without an event loop.
"""
