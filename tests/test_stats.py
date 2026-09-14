"""程序化统计的测试（Eval L1 层：零 LLM、可精确断言）。"""
from __future__ import annotations

from jobcopilot.core.stats import ROLE_OTHER, classify_role, compute_stats


def test_classify_role_matches_expected_buckets() -> None:
    """规则表按优先级命中，首个匹配即归类。

    ⚠️ 这里的期望值是**从现网实现实测反推**的，用于锁死行为。
    其中两例结果违反直觉，是规则顺序导致的历史怪癖（P1 调整规则时必须
    同步更新黄金数据集并说明影响）：

    - ``大模型产品经理`` → ``算法/模型``（「算法/模型」规则里的「大模型」先命中）
    - ``数据策略运营`` → ``产品经理``（「产品经理」规则里的「策略」先命中）
    """
    cases = {
        "大模型算法工程师": "算法/模型",
        "AI Agent 平台研发工程师": "研发/工程",
        "售前解决方案架构师": "架构师/Leader",
        "大模型产品经理": "算法/模型",  # 怪癖：非「产品经理」
        "安全运营专家": "安全",
        "模型评测工程师": "评测/质量",
        "数据策略运营": "产品经理",  # 怪癖：非「运营/策略」
    }
    for title, expected in cases.items():
        assert classify_role(title) == expected, title


def test_classify_role_priority_order() -> None:
    """「评测」优先级高于「算法」：命中即返回，不再往下看。"""
    assert classify_role("算法评测工程师") == "评测/质量"


def test_classify_role_case_insensitive() -> None:
    """英文关键词大小写不敏感。"""
    assert classify_role("Tech Lead, Agent Infra") == "架构师/Leader"


def test_classify_role_unknown_and_empty() -> None:
    """未命中与空标题都归入「其他」，不抛异常。"""
    assert classify_role("行政专员") == ROLE_OTHER
    assert classify_role("") == ROLE_OTHER
    assert classify_role(None) == ROLE_OTHER  # type: ignore[arg-type]


def test_compute_stats_distributions() -> None:
    """公司/方向分布按计数倒序。"""
    jobs = [
        {"title": "大模型算法工程师", "company": "字节"},
        {"title": "算法工程师", "company": "字节"},
        {"title": "产品经理", "company": "阿里"},
    ]
    stats = compute_stats(jobs)
    assert stats["company_distribution"][0] == {"name": "字节", "count": 2}
    assert stats["role_distribution"][0] == {"name": "算法/模型", "count": 2}


def test_compute_stats_company_fallback() -> None:
    """公司缺失（None/空串）归入「未知」；纯空白串保留为空名（现网行为，勿改）。"""
    stats = compute_stats([{"title": "x"}, {"title": "y", "company": "   "}])
    # 「   」是真值，会走 strip() 变成空串，而不是落到「未知」——
    # 这是原实现的既有行为，抽取时原样保留。
    assert stats["company_distribution"] == [{"name": "未知", "count": 1}, {"name": "", "count": 1}]


def test_compute_stats_hot_keywords_filtered_and_sorted() -> None:
    """热点关键词只保留出现过的，并按出现次数倒序。"""
    jobs = [
        {"title": "Harness 工程师"},
        {"title": "Harness 平台"},
        {"title": "RAG 工程师"},
    ]
    hot = compute_stats(jobs)["hot_keywords"]
    assert hot[0] == {"keyword": "Harness", "count": 2}
    assert {"keyword": "RAG", "count": 1} in hot
    assert all(h["count"] > 0 for h in hot)


def test_compute_stats_empty_jobs() -> None:
    """空列表返回空分布，不抛异常。"""
    stats = compute_stats([])
    assert stats["company_distribution"] == []
    assert stats["role_distribution"] == []
    assert stats["hot_keywords"] == []
