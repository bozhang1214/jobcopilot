"""内核端口（Protocol）定义。

端口是内核与宿主的边界：内核只认这些方法，宿主（SEKB / CLI / MCP）提供实现。
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ProfilePort(Protocol):
    """用户画像提供方。

    内核不关心画像从哪来（SEKB 从聊天记录聚合、CLI 从本地 yaml 读），
    只要能拿到一段可直接注入提示词的文本。
    """

    def load(self, user_id: str | None = None) -> str:
        """返回用户画像文本（空串表示无画像）。"""
        ...


@runtime_checkable
class KVStorePort(Protocol):
    """按 key 隔离的 JSON 键值存储端口。

    用于投递计划、分析缓存等**非结构化**持久化。
    """

    def load(self) -> dict[str, Any]:
        """读出全部数据。"""
        ...

    def save(self, data: dict[str, Any]) -> None:
        """整体写回。"""
        ...
