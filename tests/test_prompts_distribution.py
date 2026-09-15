"""提示词分发（P3）的测试：清单解析、三级回退、完整性校验、发布。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobcopilot.core.prompts.remote import (
    ENV_SOURCES,
    Manifest,
    RemoteError,
    fetch_all,
    fetch_manifest,
    resolve_sources,
    sha256_of,
    split_by_level,
    sync_to_local,
)
from jobcopilot.publish import build_manifest, publish


def make_source(root: Path, base_text: str = "# base\n内容\n", pack_text: str = "## 背景\npack 覆盖\n") -> str:
    """造一个 file:// 源，返回 URL。"""
    (root / "base").mkdir(parents=True, exist_ok=True)
    (root / "packs" / "presales").mkdir(parents=True, exist_ok=True)
    files = {
        "base/批量职位分析.md": base_text,
        "base/02_job_analysis.md": "# 单职位\n",
        "packs/presales/批量职位分析.md": pack_text,
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    manifest = {
        "schema": 1,
        "version": "test-vers",
        "packs": ["presales"],
        "files": {rel: sha256_of(t) for rel, t in files.items()},
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root.as_uri()


# ---------- 清单 ----------


def test_manifest_parse_ok() -> None:
    m = Manifest.parse({"files": {"a.md": "x"}, "version": "v1", "packs": ["p"]})
    assert m.version == "v1" and m.packs == ["p"] and m.files == {"a.md": "x"}


def test_manifest_parse_rejects_empty_files() -> None:
    with pytest.raises(RemoteError, match="files"):
        Manifest.parse({"version": "v"})


def test_manifest_parse_rejects_schema_mismatch() -> None:
    """schema 不兼容时必须报错，而不是拿到半份清单继续跑。"""
    with pytest.raises(RemoteError, match="schema"):
        Manifest.parse({"schema": 99, "files": {"a": "b"}})


def test_resolve_sources_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_SOURCES, raising=False)
    # 默认三级：自建主源 → jsDelivr（GitHub 内容的国内可达 CDN）→ GitHub raw
    default = resolve_sources()
    assert len(default) == 3
    assert default[0].startswith("https://bos-studio.tech/")
    assert any("jsdelivr" in s for s in default)
    assert any("raw.githubusercontent" in s for s in default)
    assert resolve_sources("https://x/") == ["https://x/"]
    monkeypatch.setenv(ENV_SOURCES, "https://a/,https://b/")
    assert resolve_sources() == ["https://a/", "https://b/"]


# ---------- 完整性 ----------


def test_fetch_all_verifies_digest(tmp_path: Path) -> None:
    url = make_source(tmp_path / "src")
    m = fetch_manifest(url)
    files = fetch_all(url, m)
    assert "base/批量职位分析.md" in files


def test_fetch_all_rejects_tampered_content(tmp_path: Path) -> None:
    """内容被篡改（sha256 不符）必须中止——提示词会进 LLM，不能将就。"""
    url = make_source(tmp_path / "src")
    m = fetch_manifest(url)
    p = tmp_path / "src" / "base" / "批量职位分析.md"
    p.write_text("被篡改的内容", encoding="utf-8")
    with pytest.raises(RemoteError, match="完整性校验失败"):
        fetch_all(url, m)


def test_split_by_level() -> None:
    base, pack = split_by_level(
        {"base/a.md": "A", "packs/p1/a.md": "P", "packs/p2/b.md": "Q", "README.md": "x"},
        "p1",
    )
    assert base == {"a.md": "A"}
    assert pack == {"a.md": "P"}


# ---------- 三级回退 ----------


def test_sync_from_primary(tmp_path: Path) -> None:
    url = make_source(tmp_path / "src")
    out = sync_to_local(tmp_path / "dest", pack="presales", sources=[url])
    assert out.source == url
    assert out.used_fallback is False
    assert out.version == "test-vers"
    assert "批量职位分析.md" in out.written


def test_sync_falls_back_to_secondary(tmp_path: Path) -> None:
    """主源不可用 → 自动用备源，并标记 used_fallback。"""
    good = make_source(tmp_path / "good")
    bad = (tmp_path / "missing").as_uri()
    out = sync_to_local(tmp_path / "dest", pack="presales", sources=[bad, good])
    assert out.source == good
    assert out.used_fallback is True
    assert out.errors and bad in out.errors[0]


def test_sync_falls_back_to_package_when_all_sources_down(tmp_path: Path) -> None:
    """DoD 3：主备都断 → 用包内兜底（有单测），而不是直接失败。"""
    bad1 = (tmp_path / "missing1").as_uri()
    bad2 = (tmp_path / "missing2").as_uri()
    out = sync_to_local(tmp_path / "dest", pack="presales", sources=[bad1, bad2])
    assert out.source == "package"
    assert out.used_fallback is True
    assert len(out.errors) == 2
    # 包内兜底同样要写出**完整可用**的提示词
    text = (tmp_path / "dest" / "批量职位分析.md").read_text(encoding="utf-8")
    assert "年包" in text and "track_heatmap" in text


def test_sync_package_fallback_can_be_disabled(tmp_path: Path) -> None:
    with pytest.raises(RemoteError, match="均不可用"):
        sync_to_local(
            tmp_path / "dest", sources=[(tmp_path / "x").as_uri()], package_fallback=False
        )


def test_sync_writes_composed_complete_prompts(tmp_path: Path) -> None:
    """同步下来的是 base+pack **合并后**的完整提示词（含 JSON 骨架）。

    这条很关键：本地目录是整文件优先，若只写 pack 片段会丢掉骨架，
    等于把提示词用坏（P1 的设计要点）。
    """
    # 真实提示词的结构：背景 / 任务（含 ### 小节）/ 输出格式（JSON 骨架在最后）
    src = make_source(
        tmp_path / "src",
        base_text=(
            "# base\n\n## 背景\n原始\n\n## 任务\n### 1. 甲\nA\n\n"
            '## 输出格式\n```json\n{"bottom_line": "x"}\n```\n'
        ),
        pack_text="## 背景\npack 覆盖\n",
    )
    out = sync_to_local(tmp_path / "dest", pack="presales", sources=[src])
    text = (tmp_path / "dest" / "批量职位分析.md").read_text(encoding="utf-8")
    assert "pack 覆盖" in text
    assert "bottom_line" in text, "JSON 骨架丢失（合并逻辑坏了）"
    assert out.source == src


def test_sync_respects_existing_files_without_overwrite(tmp_path: Path) -> None:
    """默认不覆盖用户改过的本地提示词。"""
    src = make_source(tmp_path / "src")
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "批量职位分析.md").write_text("用户自己改的", encoding="utf-8")

    out = sync_to_local(dest, pack="presales", sources=[src])
    assert "批量职位分析.md" not in out.written
    assert (dest / "批量职位分析.md").read_text(encoding="utf-8") == "用户自己改的"

    out2 = sync_to_local(dest, pack="presales", sources=[src], overwrite=True)
    assert "批量职位分析.md" in out2.written


