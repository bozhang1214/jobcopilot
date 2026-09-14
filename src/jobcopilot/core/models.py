"""内核领域模型（轻量 TypedDict，与 JSON 输出结构一一对应）。

用 TypedDict 而非 pydantic：内核的输出就是给 LLM/前端消费的 JSON，
不引入校验依赖，也避免把宿主的序列化方式固化下来。
"""
from __future__ import annotations

from typing import Any, TypedDict


class JobPosting(TypedDict, total=False):
    """一条职位（归一化后）。"""

    job_id: str
    title: str
    company: str
    salary: str
    city: str
    source: str
    job_url: str
    jd_text: str


class NameCount(TypedDict):
    """「名称 + 计数」分布项（公司分布 / 方向分布共用）。"""

    name: str
    count: int


class HotKeyword(TypedDict):
    """热点关键词及其在标题中的出现次数。"""

    keyword: str
    count: int


class JobStats(TypedDict):
    """程序化统计结果（不经过 LLM）。"""

    company_distribution: list[NameCount]
    role_distribution: list[NameCount]
    hot_keywords: list[HotKeyword]


class BatchReport(TypedDict, total=False):
    """批量分析报告。"""

    analyzed_at: str
    analyzed_at_ts: float
    keyword: str
    city: str
    job_count: int
    stats: JobStats
    market: dict[str, Any]
    knowledge_iteration: dict[str, Any]
    jobs: list[JobPosting]


def normalize_job(j: dict[str, Any]) -> JobPosting:
    """归一化前端传入的职位字典，补齐必需字段。

    与 SEKB 原 ``market._normalize_job`` 行为逐字段一致。
    """
    return {
        "job_id": j.get("job_id", ""),
        "title": (j.get("title") or "").strip(),
        "company": (j.get("company") or "").strip(),
        "salary": (j.get("salary") or "").strip(),
        "city": (j.get("city") or "").strip(),
        "source": (j.get("source") or "手动上传").strip(),
        "job_url": j.get("job_url", ""),
        "jd_text": (j.get("jd_text") or "").strip(),
    }
