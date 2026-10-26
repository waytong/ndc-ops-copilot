"""Provider-neutral function tool bridge for LLM orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from typing import Any, Callable, Protocol

from .agent import NdcAgent
from .models import (
    ActionRisk,
    AgentError,
    ExpiredError,
    NotFoundError,
    Passenger,
    SearchRequest,
    ValidationError,
)
from .serialization import to_primitive


JsonObject = dict[str, Any]
ToolHandler = Callable[[JsonObject], Any]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: JsonObject
    risk: ActionRisk = ActionRisk.READ

    def as_function_tool(self) -> JsonObject:
        """Return the common function-tool shape used by major LLM APIs."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: JsonObject | str


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    status: str
    output: Any

    def as_message(self) -> JsonObject:
        return {
            "role": "tool",
            "tool_call_id": self.call_id,
            "name": self.name,
            "content": json.dumps(to_primitive(self.output), ensure_ascii=False),
        }


@dataclass(frozen=True)
class AssistantTurn:
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()


class LlmClient(Protocol):
    """Minimal client contract implemented by an OpenAI, Anthropic, or local adapter."""

    def complete(
        self,
        messages: list[JsonObject],
        tools: list[JsonObject],
    ) -> AssistantTurn:
        raise NotImplementedError


@dataclass(frozen=True)
class ToolLoopResult:
    status: str
    content: str
    tool_results: tuple[ToolResult, ...]
    steps: int


