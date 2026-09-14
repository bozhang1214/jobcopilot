"""评估指标：L2 聚合统计 + L3 LLM-as-Judge。

L2（零 LLM）只做**聚合与对比**，真正的判定在 :mod:`jobcopilot.evals.runner`。
L3 需要真实 LLM，按需运行（默认不在 CI 跑，见守门员方案 B）。
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from jobcopilot.core.messages import LLMPort, extract_text, system, user

#: L3 的五个评审维度（与实施计划一致）
JUDGE_METRICS: tuple[str, ...] = (
    "jd_coverage",
    "groundedness",
    "actionability",
    "track_coverage",
    "salary_anchored",
)

#: 每维 1~5 分
JUDGE_SCALE_MIN = 1
JUDGE_SCALE_MAX = 5

JUDGE_SYSTEM_PROMPT = """你是严格的求职分析报告评审员。请对给定报告按五个维度打分（1~5 分整数）。

维度定义：
- jd_coverage（JD 覆盖度）：是否覆盖了 JD 里的关键要求，有无明显遗漏
- groundedness（如实性）：结论是否有报告内的事实依据，**没有编造**公司/薪资/岗位信息
- actionability（可执行性）：给出的建议是否具体可执行（而非"多学习""提升能力"这类空话）
- track_coverage（赛道划分合理性）：赛道/产品线/客户维度的划分是否合理、是否贴合数据
- salary_anchored（薪资有据）：薪资区间是否有 JD 数据支撑，而非凭空给出

评分锚点：1=很差/明显错误，3=及格，5=优秀且无编造。

只输出 JSON，不要任何多余文字：
```json
{
  "jd_coverage": 4, "groundedness": 5, "actionability": 3,
  "track_coverage": 4, "salary_anchored": 4,
  "reason": "一句话总评"
}
```"""


@dataclass
class JudgeScore:
    """一次 L3 评审的结果。"""

    scores: dict[str, int] = field(default_factory=dict)
    reason: str = ""
    raw: str = ""

    @property
    def mean(self) -> float:
        """各维度均分（无有效分数时为 0）。"""
        values = [v for v in self.scores.values() if v > 0]
        return round(sum(values) / len(values), 3) if values else 0.0

    def to_dict(self) -> dict[str, Any]:
        """转成可序列化字典。"""
        return {"scores": self.scores, "mean": self.mean, "reason": self.reason[:200]}


def parse_judge_score(raw: Any) -> JudgeScore:
    """从裁判输出里稳健解析分数。

    LLM 爱加代码围栏和解释文字，这里先剥围栏再取大括号区间；
    分数会被夹到 ``[1, 5]``，非整数或非法值直接丢弃（宁缺勿错）。
    """
    text = raw if isinstance(raw, str) else str(raw)
    text = re.sub(r"```(?:json)?\s*", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return JudgeScore(raw=text[:200])
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return JudgeScore(raw=text[:200])

    scores: dict[str, int] = {}
    for key in JUDGE_METRICS:
        value = data.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            iv = int(value)
            if JUDGE_SCALE_MIN <= iv <= JUDGE_SCALE_MAX:
                scores[key] = iv
    return JudgeScore(scores=scores, reason=str(data.get("reason", "")), raw=text[:200])


async def judge_report(
    llm: LLMPort, report: Mapping[str, Any], role: str = "job_analysis"
) -> JudgeScore:
    """用 LLM 对一份分析报告打分（L3）。

    只把**报告的 LLM 产出部分**喂给裁判（stats 是程序算的，不需要评）；
    控制输入长度避免超限。
    """
    payload = {
        "market": report.get("market"),
        "knowledge_iteration": report.get("knowledge_iteration"),
        "job_count": report.get("job_count"),
        "sample_jobs": [
            {"title": j.get("title"), "company": j.get("company"), "salary": j.get("salary")}
            for j in (report.get("jobs") or [])[:8]
        ],
    }
    body = json.dumps(payload, ensure_ascii=False)[:6000]
    resp = await llm.complete(role, [system(JUDGE_SYSTEM_PROMPT), user(body)])
    return parse_judge_score(extract_text(resp))


def aggregate_pass_rates(results: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """按断言名聚合通过率。

    Args:
        results: 每个用例的 ``AssertionSuite.to_dict()``。

    Returns:
        ``{断言名: 通过率}``；某个断言一个用例都没跑则不出现。
    """
    totals: dict[str, list[bool]] = {}
    for r in results:
        for item in r.get("results", []):
            totals.setdefault(item["name"], []).append(bool(item["passed"]))
    return {
        name: round(sum(v) / len(v), 4) for name, v in sorted(totals.items()) if v
    }


def aggregate_judge(cases: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """聚合 L3 各维度均分。"""
    acc: dict[str, list[int]] = {}
    for c in cases:
        for k, v in (c.get("judge", {}).get("scores") or {}).items():
            acc.setdefault(k, []).append(int(v))
    return {k: round(sum(v) / len(v), 3) for k, v in sorted(acc.items()) if v}
