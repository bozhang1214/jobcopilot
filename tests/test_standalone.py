"""独立可用性测试（P0 DoD ③：JobCopilot 脱离 SEKB 也能跑完整分析）。

这些用例**不 import SEKB 的任何东西**，只用包自身的 provider / analyzer /
存储，来证明内核真的可独立使用。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from jobcopilot import (
    PromptResolver,
    SingleJobAnalyzer,
    analyze_jobs_batch,
    compute_stats,
)
from jobcopilot.core.analyzers import apply_plan
from jobcopilot.storage.json_store import JsonFileStore
from tests.conftest import FakeLLM

_JD = """
岗位：AI Agent 平台开发工程师
公司：某互联网大厂
职责：负责 Agent 编排框架、工具调用与评测体系。
要求：熟悉 Python、LangGraph、RAG，有 LLM 应用落地经验。
"""

_PROFILE = """
name: 测试用户
target_role: Agent 开发
city: 北京
"""


@pytest.mark.asyncio
async def test_standalone_single_analysis_uses_bundled_prompts() -> None:
    """零配置（不传 prompt_dir）即可用包内提示词跑完整流水线。"""
    llm = FakeLLM(default='{"ok": true}')
    analyzer = SingleJobAnalyzer(llm)
    assert len(analyzer.available_steps) == 7

    result = await analyzer.analyze(jd_text=_JD, user_profile=_PROFILE, role="any")
    assert len(llm.calls) == 7
    assert result["job_analysis"] == {"ok": True}


@pytest.mark.asyncio
async def test_standalone_batch_analysis() -> None:
    """零配置即可跑批量分析并拿到结构完整报告。"""
    llm = FakeLLM(default='{"ok": true}')
    jobs = [
        {"title": "AI Agent 平台开发工程师", "company": "某大厂", "city": "北京", "jd_text": _JD},
        {"title": "RAG 工程师", "company": "某创业公司", "city": "上海", "jd_text": _JD},
    ]
    rep = await analyze_jobs_batch(llm, jobs, _PROFILE, keyword="Agent", city="北京")
    assert rep["job_count"] == 2
    assert rep["stats"]["role_distribution"]
    assert rep["market"] == {"ok": True}


def test_standalone_stats_needs_no_llm() -> None:
    """纯统计路径完全不需要 LLM——Eval L1 层可以零成本跑。"""
    stats = compute_stats([{"title": "大模型算法工程师", "company": "某厂"}])
    assert stats["role_distribution"] == [{"name": "算法/模型", "count": 1}]


def test_standalone_apply_plan_with_temp_store(tmp_path: Path) -> None:
    """投递计划可用自定义存储目录独立运行。"""
    store = JsonFileStore(tmp_path / "plan.json")
    rec = apply_plan.upsert_plan("local-user", {"company": "某厂", "title": "Agent 工程师"}, store)
    assert apply_plan.list_plans("local-user", store)[0]["id"] == rec["id"]


def test_standalone_custom_prompt_override() -> None:
    """请求级 override 可临时替换提示词（做 A-B 实验用）。"""
    r = PromptResolver(overrides={"02_job_analysis.md": "自定义提示词"})
    assert r.get("02_job_analysis.md") == "自定义提示词"
    assert r.source_of("02_job_analysis.md") == "override"


def test_package_does_not_import_sekb() -> None:
    """零循环依赖护栏：内核源码里不得出现任何 SEKB 引用。"""
    import jobcopilot

    pkg_root = Path(jobcopilot.__file__).resolve().parent
    offenders: list[str] = []
    for py in pkg_root.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        if "app.agents.job" in text or "from app." in text or "import app." in text:
            offenders.append(str(py.relative_to(pkg_root)))
    assert not offenders, f"内核出现 SEKB 依赖（违反零循环依赖）：{offenders}"


def test_package_has_no_langchain_dependency() -> None:
    """零第三方依赖护栏：内核（core + storage）不得 import langchain/openai。"""
    import jobcopilot

    pkg_root = Path(jobcopilot.__file__).resolve().parent
    offenders: list[str] = []
    for py in pkg_root.rglob("*.py"):
        if "providers" in py.parts:  # provider 允许延迟 import httpx
            continue
        text = py.read_text(encoding="utf-8")
        for banned in ("langchain", "openai", "httpx"):
            if f"import {banned}" in text or f"from {banned}" in text:
                offenders.append(f"{py.relative_to(pkg_root)}: {banned}")
    assert not offenders, f"内核引入了不应有的第三方依赖：{offenders}"
