"""provider（BYOK）的测试：不联网，全部通过替身客户端验证。"""
from __future__ import annotations

from typing import Any

import pytest

from jobcopilot.core.messages import system, user
from jobcopilot.core.providers.openai_compat import (
    MissingAPIKeyError,
    OpenAICompatLLM,
    ProviderError,
)
from jobcopilot.core.providers.presets import PRESETS, get_preset

MSGS = [system("你是分析师"), user("分析这份 JD")]


class _Resp:
    """httpx.Response 的最小替身。"""

    def __init__(self, status_code: int, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> Any:
        return self._payload


class _Client:
    """httpx.AsyncClient 的替身，按脚本返回响应并记录请求。"""

    def __init__(self, script: list[Any], record: dict[str, Any]) -> None:
        self._script = script
        self._record = record

    async def __aenter__(self) -> "_Client":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def post(self, url: str, json: Any = None, headers: Any = None) -> _Resp:
        self._record["url"] = url
        self._record["json"] = json
        self._record["headers"] = headers
        self._record.setdefault("calls", 0)
        self._record["calls"] += 1
        item = self._script[min(self._record["calls"] - 1, len(self._script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def _patch_client(monkeypatch: pytest.MonkeyPatch, script: list[Any]) -> dict[str, Any]:
    """把 httpx.AsyncClient 换成替身，返回记录字典。"""
    import httpx

    record: dict[str, Any] = {}
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: _Client(script, record), raising=True
    )
    return record


def test_presets_cover_mainland_providers() -> None:
    """预设覆盖国内主流厂商 + OpenAI。"""
    for name in ["deepseek", "qwen", "kimi", "doubao", "zhipu", "openai"]:
        assert name in PRESETS
        p = get_preset(name)
        assert p.base_url.startswith("https://")
        assert p.default_model
        assert p.env_key


def test_get_preset_unknown_lists_available() -> None:
    """未知 provider 报错时列出可用项（CLI 可直接展示给用户）。"""
    with pytest.raises(KeyError, match="未知 provider"):
        get_preset("不存在")


def test_missing_api_key_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有 Key 时构造即失败（不能等跑完流程才发现）。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(MissingAPIKeyError, match="DEEPSEEK_API_KEY"):
        OpenAICompatLLM(preset="deepseek")


def test_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """未显式传 Key 时从预设的环境变量读取。"""
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-from-env")
    llm = OpenAICompatLLM(preset="kimi")
    assert llm.model == "moonshot-v1-32k"


def test_explicit_key_and_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """显式传的 Key / model / base_url 优先于预设。"""
    llm = OpenAICompatLLM(
        api_key="sk-x", model="custom-model", preset="qwen", base_url="https://example.com/v1/"
    )
    assert llm.model == "custom-model"
    assert llm.base_url == "https://example.com/v1"  # 去掉尾部斜杠


@pytest.mark.asyncio
async def test_complete_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """正常返回：解析 choices[0].message.content，并带上鉴权头。"""
    rec = _patch_client(
        monkeypatch,
        [_Resp(200, {"choices": [{"message": {"content": "分析结果"}}]})],
    )
    llm = OpenAICompatLLM(api_key="sk-test", preset="deepseek")
    out = await llm.complete("job_analysis", MSGS)
    assert out == "分析结果"
    assert rec["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert rec["headers"]["Authorization"] == "Bearer sk-test"
    assert rec["json"]["model"] == "deepseek-chat"
    assert rec["json"]["messages"][0] == {"role": "system", "content": "你是分析师"}


@pytest.mark.asyncio
async def test_complete_auth_error_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """401 不重试（重试无意义），直接抛 ProviderError。"""
    rec = _patch_client(monkeypatch, [_Resp(401, text="unauthorized")])
    llm = OpenAICompatLLM(api_key="sk-test", preset="deepseek")
    with pytest.raises(ProviderError, match="401"):
        await llm.complete("job_analysis", MSGS)
    assert rec["calls"] == 1


@pytest.mark.asyncio
async def test_complete_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """429 会重试，重试成功后返回内容。"""
    rec = _patch_client(
        monkeypatch,
        [
            _Resp(429, text="rate limited"),
            _Resp(200, {"choices": [{"message": {"content": "重试成功"}}]}),
        ],
    )
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    llm = OpenAICompatLLM(api_key="sk-test", preset="deepseek", max_retries=2)
    assert await llm.complete("job_analysis", MSGS) == "重试成功"
    assert rec["calls"] == 2


@pytest.mark.asyncio
async def test_complete_exhausted_retries_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """重试用尽后抛 ProviderError，附上最后错误。"""
    rec = _patch_client(monkeypatch, [_Resp(503, text="unavailable")])
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    llm = OpenAICompatLLM(api_key="sk-test", preset="deepseek", max_retries=1)
    with pytest.raises(ProviderError, match="调用失败"):
        await llm.complete("job_analysis", MSGS)
    assert rec["calls"] == 2


@pytest.mark.asyncio
async def test_complete_network_error_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """网络异常也走重试。"""
    rec = _patch_client(monkeypatch, [ConnectionError("boom")])
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    llm = OpenAICompatLLM(api_key="sk-test", preset="deepseek", max_retries=1)
    with pytest.raises(ProviderError):
        await llm.complete("job_analysis", MSGS)
    assert rec["calls"] == 2


async def _no_sleep(_seconds: float) -> None:
    """把退避睡眠变成空操作，让重试测试瞬间完成。"""
    return None


@pytest.mark.asyncio
async def test_network_error_message_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    """网络类异常常常 str() 为空——报错必须带上类型与兜底说明，否则无法排障。

    这条是实测 OpenAI 在国内不可达时发现的：当时错误信息是
    「OpenAI 调用失败: 」（冒号后面空白），完全看不出发生了什么。
    """

    class _Boom:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> None:
            return None

        async def post(self, *a, **k):
            import httpx

            raise httpx.ConnectError("")  # 空消息，模拟真实网络失败

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Boom)
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    llm = OpenAICompatLLM(api_key="sk-x", preset="openai", max_retries=0)
    with pytest.raises(ProviderError) as ei:
        await llm.complete("job_analysis", MSGS)
    msg = str(ei.value)
    assert "ConnectError" in msg, msg
    assert "网络不可达" in msg, msg
