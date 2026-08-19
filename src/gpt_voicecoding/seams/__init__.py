"""The seams Bridge Core calls through. One module per seam; no implementations.

A seam exists only where something genuinely varies — every seam here names at
least two adapters, or one shipped plus one historical (ADR 0001, principle 2).
Implementations live in ``gpt_voicecoding.adapters``.
"""
