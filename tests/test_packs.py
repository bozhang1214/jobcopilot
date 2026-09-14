"""职能 pack 与解析优先级的测试（P1 DoD 1 & 2）。"""
from __future__ import annotations

from pathlib import Path

from jobcopilot.core.prompts import PromptResolver, available_packs, base_dir, packs_dir
from jobcopilot.evals.assertions import extract_json_skeleton

BATCH_PROMPT = "批量职位分析.md"


def test_packs_available() -> None:
    """三个初始职能 pack 必须在包内。"""
    packs = available_packs()
    for expected in ("presales", "product", "engineering"):
        assert expected in packs


def test_no_pack_falls_back_to_base() -> None:
    """不指定 pack 时，解析结果与 base 逐字节一致（不影响既有宿主）。"""
    r = PromptResolver()
    assert r.source_of(BATCH_PROMPT) == "base"
    assert r.get(BATCH_PROMPT) == (base_dir() / BATCH_PROMPT).read_text(encoding="utf-8")


def test_pack_overrides_sections_but_keeps_json_skeleton() -> None:
    """pack 只覆盖差异章节；JSON 输出骨架必须来自 base（防 schema 漂移）。"""
    base_text = (base_dir() / BATCH_PROMPT).read_text(encoding="utf-8")
    for pack in ("presales", "product", "engineering"):
        text = PromptResolver(pack=pack).get(BATCH_PROMPT)
        assert text != base_text
        # JSON 骨架字段一个都不能少
        for field in ("track_heatmap", "skill_threshold", "experience_reality",
                      "salary_anchor", "bottom_line"):
            assert field in text, f"{pack} 丢了骨架字段 {field}"
        # 新增章节必须落在输出格式之前
        assert text.index("### 5.") < text.index("## 输出格式")


def test_pack_content_is_role_specific() -> None:
    """各 pack 的差异化重点确实不同（售前看年包、产品看产品线、研发看技术职能）。"""
    presales = PromptResolver(pack="presales").get(BATCH_PROMPT)
    product = PromptResolver(pack="product").get(BATCH_PROMPT)
    engineering = PromptResolver(pack="engineering").get(BATCH_PROMPT)

    assert "年包" in presales and "云厂商" in presales
    assert "产品线" in product and "跨团队" in product
    assert "技术职能" in engineering and "系统设计" in engineering


def test_unknown_pack_falls_back_to_base() -> None:
    """pack 名不存在时（目录没有）安全回退到 base，不报错。"""
    r = PromptResolver(pack="不存在的pack")
    assert r.source_of(BATCH_PROMPT) == "base"
    assert r.get(BATCH_PROMPT)


def test_missing_prompt_in_pack_falls_back_to_base() -> None:
    """pack 只覆盖了批量提示词，其他提示词仍来自 base（DoD 2）。"""
    r = PromptResolver(pack="presales")
    assert r.source_of("02_job_analysis.md") == "base"
    assert r.get("02_job_analysis.md")


def test_local_dir_beats_pack(tmp_path: Path) -> None:
    """宿主本地目录优先级高于 pack（SEKB 现网行为不变）。"""
    (tmp_path / BATCH_PROMPT).write_text("本地版本", encoding="utf-8")
    r = PromptResolver(local_dir=tmp_path, pack="presales")
    assert r.source_of(BATCH_PROMPT) == "local"
    assert r.get(BATCH_PROMPT) == "本地版本"


def test_request_override_beats_everything(tmp_path: Path) -> None:
    """请求级 override 优先级最高。"""
    (tmp_path / BATCH_PROMPT).write_text("本地版本", encoding="utf-8")
    r = PromptResolver(local_dir=tmp_path, pack="presales", overrides={BATCH_PROMPT: "请求级"})
    assert r.source_of(BATCH_PROMPT) == "override"
    assert r.get(BATCH_PROMPT) == "请求级"


def test_meta_records_traceability() -> None:
    """DoD 4：能查出用了哪个 pack、覆盖了哪些章节、内容指纹。"""
    meta = PromptResolver(pack="presales").meta(BATCH_PROMPT)
    assert meta.source == "pack+base"
    assert meta.pack == "presales"
    assert meta.digest and len(meta.digest) == 16
    assert "###:1" in meta.overridden_sections
    assert meta.appended_sections == ["###:5"]
    d = meta.to_dict()
    assert d["pack"] == "presales" and d["digest"] == meta.digest


def test_meta_for_base_has_no_pack() -> None:
    """无 pack 时 meta 不含 pack 字段。"""
    meta = PromptResolver().meta(BATCH_PROMPT)
    assert meta.pack == ""
    assert "pack" not in meta.to_dict()


def test_pack_dir_layout_is_conventional() -> None:
    """pack 目录约定：packs/<职能族>/<提示词文件名>.md。"""
    for pack in ("presales", "product", "engineering"):
        p = packs_dir() / pack / BATCH_PROMPT
        assert p.is_file(), f"{pack} 缺少 {BATCH_PROMPT}"


def test_packs_do_not_contain_personal_data() -> None:
    """pack 子目录不得出现 README.md（那是 SEKB 个人画像的文件名）。

    ``packs/README.md`` 本身是目录说明文档，属正常，不在检查范围。
    """
    offenders = [p for p in packs_dir().glob("*/README.md")]
    assert not offenders, f"pack 子目录出现 README.md（疑似个人画像）: {offenders}"


def test_extract_json_skeleton_on_real_prompt() -> None:
    """骨架抽取工具能解析真实提示词里声明的输出结构。"""
    text = (base_dir() / BATCH_PROMPT).read_text(encoding="utf-8")
    skeleton = extract_json_skeleton(text)
    assert skeleton is not None
    assert "track_heatmap" in skeleton
