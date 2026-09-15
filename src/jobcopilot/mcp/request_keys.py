"""按请求解析 BYOK 凭据（HTTP 头 → LLM 客户端）。

**为什么需要它**：云端平台（扣子 / 百炼 / 千帆 / HiAgent / Dify）跑在别人的机器上，
每个调用方应当**用自己的 Key**。若只在启动时读环境变量，一台服务器就只能服务一个
Key——多用户场景下等于运营方替所有人付费。

**为什么不用自己的 contextvar**：Starlette 的 ``BaseHTTPMiddleware`` 会在**另一个
任务**里执行下游应用，middleware 里 set 的 contextvar 传不到工具处理器（已知坑）。
这里改为读 MCP **自己**的 ``request_ctx``：streamable HTTP 传输在处理每个消息时把
Starlette ``Request`` 放进该 contextvar，而工具处理器与它同任务执行。这是实测确认
的（见 ``tests/test_mcp_http.py``），不依赖我们做任何隐式传播。

**鉴权头与 BYOK 头是分开的**（刻意如此）：

- ``Authorization: Bearer <token>`` → **服务访问令牌**（``JOBCOPILOT_HTTP_TOKEN``）；
- ``X-JobCopilot-Api-Key: <key>``   → **调用方自己的 LLM Key**（BYOK）。

分开的目的：同一个 ``Authorization`` 既当门禁又当 LLM Key 会让「令牌错误」与
「Key 无效」两类问题纠缠在一起，排障困难。
"""
from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Mapping

from jobcopilot.core.messages import LLMPort
from jobcopilot.mcp.config import ServerConfig

#: 请求头：调用方自己的 LLM Key（BYOK）
HEADER_API_KEY = "x-jobcopilot-api-key"
#: 请求头：覆盖 LLM 预设（deepseek/qwen/kimi/...）
HEADER_PROVIDER = "x-jobcopilot-provider"
#: 请求头：覆盖模型名
HEADER_MODEL = "x-jobcopilot-model"

#: 请求级 LLM 客户端缓存上限（每个都持有 HTTP 连接池，不设上限会泄漏）
_LLM_CACHE_MAX = 32
_llm_cache: "OrderedDict[str, LLMPort]" = OrderedDict()


@dataclass(frozen=True)
class RequestCredentials:
    """一次请求携带的 BYOK 凭据。

    Attributes:
        api_key: 调用方提供的 LLM Key（非空）。
        provider: 覆盖预设名；``None`` 表示沿用服务端默认。
        model: 覆盖模型名；``None`` 表示沿用预设默认。
    """

    api_key: str
    provider: str | None = None
    model: str | None = None


def credentials_from_headers(
    headers: Mapping[str, str],
) -> RequestCredentials | None:
    """从 HTTP 头解析 BYOK 凭据。

    Args:
        headers: 请求头（大小写不敏感的实现即可，如 Starlette 的 ``Headers``）。

    Returns:
        有 ``X-JobCopilot-Api-Key`` 时返回凭据，否则 ``None``（回落服务端配置）。

    Note:
        ``Authorization`` 头**不**参与解析——它属于服务访问令牌，见模块文档。
    """
    api_key = (headers.get(HEADER_API_KEY) or "").strip()
    if not api_key:
        return None
    provider = (headers.get(HEADER_PROVIDER) or "").strip() or None
    model = (headers.get(HEADER_MODEL) or "").strip() or None
    return RequestCredentials(api_key=api_key, provider=provider, model=model)


def current_request() -> Any | None:
    """取「当前正在处理的」Starlette ``Request``。

    走 lowlevel 的 ``request_ctx`` contextvar：MCP 的 streamable HTTP 传输在处理每个
    消息时会 ``request_ctx.set(RequestContext(request=<Starlette Request>, ...))``，
    而工具处理器在**同一个任务**里被 await——所以这里能直接读到。

    这条路径由 ``tests/test_mcp_http.py`` 的端到端用例守着（真的起 HTTP 服务、
    真的带请求头调工具、断言拿到的就是那个头）。曾评估过用 FastMCP 的 ``Context``
    注入（``ctx: Context`` 参数）——那是官方支持写法，但会给 6 个工具都加参数，
    且需要 ``Context`` 在模块全局可解析（本项目用了 ``from __future__ import
    annotations``，解析不到时会**静默**不注入，反而更危险）。

    Returns:
        Starlette ``Request``；stdio 传输、或 SDK 不再把请求放进 contextvar 时
        返回 ``None``（调用方 ``guard`` 会打 error 日志，不会静默降级）。
    """
    try:
        from mcp.server.lowlevel.server import request_ctx
    except ImportError:  # pragma: no cover - 未装 MCP 依赖
        return None
    try:
        req_ctx = request_ctx.get()
    except LookupError:  # 不在请求上下文里（stdio / 直接调用工具函数）
        return None
    return getattr(req_ctx, "request", None)


def credentials_from_request(request: Any | None) -> RequestCredentials | None:
    """从 Starlette ``Request`` 解析 BYOK 凭据（无请求头则 ``None``）。"""
    if request is None:
        return None
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    return credentials_from_headers(headers)


def _cache_key(creds: RequestCredentials, config: ServerConfig) -> str:
    """构造缓存键：**不把明文 Key 当键**，只放它的短哈希。"""
    digest = hashlib.sha256(creds.api_key.encode("utf-8")).hexdigest()[:16]
    return f"{creds.provider or config.provider}|{creds.model or config.model}|{digest}"


def resolve_llm(creds: RequestCredentials, config: ServerConfig) -> LLMPort:
    """按凭据构造（并缓存）LLM 客户端。

    Args:
        creds: 请求携带的凭据。
        config: 服务端配置（提供 provider / model 的默认值）。

    Returns:
        真实可用的 LLM 客户端。带 LRU 缓存，避免每个请求都重建连接池。
    """
    key = _cache_key(creds, config)
    hit = _llm_cache.get(key)
    if hit is not None:
        _llm_cache.move_to_end(key)
        return hit

    from jobcopilot.core.providers import OpenAICompatLLM

    llm = OpenAICompatLLM(
        api_key=creds.api_key,
        model=creds.model or None,
        preset=creds.provider or config.provider,
    )
    _llm_cache[key] = llm
    while len(_llm_cache) > _LLM_CACHE_MAX:
        _llm_cache.popitem(last=False)
    return llm


def clear_llm_cache() -> None:
    """清空请求级 LLM 缓存（测试用，也可用于轮换 Key 后释放连接）。"""
    _llm_cache.clear()
