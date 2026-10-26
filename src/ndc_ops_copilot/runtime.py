"""Default runtime wiring shared by the CLI and MCP server."""

from __future__ import annotations

from .agent import NdcAgent
from .providers.mock import MockSupplierAdapter
from .providers.routing import MultiSupplierRouter, SupplierRegistration


def build_default_agent() -> NdcAgent:
    """Build the default development runtime with two mock suppliers."""
    router = MultiSupplierRouter(
        [
            SupplierRegistration(
                MockSupplierAdapter("TRAVELPORT", fare_factor="1.00"),
                priority=10,
            ),
            SupplierRegistration(
                MockSupplierAdapter("AIRLINE_NDC", fare_factor="0.96"),
                priority=20,
            ),
        ]
    )
    return NdcAgent(router)
