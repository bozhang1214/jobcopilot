"""Eval 运行器：把黄金数据集跑成一份可比对的评估报告。

三层（详见实施计划 §3）::

    L1  程序化断言   零 LLM   —— 结构 / 统计口径 / 段落非空 / 提示词骨架一致性
    L2  基线回归     零 LLM   —— 提示词指纹 + 指标不得低于基线
    L3  LLM-as-Judge 有成本   —— 覆盖度 / 如实性 / 可执行性 / 赛道 / 薪资依据

L1 默认用 :class:`SkeletonStubLLM`（从提示词骨架造确定性回答），
因此**完全不需要网络与 Key**，CI 可放心跑；这也是「守门员方案 B」的基础。

L2 的核心是**提示词指纹**：只要提示词变了而基线没更新，L2 就失败——
用零成本的方式逼作者「改提示词必须重新基线化（并在本地跑一次 L3）」。
"""
from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jobcopilot.core.analyzers.batch import PROMPT_KNOWLEDGE, PROMPT_MARKET, analyze_jobs_batch
from jobcopilot.core.analyzers.single import SingleJobAnalyzer
from jobcopilot.core.messages import Message
from jobcopilot.core.prompts.resolver import PromptResolver, base_dir
from jobcopilot.evals.assertions import (
    AssertionSuite,
    build_stub_payload,
    extract_json_skeleton,
    job_count_match,
    prompt_schema_conformance,
    schema_valid,
    sections_complete,
    stats_exact_match,
)
from jobcopilot.evals.metrics import (
    JUDGE_METRICS,
    aggregate_judge,
    aggregate_pass_rates,
    judge_report,
)

#: 基线默认路径（包内）
DEFAULT_BASELINE = Path(__file__).resolve().parent / "baselines" / "v1.json"
#: 数据集默认目录（包内）
DEFAULT_DATASETS = Path(__file__).resolve().parent / "datasets"


class SkeletonStubLLM:
    """从提示词的 JSON 骨架造回答的**确定性**假 LLM（零成本、可离线）。

    它让 L1 能验证「提示词 → 解析 → 报告」整条链路与骨架一致性，
    而不需要网络和 API Key。骨架里的占位文本会被原样回填——
    反正 L1 只断言结构与统计，不评判内容质量。
    """

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, role: str, messages: list[Message]) -> str:
        """按提示词声明的骨架回填一个类型合规的最小对象。"""
        self.calls += 1
        prompt = next((m.content for m in messages if m.role == "system"), "")
        skeleton = extract_json_skeleton(prompt)
        payload = build_stub_payload(skeleton) if skeleton else {}
        return json.dumps(payload, ensure_ascii=False)


@dataclass
class CaseResult:
    """单个用例的评估结果。"""

    case_id: str
    dataset: str
    pack: str
    assertions: AssertionSuite
    report: Mapping[str, Any] = field(default_factory=dict)
    judge: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, keep_report: bool = False) -> dict[str, Any]:
        """转成可序列化字典（默认不内嵌整份报告，避免基线文件过大）。"""
        out: dict[str, Any] = {
            "case_id": self.case_id,
            "dataset": self.dataset,
            "pack": self.pack,
            "assertions": self.assertions.to_dict(),
        }
        if self.judge:
            out["judge"] = self.judge
        if keep_report:
            out["report"] = self.report
        return out


