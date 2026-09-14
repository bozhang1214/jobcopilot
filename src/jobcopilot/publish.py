"""把包内提示词发布成「可分发目录」（给 jobcopilot-prompts 仓库 / nginx 静态目录）。

发布产物::

    <out>/
    ├── README.md
    ├── manifest.json          # 版本 + 每个文件的 sha256
    ├── base/*.md              # 通用提示词
    └── packs/<职能族>/*.md    # 职能 pack（章节级覆盖片段）

设计要点
--------
- **version 由内容决定**（所有文件 sha256 的再哈希），因此发布是**幂等**的：
  内容没变就不会产生 diff，避免「每天跑一次发布就多一个空提交」。
- 逐个文件写 sha256，消费端（``jobcopilot pull --remote``）会**校验后再落盘**——
  提示词会被注入 LLM，被篡改的后果比下载失败严重得多。
- 单向：**包内是内容源，本目录是分发产物**。因为 eval 基线与测试都锚在包内，
  保持单一事实源才能让「提示词指纹守门员」有意义。

用法::

    jobcopilot publish --out /path/to/jobcopilot-prompts
    jobcopilot publish --out /var/www/jobcopilot-prompts --check

放在包里（而不是仓库 scripts/）是为了让**任何装了 jobcopilot 的环境**都能发布——
服务器上就不用再装一份 Python 依赖，直接 ``docker run --rm ... jobcopilot publish``。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA = 1

README = """# jobcopilot-prompts

> JobCopilot 的提示词分发仓库（**由脚本生成，请勿手改**）。

```bash
# 拉取最新提示词到本地（会自动校验 sha256）
jobcopilot pull --remote

# 或指定职能包
jobcopilot pull --remote --pack presales
```

## 目录

```
manifest.json          # 版本 + 每个文件的 sha256（供完整性校验）
base/                  # 通用提示词（兜底，一定有）
packs/<职能族>/        # 职能 pack，**只写差异章节**，其余继承 base
```

## 内容从哪来

本目录由 `jobcopilot` 仓库的 `scripts/publish_prompts.py` 生成：
**包内 `src/jobcopilot/core/prompts/` 是内容源，本仓库是分发产物**（单向）。

这样能让「提示词指纹」守门员（`jobcopilot eval --level 12`）保持有意义——
它锚定的是包内那一份。若想改提示词，请改包内源码并跑一次 eval 基线。

## 消费端的三级回退

1. 本仓库对应的自建端点（`https://bos-studio.tech/prompts/`）
2. GitHub 备源（`raw.githubusercontent.com/.../jobcopilot-prompts/main/`）
3. 包内兜底（断网也能用）

任一级失败会自动回落到下一级，并在结果里说明实际用了哪个源。
"""


def sha256_text(text: str) -> str:
    """与消费端一致的口径：对文件文本（UTF-8）算 sha256。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_manifest(files: dict[str, str], generator: str) -> dict[str, Any]:
    """按文件内容生成清单；version 由全部内容决定（保证发布幂等）。"""
    digests = {rel: sha256_text(text) for rel, text in sorted(files.items())}
    combined = hashlib.sha256("".join(f"{k}:{v}" for k, v in digests.items()).encode())
    return {
        "schema": MANIFEST_SCHEMA,
        "version": combined.hexdigest()[:12],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generator": generator,
        "packs": sorted(
            {rel.split("/")[1] for rel in files if rel.startswith("packs/") and len(rel.split("/")) > 2}
        ),
        "files": digests,
    }


def collect_package_prompts() -> dict[str, str]:
    """收集包内提示词，键为分发目录里的相对路径。"""
    from jobcopilot import __version__
    from jobcopilot.core.prompts.resolver import base_dir, packs_dir

    files: dict[str, str] = {}
    for p in sorted(base_dir().glob("*.md")):
        files[f"base/{p.name}"] = p.read_text(encoding="utf-8")
    for pack_dir in sorted(x for x in packs_dir().iterdir() if x.is_dir()):
        for p in sorted(pack_dir.glob("*.md")):
            files[f"packs/{pack_dir.name}/{p.name}"] = p.read_text(encoding="utf-8")

    if not files:
        raise SystemExit("❌ 包内没有收集到任何提示词")
    # 防泄漏护栏：个人画像文件绝不能进分发目录
    if any(Path(k).name == "README.md" for k in files):
        raise SystemExit("❌ 分发目录里出现 base/README.md（含个人画像），拒绝发布")
    print(f"  收集到 {len(files)} 个提示词（generator={__version__}）")
    return files


