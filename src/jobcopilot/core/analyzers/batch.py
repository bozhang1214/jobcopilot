"""批量职位分析（市场行情 + 职位知识迭代）。

**职责边界**：本模块只负责「拿到一批职位 → 产出分析报告」。
职位**从哪来**（爬虫 / 粘贴 / 文件导入）与报告**存到哪**（SEKB 的 JSON 缓存、
CLI 的本地文件）都是宿主的职责——内核不碰 IO，因此可脱离任何宿主独立测试。

一次分析包含两路并行 LLM 调用，各自独立降级：

- 市场行情（``批量职位分析.md``）：赛道热度、机会点、风险
- 职位知识迭代（``职位知识迭代.md``）：该方向需要补的知识体系
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from jobcopilot.core.json_utils import parse_json
from jobcopilot.core.logging import get_logger
from jobcopilot.core.messages import LLMPort, extract_text, system, user
from jobcopilot.core.models import BatchReport, JobPosting, normalize_job
from jobcopilot.core.prompts.resolver import PromptResolver
from jobcopilot.core.stats import compute_stats

logger = get_logger(__name__)

#: 市场行情提示词（站在「求职者选赛道」视角）
PROMPT_MARKET = "批量职位分析.md"
#: 职位知识迭代提示词
PROMPT_KNOWLEDGE = "职位知识迭代.md"

#: 每份 JD 喂给 LLM 的摘要截断长度（控制 token，避免几十份 JD 全文超限）
JD_BRIEF_MAX = 400

#: 报告里每条职位回带的 JD 截断长度
JD_STORE_MAX = 2000

#: 默认 LLM 角色名
DEFAULT_ROLE = "job_analysis"


def build_job_summaries(jobs: Sequence[Mapping[str, Any]], max_len: int = JD_BRIEF_MAX) -> str:
    """构造 JD 摘要文本（职位名/公司/薪资/城市 + JD 截断），供 LLM 分析。"""
    lines: list[str] = []
    for i, j in enumerate(jobs, 1):
        title = (j.get("title") or "").strip() or "（无标题）"
        meta = " | ".join(
            x for x in [
                (j.get("company") or "").strip(),
                (j.get("salary") or "").strip(),
                (j.get("city") or "").strip(),
            ] if x
        )
        jd = (j.get("jd_text") or "").strip()
        brief = jd[:max_len] + ("…" if len(jd) > max_len else "")
        line = f"{i}. 【{title}】{meta}" if meta else f"{i}. 【{title}】"
        if brief:
            line += f"\n   {brief}"
        lines.append(line)
    return "\n\n".join(lines)


async def _llm_call(
    llm: LLMPort, prompt: str, user_profile: str, job_summaries: str, role: str
) -> dict[str, Any]:
    """用给定提示词调用 LLM，返回解析后的 JSON；失败降级为空结构。"""
    payload = f"用户画像：\n{user_profile}\n\nJD 摘要列表：\n{job_summaries}"
    try:
        resp = await llm.complete(role, [system(prompt), user(payload)])
        # 批量流程历史上不剥 markdown 围栏 —— 保持 strip_fences=False 以免改变既有结果
        return parse_json(extract_text(resp), strip_fences=False, tag="批量分析")
    except Exception as e:  # noqa: BLE001
        logger.error(f"批量分析 LLM 调用失败 error={str(e)[:200]}")
        return {}


def prepare_jobs(jobs: list[dict[str, Any]]) -> list[JobPosting]:
    """归一化并过滤职位：丢掉既无标题又无 JD 的脏数据。"""
    return [
        normalize_job(j) for j in (jobs or []) if (j.get("title") or j.get("jd_text"))
    ]


async def analyze_jobs_batch(
    llm: LLMPort,
    jobs: list[dict[str, Any]],
    user_profile: str = "",
    *,
    keyword: str = "",
    city: str = "",
    role: str = DEFAULT_ROLE,
    prompt_dir: str | None = None,
    pack: str | None = None,
    resolver: PromptResolver | None = None,
) -> BatchReport:
    """对一批职位做批量分析，返回报告字典（**不落盘**）。

    Args:
        llm: LLM 端口实现。
        jobs: 职位列表（原始 dict 即可，内部会归一化）。
        user_profile: 用户画像文本，注入两路提示词。
        keyword: 本次分析的关键词（仅回填进报告，不参与分析）。
        city: 本次分析的城市（仅回填进报告）。
        role: LLM 角色名。
        prompt_dir: 宿主本地提示词目录。
        pack: 职能族名。
        resolver: 直接注入的解析器（给了就忽略 ``prompt_dir`` / ``pack``）。

    Returns:
        :class:`BatchReport`：含 ``stats`` / ``market`` / ``knowledge_iteration``
        / ``jobs`` 等字段。即使两路 LLM 全部失败也会返回结构完整的报告
        （对应字段为空 dict），保证前端不会拿到 None。
    """
    res = resolver or PromptResolver(local_dir=prompt_dir, pack=pack)
    cleaned = prepare_jobs(jobs)
    stats = compute_stats(cleaned)
    job_summaries = build_job_summaries(cleaned)

    market_prompt = res.get(PROMPT_MARKET)
    knowledge_prompt = res.get(PROMPT_KNOWLEDGE)
    if not market_prompt:
        logger.warning(f"批量分析提示词缺失，跳过 source={res.source_of(PROMPT_MARKET)}")
    if not knowledge_prompt:
        logger.warning(
            f"职位知识迭代提示词缺失，跳过 source={res.source_of(PROMPT_KNOWLEDGE)}"
        )

    # 并行：市场行情 + 知识迭代，各自独立降级
    market, knowledge = await asyncio.gather(
        _llm_call(llm, market_prompt, user_profile, job_summaries, role) if market_prompt else _empty(),
        _llm_call(llm, knowledge_prompt, user_profile, job_summaries, role) if knowledge_prompt else _empty(),
    )

    return {
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        "analyzed_at_ts": time.time(),
        "keyword": keyword,
        "city": city,
        "job_count": len(cleaned),
        "stats": stats,
        "market": market,
        "knowledge_iteration": knowledge,
        "jobs": [
            {
                "job_id": j.get("job_id", ""),
                "title": j.get("title", ""),
                "company": j.get("company", ""),
                "salary": j.get("salary", ""),
                "city": j.get("city", ""),
                "source": j.get("source", ""),
                "job_url": j.get("job_url", ""),
                "jd_text": (j.get("jd_text") or "")[:JD_STORE_MAX],
            }
            for j in cleaned
        ],
    }


async def _empty() -> dict[str, Any]:
    """提示词缺失时的空结果（保持与「调用失败」同样的降级语义）。"""
    return {}
