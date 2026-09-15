"""单个 JD 的全流程分析（多步骤流水线）。

流程::

    02 深度分析 → 03 知识点优先级 + 05 差距分析（并行）
        → 04 面试 Q&A + 06 简历建议 + 08 项目迭代 + 09 求职策略（并行）

每一步**独立降级**：LLM 调用异常或 JSON 解析失败时记录日志并返回空结构，
不让整个分析崩溃（下游步骤用空结构继续执行）。

01 职位筛选针对「职位列表」输入，单个 JD 深度分析不适用，故跳过；
后续如需批量筛选，可复用 ``01_job_filter.md``。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from jobcopilot.core.json_utils import parse_json
from jobcopilot.core.logging import get_logger
from jobcopilot.core.messages import LLMPort, extract_text, system, user
from jobcopilot.core.prompts.budget import per_part_budget, with_budget
from jobcopilot.core.prompts.resolver import PromptResolver

logger = get_logger(__name__)

# 各步骤 → 提示词文件名
# 注：learning_plan（07）已从单职位流程移除——学习/知识迭代规划上移到批量分析的
# 「职位知识迭代」，因为它本质是「基于市场共性」的长期成长，而非「基于单个 JD」的碎片化计划。
STEP_PROMPT_FILES: dict[str, str] = {
    "job_analysis": "02_job_analysis.md",
    "knowledge_priority": "03_knowledge_priority.md",
    "interview_qa": "04_interview_qa.md",
    "gap_analysis": "05_gap_analysis.md",
    "resume_advice": "06_resume_advice.md",
    "project_iteration": "08_project_iteration.md",
    "job_strategy": "09_job_strategy.md",
}

#: 步骤缺失提示词时的提示（保持与原实现同文案）
_SKIP_NOTICE = "请严格按输出格式返回 JSON，不要输出任何多余文字。"


class SingleJobAnalyzer:
    """基于 LLM 的单职位分析流水线（多步骤，逐步降级）。

    Args:
        llm: LLM 端口实现。
        prompt_dir: 宿主本地提示词目录（优先级高于包内 ``base/``）。
        pack: 职能族名（对应包内 ``packs/<pack>/``）。
        resolver: 直接注入的解析器（给了就忽略 ``prompt_dir`` / ``pack``）。
        max_chars: 全报告字符预算（按 7 段均摊给模型，**不做事后裁剪**）。
    """

    def __init__(
        self,
        llm: LLMPort,
        prompt_dir: str | None = None,
        pack: str | None = None,
        resolver: PromptResolver | None = None,
        max_chars: int | None = None,
    ) -> None:
        self._llm = llm
        self._resolver = resolver or PromptResolver(local_dir=prompt_dir, pack=pack)
        # 总字符预算按段数均摊（单职位固定 7 段）。职责是「让模型写短」，
        # 不做任何事后裁剪——见 core/prompts/budget.py 里的原因。
        self._step_budget = (
            per_part_budget(max_chars, len(STEP_PROMPT_FILES)) if max_chars else None
        )
        self._prompts: dict[str, str] = {}
        for step, filename in STEP_PROMPT_FILES.items():
            text = self._resolver.get(filename)
            if text:
                self._prompts[step] = text
            else:
                logger.warning(
                    f"职位分析提示词缺失，该步骤将跳过 step={step} "
                    f"source={self._resolver.source_of(filename)}"
                )
        if not self._prompts:
            logger.warning("未找到任何职位分析提示词文件")

    @property
    def available_steps(self) -> list[str]:
        """返回已成功加载提示词的步骤名（便于自检 / 观测）。"""
        return sorted(self._prompts)

    # ---------- 编排 ----------

    async def analyze(
        self,
        jd_text: str,
        job_meta: dict[str, Any] | None = None,
        user_profile: str = "",
        role: str = "job_analysis",
        resume_summary: str = "",
        existing_projects: str = "",
    ) -> dict[str, Any]:
        """对单个职位 JD 执行全流程分析，返回聚合的各步结构化结果。

        Args:
            jd_text: 职位 JD 正文（空则抛 ``ValueError``）。
            job_meta: 职位元信息（公司 / 薪资 / 城市等），会序列化后注入提示词。
            user_profile: 用户画像文本。
            role: LLM 角色名（决定走哪套模型配置）。
            resume_summary: 简历摘要（供 06 简历建议用）。
            existing_projects: 已有项目（供 08 项目迭代用）。

        Returns:
            含 ``job_analysis`` / ``knowledge_priority`` / ``interview_qa`` /
            ``gap_analysis`` / ``resume_advice`` / ``project_iteration`` /
            ``job_strategy`` 七个键的字典。
        """
        jd_text = (jd_text or "").strip()
        if not jd_text:
            raise ValueError("jd_text 不能为空")
        meta_json = json.dumps(job_meta, ensure_ascii=False) if job_meta else "无"

        # 1) 深度分析：后续所有步骤的输入源
        job_analysis = await self._step_job_analysis(jd_text, meta_json, role)

        # 2) 并行：知识点优先级（依赖 02）+ 差距分析（依赖 02）
        priorities, gap = await asyncio.gather(
            self._step_knowledge_priority(job_analysis, user_profile, role),
            self._step_gap_analysis(job_analysis, user_profile, role),
        )

        # 3) 并行：面试 Q&A（依赖 03）+ 简历建议/项目迭代（依赖 05）+ 求职策略（依赖 02）
        interview, resume, project, strategy = await asyncio.gather(
            self._step_interview_qa(job_analysis, priorities, user_profile, role),
            self._step_resume_advice(job_analysis, gap, user_profile, role, resume_summary),
            self._step_project_iteration(gap, user_profile, role, existing_projects),
            self._step_job_strategy(job_analysis, user_profile, role),
        )

        return {
            "job_analysis": job_analysis,
            "knowledge_priority": priorities,
            "interview_qa": interview,
            "gap_analysis": gap,
            "resume_advice": resume,
            "project_iteration": project,
            "job_strategy": strategy,
        }

    # ---------- 各步骤 ----------

    async def _step_job_analysis(self, jd_text: str, meta_json: str, role: str) -> dict[str, Any]:
        return await self._call_step(
            "job_analysis", {"jd_text": jd_text, "job_meta": meta_json}, role
        )

    async def _step_knowledge_priority(
        self, job_analysis: dict[str, Any], user_profile: str, role: str
    ) -> dict[str, Any]:
        return await self._call_step(
            "knowledge_priority",
            {"job_analyses": self._dumps([job_analysis]), "user_profile": user_profile},
            role,
        )

    async def _step_interview_qa(
        self, job_analysis: dict[str, Any], priorities: dict[str, Any], user_profile: str, role: str
    ) -> dict[str, Any]:
        return await self._call_step(
            "interview_qa",
            {
                "job_analyses": self._dumps([job_analysis]),
                "knowledge_priority": self._dumps(priorities),
                "user_profile": user_profile,
            },
            role,
        )

    async def _step_gap_analysis(
        self, job_analysis: dict[str, Any], user_profile: str, role: str
    ) -> dict[str, Any]:
        return await self._call_step(
            "gap_analysis",
            {"user_profile": user_profile, "jd_requirements": self._dumps(job_analysis)},
            role,
        )

    async def _step_resume_advice(
        self,
        job_analysis: dict[str, Any],
        gap: dict[str, Any],
        user_profile: str,
        role: str,
        resume_summary: str,
    ) -> dict[str, Any]:
        return await self._call_step(
            "resume_advice",
            {
                "user_profile": user_profile,
                "jd_requirements": self._dumps(job_analysis),
                "gap_analysis": self._dumps(gap),
                "resume_summary": resume_summary or "无",
            },
            role,
        )

    async def _step_project_iteration(
        self, gap: dict[str, Any], user_profile: str, role: str, existing_projects: str
    ) -> dict[str, Any]:
        return await self._call_step(
            "project_iteration",
            {
                "gap_analysis": self._dumps(gap),
                "user_profile": user_profile,
                "existing_projects": existing_projects or "无",
            },
            role,
        )

    async def _step_job_strategy(
        self, job_analysis: dict[str, Any], user_profile: str, role: str
    ) -> dict[str, Any]:
        return await self._call_step(
            "job_strategy",
            {"job_analyses": self._dumps([job_analysis]), "user_profile": user_profile},
            role,
        )

    # ---------- 底层调用 ----------

    async def _call_step(self, step: str, replacements: dict[str, str], role: str) -> dict[str, Any]:
        """用占位符替换后的提示词调用 LLM，稳健解析 JSON；失败降级为空结构。"""
        prompt = self._prompts.get(step)
        if not prompt:
            logger.warning(f"步骤提示词缺失，跳过 step={step}")
            return {}
        for key, value in replacements.items():
            prompt = prompt.replace("{{" + key + "}}", value)
        messages = [system(with_budget(prompt, self._step_budget)), user(_SKIP_NOTICE)]
        try:
            resp = await self._llm.complete(role, messages)
            return parse_json(extract_text(resp), tag="职位分析")
        except Exception as e:  # noqa: BLE001
            logger.error(f"职位分析步骤失败，降级为空结构 step={step} error={str(e)[:200]}")
            return {}

    @staticmethod
    def _dumps(data: Any) -> str:
        return json.dumps(data, ensure_ascii=False)