def check_packs_preserve_skeleton(files: dict[str, str]) -> list[str]:
    """校验每个 pack 组合后**没有破坏 base 声明的 JSON 骨架**。

    为什么需要这道闸门：pack 走章节级合并，而「一个标题的块」会一直延伸到下一个标题。
    如果某个 pack 覆盖的标题恰好是最后一个标题，它后面不是标题的内容（很可能就是
    JSON 输出骨架）会被一并替换掉——组合出来的提示词看着正常，实际已丢掉输出契约。
    发布是所有消费方的上游，在这里拦住代价最小。
    """
    from jobcopilot.core.prompts.compose import compose
    from jobcopilot.evals.assertions import extract_json_skeleton

    problems: list[str] = []
    base_files = {k[len("base/"):]: v for k, v in files.items() if k.startswith("base/")}
    for rel, pack_text in files.items():
        if not rel.startswith("packs/"):
            continue
        name = rel.split("/", 2)[2]
        base_text = base_files.get(name)
        if base_text is None:
            continue  # pack 独有的提示词，无 base 可比
        merged, _ = compose(base_text, pack_text)
        before = extract_json_skeleton(base_text) or {}
        after = extract_json_skeleton(merged) or {}
        lost = sorted(set(before) - set(after))
        if lost:
            problems.append(f"{rel} 覆盖后丢失 JSON 骨架字段: {lost}")
    return problems


def publish(out: Path, check_only: bool = False) -> int:
    """把包内提示词发布到 ``out``。

    Args:
        out: 目标目录（jobcopilot-prompts 检出，或 nginx 静态目录）。
        check_only: 只校验「产物是否与包内一致」，不写文件（给 CI 用）。

    Returns:
        进程退出码：0 成功/一致；1 需要更新（check 模式）或内容为空。
    """
    from jobcopilot import __version__

    files = collect_package_prompts()

    problems = check_packs_preserve_skeleton(files)
    if problems:
        print("❌ 拒绝发布：以下 pack 会破坏 base 的 JSON 输出骨架")
        for p in problems:
            print(f"   - {p}")
        print("   检查 pack 是否覆盖了最后一个标题（其后的内容会被整块替换）")
        return 1

    manifest = build_manifest(files, f"jobcopilot {__version__}")

    if check_only:
        current = out / "manifest.json"
        if not current.exists():
            print("❌ 目标目录没有 manifest.json（尚未发布过）")
            return 1
        old = json.loads(current.read_text(encoding="utf-8"))
        if old.get("files") == manifest["files"]:
            print(f"✅ 分发目录与包内一致（version={manifest['version']}）")
            return 0
        changed = sorted(
            k for k in set(old.get("files", {})) | set(manifest["files"])
            if old.get("files", {}).get(k) != manifest["files"].get(k)
        )
        print(f"⚠️ 分发目录与包内不一致（{len(changed)} 个文件）: {changed[:6]}")
        print("   运行 python scripts/publish_prompts.py --out <dir> 更新")
        return 1

    if out.exists():
        # 清掉旧的 base/ 与 packs/，避免删除提示词后旧文件残留继续被分发
        for sub in ("base", "packs"):
            shutil.rmtree(out / sub, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)

    for rel, text in files.items():
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out / "README.md").write_text(README, encoding="utf-8")

    print(f"✅ 已发布到 {out}")
    print(f"   version={manifest['version']}  files={len(files)}  packs={manifest['packs']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    p = argparse.ArgumentParser(description="发布 JobCopilot 提示词到分发目录")
    p.add_argument("--out", required=True, help="目标目录（prompts 仓库检出或 nginx 静态目录）")
    p.add_argument("--check", action="store_true", help="只校验一致性，不写文件")
    args = p.parse_args(argv)
    return publish(Path(args.out).expanduser(), check_only=args.check)


if __name__ == "__main__":
    sys.exit(main())
