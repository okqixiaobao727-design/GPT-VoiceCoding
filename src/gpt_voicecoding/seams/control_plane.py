"""The control plane — an interface Bridge Core *exposes*, rather than calls out through.

Status queries and switch flips, carried as JSON over a Unix domain socket.

Surfaces that speak it: the menu-bar shell, ``bridgectl``, the Companion Channel,
and spoken commands inside a Live Call. It is never gated by any switch — see
ADR 0002, which is absolute.
"""
