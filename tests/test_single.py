"""单职位分析流水线的测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from jobcopilot.core.analyzers.single import STEP_PROMPT_FILES, SingleJobAnalyzer
from jobcopilot.core.json_utils import parse_json
from jobcopilot.core.messages import Message
from jobcopilot.core.prompts import PromptResolver
from tests.conftest import FakeLLM

# 每步一个合法 JSON，7 步共 7 次调用
_STEP_JSON = [
    '{"core_requirements": ["Python"]}',
    '{"priority": ["RAG"]}',
    '{"questions": ["Q1"]}',
    '{"gaps": ["向量库"]}',
    '{"advice": ["补项目"]}',
    '{"iterations": ["加评测"]}',
    '{"strategy": ["主攻大厂"]}',
]


@pytest.mark.asyncio
async def test_analyze_returns_seven_sections() -> None:
    """一次 analyze 返回七个步骤的结果，顺序与映射表一致。"""
    llm = FakeLLM(responses=list(_STEP_JSON))
    result = await SingleJobAnalyzer(llm).analyze(
        jd_text="招聘 Agent 开发工程师", user_profile="15 年经验"
    )
    assert set(result) == set(STEP_PROMPT_FILES)
    assert result["job_analysis"] == {"core_requirements": ["Python"]}
    assert result["job_strategy"] == {"strategy": ["主攻大厂"]}
    assert len(llm.calls) == 7


@pytest.mark.asyncio
async def test_analyze_uses_system_and_user_messages() -> None:
    """每次调用是 system(提示词) + user(固定指令) 两条消息。"""
    llm = FakeLLM(responses=list(_STEP_JSON))
    await SingleJobAnalyzer(llm).analyze(jd_text="JD 正文")
    role, messages = llm.calls[0]
    assert role == "job_analysis"
    assert [m.role for m in messages] == ["system", "user"]
    assert "请严格按输出格式返回 JSON" in messages[1].content


@pytest.mark.asyncio
async def test_analyze_substitutes_placeholders() -> None:
    """提示词里的 {{jd_text}} / {{job_meta}} 被真实值替换。"""
    llm = FakeLLM(responses=list(_STEP_JSON))
    await SingleJobAnalyzer(llm).analyze(
        jd_text="唯一的JD正文标记", job_meta={"company": "某公司"}
    )
    assert "唯一的JD正文标记" in llm.prompts[0]
    assert "某公司" in llm.prompts[0]
    assert "{{jd_text}}" not in llm.prompts[0]


@pytest.mark.asyncio
async def test_local_dir_missing_file_falls_back_to_base(tmp_path: Path) -> None:
    """默认（include_base=True）下本地缺文件时回落到包内 base，保证报告不缺块。"""
    llm = FakeLLM(responses=list(_STEP_JSON))
    analyzer = SingleJobAnalyzer(llm, prompt_dir=str(tmp_path))
    assert analyzer.available_steps == sorted(STEP_PROMPT_FILES)
    await analyzer.analyze(jd_text="JD")
    assert len(llm.calls) == 7


@pytest.mark.asyncio
async def test_analyze_empty_jd_raises() -> None:
    """空 JD 直接抛 ValueError（不浪费 7 次 LLM 调用）。"""
    with pytest.raises(ValueError, match="jd_text 不能为空"):
        await SingleJobAnalyzer(FakeLLM()).analyze(jd_text="   ")


@pytest.mark.asyncio
async def test_analyze_degrades_on_llm_error() -> None:
    """LLM 抛异常时该步骤降级为空结构，其余步骤继续跑完。"""
    llm = FakeLLM(raises=RuntimeError("boom"))
    result = await SingleJobAnalyzer(llm).analyze(jd_text="JD")
    assert all(v == {} for v in result.values())
    assert len(llm.calls) == 7


@pytest.mark.asyncio
async def test_analyze_degrades_on_bad_json() -> None:
    """返回非 JSON 时降级为空结构，不抛异常。"""
    llm = FakeLLM(default="这不是 JSON")
    result = await SingleJobAnalyzer(llm).analyze(jd_text="JD")
    assert result["job_analysis"] == {}


@pytest.mark.asyncio
async def test_missing_prompt_skips_step(tmp_path: Path) -> None:
    """严格模式（include_base=False）下提示词全缺 → 不调用 LLM，返回全空结构。"""
    llm = FakeLLM()
    analyzer = SingleJobAnalyzer(
        llm, resolver=PromptResolver(local_dir=tmp_path, include_base=False)
    )
    result = await analyzer.analyze(jd_text="JD")
    assert analyzer.available_steps == []
    assert llm.calls == []
    assert all(v == {} for v in result.values())


@pytest.mark.asyncio
async def test_partial_prompt_load(tmp_path: Path) -> None:
    """只提供部分提示词时，仅加载到的那一步会真正调用 LLM。"""
    (tmp_path / "02_job_analysis.md").write_text("只用 {{jd_text}}", encoding="utf-8")
    llm = FakeLLM(responses=['{"ok": true}'])
    analyzer = SingleJobAnalyzer(
        llm, resolver=PromptResolver(local_dir=tmp_path, include_base=False)
    )
    assert analyzer.available_steps == ["job_analysis"]
    result = await analyzer.analyze(jd_text="JD")
    assert result["job_analysis"] == {"ok": True}
    assert len(llm.calls) == 1


class _ContentObj:
    """模拟 langchain 响应的 ``.content`` 属性。"""

    def __init__(self, content: str) -> None:
        self.content = content


@pytest.mark.asyncio
async def test_analyze_accepts_content_object_response() -> None:
    """LLM 返回带 .content 的对象时也能正确取文本。"""

    class ObjLLM:
        async def complete(self, role: str, messages: list[Message]) -> object:
            return _ContentObj('{"from": "object"}')

    result = await SingleJobAnalyzer(ObjLLM()).analyze(jd_text="JD")  # type: ignore[arg-type]
    assert result["job_analysis"] == {"from": "object"}


# ---------- JSON 解析 ----------


def test_parse_json_strips_fences_by_default() -> None:
    """默认剥掉 markdown 代码围栏。"""
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('```\n{"a": 2}\n```') == {"a": 2}


def test_parse_json_ignores_surrounding_text() -> None:
    """只取首个 { 到末个 } 之间的内容。"""
    assert parse_json('好的，结果如下：{"a": 1} 以上。') == {"a": 1}


def test_parse_json_no_fence_strip_when_disabled() -> None:
    """strip_fences=False 时不剥围栏（批量流程的历史行为）。"""
    assert parse_json("```json\n{\"a\": 1}\n```", strip_fences=False) == {"a": 1}
    # 围栏在 JSON 之外且被 {} 包住时会失败——这正是两者的行为差异
    assert parse_json("```json\n{\"a\": 1}\n```extra", strip_fences=False) == {"a": 1}


def test_parse_json_returns_empty_on_garbage() -> None:
    """解析不了就返回空字典，绝不抛异常。"""
    assert parse_json("没有大括号") == {}
    assert parse_json("{坏 JSON}") == {}
    assert parse_json("") == {}
    assert parse_json("{") == {}
