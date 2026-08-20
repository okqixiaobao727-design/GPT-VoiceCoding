"""The control plane's mechanism: framing, sockets, and translation.

The *vocabulary* — the closed action set, the closed error set, the two
envelopes — is `seams.control_plane`, because it is the one thing the engine and
every surface share. This package is everything that carries it:

- `payloads` — Bridge Core's own objects rendered into JSON-able documents, and
  the wire's addresses read back into Bridge Core's identities. Translation
  only: no decision is taken here, and nothing here is allowed to soften a
  refusal into a success.
- `actions` — one request, one Bridge Core verb, one reply. It holds no policy
  and no state; it maps refusals onto codes so a refusal keeps its identity
  across the wire, and it never consults switch state (ADR 0002).
- `commands` — the one command line both `bridgectl` and the Companion
  Channel's `/` grammar are parsed by, so there is one command set rather than
  two implementations of one.
- `server` — the AF_UNIX JSON-lines listener the engine serves.
- `client` — the bounded, timeout-bearing dialler a surface uses.

The socket mechanism is rewritten from the reference implementation's
`bridge/protocol.py`, which is the one thing #3 authorises reusing: protocol
parsing is surface mechanism, never business policy, and none of it may live
inside Bridge Core.
"""
