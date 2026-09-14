"""章节级合并（compose）的测试。"""
from __future__ import annotations

from jobcopilot.core.prompts.compose import (
    FULL_OVERRIDE_MARKER,
    compose,
    digest,
    section_key,
    split_blocks,
)

BASE = """# 标题

## 背景
原始背景

## 任务
任务引言

### 1. 第一节
第一节内容

### 2. 第二节
第二节内容

## 输出格式
```json
{"a": 1}
```
"""


def test_section_key_numbered_ignores_title_text() -> None:
    """数字标题只认序号 —— pack 可以自由改写标题文案仍能对上。"""
    assert section_key(3, "2. 技能栈「门槛线」") == "###:2"
    assert section_key(3, "2、完全不同的写法") == "###:2"
    assert section_key(3, "2 无标点") == "###:2"


def test_section_key_text_normalized() -> None:
    """非数字标题按规范化文字匹配（忽略空白与标点）。"""
    assert section_key(2, "背景") == "##:背景"
    assert section_key(2, "输出格式（严格 JSON）") == section_key(2, "输出格式严格JSON")


def test_split_blocks_flat() -> None:
    """按标题扁平成块：子标题各自成块，不并入父块。"""
    blocks = split_blocks(BASE)
    keys = [b.key for b in blocks]
    assert "#:标题" in keys
    assert "##:任务" in keys
    assert "###:1" in keys and "###:2" in keys
    assert "##:输出格式严格json只输出json" not in keys  # 该文件里没这句
    # 「## 任务」块只含引言，不含 ### 子节
    task = next(b for b in blocks if b.key == "##:任务")
    assert "任务引言" in task.text
    assert "第一节内容" not in task.text


def test_compose_replaces_only_listed_sections() -> None:
    """只替换 pack 写到的章节，其余原样保留。"""
    override = "### 2. 第二节（改写版）\n新内容\n"
    merged, report = compose(BASE, override)
    assert "新内容" in merged
    assert "第二节内容" not in merged
    # 未覆盖的章节必须原样保留
    assert "原始背景" in merged
    assert "第一节内容" in merged
    assert '{"a": 1}' in merged
    assert report.matched == ["###:2"]
    assert report.appended == []


def test_compose_appends_new_section_after_last_replaced() -> None:
    """pack 新增的章节插在最后一个被覆盖章节之后，而不是文件末尾。

    这条很关键：否则新章节会掉到 JSON 输出骨架后面，读起来莫名其妙。
    """
    override = "### 2. 改写\nX\n\n### 3. 新增章节\nY\n"
    merged, report = compose(BASE, override)
    assert report.appended == ["###:3"]
    assert merged.index("### 3. 新增章节") < merged.index("## 输出格式")


def test_compose_full_override_marker() -> None:
    """带 override: full 标记时整段替换，不做章节合并。"""
    override = f"{FULL_OVERRIDE_MARKER}\n# 全新提示词\n全部内容\n"
    merged, report = compose(BASE, override)
    assert report.full_replace is True
    assert merged.startswith("# 全新提示词")
    assert "原始背景" not in merged
    assert FULL_OVERRIDE_MARKER not in merged


def test_compose_preserves_unmatched_base_sections() -> None:
    """pack 只给一个章节时，base 其余内容一字不动。"""
    merged, _ = compose(BASE, "## 背景\n新背景\n")
    assert "新背景" in merged
    assert "原始背景" not in merged
    assert "第一节内容" in merged
    assert "第二节内容" in merged


def test_compose_output_ends_with_newline() -> None:
    """合并结果以换行结尾（避免与后续拼接粘连）。"""
    merged, _ = compose(BASE, "## 背景\nX\n")
    assert merged.endswith("\n")


def test_digest_is_stable_and_content_sensitive() -> None:
    """指纹稳定、内容变则变。"""
    assert digest("abc") == digest("abc")
    assert digest("abc") != digest("abd")
    assert len(digest("abc")) == 16


def test_compose_preamble_is_preserved() -> None:
    """标题之前的散落正文不会被丢掉。"""
    base = "开头说明\n\n## 一\n内容\n"
    merged, _ = compose(base, "## 一\n新内容\n")
    assert "开头说明" in merged
    assert "新内容" in merged


def test_compose_headingless_override_is_full_replacement() -> None:
    """覆盖片段里没有章节标题时，按「完整提示词」处理而不是静默丢弃。

    早期版本按章节合并处理这种输入：一个标题都匹配不上 → 内容全被丢掉，
    而且**不报错**（典型的静默失效）。
    """
    merged, report = compose(BASE, "这是一份没有标题的完整提示词")
    assert "这是一份没有标题的完整提示词" in merged
    assert "原始背景" not in merged
    assert report.full_replace is True


def test_compose_empty_override_keeps_base() -> None:
    """空白覆盖片段 → 原样返回 base，不做任何破坏。"""
    merged, report = compose(BASE, "   \n\n  ")
    assert merged == BASE
    assert report.full_replace is False
