"""HTTP 形态的端到端测试：BYOK 请求头、访问令牌、Host 白名单。

为什么必须端到端：BYOK 依赖「工具处理器能读到本次请求的请求头」，这一点取决于
MCP SDK 内部的任务调度方式（``request_ctx`` contextvar 是否同任务可见）。只做单元
测试无法证明它真的成立——所以这里**真的起一个 streamable HTTP 服务**再带请求头调工具。

全部跑在回环地址上，不需要任何外部 LLM（用假 LLM 替换）。
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time
from typing import Any

import pytest

pytest.importorskip("mcp", reason="未安装 mcp 依赖")
pytest.importorskip("uvicorn", reason="未安装 uvicorn（随 mcp 提供）")

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402

from jobcopilot.mcp import request_keys  # noqa: E402
from jobcopilot.mcp.config import ServerConfig, is_loopback_host  # noqa: E402
from jobcopilot.mcp.server import (  # noqa: E402
    apply_transport_security,
    build_http_app,
    build_server,
)
from jobcopilot.mcp.tools import ToolContext  # noqa: E402

TOKEN = "tok-0123456789abcdef"


# ============================================================
# 假 LLM：区分「服务端默认 LLM」与「请求级 BYOK LLM」
# ============================================================


class StartupLLM:
    """服务端启动时注入的默认 LLM；被调用即记录（用于断言 BYOK 生效时它不被用）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, role: str, messages: Any) -> Any:
        self.calls += 1
        return '{"ok": true}'


class RecordingLLM:
    """替换 ``OpenAICompatLLM``：记录被构造时使用的 api_key。"""

    #: 记录每次构造用到的 Key（跨测试需手动清空）
    keys: list[str] = []

    def __init__(self, api_key: str | None = None, model: str | None = None, preset: str | None = None):
        RecordingLLM.keys.append(api_key or "")
        self.api_key = api_key
        self.preset = preset
        self.calls = 0

    async def complete(self, role: str, messages: Any) -> Any:
        self.calls += 1
        return '{"ok": true}'


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Any:
    """每个用例前清空请求级 LLM 缓存与记录（缓存是模块级全局）。"""
    request_keys.clear_llm_cache()
    RecordingLLM.keys = []
    yield
    request_keys.clear_llm_cache()
    RecordingLLM.keys = []


# ============================================================
# 一、绑定安全闸（不需要起服务）
# ============================================================


def test_is_loopback_host() -> None:
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("localhost")
    assert is_loopback_host("::1")
    assert not is_loopback_host("0.0.0.0")
    assert not is_loopback_host("10.0.0.5")


def test_bind_error_none_for_stdio() -> None:
    cfg = ServerConfig(transport="stdio", host="0.0.0.0", http_token="")
    assert cfg.http_bind_error() is None


def test_bind_error_none_for_loopback_without_token() -> None:
    cfg = ServerConfig(transport="streamable-http", host="127.0.0.1")
    assert cfg.http_bind_error() is None


def test_bind_error_rejects_public_without_token() -> None:
    """对外暴露且无令牌 → 必须拒绝，并给出可操作提示。"""
    cfg = ServerConfig(transport="streamable-http", host="0.0.0.0")
    err = cfg.http_bind_error()
    assert err is not None
    assert "JOBCOPILOT_HTTP_TOKEN" in err
    assert "--host 127.0.0.1" in err


def test_bind_error_requires_allowed_hosts_once_token_set() -> None:
    """配了令牌但没配 Host 白名单也要拒——否则云端平台只会收到 421，很难排障。"""
    cfg = ServerConfig(transport="streamable-http", host="0.0.0.0", http_token=TOKEN)
    err = cfg.http_bind_error()
    assert err is not None
    assert "JOBCOPILOT_HTTP_ALLOWED_HOSTS" in err
    assert "421" in err


def test_bind_error_passes_with_token_and_hosts() -> None:
    cfg = ServerConfig(
        transport="streamable-http",
        host="0.0.0.0",
        http_token=TOKEN,
        allowed_hosts=["jobcopilot.example.com"],
    )
    assert cfg.http_bind_error() is None


def test_bind_error_escape_hatch() -> None:
    """显式放弃保护时放行（仅限可信内网）。"""
    cfg = ServerConfig(transport="streamable-http", host="0.0.0.0", allow_public_bind=True)
    assert cfg.http_bind_error() is None


