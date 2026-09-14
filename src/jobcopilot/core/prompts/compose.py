"""提示词章节级合并：让职能 pack **只写差异段落**，其余继承 base。

为什么需要它（而不是整文件替换）
--------------------------------
现网提示词是「叙述 + 严格 JSON 输出骨架」混在一个文件里。若 pack 走整文件替换：

- 每个 pack 都要复制一遍 JSON 骨架 → **schema 漂移风险**（改 base 忘改 pack）；
- 3 个 pack × 若干提示词 → 维护量成倍。

章节级合并把**叙述部分**（可自由改写）与**输出骨架**（必须一致）解耦：
pack 只覆盖 `## 任务` 下的各 `###` 小节，JSON 骨架永远来自 base。

匹配规则
--------
每个标题切一个「块」（扁平切分——子标题各自成块，不并入父块）。块的 key：

- 标题以数字开头（``### 2. 技能栈…``）→ key 用 ``###:2``，**只认序号**
  （这样 pack 可以自由改写标题文案，例如「技能栈（售前版）」仍能对上）；
- 其他标题 → key 用 ``##:<规范化标题>``。

匹配不上的 pack 块**追加到末尾**并计入报告（不静默丢弃），便于 pack 新增小节；
调用方/测试可据此断言「该匹配的都匹配上了」。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 包内提示词若以此开头，表示「整文件替换」而非章节合并（逃生舱）
FULL_OVERRIDE_MARKER = "<!-- override: full -->"

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_NUMBERED_RE = re.compile(r"^(\d+)\s*[.、)）]?\s*(.*)$")
_PUNCT_RE = re.compile(r"[\s　:：,，.。、()（）「」【】\[\]\"'`]+")


@dataclass(frozen=True)
class Block:
    """一个标题块（标题行 + 到下一个标题为止的正文）。"""

    level: int
    title: str
    key: str
    lines: tuple[str, ...]

    @property
    def text(self) -> str:
        """块原文（含标题行与末尾换行）。"""
        return "".join(self.lines)


@dataclass
class ComposeReport:
    """合并结果说明（供日志 / 测试断言 / 可追溯性使用）。"""

    matched: list[str] = field(default_factory=list)
    appended: list[str] = field(default_factory=list)
    full_replace: bool = False

    @property
    def ok(self) -> bool:
        """是否所有 override 块都对上了 base（追加块视为「新增小节」，也算正常）。"""
        return not self.appended or True


def section_key(level: int, title: str) -> str:
    """把标题转成匹配 key。

    数字标题只认序号（``###:2``），非数字标题用规范化后的文字。
    """
    prefix = "#" * level
    m = _NUMBERED_RE.match(title.strip())
    if m:
        return f"{prefix}:{m.group(1)}"
    normalized = _PUNCT_RE.sub("", title.strip().lower())
    return f"{prefix}:{normalized}"


def split_blocks(text: str) -> list[Block]:
    """把 markdown 按标题扁平成块（子标题各自成块，不并入父块）。

    标题之前的正文（若有）作为 level=0 的「前言块」保留，保证合并后不丢内容。
    """
    lines = text.splitlines(keepends=True)
    blocks: list[Block] = []
    current: list[str] = []
    current_meta: tuple[int, str, str] | None = None

    def flush() -> None:
        nonlocal current, current_meta
        if current:
            level, title, key = current_meta or (0, "", "__preamble__")
            blocks.append(Block(level=level, title=title, key=key, lines=tuple(current)))
        current, current_meta = [], None

    for line in lines:
        m = _HEADING_RE.match(line.rstrip("\n"))
        if m:
            flush()
            level = len(m.group(1))
            title = m.group(2).strip()
            current_meta = (level, title, section_key(level, title))
            current = [line]
        else:
            current.append(line)
    flush()
    return blocks


def compose(base_text: str, override_text: str) -> tuple[str, ComposeReport]:
    """把 override 的块并入 base，返回 ``(合并后文本, 合并报告)``。

    Args:
        base_text: 基础提示词全文（通常是包内 ``base/``）。
        override_text: pack 提供的覆盖片段；含 ``<!-- override: full -->`` 时整段替换。

    Returns:
        合并后的提示词正文与 :class:`ComposeReport`（哪些块被替换、哪些被追加）。
    """
    report = ComposeReport()

    if FULL_OVERRIDE_MARKER in override_text:
        report.full_replace = True
        cleaned = override_text.replace(FULL_OVERRIDE_MARKER, "", 1).lstrip("\n")
        return cleaned, report

    base_blocks = split_blocks(base_text)
    override_blocks = split_blocks(override_text)
    overrides = {b.key: b for b in override_blocks if b.key != "__preamble__"}

    # 覆盖片段里**一个章节标题都没有** → 它本来就不是「章节覆盖」，而是完整提示词。
    # 若按章节合并处理，这段内容会被静默丢弃（早期版本就踩过这个坑）。
    # 整文件替换一律**原样返回**，保证与「本地目录整文件优先」的语义一致。
    if not overrides:
        if override_text.strip():
            report.full_replace = True
            return override_text, report
        return base_text, report

    out: list[str] = []
    last_replaced_at: int | None = None
    for b in base_blocks:
        if b.key in overrides:
            out.append(overrides.pop(b.key).text)
            report.matched.append(b.key)
            last_replaced_at = len(out) - 1
        else:
            out.append(b.text)

    # 未匹配上的 override 块（pack 新增的小节）插到**最后一个被覆盖的块之后**，
    # 而不是文件末尾——否则会掉到 JSON 输出骨架后面，读起来莫名其妙。
    if overrides:
        insert_at = len(out) if last_replaced_at is None else last_replaced_at + 1
        extra = [b.text for b in overrides.values()]
        out[insert_at:insert_at] = extra
        report.appended.extend(overrides.keys())

    merged = "".join(out)
    if not merged.endswith("\n"):
        merged += "\n"
    return merged, report


def digest(text: str) -> str:
    """提示词内容指纹（sha256 前 16 位），用于基线与可追溯性。"""
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
