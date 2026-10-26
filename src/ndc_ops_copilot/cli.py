"""Interactive CLI for NDC Ops Copilot."""

from __future__ import annotations

import json
import sys

from .agent import NdcAgent
from .llm_bridge import AgentToolBridge
from .models import AgentError, Passenger, ValidationError
from .parser import ParsedCommand, parse_command, parse_search_args
from .runtime import build_default_agent
from .serialization import to_primitive


HELP = """可用命令:
  search SHA FRA 2027-05-01 [成人数]   搜索航班
  查询 SHA 到 FRA 2027-05-01 1人      中文搜索
  price <OfferID>                      核价
  book <OfferID>                       生成订单确认令牌
  confirm <Token> ZHANG/SAN [...]      确认并创建订单
  order <OrderID>                      查询订单
  state                                查看当前会话状态
  suppliers                            查看供应商路由状态
  tools                                查看提供给LLM的工具清单
  help                                 查看帮助
  quit                                 退出
"""


def execute(
    agent: NdcAgent,
    parsed: ParsedCommand,
    bridge: AgentToolBridge | None = None,
):
    if parsed.name == "search":
        return agent.search(parse_search_args(parsed.args))
    if parsed.name == "price":
        _require_count(parsed, 1, "price <OfferID>")
        return agent.price_offer(parsed.args[0])
    if parsed.name == "book":
        _require_count(parsed, 1, "book <OfferID>")
        return agent.prepare_order(parsed.args[0])
    if parsed.name == "confirm":
        if len(parsed.args) < 2:
            raise ValidationError("用法: confirm <Token> ZHANG/SAN [其他乘机人]")
        passengers = tuple(Passenger(name) for name in parsed.args[1:])
        return agent.confirm_order(parsed.args[0], passengers)
    if parsed.name == "order":
        _require_count(parsed, 1, "order <OrderID>")
        return agent.retrieve_order(parsed.args[0])
    if parsed.name == "state":
        return agent.state
    if parsed.name == "suppliers":
        status = getattr(agent.adapter, "status", None)
        if status is None:
            return [{"supplier": agent.adapter.name, "mode": "single"}]
        return status()
    if parsed.name == "tools":
        return (bridge or AgentToolBridge(agent)).manifest()
    if parsed.name == "help":
        return HELP
    if parsed.name == "quit":
        return None
    raise ValidationError(f"未知命令: {parsed.name}，输入 help 查看帮助")


def _require_count(parsed: ParsedCommand, count: int, usage: str) -> None:
    if len(parsed.args) != count:
        raise ValidationError(f"用法: {usage}")


def render(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(to_primitive(value), ensure_ascii=False, indent=2)


def main() -> int:
    agent = build_default_agent()
    bridge = AgentToolBridge(agent)

    if len(sys.argv) > 1:
        try:
            result = execute(agent, parse_command(" ".join(sys.argv[1:])), bridge)
            if result is not None:
                print(render(result))
            return 0
        except AgentError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    print("NDC Ops Copilot (Mock Mode)")
    print(HELP)
    while True:
        try:
            text = input("ndc> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        try:
            parsed = parse_command(text)
            if parsed.name == "quit":
                return 0
            result = execute(agent, parsed, bridge)
            if result is not None:
                print(render(result))
        except AgentError as exc:
            print(f"ERROR: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
