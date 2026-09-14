"""OpenAI 兼容协议的 LLM 客户端（BYOK）。

覆盖 DeepSeek / 通义千问 / Kimi / 豆包 / 智谱 / OpenAI 等一切兼容
``POST {base_url}/chat/completions`` 的服务。

设计取舍：
- 用 ``httpx`` 直连而不引 openai SDK —— 少一层大版本耦合，且能精确控制超时/重试；
- ``httpx`` 是**可选依赖**（``pip install jobcopilot[providers]``），
  内核本身零第三方依赖，宿主（如 SEKB）自带 LLM 时可完全不装。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

from jobcopilot.core.messages import LLMPort, Message
from jobcopilot.core.providers.presets import ProviderPreset, get_preset


class ProviderError(RuntimeError):
    """LLM 调用失败（网络 / 鉴权 / 服务端错误，重试后仍未成功）。"""


class MissingAPIKeyError(ProviderError):
    """未找到 API Key。"""


class OpenAICompatLLM:
    """OpenAI 兼容协议的 LLM 客户端。

    Args:
        api_key: API Key；``None`` 时从 ``preset.env_key`` 指定的环境变量读。
        model: 模型名；``None`` 时用预设默认值。
        preset: 预设名（见 :data:`~jobcopilot.core.providers.presets.PRESETS`）
            或 :class:`ProviderPreset` 实例。
        base_url: 直接指定端点（给了就优先于 preset 的）。
        timeout: 单次请求超时（秒）。
        max_retries: 失败重试次数（指数退避）。
        temperature: 采样温度。

    Raises:
        MissingAPIKeyError: 构造时就拿不到 Key（**尽早失败**，别等到跑完流程才报）。
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        preset: str | ProviderPreset = "deepseek",
        base_url: str | None = None,
        timeout: float = 120.0,
        max_retries: int = 2,
        temperature: float = 0.3,
    ) -> None:
        p = get_preset(preset) if isinstance(preset, str) else preset
        self._preset = p
        self._base_url = (base_url or p.base_url).rstrip("/")
        self._model = model or p.default_model
        self._timeout = timeout
        self._max_retries = max_retries
        self._temperature = temperature

        key = api_key or os.environ.get(p.env_key, "")
        if not key:
            raise MissingAPIKeyError(
                f"未找到 {p.label} 的 API Key；请设置环境变量 {p.env_key}，"
                f"或用 --api-key 传入。"
            )
        self._api_key = key

    @property
    def model(self) -> str:
        """当前模型名。"""
        return self._model

    @property
    def base_url(self) -> str:
        """当前端点。"""
        return self._base_url

    async def complete(self, role: str, messages: list[Message]) -> str:
        """调用 ``/chat/completions``，返回首个 choice 的正文。

        Args:
            role: 角色名（当前实现不区分角色——内核的 ``role`` 用于宿主侧的
                模型路由，独立使用时忽略即可，保留是为了满足 ``LLMPort``）。
            messages: 消息列表。

        Raises:
            ProviderError: 网络异常或非 2xx 响应（重试后仍失败）。
        """
        import httpx  # 延迟导入：不装 providers extra 也能 import 本模块

        url = f"{self._base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [m.to_dict() for m in messages],
            "temperature": self._temperature,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        last_err: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code >= 400:
                    raise ProviderError(
                        f"{self._preset.label} 返回 {resp.status_code}: {resp.text[:300]}"
                    )
                data = resp.json()
                return str(data["choices"][0]["message"]["content"])
            except ProviderError as e:
                last_err = e
                # 4xx 里只有 429 值得重试，其余（401/403/400）重试无意义
                if not _retryable_http(str(e)):
                    raise
            except Exception as e:  # noqa: BLE001
                last_err = e
            if attempt < self._max_retries:
                await asyncio.sleep(2**attempt)

        # 报错必须能排障：网络类异常（ConnectError/Timeout）的 str() 常常是空的，
        # 只写 "调用失败: " 会让人完全无从下手。带上异常类型名与兜底说明。
        detail = str(last_err).strip() or "无错误详情，通常是网络不可达或超时"
        raise ProviderError(
            f"{self._preset.label} 调用失败（{type(last_err).__name__}）: {detail}"
        )


def _retryable_http(msg: str) -> bool:
    """判断错误信息里的 HTTP 状态码是否值得重试（429 / 5xx）。"""
    for code in (" 429", " 500", " 502", " 503", " 504"):
        if code in msg:
            return True
    return "返回 4" not in msg and "返回 5" not in msg


__all__ = ["OpenAICompatLLM", "ProviderError", "MissingAPIKeyError", "LLMPort"]
