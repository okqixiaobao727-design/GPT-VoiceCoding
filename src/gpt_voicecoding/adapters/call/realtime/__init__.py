"""The bridge-owned realtime call, and the factory a configuration file names.

`config.toml` points at `gpt_voicecoding.adapters.call.realtime:realtime_call`,
and the composition root calls it with the event sink and this seam's settings
table. Nothing else imports an adapter (ADR 0001).

The factory is also where the voice extra is proved to be installed. Doing it
here — while the engine is still being assembled — is what turns "configured but
not loadable" into a refusal to start, rather than into an outage the user
discovers at the moment they try to talk.
"""

from __future__ import annotations

from typing import Any

from gpt_voicecoding.adapters.call.realtime.adapter import (
    APPROVAL_POLICY,
    SANDBOX,
    DelegatedTurnError,
    RealtimeCallAdapter,
)
from gpt_voicecoding.adapters.call.realtime.settings import (
    DEFAULT_REALTIME_MODEL,
    RealtimeCallSettings,
    SettingsError,
)
from gpt_voicecoding.adapters.call.realtime.transport import (
    CallTransport,
    TransportError,
    TransportFactory,
)

__all__ = [
    "APPROVAL_POLICY",
    "DEFAULT_REALTIME_MODEL",
    "SANDBOX",
    "CallTransport",
    "DelegatedTurnError",
    "RealtimeCallAdapter",
    "RealtimeCallSettings",
    "SettingsError",
    "TransportError",
    "TransportFactory",
    "realtime_call",
]


def realtime_call(
    *,
    sink: Any = None,
    settings: dict[str, Any] | None = None,
    transport_factory: TransportFactory | None = None,
) -> RealtimeCallAdapter:
    """Build the adapter from an opaque settings table, refusing keys it lacks."""
    read = RealtimeCallSettings.of(settings)
    return RealtimeCallAdapter(
        sink=sink,
        settings=read,
        transport_factory=transport_factory or _audio_from(read),
    )


def _audio_from(settings: RealtimeCallSettings) -> TransportFactory:
    """The real audio path, with the voice extra proved present before it is needed."""
    from gpt_voicecoding.adapters.call.realtime import webrtc

    webrtc.probe()

    def build() -> CallTransport:
        return webrtc.webrtc_transport(
            input_device=settings.input_device, output_device=settings.output_device
        )

    return build
