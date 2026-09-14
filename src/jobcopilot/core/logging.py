"""日志接入点（宿主可注入自己的 logger）。

内核默认用标准库 ``logging``；宿主可以在 import 后调用
:func:`set_logger_factory` 把内核日志接进自家的日志管道
（例如 SEKB 的 structlog：JSON 格式 + 敏感字段脱敏 + trace_id）。

实现要点：
- :func:`get_logger` 返回**惰性代理**，每次写日志时才向当前工厂取真实 logger，
  因此宿主「先 import 内核、后设置工厂」也不会失效；
- 消息统一用 f-string 拼好再传，不依赖 ``%`` 占位符或额外 kwargs，
  这样标准库 logger 与 structlog logger 都能正确输出。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

#: logger 工厂签名：名称 → logger 对象
LoggerFactory = Callable[[str], Any]

_factory: LoggerFactory = logging.getLogger


def set_logger_factory(factory: LoggerFactory) -> None:
    """设置全局 logger 工厂（宿主启动时调用一次即可）。"""
    global _factory
    _factory = factory


def reset_logger_factory() -> None:
    """恢复默认（标准库 logging）——主要给测试用。"""
    global _factory
    _factory = logging.getLogger


def get_logger(name: str) -> Any:
    """返回惰性 logger 代理（属性访问时才绑定到当前工厂的真实 logger）。"""
    return _LazyLogger(name)


class _LazyLogger:
    """把属性访问转发给「当前工厂」产出的 logger。"""

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, item: str) -> Any:
        return getattr(_factory(self._name), item)
