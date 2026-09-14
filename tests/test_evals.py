"""Eval 骨架的测试：L1 断言（正/反向）、骨架解析、L2 基线比对、L3 解析。"""
from __future__ import annotations

import json

import pytest

from jobcopilot.evals.assertions import (
    AssertionSuite,
    build_stub_payload,
    enum_valid,
    extract_json_skeleton,
    job_count_match,
    prompt_schema_conformance,
    schema_valid,
    sections_complete,
    stats_exact_match,
)
from jobcopilot.evals.metrics import aggregate_pass_rates, parse_judge_score
from jobcopilot.evals.runner import (
    SkeletonStubLLM,
    collect_prompt_digests,
    compare_with_baseline,
    load_datasets,
    run_eval,
)

# ---------- 测试数据 ----------

GOOD_REPORT = {
    "keyword": "Agent",
    "city": "北京",
    "job_count": 2,
    "stats": {
        "company_distribution": [{"name": "字节", "count": 1}, {"name": "阿里", "count": 1}],
        "role_distribution": [{"name": "研发/工程", "count": 1}, {"name": "算法/模型", "count": 1}],
        "hot_keywords": [{"keyword": "大模型", "count": 1}],
    },
    "market": {"track_heatmap": []},
    "knowledge_iteration": {"foundation": []},
    "jobs": [
        {"title": "AI Agent 平台开发工程师", "company": "字节"},
        {"title": "大模型算法工程师", "company": "阿里"},
    ],
}


# ---------- L1 断言：正向 ----------


def test_schema_valid_positive() -> None:
    assert schema_valid(GOOD_REPORT).passed


def test_schema_valid_negative_missing_and_wrong_type() -> None:
    r = schema_valid({k: v for k, v in GOOD_REPORT.items() if k != "market"})
    assert not r.passed and "market" in r.detail
    bad = dict(GOOD_REPORT, job_count="2")
    assert not schema_valid(bad).passed


def test_job_count_match_negative() -> None:
    assert job_count_match(GOOD_REPORT).passed
    assert not job_count_match(dict(GOOD_REPORT, job_count=99)).passed


def test_stats_exact_match_positive_and_negative() -> None:
    """stats 是程序算的，必须能精确比对；被篡改必须红。"""
    assert stats_exact_match(GOOD_REPORT).passed

    tampered = json.loads(json.dumps(GOOD_REPORT))
    tampered["stats"]["hot_keywords"] = [{"keyword": "编造的", "count": 9}]
    r = stats_exact_match(tampered)
    assert not r.passed
    assert "hot_keywords" in r.detail


def test_sections_complete_positive_and_negative() -> None:
    full = {k: {"x": 1} for k in (
        "job_analysis", "knowledge_priority", "interview_qa", "gap_analysis",
        "resume_advice", "project_iteration", "job_strategy",
    )}
    assert sections_complete(full).passed

    missing = dict(full)
    missing["gap_analysis"] = {}
    r = sections_complete(missing)
    assert not r.passed and "gap_analysis" in r.detail


def test_enum_valid_positive_and_negative() -> None:
    spec = {"plans[].status": {"planned", "applied"}}
    ok = {"plans": [{"status": "planned"}, {"status": "applied"}]}
    assert enum_valid(ok, spec).passed

    bad = {"plans": [{"status": "已投"}]}
    r = enum_valid(bad, spec)
    assert not r.passed and "plans[0]" in r.detail


def test_prompt_schema_conformance_positive_and_negative() -> None:
    prompt = '```json\n{"a": "x", "b": [1]}\n```'
    assert prompt_schema_conformance({"a": "v", "b": []}, prompt).passed
    assert not prompt_schema_conformance({"a": "v"}, prompt).passed
    assert not prompt_schema_conformance({"a": "v", "b": "not-a-list"}, prompt).passed
    assert not prompt_schema_conformance({}, prompt).passed


def test_prompt_schema_conformance_skips_when_no_skeleton() -> None:
    r = prompt_schema_conformance({"anything": 1}, "没有 JSON 块的提示词")
    assert r.passed and "跳过" in r.detail


# ---------- 骨架解析（容忍伪 JSON）----------


def test_extract_skeleton_handles_valid_json() -> None:
    s = extract_json_skeleton('```json\n{"a": "x", "n": 1, "l": [], "d": {}}\n```')
    assert s == {"a": "str", "n": "number", "l": "list", "d": "dict"}


def test_extract_skeleton_tolerates_pseudo_json() -> None:
    """真实批量提示词的骨架是伪 JSON（占位符不加引号），必须也能解析。

    这是本层最关键的一条：早期版本用 json.loads，导致两条最重要的批量提示词
    **静默跳过校验**，断言假绿。
    """
    s = extract_json_skeleton('```json\n{"track": 赛道名, "must": ["a"], "n": 3}\n```')
    assert s == {"track": "any", "must": "list", "n": "number"}


def test_extract_skeleton_real_batch_prompt() -> None:
    """真实批量提示词必须解析出完整顶层字段。"""
    from jobcopilot.core.prompts import base_dir

    for name, expected in [
        ("批量职位分析.md", {"track_heatmap", "skill_threshold", "salary_anchor", "bottom_line"}),
        ("职位知识迭代.md", {"foundation", "highlights", "milestones"}),
    ]:
        text = (base_dir() / name).read_text(encoding="utf-8")
        skeleton = extract_json_skeleton(text)
        assert skeleton is not None, f"{name} 骨架解析失败"
        assert expected <= set(skeleton), f"{name} 缺字段: {expected - set(skeleton)}"


