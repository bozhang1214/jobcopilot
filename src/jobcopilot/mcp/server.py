"""MCP Server：把内核能力暴露成 MCP 工具（stdio / streamable-http 双形态）。

分工：本模块只做**协议接线**——参数声明、错误包装、传输选择；
真正的逻辑在 :mod:`jobcopilot.mcp.tools`（可脱离 MCP 单测）。

两种传输：

- ``jobcopilot-mcp``            → stdio（本地客户端：DSH / Claude Desktop / Cursor）
- ``jobcopilot-mcp --http``     → streamable HTTP（云端平台：扣子 / 百炼 / 千帆 / HiAgent / Dify）

> 云端平台跑在别人机器上，**必须 BYOK**：Key 通过 HTTP header / 环境变量传入，
> 服务器不保存、不留存。``source_path`` 在 HTTP 形态下默认禁用（任意文件读取风险）。

HTTP 形态的调用方带自己的 Key（BYOK）：::

    X-JobCopilot-Api-Key: <你的 Key>
    X-JobCopilot-Provider: deepseek     # 可选，覆盖预设
    X-JobCopilot-Model: deepseek-chat   # 可选，覆盖模型
    Authorization: Bearer <访问令牌>     # 仅当服务端设了 JOBCOPILOT_HTTP_TOKEN

读取请求头依赖 lowlevel 的 ``request_ctx``：MCP 的 streamable HTTP 传输在处理每个
消息时把 ``RequestContext(request=<Starlette Request>)`` set 进该 contextvar，而工具
处理器在**同一个任务**里执行——因此能直接读到（已由 tests/test_mcp_http.py 端到端
用例证实）。若哪天真读不到了，:func:`build_server` 里的 ``guard`` 会打 **error 日志**
而不是安静地改用服务端的 Key。
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any
from urllib.parse import parse_qs

from jobcopilot import __version__
from jobcopilot.core.logging import get_logger
from jobcopilot.core.messages import LLMPort
from jobcopilot.mcp.config import ServerConfig
from jobcopilot.mcp.request_keys import current_request
from jobcopilot.mcp.tools import (
    ToolContext,
    ToolError,
)
from jobcopilot.mcp.tools import (
    analyze_job as _analyze_job,
)
from jobcopilot.mcp.tools import (
    analyze_jobs_batch as _analyze_jobs_batch,
)
from jobcopilot.mcp.tools import (
    get_profile as _get_profile,
)
from jobcopilot.mcp.tools import (
    list_prompt_packs as _list_prompt_packs,
)
from jobcopilot.mcp.tools import (
    save_profile as _save_profile,
)
from jobcopilot.mcp.tools import (
    sync_prompts as _sync_prompts,
)

logger = get_logger(__name__)

SERVER_NAME = "jobcopilot"

#: Streamable HTTP 端点路径（扣子 / 百炼 / Dify / 火山 AgentKit 用）
STREAMABLE_PATH = "/mcp"
#: SSE 挂载前缀（**百度千帆的 MCP 节点只支持 SSE**）；消息端点会落在 <该前缀>/messages/
SSE_MOUNT_PATH = "/sse"

INSTRUCTIONS = """JobCopilot —— 求职分析内核。

典型用法：
1. 先 save_profile 写入求职者画像（技能/目标岗位/城市），后续分析会自动带上；
2. 单份 JD 用 analyze_job；一批职位用 analyze_jobs_batch；
3. 若客户端能访问本地文件，优先用 source_path 传职位数据（避免把长文本当参数烧 token）；
4. 不知道有哪些职能提示词包时先调 list_prompt_packs。

