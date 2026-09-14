"""职位市场程序化统计（**不经过 LLM**，纯确定性计算）。

这一层是整条流水线里唯一「可精确断言」的部分：同样的职位列表必然得到同样的
统计结果。因此它也是 Eval L1 层（零 LLM 成本）的主要抓手。

搬到独立包后**规则表保持原样**——规则即产品，改动会直接改变报告口径。
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from jobcopilot.core.models import HotKeyword, JobStats

# 职位方向分类关键词（按优先级，首个命中即归类）
ROLE_RULES: list[tuple[str, list[str]]] = [
    ("评测/质量", ["评测", "评估", "Evaluation", "测试"]),
    ("安全", ["安全"]),
    ("算法/模型", ["算法", "NLP", "大模型", "LLM", "模型", "AIOps"]),
    ("架构师/Leader", ["架构师", "Tech Lead", "技术负责人", "Leader", "架构研发"]),
    ("产品经理", ["产品经理", "产品", "PM", "策略"]),
    ("运营/策略", ["运营", "数据策略", "数据"]),
    (
        "研发/工程",
        ["后端", "引擎", "研发工程师", "开发工程师", "Harness", "Infra", "基础设施", "编排", "Orchestration", "应用"],
    ),
]

# 热点技术关键词（在标题里统计频次）
HOT_KEYWORDS: list[str] = [
    "Harness", "Infra", "编排", "Orchestration", "评测", "Evaluation",
    "Claw", "ArkClaw", "RAG", "大模型", "LLM", "多模态", "AIOps", "SOC", "安全",
]

# 未命中任何规则时的兜底分类
ROLE_OTHER = "其他"


def classify_role(title: str) -> str:
    """按 ``ROLE_RULES`` 把职位标题归类；未命中返回 ``"其他"``。"""
    t = (title or "").lower()
    for role, keywords in ROLE_RULES:
        if any(k.lower() in t for k in keywords):
            return role
    return ROLE_OTHER


def compute_stats(jobs: Sequence[Mapping[str, Any]]) -> JobStats:
    """程序化统计：公司 / 方向 / 热点关键词。

    Args:
        jobs: 职位列表（dict，至少可能含 ``company`` / ``title``）。

    Returns:
        含 ``company_distribution`` / ``role_distribution`` / ``hot_keywords``
        的统计字典；分布按计数倒序。
    """
    company = Counter((j.get("company") or "未知").strip() for j in jobs)
    role = Counter(classify_role(j.get("title") or "") for j in jobs)

    all_titles = " ".join((j.get("title") or "") for j in jobs).lower()
    hot: list[HotKeyword] = [
        {"keyword": k, "count": all_titles.count(k.lower())}
        for k in HOT_KEYWORDS
        if all_titles.count(k.lower()) > 0
    ]
    hot.sort(key=lambda x: -x["count"])

    return {
        "company_distribution": [
            {"name": c, "count": n} for c, n in company.most_common()
        ],
        "role_distribution": [
            {"name": r, "count": n} for r, n in role.most_common()
        ],
        "hot_keywords": hot,
    }