def test_sync_unknown_pack_in_manifest(tmp_path: Path) -> None:
    src = make_source(tmp_path / "src")
    out = sync_to_local(tmp_path / "dest", pack="不存在的pack", sources=[src])
    # 该源没有这个 pack → 该源失败 → 回落包内（而包内也没有 → 仍能兜底产出 base）
    assert out.source == "package"
    assert any("不存在的pack" in e for e in out.errors)


# ---------- 发布 ----------


def test_publish_creates_manifest_and_files(tmp_path: Path) -> None:
    out = tmp_path / "dist"
    assert publish(out) == 0
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == 1
    assert manifest["packs"] == ["engineering", "presales", "product"]
    assert len(manifest["files"]) >= 15
    assert (out / "base" / "批量职位分析.md").exists()
    assert (out / "packs" / "presales" / "批量职位分析.md").exists()


def test_publish_is_idempotent(tmp_path: Path) -> None:
    """内容不变则 version 不变（发布可重复执行而不产生噪声 diff）。"""
    out = tmp_path / "dist"
    publish(out)
    v1 = json.loads((out / "manifest.json").read_text(encoding="utf-8"))["version"]
    publish(out)
    v2 = json.loads((out / "manifest.json").read_text(encoding="utf-8"))["version"]
    assert v1 == v2


