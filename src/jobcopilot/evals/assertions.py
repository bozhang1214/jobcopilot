"""L1 · 程序化断言（**零 LLM**，每次必跑）。

这是整条评估链里性价比最高的一层：不花钱、可精确断言、能挡住绝大多数低级回归。

设计原则
--------
只断言**确定性的东西**：报告的字段与类型、统计口径（程序算的，不是 LLM 生成的）、
段落的非空性、模型输出是否符合提示词里声明的 JSON 骨架。
**不对内容质量下断言**——那是 L3 的活。

如果这些断言失败，一定是 bug（代码或提示词骨架坏了），不是「模型发挥不好」。
"""
from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from jobcopilot.core.models import normalize_job
from jobcopilot.core.stats import compute_stats

#: 批量报告必须存在的顶层字段
REQUIRED_REPORT_FIELDS: dict[str, type] = {
    "keyword": str,
    "city": str,
    "job_count": int,
    "stats": dict,
    "market": dict,
    "knowledge_iteration": dict,
    "jobs": list,
}

#: 单职位分析必须产出的 7 个段落
REQUIRED_SINGLE_SECTIONS = (
    "job_analysis",
    "knowledge_priority",
    "interview_qa",
    "gap_analysis",
    "resume_advice",
    "project_iteration",
    "job_strategy",
)

_JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)


