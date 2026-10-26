from __future__ import annotations

from datetime import date, timedelta
from io import StringIO
import json
import unittest

from ndc_ops_copilot import AgentToolBridge
from ndc_ops_copilot.mcp_server import McpServer, serve
from ndc_ops_copilot.runtime import build_default_agent


class McpServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = McpServer(AgentToolBridge(build_default_agent()))
        self.future_date = (date.today() + timedelta(days=90)).isoformat()
        self.request_id = 0

    def request(self, method, params=None, server=None):
        self.request_id += 1
        message = {"jsonrpc": "2.0", "id": self.request_id, "method": method}
        if params is not None:
            message["params"] = params
        return (server or self.server).handle(message)

    def call(self, name, arguments, server=None):
        response = self.request(
            "tools/call",
            {"name": name, "arguments": arguments},
            server,
        )
        return response["result"]["structuredContent"]

    def prepare_confirmation(self, server=None):
        active_server = server or self.server
        search = self.call(
            "search_flights",
            {
                "origin": "SHA",
                "destination": "FRA",
                "departure_date": self.future_date,
                "adults": 1,
            },
            active_server,
        )
        offer_id = search["output"][0]["offer_id"]
        self.call("price_offer", {"offer_id": offer_id}, active_server)
        prepared = self.call("prepare_order", {"offer_id": offer_id}, active_server)
        return prepared["output"]["token"]

    def test_initialize_negotiates_supported_protocol(self) -> None:
        response = self.request(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}},
        )

        self.assertEqual(response["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", response["result"]["capabilities"])
        self.assertEqual(response["result"]["serverInfo"]["name"], "ndc-ops-copilot")

    def test_tools_list_exposes_six_tools_and_risk_annotations(self) -> None:
        response = self.request("tools/list")
        tools = {tool["name"]: tool for tool in response["result"]["tools"]}

        self.assertEqual(len(tools), 6)
        self.assertTrue(tools["search_flights"]["annotations"]["readOnlyHint"])
        self.assertTrue(tools["confirm_order"]["annotations"]["destructiveHint"])

    def test_tool_call_returns_structured_result(self) -> None:
        result = self.call("list_suppliers", {})

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["output"]), 2)

    def test_commit_tool_is_blocked_by_default(self) -> None:
        token = self.prepare_confirmation()
        result = self.call(
            "confirm_order",
            {
                "confirmation_token": token,
                "passengers": [{"name": "ZHANG/SAN", "type_code": "ADT"}],
            },
        )

        self.assertEqual(result["status"], "approval_required")

    def test_commit_tool_can_rely_on_mcp_host_approval(self) -> None:
        server = McpServer(
            AgentToolBridge(build_default_agent()),
            allow_commit_tools=True,
        )
        token = self.prepare_confirmation(server)
        result = self.call(
            "confirm_order",
            {
                "confirmation_token": token,
                "passengers": [{"name": "ZHANG/SAN", "type_code": "ADT"}],
            },
            server,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["output"]["status"], "confirmed")

    def test_stdio_transport_handles_parse_errors_and_notifications(self) -> None:
        input_stream = StringIO(
            "not-json\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "ping",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                }
            )
            + "\n"
        )
        output_stream = StringIO()

        serve(self.server, input_stream, output_stream)

        responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[1]["result"], {})
        self.assertEqual(len(responses), 2)


if __name__ == "__main__":
    unittest.main()
