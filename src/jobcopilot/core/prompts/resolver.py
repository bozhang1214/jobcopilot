"""提示词解析（多级回退）。

解析优先级（**从高到低**，首个命中即返回）::

    1. 请求级 override   —— 调用方直接传文本（临时实验 / A-B 测试用）
    2. 本地目录          —— 宿主的 prompt 目录（SEKB 传 prompt/job，保留现网可改）
    3. 包内 packs/<职能族> —— 按职能细分的提示词包（P1 起提供内容）
    4. 包内 base          —— 通用提示词（随包发布，兜底）

「职能族」而非「行业」作为分层轴：同一职能（售前 / 产品 / 研发）在不同行业
面对的 JD 结构高度相似，而不同职能即便同行业也差异巨大。
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

__all__ = ["PromptResolver", "base_dir", "packs_dir", "prompt_source_of"]

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


def prompt_source_of(filename: str, resolver: "PromptResolver | None" = None) -> str:
    """返回某提示词最终来自哪一级（便于排障 / 观测）。

    Returns:
        ``"override"`` / ``"local"`` / ``"pack"`` / ``"base"`` / ``"missing"``。
    """
    r = resolver or PromptResolver()
    return r.source_of(filename)


class PromptResolver:
    """按优先级查找提示词文件。

    Args:
        local_dir: 宿主本地提示词目录（可为 ``None``）。目录不存在时自动跳过该级。
        pack: 职能族名（对应 ``packs/<pack>/``）；``None`` 或目录不存在时跳过该级。
        overrides: 请求级覆盖，``{文件名: 正文}``。
        include_base: 是否允许回落到包内 ``base/``。默认 ``True``；
            宿主若要求「提示词目录里没有就必须跳过”，可设为 ``False`` 走严格模式。
    """

    def __init__(
        self,
        local_dir: str | Path | None = None,
        pack: str | None = None,
        overrides: Mapping[str, str] | None = None,
        include_base: bool = True,
    ) -> None:
        self._local = Path(local_dir) if local_dir else None
        self._pack = pack
        self._overrides: Mapping[str, str] = overrides or {}
        self._include_base = include_base

    # ---------- 查询 ----------

    def resolve(self, filename: str) -> Path | None:
        """返回该提示词命中的文件路径；全部未命中返回 ``None``。

        请求级 override 只在内存里，没有文件路径，因此本方法对它返回 ``None``，
        需要用 :meth:`get` 或 :meth:`source_of` 才能看到 override。
        """
        for path, _ in self._candidates(filename):
            if path.exists():
                return path
        return None

    def get(self, filename: str) -> str:
        """读取提示词正文；缺失返回空串（调用方据此跳过该步骤）。"""
        if filename in self._overrides:
            return self._overrides[filename]
        path = self.resolve(filename)
        if path is None:
            return ""
        return path.read_text(encoding="utf-8")

    def source_of(self, filename: str) -> str:
        """返回命中的级别名：override / local / pack / base / missing。"""
        if filename in self._overrides:
            return "override"
        for path, level in self._candidates(filename):
            if path.exists():
                return level
        return "missing"

    def available(self) -> list[str]:
        """列出所有级别的可用提示词文件名（去重、排序）。"""
        names: set[str] = set(self._overrides)
        for root in self._search_roots():
            names.update(p.name for p in root.glob("*.md"))
        return sorted(names)

    # ---------- 内部 ----------

    def _candidates(self, filename: str) -> list[tuple[Path, str]]:
        """按优先级返回 ``(路径, 级别名)`` 列表。"""
        out: list[tuple[Path, str]] = []
        if self._local is not None:
            out.append((self._local / filename, "local"))
        if self._pack:
            out.append((_PACKS / self._pack / filename, "pack"))
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
