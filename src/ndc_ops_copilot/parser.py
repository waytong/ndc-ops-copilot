"""Small command parser that can later be replaced by an LLM planner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
import shlex
import unicodedata

from .models import ValidationError


@dataclass(frozen=True)
class ParsedCommand:
    name: str
    args: tuple[str, ...] = ()


ALIASES = {
    "查询": "search",
    "搜索": "search",
    "核价": "price",
    "预订": "book",
    "确认": "confirm",
    "订单": "order",
    "状态": "state",
    "供应商": "suppliers",
    "工具": "tools",
    "帮助": "help",
    "退出": "quit",
}


_HYPHENS = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\ufe63": "-",
        "\uff0d": "-",
    }
)


def _normalize_command_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).translate(_HYPHENS)
    normalized = "".join(
        " " if unicodedata.category(character) == "Cf" else character
        for character in normalized
    )
    return " ".join(normalized.split())


def parse_command(text: str) -> ParsedCommand:
    raw = _normalize_command_text(text)
    if not raw:
        raise ValidationError("请输入命令")

    natural_search = re.fullmatch(
        r"(?:查询|搜索)\s+([A-Za-z]{3})\s*(?:到|->)\s*([A-Za-z]{3})\s+"
        r"(\d{4}-\d{2}-\d{2})(?:\s+(\d+)\s*人?)?",
        raw,
    )
    if natural_search:
        origin, destination, departure, adults = natural_search.groups()
        return ParsedCommand(
            "search", (origin, destination, departure, adults or "1")
        )

    try:
        parts = shlex.split(raw)
    except ValueError as exc:
        raise ValidationError(f"命令格式错误: {exc}") from exc

    command = ALIASES.get(parts[0].lower(), parts[0].lower())
    return ParsedCommand(command, tuple(parts[1:]))


def parse_search_args(args: tuple[str, ...]):
    from .models import SearchRequest

    if len(args) not in {3, 4}:
        raise ValidationError("用法: search SHA FRA 2027-05-01 [成人数]")
    try:
        departure = date.fromisoformat(args[2])
        adults = int(args[3].removesuffix("人")) if len(args) == 4 else 1
    except ValueError as exc:
        raise ValidationError("日期应为YYYY-MM-DD，成人数应为整数") from exc
    return SearchRequest(args[0], args[1], departure, adults)