class AgentToolBridge:
    """Exports NdcAgent methods as validated tools with an approval boundary."""

    def __init__(self, agent: NdcAgent) -> None:
        self.agent = agent
        self._approved_commits: dict[tuple[str, str], str] = {}
        self._specs = {spec.name: spec for spec in self._build_specs()}
        self._handlers: dict[str, ToolHandler] = {
            "search_flights": self._search_flights,
            "price_offer": self._price_offer,
            "prepare_order": self._prepare_order,
            "confirm_order": self._confirm_order,
            "retrieve_order": self._retrieve_order,
            "list_suppliers": self._list_suppliers,
        }

    def tool_definitions(self) -> list[JsonObject]:
        return [spec.as_function_tool() for spec in self._specs.values()]

    def manifest(self) -> list[JsonObject]:
        return [
            {
                **spec.as_function_tool(),
                "risk": spec.risk.value,
            }
            for spec in self._specs.values()
        ]

    def tool_risk(self, name: str) -> ActionRisk | None:
        """Return the declared risk without exposing approval as a model tool."""
        spec = self._specs.get(name)
        return spec.risk if spec is not None else None

    def approve(self, call: ToolCall) -> None:
        """Approve the exact commit arguments outside the model-visible surface."""
        spec = self._specs.get(call.name)
        if spec is None or spec.risk != ActionRisk.COMMIT:
            raise ValidationError("只能批准commit类型的工具调用")
        arguments = self._parse_arguments(call.arguments)
        self._validate_argument_names(spec, arguments)
        confirmation_token = self._required_str(arguments, "confirmation_token")
        pending = self.agent.state.pending_actions.get(confirmation_token)
        if pending is None:
            raise NotFoundError("确认令牌不存在或已经使用")
        if pending.is_expired():
            raise ExpiredError("确认令牌已过期，请重新发起预订")
        self._approved_commits[(call.name, confirmation_token)] = self._fingerprint(
            arguments
        )

    def execute(self, call: ToolCall) -> ToolResult:
        spec = self._specs.get(call.name)
        handler = self._handlers.get(call.name)
        if spec is None or handler is None:
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                status="error",
                output={"error": f"未知工具: {call.name}"},
            )

        try:
            arguments = self._parse_arguments(call.arguments)
            self._validate_argument_names(spec, arguments)
            if spec.risk == ActionRisk.COMMIT:
                token = arguments.get("confirmation_token")
                approval_key = (call.name, token) if isinstance(token, str) else None
                approved = (
                    self._approved_commits.get(approval_key)
                    if approval_key is not None
                    else None
                )
                if approved != self._fingerprint(arguments):
                    return ToolResult(
                        call_id=call.call_id,
                        name=call.name,
                        status="approval_required",
                        output={
                            "confirmation_token": token,
                            "message": "该操作会创建订单，需要用户在宿主程序中明确批准",
                        },
                    )
                del self._approved_commits[approval_key]

            output = handler(arguments)
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                status="ok",
                output=to_primitive(output),
            )
        except AgentError as exc:
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                status="error",
                output={"error": str(exc), "error_type": type(exc).__name__},
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                status="error",
                output={"error": f"工具参数错误: {exc}"},
            )
        except Exception as exc:
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                status="error",
                output={
                    "error": "工具执行失败",
                    "error_type": type(exc).__name__,
                },
            )

    def _search_flights(self, arguments: JsonObject):
        try:
            departure_date = date.fromisoformat(self._required_str(arguments, "departure_date"))
            adults = int(arguments.get("adults", 1))
        except ValueError as exc:
            raise ValidationError("departure_date应为YYYY-MM-DD，adults应为整数") from exc
        return self.agent.search(
            SearchRequest(
                origin=self._required_str(arguments, "origin"),
                destination=self._required_str(arguments, "destination"),
                departure_date=departure_date,
                adults=adults,
            )
        )

    def _price_offer(self, arguments: JsonObject):
        return self.agent.price_offer(self._required_str(arguments, "offer_id"))

    def _prepare_order(self, arguments: JsonObject):
        return self.agent.prepare_order(self._required_str(arguments, "offer_id"))

    def _confirm_order(self, arguments: JsonObject):
        token = self._required_str(arguments, "confirmation_token")
        raw_passengers = arguments.get("passengers")
        if not isinstance(raw_passengers, list) or not raw_passengers:
            raise ValidationError("passengers必须是非空数组")
        passengers: list[Passenger] = []
        for item in raw_passengers:
            if not isinstance(item, dict):
                raise ValidationError("每个乘机人必须是对象")
            passengers.append(
                Passenger(
                    name=self._required_str(item, "name"),
                    type_code=str(item.get("type_code", "ADT")),
                )
            )
        return self.agent.confirm_order(token, tuple(passengers))

    def _retrieve_order(self, arguments: JsonObject):
        return self.agent.retrieve_order(self._required_str(arguments, "order_id"))

    def _list_suppliers(self, arguments: JsonObject):
        status = getattr(self.agent.adapter, "status", None)
        if status is None:
            return [{"supplier": self.agent.adapter.name, "mode": "single"}]
        return status()

    @staticmethod
    def _parse_arguments(arguments: JsonObject | str) -> JsonObject:
        parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
        if not isinstance(parsed, dict):
            raise ValidationError("工具参数必须是JSON对象")
        return parsed

    @staticmethod
    def _required_str(arguments: JsonObject, name: str) -> str:
        value = arguments.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"缺少字符串参数: {name}")
        return value.strip()

    @staticmethod
    def _fingerprint(arguments: JsonObject) -> str:
        return json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _validate_argument_names(spec: ToolSpec, arguments: JsonObject) -> None:
        properties = set(spec.parameters.get("properties", {}))
        unexpected = set(arguments) - properties
        if unexpected:
            raise ValidationError(f"不支持的参数: {', '.join(sorted(unexpected))}")
        missing = set(spec.parameters.get("required", [])) - set(arguments)
        if missing:
            raise ValidationError(f"缺少参数: {', '.join(sorted(missing))}")

    @staticmethod
    def _build_specs() -> tuple[ToolSpec, ...]:
        passenger_schema = {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Passenger name in LAST/FIRST format",
                },
                "type_code": {
                    "type": "string",
                    "enum": ["ADT", "CHD", "INF"],
                    "default": "ADT",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        }
        return (
            ToolSpec(
                name="search_flights",
                description="Search and aggregate flight offers from eligible suppliers.",
                parameters={
                    "type": "object",
                    "properties": {
                        "origin": {"type": "string", "description": "3-letter airport code"},
                        "destination": {"type": "string", "description": "3-letter airport code"},
                        "departure_date": {"type": "string", "format": "date"},
                        "adults": {"type": "integer", "minimum": 1, "maximum": 9, "default": 1},
                    },
                    "required": ["origin", "destination", "departure_date"],
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                name="price_offer",
                description="Reprice an offer returned by search_flights.",
                parameters={
                    "type": "object",
                    "properties": {"offer_id": {"type": "string"}},
                    "required": ["offer_id"],
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                name="prepare_order",
                description="Prepare a priced offer and request user confirmation.",
                parameters={
                    "type": "object",
                    "properties": {"offer_id": {"type": "string"}},
                    "required": ["offer_id"],
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                name="confirm_order",
                description="Create an order after host-side user approval.",
                parameters={
                    "type": "object",
                    "properties": {
                        "confirmation_token": {"type": "string"},
                        "passengers": {
                            "type": "array",
                            "items": passenger_schema,
                            "minItems": 1,
                        },
                    },
                    "required": ["confirmation_token", "passengers"],
                    "additionalProperties": False,
                },
                risk=ActionRisk.COMMIT,
            ),
            ToolSpec(
                name="retrieve_order",
                description="Retrieve an order from the supplier that owns it.",
                parameters={
                    "type": "object",
                    "properties": {"order_id": {"type": "string"}},
                    "required": ["order_id"],
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                name="list_suppliers",
                description="List supplier routing and health status.",
                parameters={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            ),
        )


class ToolLoop:
    """Bounded model/tool loop. Provider adapters only implement LlmClient."""

    def __init__(
        self,
        client: LlmClient,
        bridge: AgentToolBridge,
        *,
        system_prompt: str = "You are an airline retailing assistant. Use tools for facts and transactions.",
        max_steps: int = 8,
    ) -> None:
        self.client = client
        self.bridge = bridge
        self.max_steps = max(1, max_steps)
        self.messages: list[JsonObject] = [
            {"role": "system", "content": system_prompt}
        ]

    def send(self, user_message: str) -> ToolLoopResult:
        self.messages.append({"role": "user", "content": user_message})
        collected: list[ToolResult] = []

        for step in range(1, self.max_steps + 1):
            turn = self.client.complete(self.messages, self.bridge.tool_definitions())
            self.messages.append(
                {
                    "role": "assistant",
                    "content": turn.content,
                    "tool_calls": [to_primitive(call) for call in turn.tool_calls],
                }
            )
            if not turn.tool_calls:
                return ToolLoopResult("completed", turn.content, tuple(collected), step)

            approval_required = False
            for call in turn.tool_calls:
                result = self.bridge.execute(call)
                collected.append(result)
                self.messages.append(result.as_message())
                approval_required = approval_required or result.status == "approval_required"

            if approval_required:
                return ToolLoopResult(
                    "approval_required",
                    "操作需要用户确认",
                    tuple(collected),
                    step,
                )

        return ToolLoopResult(
            "max_steps_reached",
            "工具调用达到最大步数",
            tuple(collected),
            self.max_steps,
        )