def test_transport_security_expands_bare_hostname() -> None:
    """SDK 只认精确匹配或 host:*，所以裸域名必须自动补一份带端口的。"""
    cfg = ServerConfig(
        transport="streamable-http",
        allowed_hosts=["jobcopilot.example.com", "10.0.0.5:9000"],
    )
    ts = cfg.transport_security()
    hosts = ts.allowed_hosts
    assert "jobcopilot.example.com" in hosts
    assert "jobcopilot.example.com:*" in hosts
    # 已经带端口的原样保留，不重复补
    assert "10.0.0.5:9000" in hosts
    assert "10.0.0.5:9000:*" not in hosts
    # 回环始终放行（本机自检用）
    assert "127.0.0.1:*" in hosts
    assert ts.enable is True
    # 保序去重
    assert len(hosts) == len(set(hosts))


# ============================================================
# 二、请求头解析（不需要起服务）
# ============================================================


class _FakeRequest:
    """最小请求替身：只需要 ``headers``。"""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


def test_credentials_none_without_header() -> None:
    assert request_keys.credentials_from_headers({}) is None
    assert request_keys.credentials_from_headers({"authorization": "Bearer x"}) is None


def test_credentials_from_headers() -> None:
    creds = request_keys.credentials_from_headers(
        {
            "x-jobcopilot-api-key": " k1 ",
            "x-jobcopilot-provider": "qwen",
            "x-jobcopilot-model": "qwen-max",
        }
    )
    assert creds is not None
    assert creds.api_key == "k1"
    assert creds.provider == "qwen"
    assert creds.model == "qwen-max"


def test_credentials_from_headers_optional_overrides() -> None:
    creds = request_keys.credentials_from_headers({"x-jobcopilot-api-key": "k1"})
    assert creds is not None
    assert (creds.provider, creds.model) == (None, None)


def test_authorization_is_not_treated_as_llm_key() -> None:
    """默认不把鉴权头当 LLM Key：混用会让「令牌错」与「Key 无效」纠缠在一起。"""
    creds = request_keys.credentials_from_headers({"authorization": f"Bearer {TOKEN}"})
    assert creds is None


def test_authorization_used_as_llm_key_when_allowed() -> None:
    """服务端**没设**访问令牌时，顺着扣子/Dify 的 UI 惯例接受 Authorization 里的 Key。"""
    creds = request_keys.credentials_from_headers(
        {"authorization": "Bearer sk-from-platform"}, allow_authorization=True
    )
    assert creds is not None
    assert creds.api_key == "sk-from-platform"


def test_explicit_header_wins_over_authorization() -> None:
    """两个头都在时以显式头为准（「带了服务令牌又带自己 Key」的组合）。"""
    creds = request_keys.credentials_from_headers(
        {"authorization": f"Bearer {TOKEN}", "x-jobcopilot-api-key": "sk-mine"},
        allow_authorization=True,
    )
    assert creds is not None
    assert creds.api_key == "sk-mine"


def test_authorization_non_bearer_is_ignored() -> None:
    """非 Bearer 形式的 Authorization 不当 Key（如 Basic / 平台自定义方案）。"""
    creds = request_keys.credentials_from_headers(
        {"authorization": "Basic dXNlcjpwYXNz"}, allow_authorization=True
    )
    assert creds is None


def test_authorization_fallback_respects_server_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """设了服务令牌 → Authorization 归令牌，不能当成 LLM Key。"""
    monkeypatch.setattr("jobcopilot.core.providers.OpenAICompatLLM", RecordingLLM)
    startup = StartupLLM()

    with_token = ToolContext(
        llm=startup,
        config=ServerConfig(transport="streamable-http", http_token=TOKEN),
    )
    assert with_token.with_request(_FakeRequest({"authorization": "Bearer sk-x"})).llm is startup
    assert RecordingLLM.keys == []

    no_token = ToolContext(llm=startup, config=ServerConfig(transport="streamable-http"))
    derived = no_token.with_request(_FakeRequest({"authorization": "Bearer sk-x"}))
    assert derived.llm is not startup
    assert RecordingLLM.keys == ["sk-x"]


def test_current_request_is_none_outside_request() -> None:
    """不在请求上下文里（如直接调工具函数）应返回 None，而不是抛异常。"""
    assert request_keys.current_request() is None


def test_with_request_none_returns_self() -> None:
    ctx = ToolContext(llm=StartupLLM(), config=ServerConfig(transport="stdio"))
    assert ctx.with_request(None) is ctx


