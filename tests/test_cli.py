"""CLI 的测试（P1 DoD 1：`jobcopilot pull` 能拉 pack 并生效）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobcopilot.cli.main import build_parser, main
from jobcopilot.core.prompts import PromptResolver


def test_parser_requires_command() -> None:
    """不传子命令应报错退出（argparse 行为）。"""
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--version"])
    assert "jobcopilot" in capsys.readouterr().out


# ---------- pull ----------


def test_pull_writes_composed_prompts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """pull 写出的是**合并后的完整提示词**，不是 pack 片段。

    这点很关键：本地目录是「整文件优先」，如果拉下去的是片段，
    会丢掉 base 的 JSON 骨架，把提示词用坏。
    """
    assert main(["pull", "--pack", "presales", "--dest", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "写入" in out

    batch = (tmp_path / "批量职位分析.md").read_text(encoding="utf-8")
    assert "年包" in batch, "presales 内容未生效"
    assert "track_heatmap" in batch, "JSON 骨架丢失"
    assert "bottom_line" in batch

    # 拉下来的提示词必须能被本地目录直接解析（优先级最高）
    r = PromptResolver(local_dir=tmp_path)
    assert r.source_of("批量职位分析.md") == "local"
    assert r.get("批量职位分析.md") == batch


def test_pull_writes_manifest(tmp_path: Path) -> None:
    """pull 落一份 manifest，便于追溯来源与指纹。"""
    main(["pull", "--pack", "product", "--dest", str(tmp_path)])
    manifest = json.loads((tmp_path / ".jobcopilot-manifest.json").read_text(encoding="utf-8"))
    assert manifest["pack"] == "product"
    assert "批量职位分析.md" in manifest["prompts"]
    assert manifest["prompts"]["批量职位分析.md"]["digest"]


def test_pull_without_pack(tmp_path: Path) -> None:
    """不带 pack 时只拉 base。"""
    assert main(["pull", "--dest", str(tmp_path)]) == 0
    assert "年包" not in (tmp_path / "批量职位分析.md").read_text(encoding="utf-8")


def test_pull_skips_existing_without_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """已存在时不覆盖（保护用户改过的提示词），--force 才覆盖。"""
    main(["pull", "--dest", str(tmp_path)])
    (tmp_path / "批量职位分析.md").write_text("我改过的内容", encoding="utf-8")

    main(["pull", "--dest", str(tmp_path)])
    assert (tmp_path / "批量职位分析.md").read_text(encoding="utf-8") == "我改过的内容"
    assert "跳过" in capsys.readouterr().out

    main(["pull", "--dest", str(tmp_path), "--force"])
    assert "我改过的内容" not in (tmp_path / "批量职位分析.md").read_text(encoding="utf-8")


def test_pull_unknown_pack_fails(tmp_path: Path) -> None:
    assert main(["pull", "--pack", "不存在的pack", "--dest", str(tmp_path)]) == 2


# ---------- pack ----------


def test_pack_list(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["pack"]) == 0
    out = capsys.readouterr().out
    for name in ("presales", "product", "engineering"):
        assert name in out


def test_pack_detail_shows_coverage(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["pack", "presales"]) == 0
    out = capsys.readouterr().out
    assert "覆盖章节" in out
    assert "新增章节" in out


def test_pack_unknown(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["pack", "不存在"]) == 2
    assert "未知 pack" in capsys.readouterr().out


# ---------- doctor ----------


def test_doctor_passes(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert "自检" in out
    assert "职能 pack" in out
    assert rc == 0


# ---------- eval ----------


def test_eval_level12_with_stub(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """零成本评估：默认 stub，不联网、不需要 Key。"""
    baseline = tmp_path / "b.json"
    assert main(["eval", "--level", "12", "--update-baseline", "--baseline", str(baseline)]) == 0
    assert baseline.exists()

    # 再跑一次，与基线比对应一致
    assert main(["eval", "--level", "12", "--baseline", str(baseline)]) == 0
    assert "与基线一致" in capsys.readouterr().out


def test_eval_fails_on_prompt_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """守门员行为：提示词指纹与基线不符时退出码非 0。"""
    baseline = tmp_path / "b.json"
    main(["eval", "--level", "12", "--update-baseline", "--baseline", str(baseline)])

    data = json.loads(baseline.read_text(encoding="utf-8"))
    data["prompt_digests"]["base/批量职位分析.md"] = "0123456789abcdef"
    baseline.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    assert main(["eval", "--level", "12", "--baseline", str(baseline)]) == 1
    assert "指纹" in capsys.readouterr().out
