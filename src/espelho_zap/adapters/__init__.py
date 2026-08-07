"""Host-neutral capture adapters for the portable mirror."""

from .base import (
    AdapterCapabilities,
    AdapterContractError,
    InboundAdapter,
    RawInboundMessage,
    RawMediaRef,
    normalize_inbound,
)
from .openclaw_jsonl import OpenClawJSONLAdapter
from .hermes_bridge import HermesBridgeError, HermesBridgeObserver, ObserverResult

__all__ = [
    "AdapterCapabilities",
    "AdapterContractError",
    "InboundAdapter",
    "HermesBridgeError",
    "HermesBridgeObserver",
    "ObserverResult",
    "OpenClawJSONLAdapter",
    "RawInboundMessage",
    "RawMediaRef",
    "normalize_inbound",
]