def test_with_request_switches_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """请求里带 Key → 换成请求级 LLM；不带 → 沿用服务端 LLM。"""
    monkeypatch.setattr("jobcopilot.core.providers.OpenAICompatLLM", RecordingLLM)
    startup = StartupLLM()
    ctx = ToolContext(llm=startup, config=ServerConfig(transport="streamable-http"))

    # 不带 → 服务端 LLM
    assert ctx.with_request(_FakeRequest({})).llm is startup

    # 带 → 请求级 LLM，且 Key 就是请求里的那个
    derived = ctx.with_request(_FakeRequest({"x-jobcopilot-api-key": "k-byok"}))
    assert derived.llm is not startup
    assert RecordingLLM.keys == ["k-byok"]

    # 同一个 Key 再取一次 → 命中缓存，不重复构造
    ctx.with_request(_FakeRequest({"x-jobcopilot-api-key": "k-byok"}))
    assert RecordingLLM.keys == ["k-byok"]


# ============================================================
# 三、端到端：真起 streamable HTTP 服务
# ============================================================


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class HttpServer:
    """在后台线程里跑一个真实的 MCP HTTP 服务（走生产路径 build_http_app）。

    刻意直接用生产的 ``build_http_app``：这样测试覆盖的就是真实部署时的那套
    routes + lifespan 组合（streamable HTTP + SSE 两条传输），而不是另搭一份。
    """

    def __init__(self, startup_llm: Any, *, token: str | None, base_path: str = "") -> None:
        self.port = _free_port()
        self.base_path = base_path
        self.cfg = ServerConfig(
            transport="streamable-http",
            host="127.0.0.1",
            port=self.port,
            http_token=token or "",
            allowed_hosts=["127.0.0.1"],
            http_base_path=base_path,
        )
        self.server = build_server(self.cfg, llm=startup_llm)
        self.server.settings.host = self.cfg.host
        self.server.settings.port = self.cfg.port
        apply_transport_security(self.server, self.cfg)
        app = build_http_app(self.server, self.cfg, token or "")
        uv = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error")
        self._uv = uvicorn.Server(uv)
        self._thread = threading.Thread(target=self._uv.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}{self.base_path}/mcp"

    @property
    def sse_url(self) -> str:
        return f"http://127.0.0.1:{self.port}{self.base_path}/sse"

    def __enter__(self) -> "HttpServer":
        self._thread.start()
        deadline = time.time() + 15
        while not self._uv.started:
            if time.time() > deadline:  # pragma: no cover
                raise RuntimeError("HTTP 服务启动超时")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc: Any) -> None:
        self._uv.should_exit = True
        self._thread.join(timeout=15)


def _mcp_call(url: str, headers: dict[str, str], tool: str, args: dict[str, Any]) -> Any:
    """用官方 MCP 客户端调一次工具，返回结构化结果。"""

    async def run() -> Any:
        async with streamablehttp_client(url, headers=headers) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await s.call_tool(tool, args)

    return asyncio.run(run())


def test_http_serves_tools_with_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jobcopilot.core.providers.OpenAICompatLLM", RecordingLLM)
    with HttpServer(StartupLLM(), token=TOKEN) as srv:
        async def list_tools() -> list[str]:
            async with streamablehttp_client(
                srv.url, headers={"Authorization": f"Bearer {TOKEN}"}
            ) as (r, w, _):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    res = await s.list_tools()
                    return [t.name for t in res.tools]

        names = asyncio.run(list_tools())
        assert "analyze_job" in names
        assert "analyze_jobs_batch" in names
        assert len(names) == 6


def test_http_rejects_missing_token() -> None:
    with HttpServer(StartupLLM(), token=TOKEN) as srv:
        r = httpx.post(
            srv.url,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"content-type": "application/json", "accept": "application/json, text/event-stream"},
            timeout=10,
        )
        assert r.status_code == 401
        assert "访问令牌" in r.json()["error"]


def test_http_rejects_wrong_token() -> None:
    with HttpServer(StartupLLM(), token=TOKEN) as srv:
        r = httpx.post(
            srv.url,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
                "authorization": "Bearer wrong-token",
            },
            timeout=10,
        )
        assert r.status_code == 401
        assert r.json()["error"] == "访问令牌不正确"