分析结果里的 prompt_meta 记录本次使用的提示词版本，便于复现。
"""


class MissingKeyLLM:
    """未配置 Key 时的占位 LLM：**照常启动**，调用时才报清晰错误。

    为什么不在启动时直接退出：MCP 客户端（DSH / Claude Desktop）在服务端启动
    失败时**只会显示"没有工具"**，用户完全看不出是缺 Key——而这时连
    ``list_prompt_packs`` / ``get_profile`` 这类**不需要 LLM** 的工具也用不了。
    改成推迟到调用时报错，既保住了非 LLM 工具，又能在模型/用户面前直接给出
    「设哪个环境变量」的可操作提示。
    """

    #: 标记：让工具层能**提前**识别出「占位 LLM」并直接报配置错误。
    #  否则分析器的逐步降级会把配置错误吞掉，用户只看到「7 段全空」。
    is_placeholder = True

    def __init__(self, config: ServerConfig, reason: str) -> None:
        self._config = config
        self._reason = reason

    @property
    def reason(self) -> str:
        """缺少 Key 的具体原因（用于拼装可操作的错误文案）。"""
        return self._reason

    async def complete(self, role: str, messages: Any) -> Any:
        """任何 LLM 调用都抛出带配置示例的清晰错误。"""
        raise RuntimeError(
            f"{self._reason}。请在 MCP 客户端的环境变量里配置，例如："
            f'{{"JOBCOPILOT_LLM_PROVIDER": "{self._config.provider}", '
            '"JOBCOPILOT_LLM_API_KEY": "<your-key>"}}'
        )


def make_llm(config: ServerConfig) -> LLMPort:
    """按 BYOK 配置构造 LLM 客户端。

    缺少 Key 时**不退出**，而是返回 :class:`MissingKeyLLM` 并在 stderr 打印醒目提示：
    这样 MCP 客户端仍能列出并调用不需 LLM 的工具，需要 LLM 的工具则给出可操作错误。
    """
    from jobcopilot.core.providers import MissingAPIKeyError, OpenAICompatLLM

    try:
        return OpenAICompatLLM(
            api_key=config.api_key or None,
            model=config.model or None,
            preset=config.provider,
        )
    except MissingAPIKeyError as e:
        print(f"⚠️  未配置 LLM API Key（{e}）", file=sys.stderr)
        print(
            "   服务仍会启动：不需 LLM 的工具可用；需要 LLM 的工具会返回可操作的错误。\n"
            "   配置示例（MCP 客户端 env 段）：\n"
            f'     JOBCOPILOT_LLM_PROVIDER: {config.provider}\n'
            "     JOBCOPILOT_LLM_API_KEY: <your-key>",
            file=sys.stderr,
        )
        return MissingKeyLLM(config, str(e))


def build_server(config: ServerConfig | None = None, llm: LLMPort | None = None) -> Any:
    """构造并返回 FastMCP server 实例。

    Args:
        config: Server 配置；``None`` 时从环境变量读。
        llm: 注入 LLM（测试用）；``None`` 时按配置构造。

    Returns:
        已注册全部工具的 ``FastMCP`` 实例。
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:  # pragma: no cover - 依赖缺失时的友好提示
        raise SystemExit(
            "未安装 MCP 依赖：pip install 'jobcopilot[mcp]'"
        ) from e

    cfg = config or ServerConfig.from_env()
    ctx = ToolContext(llm=llm or make_llm(cfg), config=cfg)

    mcp = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS)

    async def guard(fn: Any, **kwargs: Any) -> dict[str, Any]:
        """统一错误包装 + 按请求解析 BYOK。

        ``ctx`` 在启动时只建一次（持有服务端默认 Key）。这里按**本次请求**派生一份
        上下文：调用方在请求头里带了自己的 Key 就用它，否则回落服务端配置。
        这样 :mod:`jobcopilot.mcp.tools` 的实现仍然只读 ``ctx.llm``。
        """
        req = current_request()
        if cfg.is_http and req is None:
            # ⚠️ 不静默：读不到请求 = BYOK 头会被忽略、改用服务端 Key（要花钱的）
            logger.error(
                "HTTP 形态下取不到原始请求，BYOK 请求头本轮会被忽略，"
                "将使用服务端配置的 Key。若 MCP SDK 升级后出现此日志，"
                "说明工具不再与请求同任务执行，需改用 FastMCP 的 Context 注入。"
            )
        call_ctx = ctx.with_request(req)
        try:
            out: dict[str, Any] = await fn(call_ctx, **kwargs)
            return out
        except ToolError as e:
            logger.warning(f"工具参数问题: {e}")
            return {"error": str(e), "hint": "请修正参数后重试"}
        except Exception as e:  # noqa: BLE001
            logger.error(f"工具执行失败: {str(e)[:200]}")
            return {"error": f"内部错误: {str(e)[:200]}"}

    @mcp.tool()
    async def analyze_job(
        jd_text: str | None = None,
        source_path: str | None = None,
        job_type: str | None = None,
        prompt_pack: str | None = None,
        prompt_override: str | None = None,
        user_profile: str | None = None,
    ) -> dict[str, Any]:
        """单职位深度分析（7 段：深度解析/知识优先级/面试问答/差距/简历/项目/策略）。

        Args:
            jd_text: JD 正文。与 source_path 二选一。
            source_path: 服务端本地 JD 文件路径（本地模式推荐，避免长文本当参数）。
            job_type: 职位类型（presales/product/engineering 之一），等价于 prompt_pack。
            prompt_pack: 职能提示词包名。
            prompt_override: 直接覆盖"批量职位分析"提示词的整段文本（高级用法）。
            user_profile: 按请求注入的求职者画像（多用户宿主用；不传则用已保存的）。
        """
        return await guard(
            _analyze_job,
            jd_text=jd_text,
            source_path=source_path,
            job_type=job_type,
            prompt_pack=prompt_pack,
            prompt_override=prompt_override,
            user_profile=user_profile,
        )

    @mcp.tool()
    async def analyze_jobs_batch(
        jobs: list[dict[str, Any]] | str | None = None,
        source_path: str | None = None,
        keyword: str | None = None,
        city: str | None = None,
        prompt_pack: str | None = None,
        prompt_override: str | None = None,
        user_profile: str | None = None,
    ) -> dict[str, Any]:
        """批量职位市场分析（赛道热力/技能门槛/薪资锚点 + 知识迭代建议）。

        Args:
            jobs: 职位列表（每项含 title/company/salary/city/jd_text）。
                也接受 JSON 字符串——Dify / 扣子的 OpenAPI 路线无法声明嵌套对象数组，
                只能把结构化数据当字符串传。
            source_path: 服务端本地文件（推荐用于几十上百个职位，避免烧 token）。
            keyword: 本次分析的关键词（写进报告）。
            city: 城市（写进报告）。
            prompt_pack: 职能提示词包名。
            prompt_override: 覆盖"批量职位分析"提示词的整段文本。
            user_profile: 按请求注入的求职者画像（多用户宿主用；不传则用已保存的）。
        """
        return await guard(
            _analyze_jobs_batch,
            jobs=jobs,
            source_path=source_path,
            keyword=keyword,
            city=city,
            prompt_pack=prompt_pack,
            prompt_override=prompt_override,
            user_profile=user_profile,
        )

    @mcp.tool()
    async def get_profile() -> dict[str, Any]:
        """读取已保存的求职者画像（空表示尚未设置）。"""
        try:
            return _get_profile(ctx)
        except ToolError as e:
            return {"error": str(e)}

    @mcp.tool()
    async def save_profile(profile: str) -> dict[str, Any]:
        """保存求职者画像（文本，格式自由）。之后的分析会自动带上它。

        Args:
            profile: 画像文本，建议包含目标岗位、城市、技能强项与短板。
        """
        try:
            return _save_profile(ctx, profile)
        except ToolError as e:
            return {"error": str(e)}

    @mcp.tool()
    async def list_prompt_packs() -> dict[str, Any]:
        """列出可用的职能提示词包（售前/产品/研发）与本地提示词目录状态。"""
        try:
            return _list_prompt_packs(ctx)
        except ToolError as e:
            return {"error": str(e)}

    @mcp.tool()
    async def sync_prompts(pack: str | None = None) -> dict[str, Any]:
        """把提示词同步到本地目录（之后可直接编辑，改完立即生效）。

        Args:
            pack: 职能包名；不传则只同步通用 base 提示词。
        """
        try:
            return _sync_prompts(ctx, pack=pack)
        except ToolError as e:
            return {"error": str(e)}

    return mcp


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    p = argparse.ArgumentParser(
        prog="jobcopilot-mcp", description="JobCopilot MCP Server（stdio / HTTP）"
    )
    p.add_argument("--version", action="version", version=f"jobcopilot-mcp {__version__}")
    p.add_argument("--http", action="store_true", help="用 streamable HTTP 传输（默认 stdio）")
    p.add_argument(
        "--host",
        default=None,
        help="HTTP 监听地址（默认 127.0.0.1）。非回环地址必须设 JOBCOPILOT_HTTP_TOKEN "
        "并配 JOBCOPILOT_HTTP_ALLOWED_HOSTS，否则拒绝启动",
    )
    p.add_argument("--port", type=int, default=None, help="HTTP 端口（默认 8765）")
    p.add_argument("--provider", default=None, help="LLM 预设（默认取环境变量）")
    p.add_argument("--model", default=None, help="覆盖模型名")
    p.add_argument(
        "--allow-source-path",
        action="store_true",
        help="HTTP 形态下显式允许 source_path 读本地文件（默认禁用）",
    )
    p.add_argument(
        "--allow-public-bind",
        action="store_true",
        help="显式放弃「非回环绑定必须带访问令牌」的保护（仅限可信内网）",
    )
    return p


