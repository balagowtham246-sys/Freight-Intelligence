"""Simulated data for demonstration surfaces.

Everything in this package is fabricated. Nothing here is ingested, nothing is
persisted, and nothing feeds a model or a forecast. The package exists so that
the boundary is visible in the import path: ``app.sim`` is never a source of
truth, and a reader who sees it in a traceback knows immediately that the
figures involved are illustrative.

This is deliberately separate from ``app.domain``, which is the deterministic
and auditable half of the platform. Ship physics and port constraints belong
there. Invented vessel positions do not.
"""
from __future__ import annotations

from .fleet import fleet_snapshot

__all__ = ["fleet_snapshot"]
