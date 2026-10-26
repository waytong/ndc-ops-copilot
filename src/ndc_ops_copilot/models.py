"""Typed domain models and validation rules for the agent pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
import re
from typing import Any


class AgentError(Exception):
    """Base exception surfaced as a user-safe agent error."""


class ValidationError(AgentError):
    """Raised when an input violates a domain contract."""


class NotFoundError(AgentError):
    """Raised when an offer, order, or confirmation does not exist."""


class ConflictError(AgentError):
    """Raised when the requested action conflicts with session state."""


class ExpiredError(AgentError):
    """Raised when an offer or confirmation is no longer valid."""


class SupplierUnavailableError(AgentError):
    """Raised when no eligible supplier can complete an operation."""


class ActionRisk(str, Enum):
    READ = "read"
    COMMIT = "commit"


class OrderStatus(str, Enum):
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class TicketStatus(str, Enum):
    NOT_TICKETED = "not_ticketed"
    TICKETED = "ticketed"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_airport(value: str) -> str:
    code = value.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise ValidationError(f"机场代码必须是3位字母: {value!r}")
    return code


@dataclass(frozen=True)
class Passenger:
    name: str
    type_code: str = "ADT"

    def __post_init__(self) -> None:
        normalized_name = self.name.strip().upper()
        normalized_type = self.type_code.strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z '-]{0,39}/[A-Z][A-Z '-]{0,39}", normalized_name):
            raise ValidationError("乘机人姓名格式应为 LAST/FIRST，例如 ZHANG/SAN")
        if normalized_type not in {"ADT", "CHD", "INF"}:
            raise ValidationError("旅客类型仅支持 ADT、CHD、INF")
        object.__setattr__(self, "name", normalized_name)
        object.__setattr__(self, "type_code", normalized_type)


@dataclass(frozen=True)
class SearchRequest:
    origin: str
    destination: str
    departure_date: date
    adults: int = 1

    def __post_init__(self) -> None:
        origin = normalize_airport(self.origin)
        destination = normalize_airport(self.destination)
        if origin == destination:
            raise ValidationError("出发机场和到达机场不能相同")
        if self.departure_date < date.today():
            raise ValidationError("出发日期不能早于今天")
        if not 1 <= self.adults <= 9:
            raise ValidationError("成人数量必须在1到9之间")
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "destination", destination)

    @property
    def intent_key(self) -> tuple[str, str, date, int]:
        return self.origin, self.destination, self.departure_date, self.adults


@dataclass(frozen=True)
class Offer:
    offer_id: str
    owner: str
    origin: str
    destination: str
    departure_date: date
    flight_number: str
    cabin: str
    baggage: str
    currency: str
    total_amount: Decimal
    expires_at: datetime

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.expires_at <= (now or utc_now())


@dataclass(frozen=True)
class PricedOffer:
    offer: Offer
    total_amount: Decimal
    currency: str
    priced_at: datetime
    expires_at: datetime

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.expires_at <= (now or utc_now())


@dataclass(frozen=True)
class PendingAction:
    token: str
    action: str
    risk: ActionRisk
    offer_id: str
    summary: str
    expires_at: datetime

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.expires_at <= (now or utc_now())


@dataclass(frozen=True)
class Order:
    order_id: str
    pnr: str
    owner: str
    offer_id: str
    passengers: tuple[Passenger, ...]
    currency: str
    total_amount: Decimal
    status: OrderStatus
    ticket_status: TicketStatus
    created_at: datetime


@dataclass
class SessionState:
    session_id: str
    intent_key: tuple[str, str, date, int] | None = None
    offers: dict[str, Offer] = field(default_factory=dict)
    priced_offers: dict[str, PricedOffer] = field(default_factory=dict)
    pending_actions: dict[str, PendingAction] = field(default_factory=dict)
    orders: dict[str, Order] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    def record(self, event: str, **data: Any) -> None:
        self.events.append({"at": utc_now().isoformat(), "event": event, **data})
