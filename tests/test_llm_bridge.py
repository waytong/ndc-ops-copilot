from __future__ import annotations

from datetime import date, timedelta
import unittest

from ndc_ops_copilot import (
    AgentToolBridge,
    AssistantTurn,
    MockSupplierAdapter,
    MultiSupplierRouter,
    NdcAgent,
    SupplierRegistration,
    ToolCall,
    ToolLoop,
)


class ScriptedClient:
    def __init__(self, turns):
        self.turns = list(turns)
        self.received_tools = []

    def complete(self, messages, tools):
        self.received_tools = tools
        return self.turns.pop(0)


def make_bridge() -> AgentToolBridge:
    router = MultiSupplierRouter(
        [
            SupplierRegistration(MockSupplierAdapter("GDS"), priority=10),
            SupplierRegistration(MockSupplierAdapter("NDC", "0.95"), priority=20),
        ]
    )
    return AgentToolBridge(NdcAgent(router, session_id="bridge-test"))


class AgentToolBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bridge = make_bridge()
        self.future_date = (date.today() + timedelta(days=90)).isoformat()

    def call(self, name, arguments, call_id="call-1"):
        return self.bridge.execute(ToolCall(call_id, name, arguments))

    def test_exports_function_tools_and_risk_manifest(self) -> None:
        definitions = self.bridge.tool_definitions()
        manifest = self.bridge.manifest()

        self.assertEqual(len(definitions), 6)
        self.assertEqual({item["type"] for item in definitions}, {"function"})
        risks = {item["function"]["name"]: item["risk"] for item in manifest}
        self.assertEqual(risks["confirm_order"], "commit")
        self.assertEqual(risks["search_flights"], "read")

    def test_full_tool_flow_requires_host_approval(self) -> None:
        search = self.call(
            "search_flights",
            {
                "origin": "SHA",
                "destination": "FRA",
                "departure_date": self.future_date,
                "adults": 1,
            },
        )
        self.assertEqual(search.status, "ok")
        offer_id = search.output[0]["offer_id"]

        priced = self.call("price_offer", {"offer_id": offer_id}, "call-2")
        prepared = self.call("prepare_order", {"offer_id": offer_id}, "call-3")
        token = prepared.output["token"]
        confirm_args = {
            "confirmation_token": token,
            "passengers": [{"name": "ZHANG/SAN", "type_code": "ADT"}],
        }

        confirm_call = ToolCall("call-4", "confirm_order", confirm_args)
        blocked = self.bridge.execute(confirm_call)
        self.assertEqual(blocked.status, "approval_required")

        self.bridge.approve(confirm_call)
        confirmed = self.bridge.execute(confirm_call)

        self.assertEqual(priced.status, "ok")
        self.assertEqual(prepared.status, "ok")
        self.assertEqual(confirmed.status, "ok")
        self.assertIn("::ORD-", confirmed.output["order_id"])

    def test_approval_is_bound_to_exact_passenger_arguments(self) -> None:
        search = self.call(
            "search_flights",
            {
                "origin": "SHA",
                "destination": "FRA",
                "departure_date": self.future_date,
                "adults": 1,
            },
        )
        offer_id = search.output[0]["offer_id"]
        self.call("price_offer", {"offer_id": offer_id}, "price")
        prepared = self.call("prepare_order", {"offer_id": offer_id}, "prepare")
        token = prepared.output["token"]
        approved_call = ToolCall(
            "confirm",
            "confirm_order",
            {
                "confirmation_token": token,
                "passengers": [{"name": "ZHANG/SAN", "type_code": "ADT"}],
            },
        )
        self.bridge.approve(approved_call)

        modified_call = ToolCall(
            "confirm",
            "confirm_order",
            {
                "confirmation_token": token,
                "passengers": [{"name": "LI/MING", "type_code": "ADT"}],
            },
        )
        result = self.bridge.execute(modified_call)

        self.assertEqual(result.status, "approval_required")

    def test_invalid_tool_arguments_return_structured_error(self) -> None:
        result = self.call(
            "search_flights",
            {
                "origin": "SHA",
                "destination": "FRA",
                "departure_date": self.future_date,
                "invented": True,
            },
        )

        self.assertEqual(result.status, "error")
        self.assertIn("不支持的参数", result.output["error"])

    def test_bounded_tool_loop_executes_calls_and_returns_final_text(self) -> None:
        client = ScriptedClient(
            [
                AssistantTurn(
                    tool_calls=(
                        ToolCall(
                            "search-1",
                            "search_flights",
                            {
                                "origin": "SHA",
                                "destination": "FRA",
                                "departure_date": self.future_date,
                                "adults": 1,
                            },
                        ),
                    )
                ),
                AssistantTurn(content="已找到可用航班。"),
            ]
        )
        loop = ToolLoop(client, self.bridge, max_steps=4)

        result = loop.send("查询上海到法兰克福的航班")

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.content, "已找到可用航班。")
        self.assertEqual(result.tool_results[0].status, "ok")
        self.assertEqual(len(client.received_tools), 6)

    def test_tool_loop_stops_at_max_steps(self) -> None:
        class RepeatingClient:
            def complete(self, messages, tools):
                return AssistantTurn(
                    tool_calls=(ToolCall("repeat", "list_suppliers", {}),)
                )

        loop = ToolLoop(RepeatingClient(), self.bridge, max_steps=2)

        result = loop.send("继续调用工具")

        self.assertEqual(result.status, "max_steps_reached")
        self.assertEqual(result.steps, 2)
        self.assertEqual(len(result.tool_results), 2)


if __name__ == "__main__":
    unittest.main()
