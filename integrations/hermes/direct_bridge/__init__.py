"""Fail-closed helpers for an externally paired Hermes WhatsApp bridge."""

from .bridge_guard import GUARD_MARKER, bridge_is_guarded, patch_bridge_source

__all__ = ["GUARD_MARKER", "bridge_is_guarded", "patch_bridge_source"]
