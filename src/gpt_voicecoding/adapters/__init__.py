"""Adapter implementations, one subpackage per seam.

Every protocol library the system uses — WebRTC, Telegram, JSON-RPC framing, tmux
— is imported from inside this package and nowhere else (ADR 0001, principle 1).
"""
