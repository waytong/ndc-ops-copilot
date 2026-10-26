"""Minimal Model Context Protocol server using JSON-RPC over stdio."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any, TextIO

from .llm_bridge import AgentToolBridge, ToolCall, ToolResult
from .models import ActionRisk, AgentError
from .runtime import build_default_agent


JsonObject = dict[str, Any]
SUPPORTED_PROTOCOL_VERSIONS = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)


@dataclass(frozen=True)
class JsonRpcError(Exception):
    code: int
    message: str
    data: Any = None


class McpServer:
    """Expose AgentToolBridge through the MCP tools capability."""

    def __init__(
        self,
        bridge: AgentToolBridge,
        *,
        allow_commit_tools: bool = False,
    ) -> None:
        self.bridge = bridge
        self.allow_commit_tools = allow_commit_tools
        self._call_sequence = 0

    def handle(self, request: Any) -> JsonObject | None:
        """Handle one decoded JSON-RPC message."""
        request_id = request.get("id") if isinstance(request, dict) else None
        try:
            self._validate_request(request)
            method = request["method"]
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise JsonRpcError(-32602, "params must be an object")

            if method == "notifications/initialized":
                return None
            if method == "initialize":
                result = self._initialize(params)
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = self._list_tools()
            elif method == "tools/call":
                result = self._call_tool(params, request_id)
            else:
                raise JsonRpcError(-32601, f"Method not found: {method}")
            if "id" not in request:
                return None
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except JsonRpcError as exc:
            if isinstance(request, dict) and "id" not in request:
                return None
            error: JsonObject = {"code": exc.code, "message": exc.message}
            if exc.data is not None:
                error["data"] = exc.data
            return {"jsonrpc": "2.0", "id": request_id, "error": error}
        except Exception as exc:
            if isinstance(request, dict) and "id" not in request:
                return None
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32603,
                    "message": "Internal error",
                    "data": {"error_type": type(exc).__name__},
                },
            }

    def _initialize(self, params: JsonObject) -> JsonObject:
        requested = params.get("protocolVersion")
        protocol_version = (
            requested
            if requested in SUPPORTED_PROTOCOL_VERSIONS
            else SUPPORTED_PROTOCOL_VERSIONS[0]
        )
        return {
            "protocolVersion": protocol_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "ndc-ops-copilot", "version": "0.3.0"},
            "instructions": (
                "Search and service airline offers through the available tools. "
                "Order creation may require approval by the MCP host."
            ),
        }

    def _list_tools(self) -> JsonObject:
        tools = []
        for item in self.bridge.manifest():
            function = item["function"]
            risk = item["risk"]
            name = function["name"]
            tools.append(
                {
                    "name": name,
                    "description": function["description"],
                    "inputSchema": function["parameters"],
                    "annotations": {
                        "readOnlyHint": risk == ActionRisk.READ.value,
                        "destructiveHint": risk == ActionRisk.COMMIT.value,
                        "idempotentHint": name in {"retrieve_order", "list_suppliers"},
                        "openWorldHint": name != "list_suppliers",
                    },
                }
            )
        return {"tools": tools}

    def _call_tool(self, params: JsonObject, request_id: Any) -> JsonObject:
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not name:
            raise JsonRpcError(-32602, "tools/call requires a non-empty name")
        if not isinstance(arguments, dict):
            raise JsonRpcError(-32602, "tools/call arguments must be an object")

        self._call_sequence += 1
        call = ToolCall(
            call_id=f"mcp-{request_id}-{self._call_sequence}",
            name=name,
            arguments=arguments,
        )
        risk = self.bridge.tool_risk(name)
        if risk == ActionRisk.COMMIT and not self.allow_commit_tools:
            result = ToolResult(
                call_id=call.call_id,
                name=name,
                status="approval_required",
                output={
                    "message": (
                        "MCP服务端默认禁止提交操作。请在确认客户端会对该工具进行人工审批后，"
                        "使用--allow-commit-tools启动服务。"
                    )
                },
            )
        elif risk == ActionRisk.COMMIT:
            try:
                self.bridge.approve(call)
                result = self.bridge.execute(call)
            except AgentError as exc:
                result = ToolResult(
                    call_id=call.call_id,
                    name=name,
                    status="error",
                    output={"error": str(exc), "error_type": type(exc).__name__},
                )
        else:
            result = self.bridge.execute(call)

        payload = {"status": result.status, "output": result.output}
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(payload, ensure_ascii=False),
                }
            ],
            "structuredContent": payload,
            "isError": result.status != "ok",
        }

    @staticmethod
    def _validate_request(request: Any) -> None:
        if not isinstance(request, dict):
            raise JsonRpcError(-32600, "Invalid Request")
        if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            raise JsonRpcError(-32600, "Invalid Request")


def serve(server: McpServer, input_stream: TextIO, output_stream: TextIO) -> None:
    """Run a newline-delimited MCP stdio transport until stdin closes."""
    for raw_line in input_stream:
        if not raw_line.strip():
            continue
        try:
            request = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            response: JsonObject | None = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32700,
                    "message": "Parse error",
                    "data": {"line": exc.lineno, "column": exc.colno},
                },
            }
        else:
            response = server.handle(request)
        if response is not None:
            output_stream.write(
                json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            output_stream.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NDC Ops Copilot MCP stdio server")
    parser.add_argument(
        "--allow-commit-tools",
        action="store_true",
        help="Allow commit tools after the MCP host performs its own user approval",
    )
    args = parser.parse_args(argv)
    server = McpServer(
        AgentToolBridge(build_default_agent()),
        allow_commit_tools=args.allow_commit_tools,
    )
    try:
        serve(server, sys.stdin, sys.stdout)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