def test_publish_check_detects_drift(tmp_path: Path) -> None:
    out = tmp_path / "dist"
    publish(out)
    assert publish(out, check_only=True) == 0

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"]["base/批量职位分析.md"] = "0" * 64
    (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert publish(out, check_only=True) == 1


def test_publish_check_without_manifest(tmp_path: Path) -> None:
    assert publish(tmp_path / "empty", check_only=True) == 1


def test_publish_removes_stale_files(tmp_path: Path) -> None:
    """上一次发布多出来的文件必须被清掉，否则会继续被分发出去。"""
    out = tmp_path / "dist"
    publish(out)
    stale = out / "base" / "已删除的提示词.md"
    stale.write_text("旧内容", encoding="utf-8")
    publish(out)
    assert not stale.exists()


def test_publish_manifest_digests_match_files(tmp_path: Path) -> None:
    """清单里的 sha256 必须与真实文件一致（否则消费端必然校验失败）。"""
    out = tmp_path / "dist"
    publish(out)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    for rel, expected in manifest["files"].items():
        assert sha256_of((out / rel).read_text(encoding="utf-8")) == expected, rel


def test_build_manifest_version_is_content_derived() -> None:
    """version 由内容决定：同内容同版本、改内容换版本。"""
    a = build_manifest({"base/x.md": "A"}, "gen")
    b = build_manifest({"base/x.md": "A"}, "gen")
    c = build_manifest({"base/x.md": "B"}, "gen")
    assert a["version"] == b["version"]
    assert a["version"] != c["version"]


def test_publish_refuses_personal_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """防泄漏护栏：base/README.md（个人画像）绝不能进分发目录。"""
    from jobcopilot.core.prompts import base_dir

    fake = base_dir() / "README.md"
    fake.write_text("name: 某人\n", encoding="utf-8")
    try:
        with pytest.raises(SystemExit, match="README"):
            publish(tmp_path / "dist")
    finally:
        fake.unlink()


def test_publish_refuses_pack_that_breaks_json_skeleton(tmp_path: Path, monkeypatch) -> None:
    """发布闸门：pack 若把 JSON 骨架整块覆盖掉，必须拒绝发布。

    成因：章节块的边界是「到下一个标题为止」，所以覆盖**最后一个标题**时，
    它后面的非标题内容（很可能正是输出骨架）会被一并替换。这种提示词
    表面上组合成功，实际已丢掉输出契约，必须在发布这一上游拦住。
    """
    from jobcopilot.core.prompts import base_dir, packs_dir

    pack_file = packs_dir() / "presales" / "批量职位分析.md"
    original = pack_file.read_text(encoding="utf-8")
    try:
        # 用**与 base 完全相同**的标题才能命中覆盖；该标题是最后一个，
        # 其块包含后面的 JSON 骨架 → 一并被替换 → 闸门应拦下
        pack_file.write_text(
            original + "\n## 输出格式（严格 JSON，只输出 JSON，不要任何多余文字）\n自由发挥\n",
            encoding="utf-8",
        )
        assert publish(tmp_path / "dist") == 1
        assert not (tmp_path / "dist" / "manifest.json").exists()
    finally:
        pack_file.write_text(original, encoding="utf-8")
    assert base_dir().exists()


def test_join_percent_encodes_non_ascii_paths() -> None:
    """中文文件名必须被百分号编码。

    真实端点（https）下，未编码的 URL 会让 urllib 抛
    `UnicodeEncodeError: 'ascii' codec can't encode characters`；
    而 `file://` 不走那条路径，所以本地单测**发现不了**——这条断言专门钉住它。
    """
    from jobcopilot.core.prompts.remote import _join

    url = _join("https://example.com/prompts/", "base/批量职位分析.md")
    assert url.startswith("https://example.com/prompts/base/")
    assert "批量" not in url, "路径未编码"
    assert "%E6%89%B9%E9%87%8F" in url  # 「批量」的 UTF-8 百分号编码
    assert url.isascii(), "URL 必须全部落在 ASCII 范围内"

    # 保留路径分隔符，否则会变成一个错误的单段路径
    assert "/" in url.removeprefix("https://example.com/prompts/")


def test_sync_writes_local_manifest(tmp_path: Path) -> None:
    """同步后在本地目录落 manifest：记录来源与版本，便于排障与追溯。"""
    src = make_source(tmp_path / "src")
    dest = tmp_path / "dest"
    sync_to_local(dest, pack="presales", sources=[src])
    m = json.loads((dest / ".jobcopilot-manifest.json").read_text(encoding="utf-8"))
    assert m["source"] == src
    assert m["version"] == "test-vers"
    assert m["pack"] == "presales"
    assert m["digests"]


def test_package_fallback_writes_local_manifest(tmp_path: Path) -> None:
    """包内兜底也要落 manifest（否则事后无从判断这批提示词的来路）。"""
    dest = tmp_path / "dest"
    sync_to_local(dest, pack="presales", sources=[(tmp_path / "nope").as_uri()])
    m = json.loads((dest / ".jobcopilot-manifest.json").read_text(encoding="utf-8"))
    assert m["source"] == "package"
    assert m["digests"]