def test_http_rejects_unknown_host() -> None:
    """Host 不在白名单 → 421（SDK 的 DNS-rebinding 保护）。"""
    with HttpServer(StartupLLM(), token=TOKEN) as srv:
        r = httpx.post(
            srv.url,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
                "authorization": f"Bearer {TOKEN}",
                "host": "evil.example.com",
            },
            timeout=10,
        )
        assert r.status_code == 421


def test_http_byok_header_switches_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """端到端证明 BYOK 生效：带请求头调用时用的是**请求里的 Key**，而不是服务端的。"""
    monkeypatch.setattr("jobcopilot.core.providers.OpenAICompatLLM", RecordingLLM)
    startup = StartupLLM()
    with HttpServer(startup, token=TOKEN) as srv:
        res = _mcp_call(
            srv.url,
            {
                "Authorization": f"Bearer {TOKEN}",
                "X-JobCopilot-Api-Key": "k-from-caller",
                "X-JobCopilot-Provider": "deepseek",
            },
            "analyze_job",
            {"jd_text": "【AI 工程师】某公司 | 30-50K | 北京\n职责：做 Agent 平台。要求：熟悉 Python。"},
        )
        assert res is not None
    # 请求级 LLM 被构造，Key 就是调用方传的
    assert RecordingLLM.keys == ["k-from-caller"]
    # 服务端默认 LLM 一次都没被调用（分析走的是调用方的 Key）
    assert startup.calls == 0


def test_http_without_byok_header_uses_startup_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """不带 BYOK 头 → 回落服务端 LLM（对照组，确保上面的断言不是巧合）。"""
    monkeypatch.setattr("jobcopilot.core.providers.OpenAICompatLLM", RecordingLLM)
    startup = StartupLLM()
    with HttpServer(startup, token=TOKEN) as srv:
        _mcp_call(
            srv.url,
            {"Authorization": f"Bearer {TOKEN}"},
            "analyze_job",
            {"jd_text": "【AI 工程师】某公司 | 30-50K | 北京\n职责：做 Agent 平台。"},
        )
    assert RecordingLLM.keys == []
    assert startup.calls > 0


# ============================================================
# 四、平台硬约束（云端平台侧的限制，见 docs/integrations/）
# ============================================================


def test_tool_names_satisfy_platform_regex() -> None:
    """工具名必须匹配 ``^[a-zA-Z0-9_-]{1,64}$``。

    Dify 源码用它校验 operationId、多数 MCP 客户端也同限制；扣子未公开规则，
    但同一命名最安全。这条防止将来加工具时不小心用了中文名/点号/超长名。
    """
    import re

    pattern = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
    srv = build_server(ServerConfig(transport="stdio"), llm=StartupLLM())

    async def _names() -> list[str]:
        tools = await srv.list_tools()
        return [t.name for t in tools]

    names = asyncio.run(_names())
    assert names
    for n in names:
        assert pattern.match(n), f"工具名不符合平台正则: {n}"


def test_endpoint_get_is_not_5xx() -> None:
    """``/mcp`` 端点必须存在且不因 GET 而 5xx。

    一些平台的连通性探测会用 GET。实测本 SDK 对「无会话的 GET」返回
    ``400 {"message": "Missing session ID"}``（必须先 initialize 拿 Mcp-Session-Id），
    这是正确行为；这里断言的是「端点存在且不炸」——落到 404/5xx 就意味着部署错了。
    """
    with HttpServer(StartupLLM(), token=TOKEN) as srv:
        r = httpx.get(
            srv.url,
            headers={
                "authorization": f"Bearer {TOKEN}",
                "accept": "application/json, text/event-stream",
            },
            timeout=10,
        )
        assert r.status_code != 404, "端点不存在（URL 里必须带 /mcp）"
        assert r.status_code < 500, f"GET 不应 5xx，实际 {r.status_code}"


# ============================================================
# 五、SSE 传输与 query token 兜底（百度千帆只支持 SSE、且配置里没有 headers）
# ============================================================


