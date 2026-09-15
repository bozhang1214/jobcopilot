"""`max_chars`（输出长度约束）的测试：**约束交给 LLM，不做任何事后裁剪**。

设计背景见 ``core/prompts/budget.py``；这里锁三件事：

1. 预算换算正确（总预算均摊到段数，且有下限）；
2. 约束块**真的被拼进系统提示词**（含算出来的每段预算数字）——否则参数就是个摆设；
3. 参数非法时给可操作错误，而不是静默忽略；
4. **没有任何裁剪行为**：输出原样返回（长度由模型负责，不由我们截）。
"""
from __future__ import annotations

import pytest

from jobcopilot.core.prompts.budget import budget_instruction, per_part_budget, with_budget
from jobcopilot.mcp.config import ServerConfig
from jobcopilot.mcp.tools import (
    MAX_MAX_CHARS,
    MIN_MAX_CHARS,
    ToolContext,
    ToolError,
    analyze_job,
    analyze_jobs_batch,
    normalize_max_chars,
)
from tests.conftest import FakeLLM

# ---------- 1) 预算换算 ----------


def test_per_part_budget_splits_evenly() -> None:
    # 计入 SAFETY_FACTOR=0.8：7000*0.8/7=800、6000*0.8/2=2400
    assert per_part_budget(7000, 7) == 800
    assert per_part_budget(6000, 2) == 2400


def test_per_part_budget_respects_floor() -> None:
    """总预算很小时不能算出不可用的数字（下限兜底）。"""
    assert per_part_budget(70, 7, floor=200) == 200
    assert per_part_budget(300, 7, floor=300) == 300


def test_safety_factor_is_applied() -> None:
    """安全系数必须生效：模型对精确字符数不精确（实测 3000 预算输出 3291，超 10%）。"""
    from jobcopilot.core.prompts.budget import SAFETY_FACTOR

    assert 0 < SAFETY_FACTOR < 1
    assert per_part_budget(1000, 1) == int(1000 * SAFETY_FACTOR)


def test_per_part_budget_handles_zero_parts() -> None:
    # 段数为 0 时按单段处理，同样计入安全系数
    assert per_part_budget(1000, 0) == 800


# ---------- 2) 约束文本 ----------


def test_budget_instruction_contains_limit_and_key_rules() -> None:
    text = budget_instruction(1200)
    assert "1200" in text
    # 关键要求必须在：保住 JSON 键结构、只能写短、不许截断
    assert "键结构必须完整保留" in text
    assert "把文本值写短" in text
    assert "不要截断 JSON" in text


def test_with_budget_is_noop_without_limit() -> None:
    assert with_budget("原始提示词", None) == "原始提示词"
    assert with_budget("原始提示词", 0) == "原始提示词"


def test_with_budget_appends() -> None:
    out = with_budget("原始提示词", 500)
    assert out.startswith("原始提示词")
    assert "输出长度约束" in out


# ---------- 3) 参数校验 ----------


def test_normalize_max_chars_accepts_valid() -> None:
    assert normalize_max_chars(None) is None
    assert normalize_max_chars(3000) == 3000


def test_normalize_max_chars_rejects_bad_values() -> None:
    for bad in (True, "3000", 3.5):
        with pytest.raises(ToolError, match="必须是整数"):
            normalize_max_chars(bad)
    with pytest.raises(ToolError, match="太小"):
        normalize_max_chars(MIN_MAX_CHARS - 1)
    with pytest.raises(ToolError, match="过大"):
        normalize_max_chars(MAX_MAX_CHARS + 1)


# ---------- 4) 约束真的进了提示词 ----------


def _ctx(tmp_path, llm: FakeLLM) -> ToolContext:
    cfg = ServerConfig(
        prompts_dir=tmp_path / "prompts",
        data_dir=tmp_path,
        profile_path=tmp_path / "profile.txt",
    )
    return ToolContext(llm=llm, config=cfg)


def _system_texts(llm: FakeLLM) -> list[str]:
    """取出每次 LLM 调用里的 system 正文（``Message`` 是带 role/content 的数据类）。"""
    return [m.content for _, msgs in llm.calls for m in msgs if m.role == "system"]


@pytest.mark.asyncio
async def test_analyze_job_injects_budget_into_every_step(tmp_path) -> None:
    """单职位 7 段，每段的系统提示词里都要带预算（含每段分到的数字）。"""
    llm = FakeLLM(default='{"ok": true}')
    await analyze_job(
        _ctx(tmp_path, llm),
        jd_text="【测试】某公司 | 20-30K | 北京\n职责：写代码。",
        max_chars=7000,
    )
    systems = [t for t in _system_texts(llm) if "输出长度约束" in t]
    assert systems, "没有任何一步带上了长度约束"
    # 7000 * 0.8 / 7 = 800（含安全系数）
    assert all("不得超过 800 个字符" in t for t in systems)


@pytest.mark.asyncio
async def test_analyze_job_without_budget_has_no_constraint(tmp_path) -> None:
    """不传 max_chars 时不得擅自加约束（默认行为不变）。"""
    llm = FakeLLM(default='{"ok": true}')
    await analyze_job(_ctx(tmp_path, llm), jd_text="【测试】某公司 | 20-30K | 北京")
    assert all("输出长度约束" not in t for t in _system_texts(llm))


@pytest.mark.asyncio
async def test_analyze_jobs_batch_injects_budget(tmp_path) -> None:
    """批量两路各摊一半（6000 * 0.8 / 2 = 2400）。"""
    llm = FakeLLM(default='{"ok": true}')
    await analyze_jobs_batch(
        _ctx(tmp_path, llm),
        jobs=[{"title": "A", "jd_text": "x"}, {"title": "B", "jd_text": "y"}],
        max_chars=6000,
    )
    systems = [t for t in _system_texts(llm) if "输出长度约束" in t]
    assert systems, "批量两路都没带上长度约束"
    assert all("不得超过 2400 个字符" in t for t in systems)


def test_no_output_truncation_logic() -> None:
    """守住设计决定：**不做事后裁剪**。

    如果将来有人给输出加了按字符裁剪，这条会失败并提醒他：按字节裁剪会产出非法 JSON，
    长度应由提示词交给模型负责（见 ``core/prompts/budget.py``）。
    """
    import inspect

    from jobcopilot.core.analyzers import batch as batch_mod
    from jobcopilot.core.analyzers import single as single_mod

    for mod in (batch_mod, single_mod):
        src = inspect.getsource(mod)
        for pattern in ("[:max_chars]", "[0:max_chars]", "max_chars]", "truncate"):
            assert pattern not in src, f"{mod.__name__} 里出现了可疑的裁剪逻辑：{pattern}"
        # 只允许把 max_chars 交给提示词构造器
        assert "with_budget(" in src or "per_part_budget(" in src
