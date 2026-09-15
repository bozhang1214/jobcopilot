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

# 职位方向归类：**两级判定**，职能名词优先于领域词。
#
# 为什么改成两级（2026-09-15）：原实现是「一张表、首个命中即归类」，而「算法/模型」
# 排在「产品经理」之前，于是**领域词抢走了职能判定**，出现三处明显反直觉的结果：
#
#   - 「大模型产品经理」→ 算法/模型（「大模型」先命中，产品岗被算成算法岗）
#   - 「大模型平台架构师」→ 算法/模型（同理，架构岗被算成算法岗）
#   - 「技术售前顾问（数据平台）」→ 运营/策略（被「数据」这个极宽的词带走）
#
# 现在：先看**这个人干什么**（职能名词），再看**什么方向**（领域/工程词）。
# 注意：**桶名集合没有变**（仍是这 7 个 + 「其他」），变的是归类结果 ——
# 报告里 role_distribution 的**数字会变**，属用户可见的口径变化。
ROLE_NOUN_RULES: list[tuple[str, list[str]]] = [
    ("产品经理", ["产品经理", "产品", "PM"]),
    ("架构师/Leader", ["架构师", "技术负责人", "Tech Lead", "Leader", "架构研发"]),
    ("安全", ["安全"]),
    ("评测/质量", ["评测", "评估", "Evaluation", "测试"]),
    ("运营/策略", ["运营"]),
]

# 标题里没有职能名词时，按技术方向归类。
DOMAIN_RULES: list[tuple[str, list[str]]] = [
    ("算法/模型", ["算法", "NLP", "大模型", "LLM", "模型", "AIOps", "多模态"]),
    (
        "研发/工程",
        [
            "后端", "前端", "引擎", "研发工程师", "开发工程师",
            "Harness", "Infra", "基础设施", "编排", "Orchestration", "应用",
        ],
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
    """按「职能名词 → 领域词」两级把职位标题归类；都未命中返回 ``"其他"``。

    为什么职能优先：职位名词（产品经理 / 架构师 / 运营 / 评测 / 安全）比领域词
    （大模型 / 算法 / Infra）更能说明「这个人是干什么的」。原实现按单表顺序命中，
    导致「大模型产品经理」被算成算法岗一类的错分。

    Args:
        title: 职位标题（大小写不敏感；``None``/空串返回 ``"其他"``）。

    Returns:
        7 个方向桶之一，或 ``ROLE_OTHER``。**桶名集合未变**（改的是归类口径，
        会让报告里的 ``role_distribution`` 数字变化）。
    """
    t = (title or "").lower()
    for rules in (ROLE_NOUN_RULES, DOMAIN_RULES):
        for role, keywords in rules:
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