def _bearer_token(scope: Any) -> str | None:
    """从 ASGI scope 里取 ``Authorization: Bearer <token>``（取不到返回 None）。"""
    for key, value in scope.get("headers") or []:
        if key.lower() == b"authorization":
            raw: str = value.decode("latin-1").strip()
            prefix = "bearer "
            if raw.lower().startswith(prefix):
                return raw[len(prefix) :].strip()
            return None
    return None


def _query_token(scope: Any) -> str | None:
    """从 ``?token=`` / ``?access_token=`` 取令牌（兜底方案）。

    **为什么需要**：百度千帆的 MCP 配置 JSON **只有 url 字段、没有 headers**，
    调用方无法传自定义头，只能把令牌放进查询串（其节点还只支持 SSE）。

    ⚠️ 查询串**可能进反向代理的访问日志**，所以这属于兜底：能用
    ``Authorization: Bearer`` 时优先用它。文档里也是这个优先级。
    """
    # 显式标注打断 Any 链：scope 是 ASGI 的 Any，不标注会让 parse_qs 的结果也变 Any
    raw_qs: str = (scope.get("query_string") or b"").decode("latin-1")
    try:
        params: dict[str, list[str]] = parse_qs(raw_qs)
    except (UnicodeDecodeError, ValueError):  # pragma: no cover - 畸形查询串
        return None
    for name in ("token", "access_token"):
        values: list[str] | None = params.get(name)
        if values and values[0].strip():
            found: str = values[0].strip()
            return found
    return None


