"""MCP Server 的**协议级**集成测试。

用官方 MCP 客户端（``mcp.client.stdio``）真实拉起 ``jobcopilot-mcp`` 子进程，
走完整 stdio 握手：initialize → list_tools → call_tool。

这比「直接调函数」强得多：它验证的是**客户端真正看到的东西**
（工具名、描述、schema、错误形态），也正是 DoD 2 要求的证据。

无需真实 API Key：LLM 只在被调用时才发请求；本文件里的 LLM 类工具用假 Key
验证「失败时返回清晰错误而不是崩溃」。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

pytest.importorskip("mcp", reason="未安装 mcp 依赖")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


def payload_of(result) -> dict:
    """从工具返回里取出字典。

    FastMCP 会按返回标注决定形态：声明了 dict 返回时给 ``structuredContent``；
    否则把 JSON 塞进 ``content[0].text``。两种都兼容，测试才不会被 SDK 细节绊倒。
    """
    if getattr(result, "structuredContent", None):
        return dict(result.structuredContent)
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                continue
    return {}


EXPECTED_TOOLS = {
    "analyze_job",
    "analyze_jobs_batch",
    "get_profile",
    "save_profile",
    "list_prompt_packs",
    "sync_prompts",
    # 自检：固定小响应，用于界定「响应为空」是链路问题还是体积问题
    "self_check",
}


def _params(tmp_path) -> StdioServerParameters:
    """构造子进程参数：假 Key + 隔离的数据目录。"""
    env = {
        **os.environ,
        "JOBCOPILOT_LLM_PROVIDER": "deepseek",
        "JOBCOPILOT_LLM_API_KEY": "sk-fake-for-protocol-test",
        "JOBCOPILOT_DATA_DIR": str(tmp_path),
        "JOBCOPILOT_PROMPTS_DIR": str(tmp_path / "prompts"),
    }
    env.pop("PYTHONPATH", None)
    return StdioServerParameters(
        command=sys.executable, args=["-m", "jobcopilot.mcp.server"], env=env
    )


@pytest.mark.asyncio
async def test_server_lists_all_tools(tmp_path) -> None:
    """客户端能列出全部 6 个工具，且带描述与入参 schema。"""
    async with stdio_client(_params(tmp_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            resp = await session.list_tools()

    names = {t.name for t in resp.tools}
    assert names == EXPECTED_TOOLS, f"工具集不符: {names}"
    for tool in resp.tools:
        assert tool.description, f"{tool.name} 缺描述（客户端与模型看不到用途）"
    batch = next(t for t in resp.tools if t.name == "analyze_jobs_batch")
    props = batch.inputSchema.get("properties", {})
    assert {"jobs", "source_path", "keyword", "city", "prompt_pack"} <= set(props)


@pytest.mark.asyncio
async def test_server_info(tmp_path) -> None:
    """serverInfo 正确暴露名称（DSH / Inspector 用它显示服务）。"""
    async with stdio_client(_params(tmp_path)) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
    assert init.serverInfo.name == "jobcopilot"
    assert init.instructions and "JobCopilot" in init.instructions


@pytest.mark.asyncio
async def test_call_tool_without_llm(tmp_path) -> None:
    """不依赖 LLM 的工具必须能真实调用成功。"""
    async with stdio_client(_params(tmp_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            packs = await session.call_tool("list_prompt_packs", {})
            assert not packs.isError
            names = {p["name"] for p in payload_of(packs)["packs"]}
            assert {"presales", "product", "engineering"} <= names

            saved = await session.call_tool("save_profile", {"profile": "name: MCP 测试"})
            assert not saved.isError

            got = await session.call_tool("get_profile", {})
            assert not got.isError
            assert "MCP 测试" in payload_of(got)["profile"]


@pytest.mark.asyncio
async def test_llm_tool_failure_returns_clear_error(tmp_path) -> None:
    """LLM 不可用时，工具必须返回**可读的错误内容**而不是让连接崩掉。

    这正是 P4 DoD 3 要的「清晰错误，非静默失败」——在 MCP 层就验证掉。
    """
    async with stdio_client(_params(tmp_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool("analyze_job", {"jd_text": "招聘 Agent 工程师"})

    # 连接仍然可用（没崩），且结果里带错误说明
    payload = payload_of(res)
    assert payload.get("error"), f"应返回结构化错误，实际: {payload}"


@pytest.mark.asyncio
async def test_tool_argument_error_is_actionable(tmp_path) -> None:
    """参数错误（既没给 jd_text 也没给 source_path）要给出可操作的提示。"""
    async with stdio_client(_params(tmp_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool("analyze_job", {})

    payload = payload_of(res)
    assert "jd_text" in payload.get("error", "") and "source_path" in payload.get("error", "")
    assert payload.get("hint")


# ---------- HTTP（streamable-http）形态 ----------


def test_http_transport_disables_source_path_by_default() -> None:
    """HTTP 面向「别人的服务器 + 多用户」，默认必须禁用 source_path。

    这是安全默认值，不是可选项——开放任意路径读取等于暴露宿主机文件系统。
    """
    from jobcopilot.mcp.config import ServerConfig

    cfg = ServerConfig(transport="streamable-http")
    assert cfg.allow_source_path is False
    assert cfg.is_http is True


def test_http_transport_can_be_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """确需启用时，显式设 JOBCOPILOT_ALLOW_SOURCE_PATH=1 即可。"""
    from jobcopilot.mcp.config import ServerConfig

    monkeypatch.setenv("JOBCOPILOT_ALLOW_SOURCE_PATH", "1")
    cfg = ServerConfig(transport="streamable-http")
    assert cfg.allow_source_path is True


@pytest.mark.asyncio
async def test_http_transport_end_to_end(tmp_path) -> None:
    """真实起一个 HTTP server，用官方 HTTP 客户端握手 + 列工具 + 调工具。

    云端平台（扣子/百炼/千帆/HiAgent/Dify）走的就是这条路径，
    所以它必须被测试覆盖，而不是只靠手工 curl 过一遍。
    """
    import socket
    import subprocess
    import time

    from mcp.client.streamable_http import streamablehttp_client

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    env = {
        **os.environ,
        "JOBCOPILOT_LLM_PROVIDER": "deepseek",
        "JOBCOPILOT_LLM_API_KEY": "sk-fake-for-protocol-test",
        "JOBCOPILOT_DATA_DIR": str(tmp_path),
        "JOBCOPILOT_PROMPTS_DIR": str(tmp_path / "prompts"),
    }
    env.pop("PYTHONPATH", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "jobcopilot.mcp.server", "--http", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        url = f"http://127.0.0.1:{port}/mcp"
        last_err: Exception | None = None
        for _ in range(30):  # 等端口就绪
            try:
                async with streamablehttp_client(url) as (r, w, _):
                    async with ClientSession(r, w) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        assert {t.name for t in tools.tools} == EXPECTED_TOOLS
                        res = await session.call_tool("list_prompt_packs", {})
                        assert payload_of(res)["packs"]
                        blocked = await session.call_tool(
                            "analyze_jobs_batch", {"source_path": "/etc/passwd"}
                        )
                        assert "禁用 source_path" in payload_of(blocked).get("error", "")
                return
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(0.5)
        raise AssertionError(f"HTTP 形态未就绪: {last_err}")
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.mark.asyncio
async def test_server_starts_without_api_key(tmp_path) -> None:
    """没配 Key 时服务必须**照常启动**并列出工具。

    若启动即退出，MCP 客户端只会显示「没有工具」，用户看不出是缺 Key；
    而且连 list_prompt_packs / get_profile 这类不需要 LLM 的工具也用不了。
    """
    env = {
        **os.environ,
        "JOBCOPILOT_DATA_DIR": str(tmp_path),
        "JOBCOPILOT_PROMPTS_DIR": str(tmp_path / "prompts"),
    }
    env.pop("JOBCOPILOT_LLM_API_KEY", None)
    env.pop("DEEPSEEK_API_KEY", None)
    env.pop("PYTHONPATH", None)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "jobcopilot.mcp.server"], env=env
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == EXPECTED_TOOLS

            # 不需要 LLM 的工具必须可用
            packs = await session.call_tool("list_prompt_packs", {})
            assert not packs.isError and payload_of(packs)["packs"]

            # 需要 LLM 的工具给出**可操作**的错误（而不是让连接崩掉）
            res = await session.call_tool("analyze_job", {"jd_text": "JD"})
            err = payload_of(res).get("error", "")
            assert "JOBCOPILOT_LLM_API_KEY" in err, err
