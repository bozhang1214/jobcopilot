"""程序化统计的测试（Eval L1 层：零 LLM、可精确断言）。"""
from __future__ import annotations

from jobcopilot.core.stats import ROLE_OTHER, classify_role, compute_stats


def test_classify_role_matches_expected_buckets() -> None:
    """规则按「职能名词 → 领域词」两级命中。

    这里锁的是**修正后**的行为：职位名词优先于领域词，所以「大模型产品经理」是
    产品经理而不是算法岗、「大模型平台架构师」是架构师而不是算法岗。
    （修正前的三处反直觉结果记录在 ``stats.py`` 的注释里，并已同步更新黄金数据集。）
    """
    cases = {
        "大模型算法工程师": "算法/模型",
        "AI Agent 平台研发工程师": "研发/工程",
        "售前解决方案架构师": "架构师/Leader",
        # 修正点：领域词不再抢走职能判定
        "大模型产品经理": "产品经理",
        "大模型平台架构师": "架构师/Leader",
        "技术售前顾问（数据平台）": "其他",
        # 其余行为保持
        "安全运营专家": "安全",
        "模型评测工程师": "评测/质量",
        "数据策略运营": "运营/策略",
    }
    for title, expected in cases.items():
        assert classify_role(title) == expected, title


def test_classify_role_bucket_names_unchanged() -> None:
    """**桶名集合不得变化**（2026-09-15 与 owner 确认：只改归类口径，不新增桶）。

    新增/删除桶会改变报告里 role_distribution 的类别名，前端展示与下游聚合都要跟着改，
    属于更大的口径变更 —— 用这条断言把它变成需要显式决策的事。
    """
    from jobcopilot.core.stats import DOMAIN_RULES, ROLE_NOUN_RULES

    expected = {
        "产品经理", "架构师/Leader", "安全", "评测/质量", "运营/策略", "算法/模型", "研发/工程",
    }
    assert {r for r, _ in ROLE_NOUN_RULES} | {r for r, _ in DOMAIN_RULES} == expected


def test_dataset_role_annotations_match_classifier() -> None:
    """数据集里每条职位的 ``expected_role`` 必须与分类器判定一致。

    为什么要有这条：归类规则一改，报告里的 ``role_distribution`` 数字就变，而这是
    **用户可见**的口径变化。把期望值钉在数据里，规则再调整就有据可依 ——
    否则只能靠「跑一次看看数字对不对」，没有契约。
    """
    from jobcopilot.evals.runner import load_datasets

    batch, _ = load_datasets()
    assert batch, "批量数据集为空"
    mismatches: list[str] = []
    missing: list[str] = []
    for ds in batch:
        for j in ds["jobs"]:
            exp = j.get("expected_role")
            if not exp:
                missing.append(f"{ds['name']}/{j.get('job_id')}")
                continue
            got = classify_role(j.get("title") or "")
            if got != exp:
                mismatches.append(f"{ds['name']}/{j.get('job_id')} 《{j.get('title')}》标注={exp} 实际={got}")

    assert not missing, f"以下职位缺少 expected_role 标注：{missing}"
    assert not mismatches, "归类与标注不一致：\n  " + "\n  ".join(mismatches)


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