def test_extract_skeleton_uses_last_block() -> None:
    """多个 json 块时取最后一个（前面可能是输入示例）。"""
    prompt = '```json\n{"input_field": 1}\n```\n中间说明\n```json\n{"output_field": 2}\n```'
    assert extract_json_skeleton(prompt) == {"output_field": "number"}


def test_build_stub_payload_types() -> None:
    payload = build_stub_payload({"a": "str", "b": "number", "c": "list", "d": "any"})
    assert payload == {"a": "", "b": 0, "c": [], "d": ""}


# ---------- AssertionSuite ----------


def test_assertion_suite_rates() -> None:
    s = AssertionSuite()
    s.add("ok", True)
    s.add("bad", False, "原因")
    assert not s.passed
    assert s.pass_rate == 0.5
    assert [f.name for f in s.failures()] == ["bad"]
    assert s.to_dict()["passed"] is False


def test_aggregate_pass_rates() -> None:
    suites = [
        {"results": [{"name": "a", "passed": True}, {"name": "b", "passed": False}]},
        {"results": [{"name": "a", "passed": True}, {"name": "b", "passed": True}]},
    ]
    assert aggregate_pass_rates(suites) == {"a": 1.0, "b": 0.5}


# ---------- L2 基线 ----------


@pytest.mark.asyncio
async def test_baseline_consistent_passes() -> None:
    rep = await run_eval(level="1")
    baseline = rep.to_dict()
    ok, problems = compare_with_baseline(rep, baseline)
    assert ok, problems


@pytest.mark.asyncio
async def test_baseline_detects_prompt_change() -> None:
    """核心守门员：提示词变了而基线没更新 → 必须失败（零 LLM 成本）。"""
    rep = await run_eval(level="1")
    baseline = rep.to_dict()
    baseline["prompt_digests"]["base/批量职位分析.md"] = "deadbeefdeadbeef"
    ok, problems = compare_with_baseline(rep, baseline)
    assert not ok
    assert any("指纹" in p for p in problems)


@pytest.mark.asyncio
async def test_baseline_detects_metric_regression() -> None:
    rep = await run_eval(level="1")
    baseline = rep.to_dict()
    baseline["metrics"]["per_assertion"]["stats_exact_match"] = 1.0
    rep.cases[0].assertions.add("stats_exact_match", False, "人为制造回归")
    ok, problems = compare_with_baseline(rep, baseline)
    assert not ok
    assert any("stats_exact_match" in p for p in problems)


def test_collect_prompt_digests_covers_base_and_packs() -> None:
    d = collect_prompt_digests()
    assert any(k.startswith("base/") for k in d)
    assert "packs/presales/批量职位分析.md" in d
    assert len(set(d.values())) > 1  # 内容确实不同


# ---------- L3 解析 ----------


def test_parse_judge_score_valid() -> None:
    s = parse_judge_score(
        '{"jd_coverage": 4, "groundedness": 5, "actionability": 3,'
        ' "track_coverage": 2, "salary_anchored": 4, "reason": "还行"}'
    )
    assert s.scores == {
        "jd_coverage": 4, "groundedness": 5, "actionability": 3,
        "track_coverage": 2, "salary_anchored": 4,
    }
    assert s.mean == 3.6
    assert s.reason == "还行"


def test_parse_judge_score_strips_fences() -> None:
    s = parse_judge_score('```json\n{"jd_coverage": 5}\n```')
    assert s.scores == {"jd_coverage": 5}


def test_parse_judge_score_clamps_and_drops_invalid() -> None:
    """越界分数与非法值一律丢弃（宁缺勿错）。"""
    s = parse_judge_score('{"jd_coverage": 99, "groundedness": 0, "actionability": "好"}')
    assert s.scores == {}


def test_parse_judge_score_garbage() -> None:
    assert parse_judge_score("完全不是 JSON").scores == {}
    assert parse_judge_score("{坏}").scores == {}


# ---------- runner ----------


def test_load_datasets() -> None:
    batch, single = load_datasets()
    assert len(batch) == 3
    assert len(single) == 3
    assert all(ds.get("pack") for ds in batch)


@pytest.mark.asyncio
async def test_stub_llm_returns_skeleton_payload() -> None:
    from jobcopilot.core.messages import system
    from jobcopilot.core.prompts import base_dir

    prompt = (base_dir() / "批量职位分析.md").read_text(encoding="utf-8")
    llm = SkeletonStubLLM()
    out = json.loads(await llm.complete("r", [system(prompt)]))
    assert "track_heatmap" in out
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_run_eval_all_cases_pass_with_stub() -> None:
    rep = await run_eval(level="12")
    d = rep.to_dict()
    assert d["passed"] is True
    assert d["metrics"]["case_pass_rate"] == 1.0
    assert set(d["metrics"]["per_assertion"]) >= {
        "schema_valid", "job_count_match", "stats_exact_match",
        "prompt_schema_conformance", "sections_complete",
    }


@pytest.mark.asyncio
async def test_run_eval_l3_without_llm_degrades_gracefully() -> None:
    """L3 用 stub 也能跑通（stub 返回骨架而非评分 → 分数为空但不炸）。"""
    rep = await run_eval(level="3")
    assert all("judge" in c.to_dict() for c in rep.cases if c.dataset == "jobs")
