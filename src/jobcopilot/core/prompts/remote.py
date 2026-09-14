"""提示词远端分发：从自建端点拉提示词，带**三级回退**与完整性校验。

为什么要远端分发
----------------
提示词会持续调优，但让用户为了换个提示词去升级整个 Python 包并不合理。
独立分发让「提示词」可以独立发版，也让非代码贡献者能参与。

三级回退（按顺序尝试）
----------------------
1. **自建主源**（Gitea → nginx 静态目录，见 ``DEFAULT_SOURCES``）
2. **GitHub 备源**（raw.githubusercontent，主源故障时不至于断供）
3. **包内兜底**（随包发布的那份，永远可用——断网也能工作）

完整性
------
manifest 里带每个文件的 **sha256**；下载后逐个校验，不一致就丢弃该文件并报错。
提示词会被注入到 LLM，被中间人篡改的后果比「下载失败」严重得多。

依赖：只用标准库 ``urllib``（core 层保持零第三方依赖）；异步调用方用
``asyncio.to_thread`` 包一层即可。
"""
from __future__ import annotations

import hashlib
import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobcopilot.core.logging import get_logger

logger = get_logger(__name__)

#: 自建主源（nginx 静态目录）→ GitHub 备源
DEFAULT_SOURCES: tuple[str, ...] = (
    "https://bos-studio.tech/prompts/",
    "https://raw.githubusercontent.com/bozhang1214/jobcopilot-prompts/main/",
)

#: 环境变量：覆盖整条源链（逗号分隔）
ENV_SOURCES = "JOBCOPILOT_PROMPTS_URL"

MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA = 1
DEFAULT_TIMEOUT = 20.0


class RemoteError(Exception):
    """远端分发相关错误（网络 / 清单格式 / 校验失败）。"""


@dataclass
class Manifest:
    """提示词清单。

    Attributes:
        version: 版本标签（人可读，用于展示与排查）。
        files: ``{相对路径: sha256}``。
        packs: 可用的职能族名。
        raw: 原始字典（保留未知字段，便于向后兼容）。
    """

    version: str = ""
    files: dict[str, str] = field(default_factory=dict)
    packs: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, data: dict[str, Any]) -> "Manifest":
        """从字典解析清单。

        Raises:
            RemoteError: 缺少必需字段（宁可报错，也不要静默拿到半份清单）。
        """
        files = data.get("files")
        if not isinstance(files, dict) or not files:
            raise RemoteError("清单缺少 files 字段或为空")
        schema = data.get("schema", MANIFEST_SCHEMA)
        if schema != MANIFEST_SCHEMA:
            raise RemoteError(
                f"清单 schema={schema} 与当前版本（{MANIFEST_SCHEMA}）不兼容，"
                "请升级 jobcopilot"
            )
        return cls(
            version=str(data.get("version", "")),
            files={str(k): str(v) for k, v in files.items()},
            packs=[str(p) for p in (data.get("packs") or [])],
            raw=data,
        )


def resolve_sources(explicit: str | None = None) -> list[str]:
    """返回要尝试的源列表：显式 > 环境变量 > 默认链。"""
    if explicit:
        return [explicit]
    import os

    env = os.environ.get(ENV_SOURCES, "").strip()
    if env:
        return [s.strip() for s in env.split(",") if s.strip()]
    return list(DEFAULT_SOURCES)


