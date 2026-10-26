"""Stateful, contract-driven orchestration for the simplified NDC flow."""

from __future__ import annotations

from datetime import timedelta
import secrets

from .models import (
    ActionRisk,
    ConflictError,
    ExpiredError,
    NotFoundError,
    Offer,
    Order,
    Passenger,
    PendingAction,
    PricedOffer,
    SearchRequest,
    SessionState,
    ValidationError,
    utc_now,
)
from .providers.base import SupplierAdapter


class NdcAgent:
    """Coordinates typed tools while keeping transaction decisions deterministic."""

    def __init__(self, adapter: SupplierAdapter, session_id: str | None = None) -> None:
        self.adapter = adapter
        self.state = SessionState(session_id=session_id or secrets.token_hex(8))

    def search(self, request: SearchRequest) -> list[Offer]:
        if self.state.intent_key != request.intent_key:
            self.state.offers.clear()
            self.state.priced_offers.clear()
            self.state.pending_actions.clear()
            self.state.intent_key = request.intent_key
            self.state.record("intent_locked", intent_key=str(request.intent_key))

        offers = self.adapter.search(request)
        self.state.offers = {offer.offer_id: offer for offer in offers}
        self.state.record(
            "search_completed",
            offer_count=len(offers),
            suppliers=sorted({offer.owner for offer in offers}),
        )
        return offers

    def price_offer(self, offer_id: str) -> PricedOffer:
        offer = self._get_session_offer(offer_id)
        priced = self.adapter.price(offer)
        self.state.priced_offers[offer_id] = priced
        self.state.record(
            "offer_priced",
            offer_id=offer_id,
            total=str(priced.total_amount),
            currency=priced.currency,
        )
        return priced

    def prepare_order(self, offer_id: str) -> PendingAction:
        priced = self.state.priced_offers.get(offer_id)
        if priced is None:
            raise ConflictError("创建订单前必须先对该Offer执行核价")
        if priced.is_expired():
            raise ExpiredError("核价结果已过期，请重新核价")

        token = secrets.token_urlsafe(9)
        pending = PendingAction(
            token=token,
            action="create_order",
            risk=ActionRisk.COMMIT,
            offer_id=offer_id,
            summary=(
                f"创建订单: {priced.offer.origin}-{priced.offer.destination} "
                f"{priced.offer.departure_date.isoformat()}, "
                f"{priced.currency} {priced.total_amount}"
            ),
            expires_at=utc_now() + timedelta(minutes=5),
        )
        self.state.pending_actions[token] = pending
        self.state.record("confirmation_requested", token=token, offer_id=offer_id)
        return pending

    def confirm_order(self, token: str, passengers: tuple[Passenger, ...]) -> Order:
        pending = self.state.pending_actions.pop(token, None)
        if pending is None:
            raise NotFoundError("确认令牌不存在或已经使用")
        if pending.is_expired():
            raise ExpiredError("确认令牌已过期，请重新发起预订")
        if pending.action != "create_order":
            raise ConflictError(f"不支持的确认操作: {pending.action}")
        if not passengers:
            raise ValidationError("至少需要一名乘机人")

        priced = self.state.priced_offers.get(pending.offer_id)
        if priced is None:
            raise ConflictError("会话中的核价结果已丢失，请重新核价")
        if priced.is_expired():
            raise ExpiredError("核价结果已过期，请重新核价")

        expected_adults = self.state.intent_key[3] if self.state.intent_key else 0
        actual_adults = sum(1 for passenger in passengers if passenger.type_code == "ADT")
        if actual_adults != expected_adults:
            raise ValidationError(
                f"成人数量与搜索条件不一致: 需要{expected_adults}人，收到{actual_adults}人"
            )

        order = self.adapter.create_order(priced, passengers)
        self.state.orders[order.order_id] = order
        self.state.record("order_created", order_id=order.order_id, pnr=order.pnr)
        return order

    def retrieve_order(self, order_id: str) -> Order:
        order = self.adapter.retrieve_order(order_id)
        self.state.orders[order.order_id] = order
        self.state.record("order_retrieved", order_id=order.order_id)
        return order

    def _get_session_offer(self, offer_id: str) -> Offer:
        offer = self.state.offers.get(offer_id)
        if offer is None:
            raise NotFoundError("Offer不在当前搜索结果中，不能继续操作")
        if offer.is_expired():
            raise ExpiredError("Offer已过期，请重新搜索")
        return offer
