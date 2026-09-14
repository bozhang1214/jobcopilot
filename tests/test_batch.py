"""批量分析的测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from jobcopilot.core.analyzers.batch import (
    JD_BRIEF_MAX,
    JD_STORE_MAX,
    analyze_jobs_batch,
    build_job_summaries,
    prepare_jobs,
)
from jobcopilot.core.prompts import PromptResolver
from tests.conftest import FakeLLM

_JOBS = [
    {"title": "AI Agent 平台研发工程师", "company": "字节", "salary": "60K", "city": "北京",
     "jd_text": "负责 Agent 平台建设"},
    {"title": "大模型产品经理", "company": "阿里", "city": "杭州", "jd_text": "负责大模型产品"},
]


@pytest.mark.asyncio
async def test_analyze_jobs_batch_report_shape() -> None:
    """报告字段齐全，且两路 LLM 各调一次。"""
    llm = FakeLLM(responses=['{"tracks": []}', '{"knowledge": []}'])
    rep = await analyze_jobs_batch(llm, _JOBS, "画像", keyword="Agent", city="北京")
    assert len(llm.calls) == 2
    assert rep["keyword"] == "Agent"
    assert rep["city"] == "北京"
    assert rep["job_count"] == 2
    assert rep["market"] == {"tracks": []}
    assert rep["knowledge_iteration"] == {"knowledge": []}


@pytest.mark.asyncio
async def test_analyze_jobs_batch_includes_stats() -> None:
    """报告携带程序化统计（公司/方向/热点）。"""
    llm = FakeLLM()
    rep = await analyze_jobs_batch(llm, _JOBS)
    stats = rep["stats"]
    assert {"name": "字节", "count": 1} in stats["company_distribution"]
    assert stats["role_distribution"]


@pytest.mark.asyncio
async def test_analyze_jobs_batch_truncates_stored_jd() -> None:
    """报告里回带的 JD 截断到 JD_STORE_MAX，避免报告体积失控。"""
    long_jd = "x" * (JD_STORE_MAX + 500)
    llm = FakeLLM()
    rep = await analyze_jobs_batch(llm, [{"title": "T", "jd_text": long_jd}])
    assert len(rep["jobs"][0]["jd_text"]) == JD_STORE_MAX


@pytest.mark.asyncio
async def test_analyze_jobs_batch_keeps_timestamp_fields() -> None:
    """报告带 ISO 时间与时间戳（前端/缓存靠它判过期）。"""
    rep = await analyze_jobs_batch(FakeLLM(), _JOBS)
    assert rep["analyzed_at"].endswith("+00:00")
    assert isinstance(rep["analyzed_at_ts"], float)


@pytest.mark.asyncio
async def test_analyze_jobs_batch_degrades_per_branch() -> None:
    """一路失败不影响另一路（各自独立降级）。"""
    llm = FakeLLM(responses=["坏数据", '{"knowledge": ["ok"]}'])
    rep = await analyze_jobs_batch(llm, _JOBS)
    assert rep["market"] == {}
    assert rep["knowledge_iteration"] == {"knowledge": ["ok"]}


@pytest.mark.asyncio
async def test_analyze_jobs_batch_all_empty_jobs() -> None:
    """空职位列表也能产出结构完整的报告（不能是 None）。"""
    rep = await analyze_jobs_batch(FakeLLM(), [])
    assert rep["job_count"] == 0
    assert rep["jobs"] == []
    assert rep["stats"]["company_distribution"] == []


@pytest.mark.asyncio
async def test_analyze_jobs_batch_missing_prompts_skips_llm(tmp_path: Path) -> None:
    """严格模式下提示词全缺时不调用 LLM，但仍返回结构完整的报告。"""
    llm = FakeLLM()
    rep = await analyze_jobs_batch(
        llm, _JOBS, resolver=PromptResolver(local_dir=tmp_path, include_base=False)
    )
    assert llm.calls == []
    assert rep["market"] == {}
    assert rep["knowledge_iteration"] == {}
    assert rep["job_count"] == 2


@pytest.mark.asyncio
async def test_analyze_jobs_batch_injects_profile_into_payload() -> None:
    """用户画像与 JD 摘要都进 user 消息。"""
    llm = FakeLLM()
    await analyze_jobs_batch(llm, _JOBS, "我的画像是X")
    assert "我的画像是X" in llm.payloads[0]
    assert "AI Agent 平台研发工程师" in llm.payloads[0]


def test_prepare_jobs_drops_empty_rows() -> None:
    """既无标题又无 JD 的脏数据被丢弃。"""
    out = prepare_jobs([{"title": "有标题"}, {"jd_text": "有JD"}, {"company": "只有公司"}])
    assert len(out) == 2


def test_prepare_jobs_normalizes_fields() -> None:
    """字段被 strip 并补齐默认值。"""
    out = prepare_jobs([{"title": "  T  ", "company": "  C  "}])
    assert out[0]["title"] == "T"
    assert out[0]["company"] == "C"
    assert out[0]["source"] == "手动上传"
    assert out[0]["job_id"] == ""


def test_build_job_summaries_format() -> None:
    """摘要格式：序号 + 【标题】+ 元信息 + 缩进 JD。"""
    text = build_job_summaries([{"title": "T", "company": "C", "salary": "60K", "city": "北京",
                                 "jd_text": "JD 内容"}])
    assert text.startswith("1. 【T】C | 60K | 北京")
    assert "JD 内容" in text


def test_build_job_summaries_title_fallback() -> None:
    """无标题时显示占位符，不出现空【】。"""
    assert "（无标题）" in build_job_summaries([{"jd_text": "x"}])


def test_build_job_summaries_truncates_with_ellipsis() -> None:
    """超长 JD 截断并加省略号。"""
    text = build_job_summaries([{"title": "T", "jd_text": "x" * (JD_BRIEF_MAX + 10)}])
    assert "…" in text
    assert "x" * (JD_BRIEF_MAX + 1) not in text


def test_build_job_summaries_multiple_jobs_separated() -> None:
    """多份 JD 之间用空行分隔。"""
    text = build_job_summaries([{"title": "A"}, {"title": "B"}])
    assert "\n\n" in text
    assert "1. 【A】" in text and "2. 【B】" in text