def test_sse_transport_streams_endpoint_event() -> None:
    """``/sse`` 必须真的挂上了 SSE 传输。

    这里走 **HTTP 层**而不是官方 sse_client：sse_client 的 teardown 在测试里容易挂住
    （等流关闭），而「首个 ``event: endpoint`` 事件」已经足以证明 SSE 传输在正常工作
    —— 该事件由 SSE transport 自己发出，并告知客户端消息端点。

    为什么必须支持 SSE：**百度千帆的工作流 MCP 节点只支持 SSE**（不支持 Streamable
    HTTP），而火山 AgentKit 反过来只支持 Streamable HTTP，所以两条都得有。
    """
    with HttpServer(StartupLLM(), token=TOKEN) as srv:
        timeout = httpx.Timeout(5.0, read=5.0)
        with httpx.stream(
            "GET",
            srv.sse_url,
            headers={
                "authorization": f"Bearer {TOKEN}",
                "accept": "text/event-stream",
            },
            timeout=timeout,
        ) as r:
            assert r.status_code == 200
            assert "text/event-stream" in r.headers.get("content-type", "")
            buf = ""
            for line in r.iter_lines():
                buf += line + "\n"
                if "/sse/messages" in buf:
                    break
            assert "event: endpoint" in buf
            assert "/sse/messages" in buf


def test_sse_endpoint_requires_token() -> None:
    """SSE 端点同样受访问令牌保护（不能因为「千帆没 headers」就裸奔）。"""
    with HttpServer(StartupLLM(), token=TOKEN) as srv:
        r = httpx.get(
            srv.sse_url,
            headers={"accept": "text/event-stream"},
            timeout=10,
        )
        assert r.status_code == 401


def test_query_token_is_accepted_as_fallback() -> None:
    """``?token=`` 兜底：千帆的 MCP 配置 JSON 只有 url、没有 headers 字段。"""
    with HttpServer(StartupLLM(), token=TOKEN) as srv:
        r = httpx.post(
            f"{srv.url}?token={TOKEN}",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
            timeout=10,
        )
        # 没有 Mcp-Session-Id 时为 400，说明**已通过鉴权**进到协议层
        assert r.status_code == 400
        assert "session" in r.text.lower()


def test_query_token_wrong_is_rejected() -> None:
    with HttpServer(StartupLLM(), token=TOKEN) as srv:
        r = httpx.post(
            f"{srv.url}?token=wrong",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
            timeout=10,
        )
        assert r.status_code == 401


def test_authorization_header_wins_over_query_token() -> None:
    """显式头优先于查询串：两者都在且头是对的 → 应通过。"""
    with HttpServer(StartupLLM(), token=TOKEN) as srv:
        r = httpx.post(
            f"{srv.url}?token=stale-token",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
                "authorization": f"Bearer {TOKEN}",
            },
            timeout=10,
        )
        assert r.status_code == 400


# ============================================================
# 六、子路径部署（反代把服务挂在 /jobcopilot 下）
# ============================================================


def test_base_path_streamable_endpoint() -> None:
    """带路径前缀时，streamable 端点落在 ``<前缀>/mcp``。"""
    with HttpServer(StartupLLM(), token=TOKEN, base_path="/jobcopilot") as srv:
        assert srv.url.endswith("/jobcopilot/mcp")
        # 无会话的 POST → 400 说明鉴权已过、进到协议层
        r = httpx.post(
            srv.url,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
                "authorization": f"Bearer {TOKEN}",
            },
            timeout=10,
        )
        assert r.status_code == 400
        # 未带前缀的老路径不应存在（避免「配了前缀但实际没生效」的假象）
        r2 = httpx.post(
            f"http://127.0.0.1:{srv.port}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
                "authorization": f"Bearer {TOKEN}",
            },
            timeout=10,
        )
        assert r2.status_code == 404


def test_base_path_sse_message_endpoint_keeps_prefix() -> None:
    """**这个特性存在的理由**：SSE 把消息端点作为绝对路径告诉客户端，
    必须带上路径前缀，否则客户端会去请求 ``/sse/messages/``（丢掉 ``/jobcopilot``）
    而失败。百度千帆只支持 SSE，所以这条不能错。
    """
    with HttpServer(StartupLLM(), token=TOKEN, base_path="/jobcopilot") as srv:
        timeout = httpx.Timeout(5.0, read=5.0)
        with httpx.stream(
            "GET",
            srv.sse_url,
            headers={
                "authorization": f"Bearer {TOKEN}",
                "accept": "text/event-stream",
            },
            timeout=timeout,
        ) as r:
            assert r.status_code == 200
            buf = ""
            for line in r.iter_lines():
                buf += line + "\n"
                if "/jobcopilot/sse/messages" in buf:
                    break
            assert "event: endpoint" in buf
            assert "/jobcopilot/sse/messages" in buf
