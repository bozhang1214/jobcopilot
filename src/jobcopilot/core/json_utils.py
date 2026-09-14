"""从 LLM 响应里稳健提取 JSON。

LLM 输出的 JSON 常见两种脏法：带 markdown 代码围栏、前后有解释性文字。
两个入口**行为刻意不同**（历史原因，保持与既有分析结果一致，不要「统一」掉）：

- :func:`parse_json` 默认 ``strip_fences=True``：先剥围栏再取大括号区间（单职位流程用）。
- ``strip_fences=False``：只取大括号区间（批量流程用）。
"""
from __future__ import annotations

import json
import re
from typing import Any

from jobcopilot.core.logging import get_logger

logger = get_logger(__name__)

_FENCE_RE = re.compile(r"```(?:json)?\s*")


def _tag(name: str) -> str:
    """日志里的可选标签（空则不输出）。"""
    return f" [{name}]" if name else ""


def parse_json(raw: Any, *, strip_fences: bool = True, tag: str = "") -> dict[str, Any]:
    """从 LLM 响应中提取 JSON 对象，失败返回 ``{}``（不抛异常）。

    Args:
        raw: LLM 原始响应（``str`` 或带 ``content`` 的对象）。
        strip_fences: 是否先剥离 ```` ```json ```` 之类的代码围栏。
        tag: 出错日志里的标识（便于定位是哪一步）。

    Returns:
        解析出的字典；无法解析时为空字典。
    """
    text = raw if isinstance(raw, str) else str(raw)
    if strip_fences:
        text = _FENCE_RE.sub("", text)
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        logger.error(f"LLM 返回非 JSON{_tag(tag)} raw={text[:200]}")
        return {}
    try:
        out: dict[str, Any] = json.loads(text[start : end + 1])
        return out
    except json.JSONDecodeError as e:
        logger.error(f"JSON 解析失败{_tag(tag)} error={e} raw={text[:200]}")
        return {}