@dataclass
class EvalReport:
    """一次完整评估的结果。"""

    level: str
    cases: list[CaseResult] = field(default_factory=list)
    prompt_digests: dict[str, str] = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""
    duration_s: float = 0.0

    @property
    def pass_rate(self) -> float:
        """所有用例所有断言的总体通过率。"""
        by_name = aggregate_pass_rates([c.assertions.to_dict() for c in self.cases])
        return round(sum(by_name.values()) / len(by_name), 4) if by_name else 1.0

    @property
    def passed(self) -> bool:
        """是否所有用例都通过。"""
        return all(c.assertions.passed for c in self.cases)

    def to_dict(self, keep_report: bool = False) -> dict[str, Any]:
        """转成可序列化字典。"""
        case_dicts = [c.to_dict(keep_report=keep_report) for c in self.cases]
        out: dict[str, Any] = {
            "level": self.level,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": round(self.duration_s, 2),
            "passed": self.passed,
            "pass_rate": self.pass_rate,
            "metrics": {
                "per_assertion": aggregate_pass_rates(
                    [c.assertions.to_dict() for c in self.cases]
                ),
                "case_pass_rate": round(
                    sum(1 for c in self.cases if c.assertions.passed) / len(self.cases), 4
                )
                if self.cases
                else 1.0,
            },
            "prompt_digests": dict(self.prompt_digests),
            "cases": case_dicts,
        }
        judge = aggregate_judge(case_dicts)
        if judge:
            out["metrics"]["judge"] = judge
        return out


# ============================================================
# 数据集加载
# ============================================================