def _join(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def _ssl_context() -> "ssl.SSLContext | None":
    """构造 SSL 上下文；装了 certifi 就用它的 CA 包。

    为什么需要：macOS 上用 python.org 安装包时，系统 Python 常常没有可用 CA，
    表现为 ``CERTIFICATE_VERIFY_FAILED``。而 certifi 几乎总是随 httpx/requests
    一起装好，用它可以免去让用户手工跑 Install Certificates 命令。
    """
    try:
        import ssl

        try:
            import certifi

            return ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            return ssl.create_default_context()
    except Exception:  # noqa: BLE001 - 拿不到上下文就让 urlopen 用默认行为
        return None


def fetch_text(url: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """下载文本（UTF-8）。

    Raises:
        RemoteError: 网络错误或非 2xx。
    """
    req = urllib.request.Request(url, headers={"User-Agent": "jobcopilot"})
    try:
        with urllib.request.urlopen(  # noqa: S310 (固定 https 源)
            req, timeout=timeout, context=_ssl_context()
        ) as resp:
            # file:// 等非 HTTP 响应没有 status 属性（值为 None），不能直接比大小
            status = getattr(resp, "status", None)
            if status is not None and status >= 400:
                raise RemoteError(f"HTTP {status}: {url}")
            text: str = resp.read().decode("utf-8")
            return text
    except urllib.error.HTTPError as e:
        raise RemoteError(f"HTTP {e.code}: {url}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        msg = str(e)
        if "CERTIFICATE_VERIFY_FAILED" in msg:
            msg += "；本机缺少可用 CA 证书，可执行 pip install certifi 后重试（macOS 常见）"
        raise RemoteError(f"网络不可达: {url}（{msg}）") from e


def fetch_manifest(source: str, timeout: float = DEFAULT_TIMEOUT) -> Manifest:
    """从某个源拉并解析 manifest。"""
    return Manifest.parse(json.loads(fetch_text(_join(source, MANIFEST_NAME), timeout)))


def sha256_of(text: str) -> str:
    """文本的 sha256（与发布脚本用同一口径）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class SyncOutcome:
    """一次同步的结果。

    Attributes:
        source: 实际生效的源（``package`` 表示回落到包内）。
        used_fallback: 是否发生了回退（主源失败）。
        errors: 各源失败原因（便于排障）。
        version: 远端清单版本（回落包内时为空）。
        written / skipped: 写入 / 跳过的文件。
    """

    source: str
    used_fallback: bool = False
    errors: list[str] = field(default_factory=list)
    version: str = ""
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        """转成可序列化字典。"""
        return {
            "source": self.source,
            "used_fallback": self.used_fallback,
            "version": self.version,
            "written": self.written,
            "skipped": self.skipped,
            "errors": self.errors,
        }


def fetch_all(
    source: str, manifest: Manifest, timeout: float = DEFAULT_TIMEOUT
) -> dict[str, str]:
    """按清单下载全部文件并校验 sha256。

    Raises:
        RemoteError: 任一文件下载失败或校验不通过（**不返回半份内容**）。
    """
    out: dict[str, str] = {}
    for rel, expected in manifest.files.items():
        text = fetch_text(_join(source, rel), timeout)
        actual = sha256_of(text)
        if expected and actual != expected:
            raise RemoteError(
                f"完整性校验失败: {rel}（期望 {expected[:12]}… 实际 {actual[:12]}…）。"
                "内容可能被篡改或发布不完整，已中止同步。"
            )
        out[rel] = text
    return out


# ============================================================
# 同步编排：远端（含回退）→ 本地目录
# ============================================================


def split_by_level(files: dict[str, str], pack: str | None) -> tuple[dict[str, str], dict[str, str]]:
    """把清单里的文件按 ``base/`` 与 ``packs/<pack>/`` 拆开（键名去掉前缀）。"""
    base = {
        rel[len("base/") :]: text
        for rel, text in files.items()
        if rel.startswith("base/") and rel.endswith(".md")
    }
    pack_files: dict[str, str] = {}
    if pack:
        prefix = f"packs/{pack}/"
        pack_files = {
            rel[len(prefix) :]: text
            for rel, text in files.items()
            if rel.startswith(prefix) and rel.endswith(".md")
        }
    return base, pack_files


def _write_local(
    dest: Path,
    base_files: dict[str, str],
    pack_files: dict[str, str],
    overwrite: bool,
) -> tuple[list[str], list[str]]:
    """把「base 与 pack 合并后的完整提示词」写入本地目录。

    ⚠️ 必须写入**合并后**的完整文本：本地目录是整文件优先，只写 pack 片段
    会让它丢掉 base 的 JSON 骨架（这正是章节级合并要解决的问题）。
    """
    from jobcopilot.core.prompts.compose import compose

    written: list[str] = []
    skipped: list[str] = []
    dest.mkdir(parents=True, exist_ok=True)

    for name, base_text in sorted(base_files.items()):
        merged = base_text
        if name in pack_files:
            merged, _ = compose(base_text, pack_files[name])
        target = dest / name
        if target.exists() and target.read_text(encoding="utf-8") == merged:
            skipped.append(name)
            continue
        if target.exists() and not overwrite:
            skipped.append(name)
            continue
        target.write_text(merged, encoding="utf-8")
        written.append(name)

    # pack 独有（base 里没有）的文件：直接写入
    for name, text in sorted(pack_files.items()):
        if name in base_files:
            continue
        target = dest / name
        if target.exists() and not overwrite:
            skipped.append(name)
            continue
        target.write_text(text, encoding="utf-8")
        written.append(name)
    return written, skipped


def sync_to_local(
    dest: Path,
    pack: str | None = None,
    sources: list[str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    overwrite: bool = False,
    package_fallback: bool = True,
) -> SyncOutcome:
    """把提示词同步到本地目录，带三级回退。

    Args:
        dest: 本地提示词目录。
        pack: 职能族名（None 表示只同步 base）。
        sources: 源列表；``None`` 时用 :func:`resolve_sources`。
        timeout: 单次网络超时（秒）。
        overwrite: 是否覆盖已存在的本地文件（默认不覆盖，保护用户改动）。
        package_fallback: 所有远端都失败时是否回落到包内提示词。

    Returns:
        :class:`SyncOutcome`。``source`` 为实际生效的源；回落包内时为 ``package``。
    """
    chain = sources if sources is not None else resolve_sources()
    errors: list[str] = []

    for idx, source in enumerate(chain):
        try:
            manifest = fetch_manifest(source, timeout)
            if pack and pack not in manifest.packs:
                raise RemoteError(
                    f"该源没有名为 {pack!r} 的 pack（可用: {', '.join(manifest.packs) or '无'}）"
                )
            files = fetch_all(source, manifest, timeout)
            base_files, pack_files = split_by_level(files, pack)
            if not base_files:
                raise RemoteError("清单里没有任何 base 提示词")
            written, skipped = _write_local(dest, base_files, pack_files, overwrite)
            logger.info(
                f"提示词已从远端同步 source={source} version={manifest.version} "
                f"written={len(written)}"
            )
            return SyncOutcome(
                source=source,
                used_fallback=idx > 0,
                errors=errors,
                version=manifest.version,
                written=written,
                skipped=skipped,
            )
        except RemoteError as e:
            errors.append(f"{source} → {e}")
            logger.warning(f"提示词源不可用，尝试下一个 source={source} error={e}")

    if not package_fallback:
        raise RemoteError("所有提示词源均不可用：\n  " + "\n  ".join(errors))

    # 第三级：包内兜底（永远可用，断网也能跑）
    from jobcopilot.core.prompts.resolver import PromptResolver

    resolver = PromptResolver(pack=pack)
    base_files = {
        name: resolver.get(name)
        for name in resolver.available()
        if resolver.source_of(name) in {"base", "pack+base"}
    }
    base_files = {k: v for k, v in base_files.items() if v}
    written, skipped = _write_local(dest, base_files, {}, overwrite)
    logger.warning(
        f"所有远端源不可用，已回落到**包内**提示词（版本可能落后于线上）: {len(errors)} 个源失败"
    )
    return SyncOutcome(
        source="package",
        used_fallback=True,
        errors=errors,
        version="",
        written=written,
        skipped=skipped,
    )
