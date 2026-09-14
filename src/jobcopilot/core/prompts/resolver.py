"""提示词解析（多级回退 + 职能 pack 章节级合并）。

解析优先级（**从高到低**）::

    1. 请求级 override   —— 调用方直接传文本（临时实验 / A-B 测试用）
    2. 本地目录          —— 宿主的 prompt 目录（SEKB 传 prompt/job，保留现网可改）
    3. 包内 packs/<职能族> —— 按职能细分；**只写差异章节**，其余继承 base
    4. 包内 base          —— 通用提示词（随包发布，兜底）

「职能族」而非「行业」作为分层轴：同一职能（售前 / 产品 / 研发）在不同行业
面对的 JD 结构高度相似，而不同职能即便同行业也差异巨大。

关于第 3 级的合并语义
--------------------
本地目录与请求级 override 都是**整文件生效**（现网行为，SEKB 依赖它）。
只有 pack 走**章节级合并**：pack 文件里写到的标题覆盖 base 的同名标题，
没写到的部分原样继承 base（尤其是 JSON 输出骨架，避免 schema 漂移）。
详见 :mod:`jobcopilot.core.prompts.compose`。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from jobcopilot.core.prompts.compose import compose, digest

__all__ = ["PromptResolver", "PromptMeta", "base_dir", "packs_dir", "prompt_source_of"]

#: 包内通用提示词目录
_BASE = Path(__file__).resolve().parent / "base"
#: 包内职能族提示词目录
_PACKS = Path(__file__).resolve().parent / "packs"


def base_dir() -> Path:
    """返回包内 ``base/`` 目录。"""
    return _BASE


def packs_dir() -> Path:
    """返回包内 ``packs/`` 目录。"""
    return _PACKS


def available_packs() -> list[str]:
    """列出包内所有职能族 pack 名（排序）。"""
    if not _PACKS.exists():
        return []
    return sorted(p.name for p in _PACKS.iterdir() if p.is_dir() and not p.name.startswith("_"))


def prompt_source_of(filename: str, resolver: "PromptResolver | None" = None) -> str:
    """返回某提示词最终来自哪一级（便于排障 / 观测）。"""
    r = resolver or PromptResolver()
    return r.source_of(filename)


@dataclass
class PromptMeta:
    """某条提示词的实际生效信息（**可追溯性**的核心载体）。

    Attributes:
        filename: 提示词文件名。
        source: ``override`` / ``local`` / ``pack+base`` / ``pack`` / ``base`` / ``missing``。
        pack: 生效的职能族名（无则空串）。
        digest: 最终正文的内容指纹（sha256 前 16 位）。
        overridden_sections: pack 覆盖掉的 base 章节 key 列表。
        appended_sections: pack 新增（base 里没有）的章节 key 列表。
    """

    filename: str
    source: str
    pack: str = ""
    digest: str = ""
    overridden_sections: list[str] = field(default_factory=list)
    appended_sections: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        """转成可 JSON 序列化的字典（写进报告用）。"""
        out: dict[str, object] = {
            "source": self.source,
            "digest": self.digest,
        }
        if self.pack:
            out["pack"] = self.pack
        if self.overridden_sections:
            out["overridden_sections"] = self.overridden_sections
        if self.appended_sections:
            out["appended_sections"] = self.appended_sections
        return out


class PromptResolver:
    """按优先级查找/组装提示词。

    Args:
        local_dir: 宿主本地提示词目录（可为 ``None``）。目录不存在时自动跳过该级。
        pack: 职能族名（对应 ``packs/<pack>/``）；``None`` 或目录不存在时跳过该级。
        overrides: 请求级覆盖，``{文件名: 正文}``。
        include_base: 是否允许回落到包内 ``base/``。默认 ``True``；
            宿主若要求「提示词目录里没有就必须跳过」，可设为 ``False`` 走严格模式。
    """

    def __init__(
        self,
        local_dir: str | Path | None = None,
        pack: str | None = None,
        overrides: Mapping[str, str] | None = None,
        include_base: bool = True,
    ) -> None:
        self._local = Path(local_dir) if local_dir else None
        self._pack = pack or None
        self._overrides: Mapping[str, str] = overrides or {}
        self._include_base = include_base

    @property
    def pack(self) -> str | None:
        """当前生效的职能族名。"""
        return self._pack

    # ---------- 查询 ----------

    def resolve(self, filename: str) -> Path | None:
        """返回该提示词**最高优先级**命中的文件路径；未命中返回 ``None``。

        注意：pack 是章节合并而非整文件生效，所以这里返回的可能是 pack 文件，
        但 :meth:`get` 给出的是「base + pack 合并后」的正文。
        """
        for path, _ in self._candidates(filename):
            if path.exists():
                return path
        return None

    def get(self, filename: str) -> str:
        """读取最终生效的提示词正文；缺失返回空串（调用方据此跳过该步骤）。"""
        if filename in self._overrides:
            return self._overrides[filename]

        local = self._local / filename if self._local else None
        if local is not None and local.exists():
            return local.read_text(encoding="utf-8")

        pack_path = self._pack_path(filename)
        base_path = self._base_path(filename)

        if pack_path is not None:
            pack_text = pack_path.read_text(encoding="utf-8")
            if base_path is not None:
                merged, _ = compose(base_path.read_text(encoding="utf-8"), pack_text)
                return merged
            return pack_text  # 没有 base 可合并 → pack 自带完整内容

        if base_path is not None:
            return base_path.read_text(encoding="utf-8")
        return ""

    def source_of(self, filename: str) -> str:
        """返回命中的级别名。

        Returns:
            ``override`` / ``local`` / ``pack+base`` / ``pack`` / ``base`` / ``missing``。
        """
        if filename in self._overrides:
            return "override"
        local = self._local / filename if self._local else None
        if local is not None and local.exists():
            return "local"
        pack_path = self._pack_path(filename)
        if pack_path is not None:
            return "pack+base" if self._base_path(filename) is not None else "pack"
        if self._base_path(filename) is not None:
            return "base"
        return "missing"

    def meta(self, filename: str) -> PromptMeta:
        """返回该提示词的可追溯信息（来源 / pack / 指纹 / 覆盖了哪些章节）。"""
        text = self.get(filename)
        m = PromptMeta(filename=filename, source=self.source_of(filename), digest=digest(text) if text else "")

        if self._pack and self.source_of(filename) in {"pack+base", "pack"}:
            m.pack = self._pack
            pack_path = self._pack_path(filename)
            base_path = self._base_path(filename)
            if pack_path is not None and base_path is not None:
                _, report = compose(base_path.read_text(encoding="utf-8"), pack_path.read_text(encoding="utf-8"))
                m.overridden_sections = sorted(report.matched)
                m.appended_sections = sorted(report.appended)
        return m

    def available(self) -> list[str]:
        """列出所有级别的可用提示词文件名（去重、排序）。"""
        names: set[str] = set(self._overrides)
        for root in self._search_roots():
            names.update(p.name for p in root.glob("*.md"))
        return sorted(names)

    # ---------- 内部 ----------

    def _pack_path(self, filename: str) -> Path | None:
        if not self._pack:
            return None
        p = _PACKS / self._pack / filename
        return p if p.exists() else None

    def _base_path(self, filename: str) -> Path | None:
        if not self._include_base:
            return None
        p = _BASE / filename
        return p if p.exists() else None

    def _candidates(self, filename: str) -> list[tuple[Path, str]]:
        """按优先级返回 ``(路径, 级别名)`` 列表。"""
        out: list[tuple[Path, str]] = []
        if self._local is not None:
            out.append((self._local / filename, "local"))
        if self._pack:
            out.append((_PACKS / self._pack / filename, "pack+base"))
        if self._include_base:
            out.append((_BASE / filename, "base"))
        return out

    def _search_roots(self) -> list[Path]:
        roots: list[Path] = []
        if self._local is not None and self._local.exists():
            roots.append(self._local)
        if self._pack and (_PACKS / self._pack).exists():
            roots.append(_PACKS / self._pack)
        if self._include_base and _BASE.exists():
            roots.append(_BASE)
        return roots