async def _send_json(send: Any, status: int, payload: dict[str, Any]) -> None:
    """最简 ASGI JSON 响应（避免为一处错误响应引入额外依赖）。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BearerAuthMiddleware:
    """访问令牌校验（纯 ASGI 中间件）。

    刻意不用 Starlette 的 ``BaseHTTPMiddleware``：它会把下游放进**另一个任务**、
    并给流式响应套一层缓冲，而 streamable HTTP 与 SSE 恰恰都是流式端点。

    令牌来源优先级：``Authorization: Bearer`` > 查询串 ``?token=``。
    """

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        provided = _bearer_token(scope) or _query_token(scope)
        if provided is None:
            await _send_json(
                send,
                401,
                {
                    "error": "缺少访问令牌",
                    "hint": "请求需带 Authorization: Bearer <JOBCOPILOT_HTTP_TOKEN>"
                    "（不支持自定义头的客户端可用 ?token=<令牌> 兜底）",
                },
            )
            return
        if not secrets.compare_digest(provided, self.token):
            await _send_json(send, 401, {"error": "访问令牌不正确"})
            return
        await self.app(scope, receive, send)


def apply_transport_security(server: Any, config: ServerConfig) -> None:
    """把配置里的 Host/Origin 白名单写进 SDK 的 DNS-rebinding 保护。

    ⚠️ 不配就会沿用 SDK 默认（只放行回环地址），云端平台拿到的会是
    ``421 Invalid Host header``。
    """
    from mcp.server.transport_security import TransportSecuritySettings

    ts = config.transport_security()
    server.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=ts.enable,
        allowed_hosts=ts.allowed_hosts,
        allowed_origins=ts.allowed_origins,
    )


def build_http_app(server: Any, config: ServerConfig, token: str = "") -> Any:
    """构造**同时**提供 streamable HTTP 与 SSE 的 ASGI 应用。

    两条传输都要的原因（实测过的平台差异）：

    - **Streamable HTTP**（``POST /mcp``）：扣子、阿里百炼（其 FAQ 明确
      ``streamableHttp`` 必须对应 ``POST /mcp``）、Dify、火山 AgentKit；
    - **SSE**（``GET /sse`` + ``POST /sse/messages/``）：**百度千帆的 MCP 节点只支持
      SSE**，不支持 Streamable HTTP。

    做法：把两个子应用的 routes 合并进一个 Starlette，并用 ``AsyncExitStack``
    组合两者的 lifespan——两个 session manager 都必须进入 lifespan，否则传输不工作
    （只合并 routes 会得到一个「连得上但没有会话」的服务，很难排查）。

    **路径前缀**（``JOBCOPILOT_HTTP_BASE_PATH``）：反向代理把服务挂在子路径下时
    （如 ``https://host/jobcopilot/mcp``）**必须**设置它。原因：SSE 传输会把
    **消息端点**以绝对路径告诉客户端（``/sse/messages/?session_id=...``），
    客户端按绝对路径请求就会丢掉前缀、打到错误地址。让内核自己知道前缀，
    才能把消息端点写成 ``/jobcopilot/sse/messages/``。
    """
    from starlette.applications import Starlette

    base = config.http_base_path
    streamable_path = f"{base}{STREAMABLE_PATH}"
    sse_path = f"{base}{SSE_MOUNT_PATH}"
    # 让 SDK 用带前缀的路径注册路由（streamable 的单端点 + SSE 的流端点）
    server.settings.streamable_http_path = streamable_path
    server.settings.sse_path = sse_path

    http_app = server.streamable_http_app()
    # mount_path 决定 SSE 把「消息端点」写成什么路径：base/sse + /messages/
    sse_app = server.sse_app(mount_path=sse_path)

    @asynccontextmanager
    async def lifespan(app: Any) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(sse_app.router.lifespan_context(sse_app))
            await stack.enter_async_context(http_app.router.lifespan_context(http_app))
            yield

    app: Any = Starlette(
        routes=list(sse_app.routes) + list(http_app.routes),
        lifespan=lifespan,
    )
    if token:
        app = BearerAuthMiddleware(app, token)
    return app


def run_http(server: Any, config: ServerConfig) -> None:
    """以 streamable HTTP + SSE 方式运行（必要时套上访问令牌校验）。"""
    import uvicorn

    server.settings.host = config.host
    server.settings.port = config.port
    apply_transport_security(server, config)
    app = build_http_app(server, config, config.http_token)

    base = f"http://{config.host}:{config.port}{config.http_base_path}"
    print(
        "JobCopilot MCP Server"
        f"  streamable-http={base}{STREAMABLE_PATH}"
        f"  sse={base}{SSE_MOUNT_PATH}"
        f"  base_path={config.http_base_path or '(无)'}"
        f"  provider={config.provider}"
        f"  source_path={'允许' if config.allow_source_path else '禁用'}"
        f"  访问令牌={'已启用' if config.http_token else '未启用'}"
        f"  Host 白名单={config.allowed_hosts or '(仅回环)'}",
        file=sys.stderr,
    )
    if config.allow_public_bind and not config.is_loopback_bind:
        print(
            "⚠️  已按 JOBCOPILOT_ALLOW_PUBLIC_BIND 跳过令牌校验，任何能访问该端口的人"
            "都能用掉本机配置的 LLM Key。请确认处于可信内网。",
            file=sys.stderr,
        )
    uvicorn.run(app, host=config.host, port=config.port, log_level="info")


def main(argv: list[str] | None = None) -> int:
    """MCP Server 入口。

    Returns:
        进程退出码（2 = HTTP 绑定安全校验未通过）。
    """
    import os

    args = build_parser().parse_args(argv)
    transport = "streamable-http" if args.http else "stdio"

    if args.provider:
        os.environ["JOBCOPILOT_LLM_PROVIDER"] = args.provider
    if args.model:
        os.environ["JOBCOPILOT_LLM_MODEL"] = args.model
    if args.allow_source_path:
        os.environ["JOBCOPILOT_ALLOW_SOURCE_PATH"] = "1"
    if args.allow_public_bind:
        os.environ["JOBCOPILOT_ALLOW_PUBLIC_BIND"] = "1"

    config = ServerConfig.from_env(transport=transport)
    if args.host:
        config.host = args.host
    if args.port:
        config.port = args.port

    # 安全闸：对外绑定必须先过校验（无鉴权的 HTTP 端点 = 公开你的 LLM Key）
    bind_error = config.http_bind_error()
    if bind_error:
        print(f"❌ {bind_error}", file=sys.stderr)
        return 2

    server = build_server(config)
    if config.is_http:
        run_http(server, config)
    else:
        print(
            f"JobCopilot MCP Server（stdio）provider={config.provider}",
            file=sys.stderr,
        )
        server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["build_server", "main", "make_llm"]