def load_datasets(
    datasets_dir: Path | str = DEFAULT_DATASETS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """加载数据集目录。

    Returns:
        ``(批量数据集列表, 单职位用例列表)``。目录形如
        ``datasets/jobs/*.json`` 与 ``datasets/single/*.json``。
    """
    root = Path(datasets_dir)
    batch: list[dict[str, Any]] = []
    single: list[dict[str, Any]] = []

    jobs_dir = root / "jobs"
    if jobs_dir.is_dir():
        for p in sorted(jobs_dir.glob("*.json")):
            batch.append(json.loads(p.read_text(encoding="utf-8")))
    single_dir = root / "single"
    if single_dir.is_dir():
        for p in sorted(single_dir.glob("*.json")):
            single.extend(json.loads(p.read_text(encoding="utf-8")).get("cases", []))
    return batch, single


def collect_prompt_digests() -> dict[str, str]:
    """收集包内 base 全部提示词的指纹（L2 基线比对用）。"""
    from jobcopilot.core.prompts.compose import digest

    out: dict[str, str] = {}
    for p in sorted(base_dir().glob("*.md")):
        out[f"base/{p.name}"] = digest(p.read_text(encoding="utf-8"))
    packs_root = base_dir().parent / "packs"
    if packs_root.is_dir():
        for p in sorted(packs_root.rglob("*.md")):
            rel = p.relative_to(packs_root)
            out[f"packs/{rel.as_posix()}"] = digest(p.read_text(encoding="utf-8"))
    return out


# ============================================================
# 三层执行
# ============================================================


async def _run_batch_case(ds: dict[str, Any], llm: Any) -> CaseResult:
    """跑一个批量数据集：分析 → L1 断言。"""
    pack = ds.get("pack") or None
    resolver = PromptResolver(pack=pack)
    report = await analyze_jobs_batch(
        llm,
        ds.get("jobs") or [],
        ds.get("user_profile", ""),
        keyword=ds.get("keyword", ""),
        city=ds.get("city", ""),
        resolver=resolver,
    )
    suite = AssertionSuite()
    suite.results.append(schema_valid(report))
    suite.results.append(job_count_match(report))
    suite.results.append(stats_exact_match(report))
    # 两个 LLM 产出各自与其提示词骨架对表
    suite.results.append(
        prompt_schema_conformance(report.get("market") or {}, resolver.get(PROMPT_MARKET))
    )
    suite.results.append(
        prompt_schema_conformance(
            report.get("knowledge_iteration") or {}, resolver.get(PROMPT_KNOWLEDGE)
        )
    )
    return CaseResult(
        case_id=ds.get("name", "batch"),
        dataset="jobs",
        pack=pack or "",
        assertions=suite,
        report=report,
    )


async def _run_single_case(case: dict[str, Any], llm: Any) -> CaseResult:
    """跑一个单职位用例：7 段分析 → L1 断言。"""
    pack = case.get("pack") or None
    analyzer = SingleJobAnalyzer(llm, pack=pack)
    result = await analyzer.analyze(
        jd_text=case.get("jd_text", ""), user_profile=case.get("user_profile", "")
    )
    suite = AssertionSuite()
    suite.results.append(sections_complete(result))
    return CaseResult(
        case_id=case.get("case_id", "single"),
        dataset="single",
        pack=pack or "",
        assertions=suite,
        report=result,
    )


async def run_eval(
    datasets_dir: Path | str = DEFAULT_DATASETS,
    level: str = "12",
    llm: Any | None = None,
    judge_llm: Any | None = None,
    keep_report: bool = False,
) -> EvalReport:
    """运行评估。

    Args:
        datasets_dir: 数据集目录。
        level: ``"1"`` / ``"2"`` / ``"3"`` / ``"12"`` / ``"123"`` / ``"all"``。
        llm: 分析用 LLM；``None`` 时用零成本的 :class:`SkeletonStubLLM`。
        judge_llm: L3 评审用 LLM；``None`` 时若 level 含 3 则回退到 ``llm``。
        keep_report: 是否把完整报告写进结果（基线文件通常不需要）。

    Returns:
        :class:`EvalReport`。
    """
    t0 = time.time()
    rep = EvalReport(
        level=level,
        started_at=datetime.now(timezone.utc).isoformat(),
        prompt_digests=collect_prompt_digests(),
    )
    batch_ds, single_cases = load_datasets(datasets_dir)
    runner_llm = llm or SkeletonStubLLM()

    for ds in batch_ds:
        rep.cases.append(await _run_batch_case(ds, runner_llm))
    for case in single_cases:
        rep.cases.append(await _run_single_case(case, runner_llm))

    if "3" in level or level == "all":
        judge = judge_llm or runner_llm
        for c in rep.cases:
            if c.dataset != "jobs":
                continue
            try:
                score = await judge_report(judge, c.report)
                c.judge = score.to_dict()
            except Exception as e:  # noqa: BLE001
                c.judge = {"error": str(e)[:200], "scores": {}, "mean": 0.0}

    rep.finished_at = datetime.now(timezone.utc).isoformat()
    rep.duration_s = time.time() - t0
    return rep


# ============================================================
# L2 · 基线比对
# ============================================================


def compare_with_baseline(
    report: EvalReport, baseline: dict[str, Any], tolerance: float = 0.0
) -> tuple[bool, list[str]]:
    """把本次结果与基线比对。

    两条规则：

    1. **提示词指纹必须与基线一致** —— 不一致说明提示词改了但没重新基线化。
       这是零成本守门员的核心：它不判断「改得好不好」，只强制「改了就重新基线化」，
       而重新基线化必须先在本地跑一次 L3（有真 LLM 的那层）。
    2. **通过率不得低于基线**（允许 ``tolerance`` 的抖动）。

    Returns:
        ``(是否通过, 问题描述列表)``。
    """
    problems: list[str] = []

    base_digests = baseline.get("prompt_digests") or {}
    cur_digests = report.prompt_digests
    changed = [k for k in sorted(set(base_digests) | set(cur_digests))
               if base_digests.get(k) != cur_digests.get(k)]
    if changed:
        problems.append(
            "提示词指纹与基线不一致（改了提示词就要重新基线化，并在本地跑一次 L3）："
            + ", ".join(changed[:6])
        )

    base_metrics = (baseline.get("metrics") or {})
    base_case_rate = float(base_metrics.get("case_pass_rate", 1.0))
    if report.pass_rate + tolerance < base_case_rate:
        problems.append(
            f"整体通过率下降：{report.pass_rate} < 基线 {base_case_rate}"
        )

    for name, rate in (base_metrics.get("per_assertion") or {}).items():
        cur = report.to_dict()["metrics"]["per_assertion"].get(name)
        if cur is not None and cur + tolerance < float(rate):
            problems.append(f"断言 {name} 通过率下降：{cur} < 基线 {rate}")

    base_judge = base_metrics.get("judge") or {}
    cur_judge = report.to_dict()["metrics"].get("judge") or {}
    for name in JUDGE_METRICS:
        if name in base_judge and name in cur_judge:
            if cur_judge[name] + tolerance < float(base_judge[name]):
                problems.append(
                    f"L3 指标 {name} 下降：{cur_judge[name]} < 基线 {base_judge[name]}"
                )

    return (not problems), problems
