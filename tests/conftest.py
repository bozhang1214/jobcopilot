"""测试夹具：一个可编程的假 LLM，用于零成本、确定性地验证内核流水线。"""
from __future__ import annotations

from typing import Any

import pytest

from jobcopilot.core.messages import Message


class FakeLLM:
    """记录调用并按预设返回内容的假 LLM。

    实现 ``LLMPort``：``complete(role, messages) -> str``。

    Args:
        responses: 按调用顺序返回的响应；用尽后返回 ``default``。
        default: 响应用尽后的兜底内容。
        raises: 若给定异常，每次调用都抛它（测降级路径）。
    """

    def __init__(
        self,
        responses: list[str] | None = None,
        default: str = "{}",
        raises: Exception | None = None,
    ) -> None:
        self._responses = list(responses or [])
        self._default = default
        self._raises = raises
        self.calls: list[tuple[str, list[Message]]] = []

    async def complete(self, role: str, messages: list[Message]) -> Any:
        """记录调用并返回预设响应。"""
        self.calls.append((role, list(messages)))
        if self._raises is not None:
            raise self._raises
        if self._responses:
            return self._responses.pop(0)
        return self._default

    @property
    def prompts(self) -> list[str]:
        """返回每次调用传入的 system 消息正文（便于断言提示词注入正确）。"""
        return [m.content for _, msgs in self.calls for m in msgs if m.role == "system"]

    @property
    def payloads(self) -> list[str]:
        """返回每次调用传入的 user 消息正文（便于断言变量替换正确）。"""
        return [m.content for _, msgs in self.calls for m in msgs if m.role == "user"]


@pytest.fixture
def fake_llm() -> FakeLLM:
    """默认假 LLM（返回空 JSON 对象）。"""
    return FakeLLM()
