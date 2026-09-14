"""JobCopilot 评估骨架（L1 程序化断言 / L2 基线回归 / L3 LLM-as-Judge）。"""
from jobcopilot.evals.assertions import AssertionResult, AssertionSuite
from jobcopilot.evals.metrics import JUDGE_METRICS, JudgeScore, judge_report, parse_judge_score
from jobcopilot.evals.runner import (
    DEFAULT_BASELINE,
    DEFAULT_DATASETS,
    EvalReport,
    SkeletonStubLLM,
    compare_with_baseline,
    load_datasets,
    run_eval,
)

__all__ = [
    "run_eval",
    "load_datasets",
    "compare_with_baseline",
    "SkeletonStubLLM",
    "EvalReport",
    "DEFAULT_BASELINE",
    "DEFAULT_DATASETS",
    "AssertionSuite",
    "AssertionResult",
    "JudgeScore",
    "parse_judge_score",
    "judge_report",
    "JUDGE_METRICS",
]
