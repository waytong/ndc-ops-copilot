"""Supplier boundary used by the agent."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Offer, Order, Passenger, PricedOffer, SearchRequest


class SupplierAdapter(ABC):
    """A provider-neutral interface for NDC, GDS, or aggregator adapters."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable supplier name used for offer and order affinity."""
        raise NotImplementedError

    def supports(self, request: SearchRequest) -> bool:
        """Return whether the supplier supports this shopping request."""
        return True

    @abstractmethod
    def search(self, request: SearchRequest) -> list[Offer]:
        raise NotImplementedError

    @abstractmethod
    def price(self, offer: Offer) -> PricedOffer:
        raise NotImplementedError

    @abstractmethod
    def create_order(
        self,
        priced_offer: PricedOffer,
        passengers: tuple[Passenger, ...],
    ) -> Order:
        raise NotImplementedError

    @abstractmethod
    def retrieve_order(self, order_id: str) -> Order:
        raise NotImplementedError
