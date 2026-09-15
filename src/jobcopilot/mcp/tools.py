"""MCP 工具的**纯实现**（不依赖 MCP SDK，可独立单测）。

把工具逻辑与协议层分开的好处：
- 单测不需要起 MCP Server、不需要客户端；
- 将来换协议实现（或同时暴露 HTTP API）时逻辑不用动；
- ``source_path`` 的文件读取与路径安全检查集中在这里，只有一处需要审。

工具清单（与实施计划 P2 一致）::

    analyze_job / analyze_jobs_batch / get_profile / save_profile
    list_prompt_packs / sync_prompts
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jobcopilot.core.analyzers.batch import analyze_jobs_batch as core_analyze_batch
from jobcopilot.core.analyzers.single import STEP_PROMPT_FILES, SingleJobAnalyzer
from jobcopilot.core.logging import get_logger
from jobcopilot.core.messages import LLMPort
from jobcopilot.core.prompts.resolver import PromptResolver, available_packs, packs_dir
from jobcopilot.mcp.config import ServerConfig

logger = get_logger(__name__)

#: 单文件 JD 读取上限（防止误传巨型文件把 token 打爆）
MAX_SOURCE_BYTES = 2 * 1024 * 1024


class ToolError(Exception):
    """工具层的可预期错误（会以清晰文案返回给调用方，而不是堆栈）。"""


def ensure_llm_usable(llm: Any) -> None:
    """LLM 明显不可配置时**提前失败**，给出一条能照做的错误。

    为什么必须提前拦：分析器是「逐步降级」设计，LLM 全挂时只会返回空段落——
    配置类错误（没设 Key）会被降级逻辑吞掉，用户看到的只是「7 段全空」，
    完全不知道去改哪个环境变量。
    """
    if getattr(llm, "is_placeholder", False):
        reason = getattr(llm, "reason", "未配置 LLM API Key")
        raise ToolError(
            f"{reason}。请在 MCP 客户端的 env 里设置 JOBCOPILOT_LLM_API_KEY"
            "（以及按需设置 JOBCOPILOT_LLM_PROVIDER），然后重启客户端。"
        )


def usage_snapshot(llm: Any) -> dict[str, int]:
    """读取 LLM 的累计用量（不支持则返回全 0）。"""
    u = getattr(llm, "usage", None)
    if not isinstance(u, dict):
        return {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
    return {k: int(u.get(k, 0)) for k in ("calls", "prompt_tokens", "completion_tokens")}


def usage_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    """两次快照之差 = 本次调用的真实用量。"""
    return {k: after.get(k, 0) - before.get(k, 0) for k in before}


@dataclass
class ToolContext:
    """工具执行上下文：LLM、提示词、画像路径、安全策略。

    Attributes:
        llm: LLM 端口实现（由 server 按 BYOK 配置构造）。**HTTP 形态下这是
            服务端默认 Key**；若调用方在请求头里带了自己的 Key，应当先经
            :meth:`with_request` 派生本次请求的上下文。
        config: Server 配置（含 prompts_dir / 安全开关）。
        request: 本次请求的原始 HTTP 请求（stdio 下为 ``None``）。
    """

    llm: LLMPort
    config: ServerConfig
    request: Any = None

    # ---------- 按请求解析 BYOK ----------

    def llm_for(self, request: Any | None) -> LLMPort:
        """按请求头解析 LLM：调用方带了自己的 Key 就用它，否则回落服务端配置。

        Args:
            request: Starlette ``Request``（HTTP 形态）；``None`` 时直接回落。

        Returns:
            本次请求应当使用的 LLM 客户端。
        """
        from jobcopilot.mcp.request_keys import credentials_from_request, resolve_llm

        creds = credentials_from_request(request)
        if creds is None:
            return self.llm
        logger.info(
            f"使用调用方提供的 BYOK 凭据 provider={creds.provider or self.config.provider}"
        )
        return resolve_llm(creds, self.config)

    def with_request(self, request: Any | None) -> "ToolContext":
        """派生「本次请求」的上下文（``llm`` 已按 BYOK 解析好）。

        这样 :mod:`jobcopilot.mcp.tools` 里的实现仍然只读 ``ctx.llm``，
        不必知道 BYOK 的存在——保持纯实现可独立单测。

        Args:
            request: 原始 HTTP 请求；``None`` 时原样返回 ``self``。
        """
        if request is None:
            return self
        return ToolContext(llm=self.llm_for(request), config=self.config, request=request)

    # ---------- 提示词 ----------

    def resolver(self, pack: str | None, override: str | None = None) -> PromptResolver:
        """构造提示词解析器。

        Args:
            pack: 职能族名。
            override: 请求级提示词覆盖（**整段文本**，覆盖"批量职位分析"那条）。
        """
        overrides = {"批量职位分析.md": override} if override else None
        return PromptResolver(
            local_dir=self.config.prompts_dir,
            pack=pack or None,
            overrides=overrides,
        )

    # ---------- 文件读取（安全边界）----------

    def read_source(self, source_path: str) -> str:
        """读取 ``source_path`` 指向的文件内容。

        Raises:
            ToolError: 开关关闭、越界、不存在或过大——**一律给可读的提示**，
                不要把 FileNotFoundError 之类的堆栈甩给调用方。
        """
        if not self.config.allow_source_path:
            raise ToolError(
                "本服务已禁用 source_path（HTTP 传输默认关闭，避免任意文件读取）。"
                "如确需启用：设置 JOBCOPILOT_ALLOW_SOURCE_PATH=1，"
                "并用 JOBCOPILOT_SOURCE_ROOT 限定可读目录。"
            )
        path = Path(source_path).expanduser()
        if not path.is_absolute():
            path = (self.config.source_root or Path.cwd()) / path
        path = path.resolve()

        if self.config.source_root is not None:
            root = self.config.source_root.expanduser().resolve()
            if root not in path.parents and path != root:
                raise ToolError(f"source_path 越界：只允许读取 {root} 目录下的文件")

        if not path.exists():
            raise ToolError(f"source_path 不存在: {path}")
        if path.stat().st_size > MAX_SOURCE_BYTES:
            raise ToolError(
                f"source_path 文件过大（>{MAX_SOURCE_BYTES // 1024 // 1024}MB），"
                "请先精简或改用 jobs 参数直接传入。"
            )
        return path.read_text(encoding="utf-8", errors="replace")


# ============================================================
# 工具实现
# ============================================================


def load_jobs_from_text(text: str) -> list[dict[str, Any]]:
    """把 ``source_path`` 的内容解析成职位列表。

    支持三种形态（尽量宽容，让调用方少踩坑）：

    - JSON 数组 → 直接作为 jobs；
    - JSON 对象且含 ``jobs`` 键 → 取 ``jobs``；
    - JSON 对象且含 ``jd_text`` → 视为**单个职位**；
    - 其他（纯文本 / Markdown）→ 视为**单个 JD 正文**。
    """
    stripped = text.strip()
    if stripped.startswith(("[", "{")):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            return [{"jd_text": text}]  # 看起来像 JSON 但坏了 → 当纯文本处理
        if isinstance(data, list):
            return [j for j in data if isinstance(j, dict)]
        if isinstance(data, dict):
            if isinstance(data.get("jobs"), list):
                return [j for j in data["jobs"] if isinstance(j, dict)]
            return [data]
    return [{"jd_text": text}]


async def analyze_job(
    ctx: ToolContext,
    jd_text: str | None = None,
    source_path: str | None = None,
    job_type: str | None = None,
    prompt_pack: str | None = None,
    prompt_override: str | None = None,
    job_meta: dict[str, Any] | None = None,
    user_profile: str | None = None,
) -> dict[str, Any]:
    """单职位 7 段分析。

    Args:
        ctx: 执行上下文。
        jd_text: JD 正文（与 ``source_path`` 二选一）。
        source_path: 从服务端文件读取 JD（**本地模式推荐**：避免把长 JD 当工具参数烧 token）。
        job_type: 職位类型提示（当前用于日志/报告，pack 选择请用 ``prompt_pack``）。
        prompt_pack: 职能族 pack（presales/product/engineering）。
        prompt_override: 覆盖「批量职位分析」提示词的整段文本。
        job_meta: 职位元信息（公司/薪资/城市等）。
        user_profile: **按请求**注入的求职者画像。多用户宿主（如 SEKB）必须走这个参数——
            ``save_profile`` 是进程级全局状态，无法承载「每个用户不同画像」。

    Returns:
        含 7 个分析段落 + ``prompt_meta`` 的字典。
    """
    pack = prompt_pack or job_type
    if jd_text is None and source_path is None:
        raise ToolError("必须提供 jd_text 或 source_path 之一")
    if jd_text is None:
        jd_text = ctx.read_source(source_path or "")

    ensure_llm_usable(ctx.llm)
    resolver = ctx.resolver(pack)
    analyzer = SingleJobAnalyzer(ctx.llm, resolver=resolver)
    profile = user_profile if user_profile is not None else get_profile(ctx)["profile"]
    _before = usage_snapshot(ctx.llm)
    result = await analyzer.analyze(jd_text=jd_text, job_meta=job_meta, user_profile=profile)
    result["usage"] = usage_delta(_before, usage_snapshot(ctx.llm))

    # ⚠️ 引擎内部「每步独立降级」是对的，但**在 API 边界上不能静默**：
    #    若 7 段全空，调用方看到的会是一个「成功但没内容」的结果，完全看不出
    #    是 API Key 失效 / 余额不足 / 网络不通。这里必须主动报错。
    sections = {k: v for k, v in result.items() if k in STEP_PROMPT_FILES}
    empty = [k for k, v in sections.items() if not v]
    if sections and len(empty) == len(sections):
        raise ToolError(
            "分析全部失败：7 个段落均为空。通常是 LLM 不可用——"
            "请检查 API Key、余额、网络，或换一个 provider/model 后重试。"
        )
    if empty:
        result["warnings"] = [
            f"以下段落降级为空（可能触发限流或超时，结果不完整）: {empty}"
        ]

    # 可追溯性：记录本次实际用了哪些提示词、来自哪一级、指纹多少
    result["prompt_meta"] = {
        "pack": pack or "",
        "prompts": {
            fname: resolver.meta(fname).to_dict()
            for fname in STEP_PROMPT_FILES.values()
            if resolver.get(fname)
        },
    }
    return result


async def analyze_jobs_batch(
    ctx: ToolContext,
    jobs: list[dict[str, Any]] | None = None,
    source_path: str | None = None,
    keyword: str | None = None,
    city: str | None = None,
    prompt_pack: str | None = None,
    prompt_override: str | None = None,
    user_profile: str | None = None,
) -> dict[str, Any]:
    """批量市场分析（返回完整报告，含 stats / market / knowledge_iteration）。

    Args:
        ctx: 执行上下文。
        jobs: 职位列表；与 ``source_path`` 二选一。
        source_path: 从服务端文件读取职位列表（**88 个职位走这条路，别当参数传**）。
        keyword / city: 报告元信息。
        prompt_pack: 职能族 pack。
        prompt_override: 覆盖「批量职位分析」提示词的整段文本。
        user_profile: **按请求**注入的求职者画像（多用户宿主必须走这个参数）。

    Raises:
        ToolError: 既没给 jobs 也没给 source_path，或读文件失败。
    """
    if jobs is None and source_path is None:
        raise ToolError("必须提供 jobs 或 source_path 之一")
    ensure_llm_usable(ctx.llm)
    if jobs is None:
        jobs = load_jobs_from_text(ctx.read_source(source_path or ""))

    profile = user_profile if user_profile is not None else get_profile(ctx)["profile"]
    _before = usage_snapshot(ctx.llm)
    report = await core_analyze_batch(
        ctx.llm,
        jobs,
        profile,
        keyword=keyword or "",
        city=city or "",
        resolver=ctx.resolver(prompt_pack, prompt_override),
    )
    if not report.get("job_count"):
        logger.warning("批量分析输入为空（既无标题也无 JD 的职位会被过滤）")

    report["usage"] = usage_delta(_before, usage_snapshot(ctx.llm))

    # 同 analyze_job：两路 LLM 全失败时不能把「空报告」当成功返回
    if not report.get("market") and not report.get("knowledge_iteration"):
        raise ToolError(
            "批量分析失败：市场行情与知识迭代两路均为空。通常是 LLM 不可用——"
            "请检查 API Key、余额、网络，或换一个 provider/model 后重试。"
        )
    return dict(report)


def get_profile(ctx: ToolContext) -> dict[str, Any]:
    """读取用户画像（文本原样返回，空则 ``profile=""``）。"""
    path = ctx.config.profile_path
    if path is None or not path.exists():
        return {"profile": "", "path": str(path) if path else ""}
    return {"profile": path.read_text(encoding="utf-8"), "path": str(path)}


def save_profile(ctx: ToolContext, profile: str) -> dict[str, Any]:
    """保存用户画像（整段文本，格式自由——它最终是注入提示词的文本）。

    画像属于个人信息，只落在本地数据目录，**不上传、不进仓库**。
    """
    path = ctx.config.profile_path
    if path is None:
        raise ToolError("未配置画像路径")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(profile, encoding="utf-8")
    path.chmod(0o600)  # 个人画像按敏感文件对待
    return {"saved": True, "path": str(path), "bytes": len(profile.encode("utf-8"))}


def list_prompt_packs(ctx: ToolContext) -> dict[str, Any]:
    """列出内置职能 pack 与本地提示词目录的状态。"""
    packs = []
    for name in available_packs():
        files = sorted(p.name for p in (packs_dir() / name).glob("*.md"))
        packs.append({"name": name, "files": files})
    local = ctx.config.prompts_dir
    local_files = sorted(p.name for p in local.glob("*.md")) if local.exists() else []
    return {
        "packs": packs,
        "local_dir": str(local),
        "local_files": local_files,
        "note": "本地目录优先级最高；改完直接生效，无需重启。",
    }


def sync_prompts(
    ctx: ToolContext,
    pack: str | None = None,
    remote: bool = True,
    source: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """把提示词同步到本地目录（默认走**远端**，带三级回退）。

    ⚠️ 写出的是**合并后的完整提示词**而非 pack 片段——本地目录是整文件优先，
    只写片段会丢掉 base 的 JSON 骨架。

    Args:
        ctx: 执行上下文。
        pack: 职能族名；``None`` 表示只同步 base。
        remote: ``True`` 走远端（自建主源 → GitHub 备源 → 包内兜底）；
            ``False`` 直接用包内提示词（离线 / 排障用）。
        source: 只从该 URL 同步（覆盖默认源链）。
        overwrite: 是否覆盖已存在的本地文件（默认不覆盖，保护用户改动）。

    Returns:
        含 ``source``（实际生效的源）/ ``used_fallback`` / ``version`` / 写入列表的结果。
    """
    if pack and pack not in available_packs():
        raise ToolError(f"未知 pack: {pack}；可用: {', '.join(available_packs()) or '（无）'}")

    dest = ctx.config.prompts_dir

    if remote:
        from jobcopilot.core.prompts.remote import sync_to_local

        try:
            outcome = sync_to_local(
                dest,
                pack=pack,
                sources=[source] if source else None,
                overwrite=overwrite,
                timeout=ctx.config.remote_timeout,
            )
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"提示词同步失败: {str(e)[:200]}") from e
        result = outcome.to_dict()
        result["dir"] = str(dest)
        if outcome.source == "package":
            result["note"] = (
                "所有远端源均不可用，已回落到包内提示词（版本可能落后于线上）。"
                "如需排查：检查网络，或用 source 参数指定可用的镜像。"
            )
        return result

    # 离线模式：直接用包内提示词
    resolver = ctx.resolver(pack)
    written, skipped = [], []
    dest.mkdir(parents=True, exist_ok=True)
    for name in resolver.available():
        text = resolver.get(name)
        if not text:
            continue
        target = dest / name
        if target.exists() and not overwrite:
            skipped.append(name)
            continue
        target.write_text(text, encoding="utf-8")
        written.append(name)
    return {
        "source": "package",
        "used_fallback": False,
        "version": "",
        "written": written,
        "skipped": skipped,
        "dir": str(dest),
    }
