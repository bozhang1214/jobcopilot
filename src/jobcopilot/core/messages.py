"""消息与 LLM 端口的**中性**抽象。

设计要点：core 层**不依赖 langchain / openai 等任何 SDK**。
消息用轻量 ``Message`` 表达，由各适配器转成自家类型
（SEKB 转 ``langchain_core.messages``，HTTP provider 转 OpenAI JSON）。
这样内核可在任意宿主里复用，也不会因为上游 SDK 大版本升级而连带返工。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# 角色常量：与 OpenAI Chat Completions 的 role 对齐，便于 provider 直接透传
ROLE_SYSTEM = "system"
ROLE_USER = "user"


@dataclass(frozen=True)
class Message:
    """一条对话消息（中性表示）。

    Attributes:
        role: ``"system"`` 或 ``"user"``。
        content: 消息正文。
    """

    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        """转成 OpenAI Chat Completions 的消息字典。"""
        return {"role": self.role, "content": self.content}


def system(content: str) -> Message:
    """构造 system 消息。"""
    return Message(role=ROLE_SYSTEM, content=content)


def user(content: str) -> Message:
    """构造 user 消息。"""
    return Message(role=ROLE_USER, content=content)


@runtime_checkable
class LLMPort(Protocol):
    """LLM 调用端口。

    实现方只需保证：给定角色名与消息列表，返回**文本**（或带 ``.content``
    属性的对象，内核会自动取 ``.content``）。
    """

    async def complete(self, role: str, messages: list[Message]) -> Any:
        """调用 LLM 并返回响应（``str`` 或带 ``content`` 属性的对象）。"""
        ...


def extract_text(resp: Any) -> str:
    """从 LLM 响应中取文本：兼容 ``str`` 与带 ``.content`` 的对象。"""
    return resp.content if hasattr(resp, "content") else str(resp)
