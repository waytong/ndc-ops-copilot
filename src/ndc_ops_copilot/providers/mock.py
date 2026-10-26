"""In-memory supplier used for local development and tests."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
import re
import secrets

from ..models import (
    ExpiredError,
    NotFoundError,
    Offer,
    Order,
    OrderStatus,
    Passenger,
    PricedOffer,
    SearchRequest,
    TicketStatus,
    utc_now,
)
from .base import SupplierAdapter


class MockSupplierAdapter(SupplierAdapter):
    """Generates deterministic-looking offers without calling an external API."""

    def __init__(
        self,
        name: str = "MOCK",
        fare_factor: Decimal | str = Decimal("1.00"),
    ) -> None:
        normalized_name = name.strip().upper()
        if not re.fullmatch(r"[A-Z0-9_-]{2,30}", normalized_name):
            raise ValueError("Supplier name must contain 2-30 letters, numbers, _ or -")
        self._name = normalized_name
        self._fare_factor = Decimal(fare_factor)
        self._orders: dict[str, Order] = {}

    @property
    def name(self) -> str:
        return self._name

    def search(self, request: SearchRequest) -> list[Offer]:
        now = utc_now()
        route_factor = sum(ord(char) for char in request.origin + request.destination) % 900
        base = Decimal(1800 + route_factor) * request.adults
        templates = (
            ("MA101", "ECONOMY LIGHT", "0PC", Decimal("1.00")),
            ("MA203", "ECONOMY STANDARD", "1PC", Decimal("1.12")),
            ("MA305", "ECONOMY FLEX", "2PC", Decimal("1.35")),
        )
        offers: list[Offer] = []
        for flight_number, cabin, baggage, multiplier in templates:
            amount = (base * multiplier * self._fare_factor).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            offers.append(
                Offer(
                    offer_id=f"OFF-{secrets.token_hex(5).upper()}",
                    owner=self.name,
                    origin=request.origin,
                    destination=request.destination,
                    departure_date=request.departure_date,
                    flight_number=flight_number,
                    cabin=cabin,
                    baggage=baggage,
                    currency="CNY",
                    total_amount=amount,
                    expires_at=now + timedelta(minutes=20),
                )
            )
        return offers

    def price(self, offer: Offer) -> PricedOffer:
        if offer.is_expired():
            raise ExpiredError("Offer已过期，请重新搜索")
        now = utc_now()
        repriced_amount = (offer.total_amount * Decimal("1.003")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        return PricedOffer(
            offer=offer,
            total_amount=repriced_amount,
            currency=offer.currency,
            priced_at=now,
            expires_at=now + timedelta(minutes=10),
        )

    def create_order(
        self,
        priced_offer: PricedOffer,
        passengers: tuple[Passenger, ...],
    ) -> Order:
        if priced_offer.is_expired():
            raise ExpiredError("核价结果已过期，请重新核价")
        order = Order(
            order_id=f"ORD-{secrets.token_hex(6).upper()}",
            pnr=secrets.token_hex(3).upper(),
            owner=priced_offer.offer.owner,
            offer_id=priced_offer.offer.offer_id,
            passengers=passengers,
            currency=priced_offer.currency,
            total_amount=priced_offer.total_amount,
            status=OrderStatus.CONFIRMED,
            ticket_status=TicketStatus.NOT_TICKETED,
            created_at=utc_now(),
        )
        self._orders[order.order_id] = order
        return order

    def retrieve_order(self, order_id: str) -> Order:
        try:
            return self._orders[order_id]
        except KeyError as exc:
            raise NotFoundError(f"订单不存在: {order_id}") from exc
