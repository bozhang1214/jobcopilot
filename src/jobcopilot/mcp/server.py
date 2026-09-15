"""MCP Server：把内核能力暴露成 MCP 工具（stdio / streamable-http 双形态）。

分工：本模块只做**协议接线**——参数声明、错误包装、传输选择；
真正的逻辑在 :mod:`jobcopilot.mcp.tools`（可脱离 MCP 单测）。

两种传输：

- ``jobcopilot-mcp``            → stdio（本地客户端：DSH / Claude Desktop / Cursor）
- ``jobcopilot-mcp --http``     → streamable HTTP（云端平台：扣子 / 百炼 / 千帆 / HiAgent / Dify）

> 云端平台跑在别人机器上，**必须 BYOK**：Key 通过 HTTP header / 环境变量传入，
> 服务器不保存、不留存。``source_path`` 在 HTTP 形态下默认禁用（任意文件读取风险）。
"""
from __future__ import annotations

import argparse
import sys
from typing import Any

from jobcopilot import __version__
from jobcopilot.core.logging import get_logger
from jobcopilot.core.messages import LLMPort
from jobcopilot.mcp.config import ServerConfig
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

INSTRUCTIONS = """JobCopilot —— 求职分析内核。

典型用法：
1. 先 save_profile 写入求职者画像（技能/目标岗位/城市），后续分析会自动带上；
2. 单份 JD 用 analyze_job；一批职位用 analyze_jobs_batch；
3. 若客户端能访问本地文件，优先用 source_path 传职位数据（避免把长文本当参数烧 token）；
4. 不知道有哪些职能提示词包时先调 list_prompt_packs。

分析结果里的 prompt_meta 记录本次使用的提示词版本，便于复现。
"""


def make_llm(config: ServerConfig) -> LLMPort:
    """按 BYOK 配置构造 LLM 客户端。

    Raises:
        SystemExit: 缺少 API Key（**尽早失败并给出可操作的提示**，
            而不是等第一次工具调用才报错）。
    """
    from jobcopilot.core.providers import MissingAPIKeyError, OpenAICompatLLM

    try:
        return OpenAICompatLLM(
            api_key=config.api_key or None,
            model=config.model or None,
            preset=config.provider,
        )
    except MissingAPIKeyError as e:
        print(f"❌ 缺少 API Key：{e}", file=sys.stderr)
        print(
            "   MCP 客户端里请设置环境变量，例如：\n"
            f'     "env": {{"JOBCOPILOT_LLM_PROVIDER": "{config.provider}", '
            '"JOBCOPILOT_LLM_API_KEY": "<your-key>"}}',
            file=sys.stderr,
        )
        raise SystemExit(2) from e


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

    async def guard(fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """统一错误包装：可预期错误返回结构化提示，便于模型自我修正。"""
        try:
            out: dict[str, Any] = await fn(ctx, *args, **kwargs)
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
        jobs: list[dict[str, Any]] | None = None,
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
    p.add_argument("--host", default=None, help="HTTP 监听地址（默认 127.0.0.1）")
    p.add_argument("--port", type=int, default=None, help="HTTP 端口（默认 8765）")
    p.add_argument("--provider", default=None, help="LLM 预设（默认取环境变量）")
    p.add_argument("--model", default=None, help="覆盖模型名")
    p.add_argument(
        "--allow-source-path",
        action="store_true",
        help="HTTP 形态下显式允许 source_path 读本地文件（默认禁用）",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    """MCP Server 入口。

    Returns:
        进程退出码。
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

    config = ServerConfig.from_env(transport=transport)
    if args.host:
        config.host = args.host
    if args.port:
        config.port = args.port

    server = build_server(config)
    if config.is_http:
        # FastMCP 从 settings 读 host/port
        server.settings.host = config.host
        server.settings.port = config.port
        print(
            f"JobCopilot MCP Server（streamable-http）"
            f" http://{config.host}:{config.port}/mcp"
            f"  provider={config.provider}  source_path={'允许' if config.allow_source_path else '禁用'}",
            file=sys.stderr,
        )
    else:
        print(
            f"JobCopilot MCP Server（stdio）provider={config.provider}",
            file=sys.stderr,
        )
    server.run(transport=transport)
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["build_server", "main", "make_llm"]
