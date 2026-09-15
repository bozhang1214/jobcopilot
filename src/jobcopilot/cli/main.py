"""JobCopilot 命令行入口。

P1 阶段提供四条命令（P2 会在此基础上扩展 analyze / serve）：

- ``pull``    把提示词（base 或某个职能 pack）**合并后**拉到本地目录，
              本地目录优先级最高 → 之后改提示词不用改代码
- ``eval``    跑评估（L1 结构断言 / L2 基线回归 / L3 LLM 评审）
- ``pack``    列出/查看内置职能 pack 与解析来源
- ``doctor``  自检：提示词、pack、provider Key、数据集
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from jobcopilot import __version__

#: 本地提示词目录（pull 的目标）
ENV_PROMPTS_DIR = "JOBCOPILOT_PROMPTS_DIR"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


def default_prompts_dir() -> Path:
    """默认本地提示词目录：``$JOBCOPILOT_PROMPTS_DIR`` 或 ``~/.jobcopilot/prompts``。"""
    env = os.environ.get(ENV_PROMPTS_DIR)
    return Path(env) if env else Path.home() / ".jobcopilot" / "prompts"


# ============================================================
# pull
# ============================================================


def cmd_pull(args: argparse.Namespace) -> int:
    """把提示词拉到本地目录（默认走远端，带三级回退）。"""
    from jobcopilot.core.prompts.remote import sync_to_local
    from jobcopilot.core.prompts.resolver import available_packs

    dest = Path(args.dest) if args.dest else default_prompts_dir()
    pack = args.pack or None

    if pack and pack not in available_packs():
        print(f"❌ 未知 pack: {pack!r}；可用: {', '.join(available_packs()) or '（无）'}")
        return EXIT_USAGE

    if args.offline:
        return _pull_offline(dest, pack, force=args.force)

    try:
        outcome = sync_to_local(
            dest,
            pack=pack,
            sources=[args.source] if args.source else None,
            timeout=args.timeout,
            overwrite=args.force,
        )
    except Exception as e:  # noqa: BLE001
        print(f"❌ 同步失败: {str(e)[:300]}")
        return EXIT_FAIL

    print(f"📦 提示词目录: {dest}")
    print(f"   pack      : {pack or '（仅 base）'}")
    print(f"   生效源    : {outcome.source}" + ("  ⚠️ 已回退" if outcome.used_fallback else ""))
    if outcome.version:
        print(f"   版本      : {outcome.version}")
    skip_note = f"，跳过 {len(outcome.skipped)} 个（--force 可覆盖）" if outcome.skipped else ""
    print(f"   写入 {len(outcome.written)} 个{skip_note}")
    for err in outcome.errors:
        print(f"     ⚠️ {err[:140]}")
    if outcome.source == "package":
        print("   ⚠️ 所有远端源不可用，已回落到**包内**提示词（版本可能落后于线上）")
    print("\n该目录优先级高于包内提示词，改完直接生效（jobcopilot pack 可查看来源）。")
    return EXIT_OK


def _pull_offline(dest: Path, pack: str | None, force: bool) -> int:
    """离线模式：直接用包内提示词（不走网络）。"""
    from jobcopilot.core.prompts.resolver import PromptResolver, base_dir

    dest.mkdir(parents=True, exist_ok=True)
    resolver = PromptResolver(pack=pack)
    written, skipped = [], []
    contents: dict[str, str] = {}
    for name in resolver.available():
        text = resolver.get(name)
        if not text:
            continue
        contents[name] = text
        target = dest / name
        if target.exists() and not force:
            skipped.append(name)
            continue
        target.write_text(text, encoding="utf-8")
        written.append(name)
    # 与远端路径一致：留下来源记录（source=package）
    from jobcopilot.core.prompts.remote import write_local_manifest

    write_local_manifest(dest, "package", "", pack, contents)
    print(f"📦 提示词目录: {dest}（离线模式：仅用包内）")
    print(f"   pack      : {pack or '（仅 base）'}")
    print(f"   base 来源 : {base_dir()}")
    print(f"   写入 {len(written)} 个" + (f"，跳过已存在 {len(skipped)} 个（--force 可覆盖）" if skipped else ""))
    return EXIT_OK


# ============================================================
# pack
# ============================================================


def cmd_pack(args: argparse.Namespace) -> int:
    """列出内置 pack，或查看某个 pack 对某条提示词的覆盖情况。"""
    from jobcopilot.core.prompts.resolver import PromptResolver, available_packs, packs_dir

    packs = available_packs()
    if not args.name:
        print(f"内置职能 pack（{packs_dir()}）:")
        if not packs:
            print("  （无）")
        for p in packs:
            files = sorted(x.name for x in (packs_dir() / p).glob("*.md"))
            print(f"  - {p}: {len(files)} 个提示词覆盖 → {', '.join(files)}")
        print(f"\n本地提示词目录: {default_prompts_dir()}（不存在则用包内）")
        return EXIT_OK

    if args.name not in packs:
        print(f"❌ 未知 pack: {args.name!r}；可用: {', '.join(packs) or '（无）'}")
        return EXIT_USAGE

    resolver = PromptResolver(pack=args.name)
    print(f"pack: {args.name}")
    for name in resolver.available():
        meta = resolver.meta(name)
        mark = "★" if meta.source.startswith("pack") else " "
        print(f"  {mark} {name:28s} {meta.source}")
        if meta.overridden_sections:
            print(f"      覆盖章节: {', '.join(meta.overridden_sections)}")
        if meta.appended_sections:
            print(f"      新增章节: {', '.join(meta.appended_sections)}")
    return EXIT_OK


# ============================================================
# eval
# ============================================================


def _build_llm(args: argparse.Namespace, need: bool) -> Any:
    """按参数构造 provider；need=False 且无 Key 时返回 None（不报错）。"""
    if args.provider == "stub":
        return None
    try:
        from jobcopilot.core.providers import OpenAICompatLLM

        return OpenAICompatLLM(preset=args.provider, model=args.model)
    except Exception:  # noqa: BLE001
        if need:
            raise
        return None


def cmd_eval(args: argparse.Namespace) -> int:
    """跑评估并（可选）比对基线。"""
    from jobcopilot.evals.runner import DEFAULT_BASELINE, DEFAULT_DATASETS, compare_with_baseline, run_eval

    level = "123" if args.level == "all" else args.level
    datasets = Path(args.datasets) if args.datasets else DEFAULT_DATASETS
    baseline_path = Path(args.baseline) if args.baseline else DEFAULT_BASELINE

    need_llm = "3" in level
    try:
        llm = _build_llm(args, need=need_llm)
    except Exception as e:  # noqa: BLE001
        print(f"❌ 构造 provider 失败: {e}")
        return EXIT_FAIL
    if need_llm and llm is None and args.provider != "stub":
        print("❌ 运行 L3 需要真实 provider（或用 --provider stub 只跑结构）")
        return EXIT_USAGE

    print(f"🧪 评估 level={level} 数据集={datasets}")
    if llm is None:
        print("   分析用 LLM: SkeletonStubLLM（零成本、离线）")
    report = asyncio.run(run_eval(datasets_dir=datasets, level=level, llm=llm))
    data = report.to_dict(keep_report=args.keep_report)

    m = data["metrics"]
    print(
        f"   用例 {len(data['cases'])} 个 | 通过率 {data['pass_rate']:.2%} | "
        f"用例通过率 {m['case_pass_rate']:.2%} | 耗时 {data['duration_s']}s"
    )
    print("   每断言通过率:")
    for name, rate in data["metrics"]["per_assertion"].items():
        print(f"     {name:28s} {rate:.2%}")
    if "judge" in data["metrics"]:
        print("   L3 各维度均分:")
        for name, score in data["metrics"]["judge"].items():
            print(f"     {name:28s} {score}")

    failed = [c for c in data["cases"] if not c["assertions"]["passed"]]
    for c in failed:
        print(f"   ❌ {c['case_id']} [{c['pack'] or 'base'}]")
        for r in c["assertions"]["results"]:
            if not r["passed"]:
                print(f"        {r['name']}: {r['detail'][:160]}")

    exit_code = EXIT_OK if data["passed"] else EXIT_FAIL

    if args.update_baseline:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n📝 基线已更新: {baseline_path}")
        return exit_code

    if baseline_path.exists():
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        ok, problems = compare_with_baseline(report, baseline)
        if ok:
            print(f"\n✅ 与基线一致（{baseline_path.name}）")
        else:
            print(f"\n❌ 与基线比对失败（{baseline_path.name}）:")
            for p in problems:
                print(f"     - {p}")
            exit_code = EXIT_FAIL
    else:
        print(f"\nℹ️ 基线不存在（{baseline_path}）；用 --update-baseline 生成首版")

    return exit_code


# ============================================================
# doctor
# ============================================================


def cmd_doctor(args: argparse.Namespace) -> int:
    """自检：提示词 / pack / provider / 数据集。"""
    from jobcopilot.core.prompts.resolver import PromptResolver, available_packs, base_dir
    from jobcopilot.evals.runner import DEFAULT_DATASETS, load_datasets

    ok = True
    print(f"JobCopilot {__version__} 自检")

    base_files = sorted(p.name for p in base_dir().glob("*.md"))
    print(f"  内置 base 提示词: {len(base_files)} 个")
    if len(base_files) < 9:
        print("     ❌ 数量异常（期望 ≥9）")
        ok = False

    # 防泄漏护栏（运行时版）：base 里不得出现个人画像文件
    if (base_dir() / "README.md").exists():
        print("     ❌ base/ 出现 README.md（含个人画像），不得进开源包")
        ok = False

    packs = available_packs()
    print(f"  职能 pack: {', '.join(packs) or '（无）'}")

    local = Path(args.prompts_dir) if args.prompts_dir else default_prompts_dir()
    r = PromptResolver(local_dir=local, pack=args.pack or None)
    print(f"  本地提示词目录: {local} {'✅ 存在' if local.exists() else '（不存在，用包内）'}")
    print(f"  生效 pack: {args.pack or '（无）'}")
    sample = "批量职位分析.md"
    print(f"  抽样 {sample}: source={r.source_of(sample)}")

    try:
        from jobcopilot.core.providers import PRESETS, MissingAPIKeyError, OpenAICompatLLM

        print(f"  provider 预设: {', '.join(PRESETS)}")
        try:
            llm = OpenAICompatLLM(preset=args.provider)
            print(f"  默认 provider [{args.provider}]: ✅ Key 已配置，model={llm.model}")
        except MissingAPIKeyError as e:
            print(f"  默认 provider [{args.provider}]: ⚠️ {e}")
    except ImportError:
        print("  provider: 未安装（pip install 'jobcopilot[providers]'）")

    try:
        import mcp  # noqa: F401

        from jobcopilot.mcp.server import build_parser as _mcp_parser  # noqa: F401

        print("  MCP Server: ✅ 依赖就绪（jobcopilot-mcp 可用）")
    except ImportError as e:
        print(f"  MCP Server: ⚠️ 未安装（pip install 'jobcopilot[mcp]'）: {e}")

    try:
        batch, single = load_datasets(DEFAULT_DATASETS)
        print(f"  黄金数据集: 批量 {len(batch)} 组 / 单职位 {len(single)} 条")
        if not batch and not single:
            print("     ❌ 数据集为空")
            ok = False
    except Exception as e:  # noqa: BLE001
        print(f"  黄金数据集: ❌ {e}")
        ok = False

    print("\n✅ 自检通过" if ok else "\n❌ 自检发现问题（见上）")
    return EXIT_OK if ok else EXIT_FAIL


def _cmd_publish(args: argparse.Namespace) -> int:
    """发布提示词（委托给 jobcopilot.publish）。"""
    from jobcopilot.publish import publish

    return publish(Path(args.out).expanduser(), check_only=args.check)


# ============================================================
# run（直接跑一次分析，免 MCP 客户端）
# ============================================================


def cmd_run(args: argparse.Namespace) -> int:
    """直接跑一次分析（单职位或批量），结果打到 stdout。

    这是「不接 MCP 客户端也能用」的入口，也方便脚本化与排障。
    """
    from jobcopilot.mcp.config import ServerConfig
    from jobcopilot.mcp.tools import ToolContext, ToolError, load_jobs_from_text
    from jobcopilot.mcp.tools import analyze_job as tool_analyze_job
    from jobcopilot.mcp.tools import analyze_jobs_batch as tool_analyze_batch

    if args.provider == "stub":
        print("❌ run 需要真实 provider（不能用 stub）")
        return EXIT_USAGE
    try:
        llm = _build_llm(args, need=True)
    except Exception as e:  # noqa: BLE001
        print(f"❌ 构造 provider 失败: {e}")
        return EXIT_FAIL
    if llm is None:
        print("❌ 未能构造 provider（检查 API Key）")
        return EXIT_FAIL

    cfg = ServerConfig.from_env()
    if args.prompts_dir:
        cfg.prompts_dir = Path(args.prompts_dir)
    ctx = ToolContext(llm=llm, config=cfg)

    text = None
    if args.file:
        fp = Path(args.file)
        if not fp.exists():
            print(f"❌ 文件不存在: {fp}")
            return EXIT_USAGE
        text = fp.read_text(encoding="utf-8")

    try:
        if args.batch:
            if text is None and not args.text:
                print("❌ --batch 需要 --file 或 --text")
                return EXIT_USAGE
            jobs = load_jobs_from_text(text) if text is not None else [{"jd_text": args.text}]
            report = asyncio.run(
                tool_analyze_batch(
                    ctx, jobs=jobs, keyword=args.keyword, city=args.city, prompt_pack=args.pack
                )
            )
        else:
            report = asyncio.run(
                tool_analyze_job(
                    ctx, jd_text=args.text, source_path=args.file, prompt_pack=args.pack
                )
            )
    except ToolError as e:
        print(f"❌ {e}")
        return EXIT_FAIL

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return EXIT_OK


# ============================================================
# 入口
# ============================================================


def build_parser() -> argparse.ArgumentParser:
    """构造 argparse 解析器。"""
    p = argparse.ArgumentParser(prog="jobcopilot", description="JobCopilot 求职分析内核")
    p.add_argument("--version", action="version", version=f"jobcopilot {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("pull", help="拉取提示词到本地目录（默认走远端，含三级回退）")
    sp.add_argument("--pack", help="职能族名（presales / product / engineering …）")
    sp.add_argument("--dest", help="目标目录（默认 $JOBCOPILOT_PROMPTS_DIR 或 ~/.jobcopilot/prompts）")
    sp.add_argument("--force", action="store_true", help="覆盖已存在的文件")
    sp.add_argument("--source", help="只从该 URL 拉取（覆盖默认源链）")
    sp.add_argument("--offline", action="store_true", help="离线模式：直接用包内提示词，不走网络")
    sp.add_argument("--timeout", type=float, default=20.0, help="单次网络超时秒数（默认 20）")
    sp.set_defaults(func=cmd_pull)

    sp = sub.add_parser("pack", help="列出内置职能 pack / 查看某个 pack 的覆盖情况")
    sp.add_argument("name", nargs="?", help="pack 名；不传则列出全部")
    sp.set_defaults(func=cmd_pack)

    sp = sub.add_parser("eval", help="跑评估（L1 结构 / L2 基线 / L3 LLM 评审）")
    sp.add_argument("--level", default="12", help="1 / 2 / 3 / 12 / 123 / all（默认 12）")
    sp.add_argument("--datasets", help="数据集目录")
    sp.add_argument("--baseline", help="基线文件路径")
    sp.add_argument("--update-baseline", action="store_true", help="把本次结果写为新基线")
    sp.add_argument("--provider", default="stub", help="分析用 provider（默认 stub，零成本离线）")
    sp.add_argument("--model", help="覆盖模型名")
    sp.add_argument("--keep-report", action="store_true", help="结果里保留完整报告")
    sp.set_defaults(func=cmd_eval)

    sp = sub.add_parser("run", help="直接跑一次分析（单职位 / 批量，结果输出 JSON）")
    sp.add_argument("--file", help="JD 或职位列表文件（JSON 数组 / 含 jobs 的对象 / 纯文本）")
    sp.add_argument("--text", help="直接传 JD 正文")
    sp.add_argument("--batch", action="store_true", help="批量分析模式")
    sp.add_argument("--pack", help="职能族 pack")
    sp.add_argument("--keyword", help="批量分析的关键词")
    sp.add_argument("--city", help="批量分析的城市")
    sp.add_argument("--provider", default="deepseek", help="provider（默认 deepseek）")
    sp.add_argument("--model", help="覆盖模型名")
    sp.add_argument("--prompts-dir", help="本地提示词目录")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("publish", help="把包内提示词发布成可分发目录（含 manifest.json）")
    sp.add_argument("--out", required=True, help="目标目录（prompts 仓库检出或 nginx 静态目录）")
    sp.add_argument("--check", action="store_true", help="只校验一致性，不写文件")
    sp.set_defaults(func=_cmd_publish)

    sp = sub.add_parser("doctor", help="自检：提示词 / pack / provider / 数据集")
    sp.add_argument("--provider", default="deepseek", help="检查哪个 provider 的 Key（默认 deepseek）")
    sp.add_argument("--pack", help="检查某个 pack 的生效情况")
    sp.add_argument("--prompts-dir", help="本地提示词目录")
    sp.set_defaults(func=cmd_doctor)

    return p


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