@dataclass
class AssertionResult:
    """单条断言的结果。"""

    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转成可序列化字典。"""
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class AssertionSuite:
    """一组断言的结果集合。"""

    results: list[AssertionResult] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        """追加一条断言结果。"""
        self.results.append(AssertionResult(name=name, passed=passed, detail=detail))

    @property
    def passed(self) -> bool:
        """是否全部通过。"""
        return all(r.passed for r in self.results)

    @property
    def pass_rate(self) -> float:
        """通过率（无断言时视为 1.0）。"""
        if not self.results:
            return 1.0
        return sum(1 for r in self.results if r.passed) / len(self.results)

    def failures(self) -> list[AssertionResult]:
        """返回失败的断言。"""
        return [r for r in self.results if not r.passed]

    def to_dict(self) -> dict[str, Any]:
        """转成可序列化字典。"""
        return {
            "passed": self.passed,
            "pass_rate": round(self.pass_rate, 4),
            "results": [r.to_dict() for r in self.results],
        }


# ============================================================
# 断言实现
# ============================================================


def schema_valid(report: Mapping[str, Any]) -> AssertionResult:
    """报告顶层字段齐全且类型正确。"""
    missing = [k for k, t in REQUIRED_REPORT_FIELDS.items() if k not in report]
    if missing:
        return AssertionResult("schema_valid", False, f"缺少字段: {missing}")
    wrong = [
        f"{k}: 期望 {t.__name__}，实际 {type(report[k]).__name__}"
        for k, t in REQUIRED_REPORT_FIELDS.items()
        if not isinstance(report[k], t)
    ]
    if wrong:
        return AssertionResult("schema_valid", False, "类型错误: " + "; ".join(wrong))
    return AssertionResult("schema_valid", True)


def job_count_match(report: Mapping[str, Any]) -> AssertionResult:
    """``job_count`` 必须等于报告里实际回带的职位数。"""
    actual = len(report.get("jobs") or [])
    declared = report.get("job_count")
    ok = declared == actual
    return AssertionResult(
        "job_count_match", ok, "" if ok else f"job_count={declared} 但 jobs 有 {actual} 条"
    )


def stats_exact_match(report: Mapping[str, Any]) -> AssertionResult:
    """报告里的 ``stats`` 必须与**程序重算**的结果完全一致。

    ``stats`` 是代码算出来的（不是 LLM 生成的），所以可以精确断言；
    LLM 一旦篡改或代码口径漂移，这里立刻红。
    """
    jobs = [normalize_job(j) for j in (report.get("jobs") or [])]
    expected = compute_stats(jobs)
    actual = report.get("stats")
    if actual == expected:
        return AssertionResult("stats_exact_match", True)
    # 给出人可读的差异定位
    diffs = [
        f"{key}: 期望 {expected.get(key)!r}，实际 {(actual or {}).get(key)!r}"
        for key in expected
        if (actual or {}).get(key) != expected.get(key)
    ]
    return AssertionResult("stats_exact_match", False, "; ".join(diffs)[:400])


def sections_complete(result: Mapping[str, Any]) -> AssertionResult:
    """单职位分析 7 段都必须产出且非空（各段独立降级，但不应整段丢失）。"""
    empty = [s for s in REQUIRED_SINGLE_SECTIONS if not result.get(s)]
    if empty:
        return AssertionResult("sections_complete", False, f"空段落: {empty}")
    return AssertionResult("sections_complete", True)


def enum_valid(payload: Any, spec: dict[str, set[str]], path: str = "") -> AssertionResult:
    """校验 ``payload`` 中指定路径的枚举字段取值合法。

    Args:
        payload: 待校验的对象（dict/list）。
        spec: ``{字段路径: 允许值集合}``，路径用 ``.`` 分隔，``[]`` 表示遍历列表元素。
              例如 ``{"plans[].status": {"planned", "applied"}}``。
        path: 内部递归用，调用方不用传。
    """
    problems: list[str] = []
    for dotted, allowed in spec.items():
        for value, where in _iter_values(payload, dotted.split("."), path):
            if value not in allowed:
                problems.append(f"{where}={value!r} 不在 {sorted(allowed)}")
    if problems:
        return AssertionResult("enum_valid", False, "; ".join(problems)[:300])
    return AssertionResult("enum_valid", True)


def prompt_schema_conformance(output: Mapping[str, Any], prompt: str) -> AssertionResult:
    """模型输出必须符合**提示词里声明的 JSON 骨架**。

    这是很有价值的一条：提示词骨架与代码消费字段之间最容易悄悄漂移
    （改了骨架忘改解析、或模型少给字段），这里直接把它们钉在一起。

    ⚠️ 提示词里的骨架常是**伪 JSON**（占位符不加引号，如 ``"job_count": 招聘量``），
    所以不能直接 ``json.loads``；:func:`extract_json_skeleton` 用深度感知扫描
    只取「字段名 → 粗类型」，占位符一律记为 ``any``（只校验存在性，不校验类型）。
    """
    skeleton = extract_json_skeleton(prompt)
    if skeleton is None:
        return AssertionResult(
            "prompt_schema_conformance", True, "提示词未声明 JSON 骨架，跳过"
        )
    if not isinstance(output, dict) or not output:
        return AssertionResult("prompt_schema_conformance", False, "输出为空或非对象")

    missing = [k for k in skeleton if k not in output]
    if missing:
        return AssertionResult(
            "prompt_schema_conformance", False, f"缺少提示词声明的字段: {missing}"
        )
    type_bad = [
        f"{k}: 骨架 {skeleton[k]} vs 输出 {_type_name(output[k])}"
        for k in skeleton
        if skeleton[k] != "any"
        and not _kind_compatible(skeleton[k], output[k])
    ]
    if type_bad:
        return AssertionResult("prompt_schema_conformance", False, "; ".join(type_bad)[:300])
    return AssertionResult("prompt_schema_conformance", True)


def extract_json_skeleton(prompt: str) -> dict[str, str] | None:
    """从提示词里抽出**顶层字段 → 粗类型**的骨架（容忍伪 JSON）。

    不用 ``json.loads``：提示词骨架的占位符经常是裸词（``"job_count": 招聘量``），
    严格解析必然失败，而那恰恰是最需要校验的两条批量提示词。
    这里改用深度感知扫描，只在根对象一层收集 ``"key": <值>`` 并判断值的粗类型。

    Returns:
        ``{字段名: kind}``，kind ∈ ``str`` / ``number`` / ``bool`` / ``list`` / ``dict`` / ``any``；
        没有 JSON 代码块时返回 ``None``。
    """
    blocks = _JSON_BLOCK_RE.findall(prompt)
    if not blocks:
        return None
    # 多个 json 块时取最后一个（通常才是「输出格式」规范，前面可能是输入示例）
    text = blocks[-1]
    keys: dict[str, str] = {}
    depth = 0
    i = 0
    str_start = -1
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            if str_start < 0:
                str_start = i
            else:
                key = text[str_start + 1 : i]
                j = i + 1
                while j < len(text) and text[j] in " \t":
                    j += 1
                if depth == 1 and j < len(text) and text[j] == ":":
                    v = j + 1
                    while v < len(text) and text[v] in " \t\r\n":
                        v += 1
                    keys[key] = _value_kind(text, v)
                str_start = -1
        elif str_start < 0:
            if c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
        i += 1
    return keys or None


def build_stub_payload(skeleton: dict[str, str]) -> dict[str, Any]:
    """按骨架造一个**类型合规**的最小对象（供零成本 stub LLM 使用）。"""
    defaults: dict[str, Any] = {
        "str": "",
        "number": 0,
        "bool": False,
        "list": [],
        "dict": {},
        "any": "",
    }
    return {k: defaults.get(kind, "") for k, kind in skeleton.items()}


# ============================================================
# 内部工具
# ============================================================


def _type_name(v: Any) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "dict"
    if isinstance(v, str):
        return "str"
    if isinstance(v, (int, float)):
        return "number"
    return type(v).__name__


def _value_kind(text: str, index: int) -> str:
    """看冒号后第一个有效字符，判断值的粗类型；裸占位符（如「招聘量」）记为 any。"""
    if index >= len(text):
        return "any"
    ch = text[index]
    if ch == "[":
        return "list"
    if ch == "{":
        return "dict"
    if ch == '"':
        return "str"
    if ch.isdigit() or ch == "-":
        return "number"
    if text.startswith("true", index) or text.startswith("false", index):
        return "bool"
    return "any"


def _kind_compatible(kind: str, output_value: Any) -> bool:
    """骨架 kind 与输出实际类型是否兼容（只做粗粒度比对）。"""
    return kind == _type_name(output_value)


def _iter_values(payload: Any, parts: list[str], path: str) -> Iterator[tuple[Any, str]]:
    """按路径遍历取值，产出 ``(值, 位置描述)``。"""
    if not parts:
        yield payload, path or "<root>"
        return
    head, rest = parts[0], parts[1:]
    if head.endswith("[]"):
        key = head[:-2]
        seq = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(seq, list):
            for i, item in enumerate(seq):
                yield from _iter_values(item, rest, f"{path}.{key}[{i}]".lstrip("."))
        return
    if isinstance(payload, dict) and head in payload:
        yield from _iter_values(payload[head], rest, f"{path}.{head}".lstrip("."))
