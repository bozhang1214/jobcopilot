"""提示词解析与内置提示词包的测试。

其中 :func:`test_base_matches_sekb_prompt_dir` 是**跨仓库一致性护栏**：
只要 SEKB 现网的 ``prompt/job/`` 与包内 ``base/`` 出现任何字节差异，
SEKB 抽取后行为就可能改变——这条测试会立刻报警。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from jobcopilot.core.prompts import PromptResolver, base_dir, packs_dir


def _find_sekb_prompt_dir() -> Path | None:
    """定位 SEKB 现网的 ``prompt/job/`` 目录。

    兼容两种工作副本布局（两种布局下测试都要能跑）：

    - 嵌套：``<SEKB>/jobcopilot/`` → 兄弟目录 ``<SEKB>/prompt/job``
    - 同级：``<VSCodeSpace>/jobcopilot/`` → ``<VSCodeSpace>/SelfEvolvingKnowledgeBase/prompt/job``

    也支持用 ``SEKB_REPO`` 环境变量显式指定。
    """
    override = os.environ.get("SEKB_REPO")
    if override:
        p = Path(override) / "prompt" / "job"
        return p if p.exists() else None

    repo_root = Path(__file__).resolve().parents[1]  # jobcopilot/
    candidates = [
        repo_root.parent / "prompt" / "job",  # 嵌套：SEKB/jobcopilot/
        repo_root.parent / "SelfEvolvingKnowledgeBase" / "prompt" / "job",  # 同级
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


_SEKB_PROMPT_DIR = _find_sekb_prompt_dir()


def test_base_dir_contains_expected_prompts() -> None:
    """包内必须带齐 7 个流水线步骤提示词 + 批量分析两路提示词。"""
    names = {p.name for p in base_dir().glob("*.md")}
    for required in [
        "02_job_analysis.md",
        "03_knowledge_priority.md",
        "04_interview_qa.md",
        "05_gap_analysis.md",
        "06_resume_advice.md",
        "08_project_iteration.md",
        "09_job_strategy.md",
        "批量职位分析.md",
        "职位知识迭代.md",
    ]:
        assert required in names, f"包内缺少提示词 {required}"


def test_base_has_no_readme_profile() -> None:
    """防泄漏护栏：SEKB 的 ``prompt/job/README.md`` 内嵌了真实个人画像
    （姓名 / 前雇主 / 年龄 / 薪资），**绝不能**随开源包发布。

    这里用「文件名黑名单」而不是「内容关键词」来守——把真实姓名写进断言本身
    就是一次泄漏（这个测试的第一版就犯过这个错）。
    内容层面的兜底走 :func:`test_base_matches_sekb_prompt_dir`：它要求包内每个
    文件都必须能在 SEKB 的 ``prompt/job/`` 里找到同名同字节的来源，且显式声明
    README.md 属于「SEKB 独有、不得进包」。
    """
    assert not (base_dir() / "README.md").exists(), (
        "base/ 里出现 README.md —— 该文件含个人画像，不得进开源包"
    )


def test_base_has_no_personal_data() -> None:
    """可选的敏感词扫描：由 ``JOBCOPILOT_FORBIDDEN_TERMS`` 环境变量驱动。

    仓库里不写死任何真实姓名/公司名（写死即是泄漏）。本地或在 SEKB 侧运行 CI 时
    设置该变量即可获得强校验，例如::

        JOBCOPILOT_FORBIDDEN_TERMS="某公司,某姓名" pytest tests/test_prompts.py
    """
    raw = os.environ.get("JOBCOPILOT_FORBIDDEN_TERMS", "").strip()
    if not raw:
        pytest.skip("未设置 JOBCOPILOT_FORBIDDEN_TERMS，跳过敏感词扫描")
    forbidden = [t.strip() for t in raw.split(",") if t.strip()]
    for p in base_dir().glob("*.md"):
        text = p.read_text(encoding="utf-8")
        for word in forbidden:
            assert word not in text, f"{p.name} 命中敏感词，不得进开源包"


def test_packs_dir_exists() -> None:
    """packs 目录必须存在（内容 P1 补），否则解析器第 3 级形同虚设。"""
    assert packs_dir().exists()


def test_resolver_prefers_local_over_base(tmp_path: Path) -> None:
    """本地目录优先级高于包内 base。"""
    (tmp_path / "02_job_analysis.md").write_text("本地覆盖版本", encoding="utf-8")
    r = PromptResolver(local_dir=tmp_path)
    assert r.get("02_job_analysis.md") == "本地覆盖版本"
    assert r.source_of("02_job_analysis.md") == "local"


def test_resolver_falls_back_to_base(tmp_path: Path) -> None:
    """本地目录没有该文件时回落到包内 base。"""
    r = PromptResolver(local_dir=tmp_path)
    assert r.source_of("02_job_analysis.md") == "base"
    assert r.get("02_job_analysis.md")


def test_resolver_request_override_wins(tmp_path: Path) -> None:
    """请求级 override 优先级最高，压过本地目录。"""
    (tmp_path / "02_job_analysis.md").write_text("本地版本", encoding="utf-8")
    r = PromptResolver(local_dir=tmp_path, overrides={"02_job_analysis.md": "请求级版本"})
    assert r.get("02_job_analysis.md") == "请求级版本"
    assert r.source_of("02_job_analysis.md") == "override"


def test_resolver_pack_beats_base(tmp_path: Path) -> None:
    """职能族 pack 优先级高于通用 base。"""
    pack_dir = packs_dir() / "_test_pack"
    pack_dir.mkdir(parents=True, exist_ok=True)
    try:
        (pack_dir / "02_job_analysis.md").write_text("售前 pack 版本", encoding="utf-8")
        r = PromptResolver(pack="_test_pack")
        assert r.get("02_job_analysis.md") == "售前 pack 版本"
        assert r.source_of("02_job_analysis.md") == "pack"
    finally:
        (pack_dir / "02_job_analysis.md").unlink(missing_ok=True)
        pack_dir.rmdir()


def test_resolver_missing_returns_empty() -> None:
    """查不到的提示词返回空串，且来源标记为 missing（调用方据此跳过步骤）。"""
    r = PromptResolver()
    assert r.get("不存在的提示词.md") == ""
    assert r.source_of("不存在的提示词.md") == "missing"


def test_resolver_available_lists_all_levels(tmp_path: Path) -> None:
    """available 汇总各级文件名。"""
    (tmp_path / "自定义.md").write_text("x", encoding="utf-8")
    r = PromptResolver(local_dir=tmp_path)
    names = r.available()
    assert "自定义.md" in names
    assert "02_job_analysis.md" in names


@pytest.mark.skipif(
    _SEKB_PROMPT_DIR is None,
    reason="当前环境没有 SEKB 仓库检出，跳过跨仓库一致性校验",
)
def test_base_matches_sekb_prompt_dir() -> None:
    """包内 base 的每个提示词必须与 SEKB 现网 ``prompt/job/`` **字节一致**。

    这是 P0「行为逐字节不变」的核心护栏：抽取只搬位置、不改内容。
    个人数据文件 ``README.md`` 单独排除（见 test_base_has_no_personal_data）。
    """
    packaged = {p.name: p for p in base_dir().glob("*.md")}
    diffs: list[str] = []
    for name, src in packaged.items():
        sekb_file = _SEKB_PROMPT_DIR / name
        if not sekb_file.exists():
            diffs.append(f"{name}: SEKB 侧不存在（包内多出该文件）")
        elif sekb_file.read_bytes() != src.read_bytes():
            diffs.append(f"{name}: 字节不一致")
    # SEKB 侧独有的文件（README.md 等）不算差异，但需要显式知晓
    sekb_only = sorted(
        p.name for p in _SEKB_PROMPT_DIR.glob("*.md") if p.name not in packaged
    )
    assert not diffs, "包内提示词与 SEKB 现网不一致：" + "; ".join(diffs)
    assert "README.md" in sekb_only, "SEKB 侧的 README.md（个人画像）应保留在宿主，不进包"


def test_sekb_prompt_dir_if_present_is_usable() -> None:
    """SEKB 的 prompt 目录若存在，解析器应优先命中 local（保证现网可用）。"""
    if _SEKB_PROMPT_DIR is None:
        pytest.skip("无 SEKB 检出")
    r = PromptResolver(local_dir=os.fspath(_SEKB_PROMPT_DIR))
    assert r.source_of("批量职位分析.md") == "local"
    assert r.get("批量职位分析.md") == (_SEKB_PROMPT_DIR / "批量职位分析.md").read_text(
        encoding="utf-8"
    )
