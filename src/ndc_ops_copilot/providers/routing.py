"""Multi-supplier routing with fan-out search and supplier affinity."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from time import monotonic

from ..models import (
    NotFoundError,
    Offer,
    Order,
    Passenger,
    PricedOffer,
    SearchRequest,
    SupplierUnavailableError,
    utc_now,
)
from .base import SupplierAdapter


@dataclass(frozen=True)
class SupplierRegistration:
    """Routing configuration for one supplier."""

    adapter: SupplierAdapter
    priority: int = 100
    enabled: bool = True
    routes: frozenset[tuple[str, str]] = frozenset()

    def supports(self, request: SearchRequest) -> bool:
        if not self.enabled or not self.adapter.supports(request):
            return False
        if not self.routes:
            return True
        return (request.origin, request.destination) in self.routes


@dataclass(frozen=True)
class SupplierDiagnostic:
    supplier: str
    status: str
    offer_count: int
    duration_ms: int
    detail: str = ""


@dataclass
class SupplierHealth:
    consecutive_failures: int = 0
    circuit_open_until: datetime | None = None

    def is_available(self, now: datetime | None = None) -> bool:
        if self.circuit_open_until is None:
            return True
        return self.circuit_open_until <= (now or utc_now())


@dataclass(frozen=True)
class _OfferContext:
    adapter: SupplierAdapter
    raw_offer: Offer
    public_offer: Offer


@dataclass(frozen=True)
class _PricedContext:
    adapter: SupplierAdapter
    raw_priced_offer: PricedOffer


@dataclass(frozen=True)
class _OrderContext:
    adapter: SupplierAdapter
    raw_order_id: str


class MultiSupplierRouter(SupplierAdapter):
    """Aggregates shopping and keeps subsequent actions on the same supplier."""

    def __init__(
        self,
        registrations: list[SupplierRegistration],
        *,
        failure_threshold: int = 3,
        circuit_break_seconds: int = 30,
        max_parallel: int = 8,
    ) -> None:
        if not registrations:
            raise ValueError("At least one supplier registration is required")
        names = [registration.adapter.name for registration in registrations]
        if len(set(names)) != len(names):
            raise ValueError("Supplier names must be unique")
        self._registrations = sorted(registrations, key=lambda item: item.priority)
        self._by_name = {item.adapter.name: item for item in self._registrations}
        self._health = {name: SupplierHealth() for name in names}
        self._failure_threshold = max(1, failure_threshold)
        self._circuit_break_seconds = max(1, circuit_break_seconds)
        self._max_parallel = max(1, max_parallel)
        self._offer_context: dict[str, _OfferContext] = {}
        self._priced_context: dict[str, _PricedContext] = {}
        self._order_context: dict[str, _OrderContext] = {}
        self.last_diagnostics: tuple[SupplierDiagnostic, ...] = ()

    @property
    def name(self) -> str:
        return "MULTI_SUPPLIER"

    def search(self, request: SearchRequest) -> list[Offer]:
        eligible = [
            registration
            for registration in self._registrations
            if registration.supports(request)
            and self._health[registration.adapter.name].is_available()
        ]
        if not eligible:
            raise SupplierUnavailableError("没有可用于当前航线的供应商")

        self._offer_context.clear()
        self._priced_context.clear()
        diagnostics: list[SupplierDiagnostic] = []
        successful: list[tuple[SupplierRegistration, list[Offer]]] = []

        with ThreadPoolExecutor(max_workers=min(self._max_parallel, len(eligible))) as executor:
            futures = {
                executor.submit(self._search_one, registration, request): registration
                for registration in eligible
            }
            for future in as_completed(futures):
                registration = futures[future]
                try:
                    raw_offers, duration_ms = future.result()
                    self._record_success(registration.adapter.name)
                    successful.append((registration, raw_offers))
                    diagnostics.append(
                        SupplierDiagnostic(
                            supplier=registration.adapter.name,
                            status="success",
                            offer_count=len(raw_offers),
                            duration_ms=duration_ms,
                        )
                    )
                except Exception as exc:
                    self._record_failure(registration.adapter.name)
                    diagnostics.append(
                        SupplierDiagnostic(
                            supplier=registration.adapter.name,
                            status="failed",
                            offer_count=0,
                            duration_ms=0,
                            detail=str(exc),
                        )
                    )

        self.last_diagnostics = tuple(sorted(diagnostics, key=lambda item: item.supplier))
        if not successful:
            details = "; ".join(
                f"{item.supplier}: {item.detail}" for item in self.last_diagnostics
            )
            raise SupplierUnavailableError(f"所有供应商查询失败: {details}")

        public_offers: list[Offer] = []
        for registration, raw_offers in successful:
            for raw_offer in raw_offers:
                public_id = self._public_id(registration.adapter.name, raw_offer.offer_id)
                public_offer = replace(
                    raw_offer,
                    offer_id=public_id,
                    owner=registration.adapter.name,
                )
                self._offer_context[public_id] = _OfferContext(
                    adapter=registration.adapter,
                    raw_offer=raw_offer,
                    public_offer=public_offer,
                )
                public_offers.append(public_offer)

        priorities = {
            registration.adapter.name: registration.priority
            for registration in self._registrations
        }
        return sorted(
            public_offers,
            key=lambda offer: (offer.total_amount, priorities[offer.owner], offer.offer_id),
        )

    def price(self, offer: Offer) -> PricedOffer:
        context = self._offer_context.get(offer.offer_id)
        if context is None or context.public_offer.owner != offer.owner:
            raise NotFoundError("无法找到Offer对应的供应商路由")
        try:
            raw_priced = context.adapter.price(context.raw_offer)
            self._record_success(context.adapter.name)
        except Exception:
            self._record_failure(context.adapter.name)
            raise
        public_priced = replace(raw_priced, offer=context.public_offer)
        self._priced_context[offer.offer_id] = _PricedContext(
            adapter=context.adapter,
            raw_priced_offer=raw_priced,
        )
        return public_priced

    def create_order(
        self,
        priced_offer: PricedOffer,
        passengers: tuple[Passenger, ...],
    ) -> Order:
        public_offer_id = priced_offer.offer.offer_id
        context = self._priced_context.get(public_offer_id)
        if context is None:
            raise NotFoundError("无法找到核价结果对应的供应商路由")
        try:
            raw_order = context.adapter.create_order(context.raw_priced_offer, passengers)
            self._record_success(context.adapter.name)
        except Exception:
            self._record_failure(context.adapter.name)
            raise

        public_order_id = self._public_id(context.adapter.name, raw_order.order_id)
        public_order = replace(
            raw_order,
            order_id=public_order_id,
            owner=context.adapter.name,
            offer_id=public_offer_id,
        )
        self._order_context[public_order_id] = _OrderContext(
            adapter=context.adapter,
            raw_order_id=raw_order.order_id,
        )
        return public_order

    def retrieve_order(self, order_id: str) -> Order:
        context = self._order_context.get(order_id)
        if context is None:
            context = self._context_from_namespaced_order(order_id)
        if context is not None:
            raw_order = context.adapter.retrieve_order(context.raw_order_id)
            return replace(
                raw_order,
                order_id=self._public_id(context.adapter.name, raw_order.order_id),
                owner=context.adapter.name,
                offer_id=self._public_offer_id(context.adapter.name, raw_order.offer_id),
            )

        for registration in self._registrations:
            if not registration.enabled:
                continue
            try:
                raw_order = registration.adapter.retrieve_order(order_id)
                return replace(raw_order, owner=registration.adapter.name)
            except NotFoundError:
                continue
        raise NotFoundError(f"所有供应商均未找到订单: {order_id}")

    def status(self) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for registration in self._registrations:
            health = self._health[registration.adapter.name]
            result.append(
                {
                    "supplier": registration.adapter.name,
                    "priority": registration.priority,
                    "enabled": registration.enabled,
                    "routes": sorted(
                        f"{origin}-{destination}"
                        for origin, destination in registration.routes
                    ),
                    "consecutive_failures": health.consecutive_failures,
                    "circuit_open_until": health.circuit_open_until,
                }
            )
        return result

    @staticmethod
    def _search_one(
        registration: SupplierRegistration,
        request: SearchRequest,
    ) -> tuple[list[Offer], int]:
        started = monotonic()
        offers = registration.adapter.search(request)
        duration_ms = round((monotonic() - started) * 1000)
        return offers, duration_ms

    def _record_success(self, supplier: str) -> None:
        health = self._health[supplier]
        health.consecutive_failures = 0
        health.circuit_open_until = None

    def _record_failure(self, supplier: str) -> None:
        health = self._health[supplier]
        health.consecutive_failures += 1
        if health.consecutive_failures >= self._failure_threshold:
            health.circuit_open_until = utc_now() + timedelta(
                seconds=self._circuit_break_seconds
            )

    def _context_from_namespaced_order(self, order_id: str) -> _OrderContext | None:
        if "::" not in order_id:
            return None
        supplier, raw_order_id = order_id.split("::", 1)
        registration = self._by_name.get(supplier)
        if registration is None:
            return None
        return _OrderContext(registration.adapter, raw_order_id)

    @staticmethod
    def _public_id(supplier: str, raw_id: str) -> str:
        return f"{supplier}::{raw_id}"

    @staticmethod
    def _public_offer_id(supplier: str, raw_offer_id: str) -> str:
        prefix = f"{supplier}::"
        return raw_offer_id if raw_offer_id.startswith(prefix) else f"{prefix}{raw_offer_id}"
