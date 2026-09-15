"""MCP 工具层的测试（不依赖 MCP SDK）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobcopilot.mcp.config import ServerConfig
from jobcopilot.mcp.tools import (
    MAX_SOURCE_BYTES,
    ToolContext,
    ToolError,
    analyze_job,
    analyze_jobs_batch,
    get_profile,
    list_prompt_packs,
    load_jobs_from_text,
    save_profile,
    sync_prompts,
)
from tests.conftest import FakeLLM


def make_ctx(tmp_path: Path, **kw) -> ToolContext:
    """构造一个指向临时目录的工具上下文。"""
    cfg = ServerConfig(
        prompts_dir=tmp_path / "prompts",
        data_dir=tmp_path,
        profile_path=tmp_path / "profile.txt",
        **kw,
    )
    return ToolContext(llm=FakeLLM(default='{"ok": true}'), config=cfg)


# ---------- source_path 解析 ----------


def test_load_jobs_from_json_array() -> None:
    jobs = load_jobs_from_text(json.dumps([{"title": "A"}, {"title": "B"}]))
    assert [j["title"] for j in jobs] == ["A", "B"]


def test_load_jobs_from_object_with_jobs_key() -> None:
    jobs = load_jobs_from_text(json.dumps({"keyword": "x", "jobs": [{"title": "A"}]}))
    assert jobs == [{"title": "A"}]


def test_load_jobs_from_single_object() -> None:
    jobs = load_jobs_from_text(json.dumps({"title": "A", "jd_text": "..."}))
    assert len(jobs) == 1 and jobs[0]["title"] == "A"


def test_load_jobs_from_plain_text() -> None:
    """纯文本（Markdown / txt）当单个 JD 处理。"""
    jobs = load_jobs_from_text("# 招聘 Agent 工程师\n要求 ...")
    assert len(jobs) == 1 and "招聘 Agent 工程师" in jobs[0]["jd_text"]


def test_load_jobs_from_broken_json_falls_back_to_text() -> None:
    """看起来像 JSON 但坏了 → 当纯文本，不抛异常。"""
    jobs = load_jobs_from_text("{这不是合法 JSON")
    assert jobs[0]["jd_text"].startswith("{这不是合法 JSON")


# ---------- 路径安全边界 ----------


def test_read_source_disabled_by_default_for_http(tmp_path: Path) -> None:
    """HTTP 形态默认禁用 source_path（任意文件读取风险）。"""
    cfg = ServerConfig(transport="streamable-http", data_dir=tmp_path, prompts_dir=tmp_path)
    ctx = ToolContext(llm=FakeLLM(), config=cfg)
    assert cfg.allow_source_path is False
    with pytest.raises(ToolError, match="禁用 source_path"):
        ctx.read_source(str(tmp_path / "x.txt"))


def test_read_source_respects_root(tmp_path: Path) -> None:
    """配了 source_root 后，越界路径必须拒绝。"""
    root = tmp_path / "allowed"
    root.mkdir()
    (root / "ok.txt").write_text("hi", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    ctx = make_ctx(tmp_path, source_root=root)
    assert ctx.read_source(str(root / "ok.txt")) == "hi"
    with pytest.raises(ToolError, match="越界"):
        ctx.read_source(str(outside))


def test_read_source_missing_file(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    with pytest.raises(ToolError, match="不存在"):
        ctx.read_source(str(tmp_path / "nope.txt"))


def test_read_source_too_large(tmp_path: Path) -> None:
    big = tmp_path / "big.txt"
    big.write_text("x" * (MAX_SOURCE_BYTES + 10), encoding="utf-8")
    ctx = make_ctx(tmp_path)
    with pytest.raises(ToolError, match="过大"):
        ctx.read_source(str(big))


# ---------- 工具 ----------


@pytest.mark.asyncio
async def test_analyze_job_all_sections_failing_is_an_error(tmp_path: Path) -> None:
    """LLM 全挂时必须报错，不能把「7 段全空」当成功返回。

    引擎内部的逐步降级是对的，但在 API 边界上，静默的空结果会让调用方
    完全看不出是 Key 失效 / 余额不足 / 网络不通。
    """
    cfg = ServerConfig(
        prompts_dir=tmp_path / "prompts", data_dir=tmp_path,
        profile_path=tmp_path / "profile.txt",
    )
    ctx = ToolContext(llm=FakeLLM(raises=RuntimeError("401 unauthorized")), config=cfg)
    with pytest.raises(ToolError, match="全部失败"):
        await analyze_job(ctx, jd_text="JD")


@pytest.mark.asyncio
async def test_analyze_job_partial_failure_warns(tmp_path: Path) -> None:
    """部分段落降级时结果仍返回，但必须带 warnings 告知调用方。"""
    cfg = ServerConfig(
        prompts_dir=tmp_path / "prompts", data_dir=tmp_path,
        profile_path=tmp_path / "profile.txt",
    )
    # 第 2 次调用返回坏 JSON → 只有 knowledge_priority 为空
    ctx = ToolContext(
        llm=FakeLLM(responses=['{"ok": 1}', "坏数据", '{"ok": 1}', '{"ok": 1}',
                          '{"ok": 1}', '{"ok": 1}', '{"ok": 1}']),
        config=cfg,
    )
    out = await analyze_job(ctx, jd_text="JD")
    assert out["job_analysis"] == {"ok": 1}
    assert out.get("warnings"), "部分降级必须带 warnings"


@pytest.mark.asyncio
async def test_analyze_jobs_batch_total_failure_is_an_error(tmp_path: Path) -> None:
    """批量两路全失败同样必须报错。"""
    cfg = ServerConfig(
        prompts_dir=tmp_path / "prompts", data_dir=tmp_path,
        profile_path=tmp_path / "profile.txt",
    )
    ctx = ToolContext(llm=FakeLLM(raises=RuntimeError("401 unauthorized")), config=cfg)
    with pytest.raises(ToolError, match="批量分析失败"):
        await analyze_jobs_batch(ctx, jobs=[{"title": "A"}])


@pytest.mark.asyncio
async def test_analyze_job_with_text(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    out = await analyze_job(ctx, jd_text="招聘 Agent 工程师")
    for section in (
        "job_analysis", "knowledge_priority", "interview_qa", "gap_analysis",
        "resume_advice", "project_iteration", "job_strategy",
    ):
        assert section in out
    assert out["prompt_meta"]["prompts"]  # 可追溯


@pytest.mark.asyncio
async def test_analyze_job_from_source_path(tmp_path: Path) -> None:
    """source_path 走服务端读文件（避免长文本当参数烧 token）。"""
    jd = tmp_path / "jd.md"
    jd.write_text("# 招聘 Agent 工程师\n要求 RAG 经验", encoding="utf-8")
    out = await analyze_job(make_ctx(tmp_path), source_path=str(jd))
    assert out["job_analysis"]


@pytest.mark.asyncio
async def test_analyze_job_requires_input(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="jd_text 或 source_path"):
        await analyze_job(make_ctx(tmp_path))


@pytest.mark.asyncio
async def test_analyze_job_with_pack(tmp_path: Path) -> None:
    out = await analyze_job(make_ctx(tmp_path), jd_text="JD", prompt_pack="presales")
    assert out["prompt_meta"]["pack"] == "presales"


@pytest.mark.asyncio
async def test_analyze_jobs_batch_with_jobs(tmp_path: Path) -> None:
    out = await analyze_jobs_batch(
        make_ctx(tmp_path),
        jobs=[{"title": "AI Agent 工程师", "company": "字节"}],
        keyword="Agent",
        city="北京",
    )
    assert out["job_count"] == 1
    assert out["keyword"] == "Agent"
    assert "prompt_meta" in out


@pytest.mark.asyncio
async def test_analyze_jobs_batch_accepts_json_string(tmp_path: Path) -> None:
    """``jobs`` 也接受 JSON 字符串。

    Dify / 扣子的 OpenAPI 插件路线无法声明嵌套对象数组（Dify 源码里会把它降级成
    STRING），只能把结构化数据当字符串传——这条兼容路径让它们仍能调用批量分析。
    """
    payload = json.dumps(
        [
            {"title": "AI Agent 工程师", "company": "A"},
            {"title": "RAG 算法工程师", "company": "B"},
        ],
        ensure_ascii=False,
    )
    out = await analyze_jobs_batch(make_ctx(tmp_path), jobs=payload, keyword="Agent")
    assert out["job_count"] == 2


@pytest.mark.asyncio
async def test_analyze_jobs_batch_accepts_wrapped_json_string(tmp_path: Path) -> None:
    """``{"jobs": [...]}`` 形态的字符串同样支持。"""
    payload = json.dumps({"jobs": [{"title": "售前架构师", "company": "C"}]}, ensure_ascii=False)
    out = await analyze_jobs_batch(make_ctx(tmp_path), jobs=payload)
    assert out["job_count"] == 1


@pytest.mark.asyncio
async def test_analyze_jobs_batch_string_malformed_json_raises(tmp_path: Path) -> None:
    """**参数**里的字符串若「像 JSON 但坏了」必须报错。

    否则平台把结构化数据截断后，会被静默当成「一段乱码 JD」分析掉，调用方很难察觉
    （对照：``source_path`` 的文件内容仍保持宽容，见下一条）。
    """
    with pytest.raises(ToolError, match="看起来是 JSON"):
        await analyze_jobs_batch(make_ctx(tmp_path), jobs='[{"title": "A"}, {"title"')


@pytest.mark.asyncio
async def test_analyze_jobs_batch_string_plain_text_is_a_single_jd(tmp_path: Path) -> None:
    """不以 ``[/{`` 开头的字符串仍按「单个 JD 正文」处理（宽容路径保留）。"""
    out = await analyze_jobs_batch(make_ctx(tmp_path), jobs="招聘 Agent 工程师，要求熟悉 Python")
    assert out["job_count"] == 1


@pytest.mark.asyncio
async def test_analyze_jobs_batch_string_empty_json_list(tmp_path: Path) -> None:
    """空数组 → 0 个职位（不报错，但 job_count 为 0，调用方可据此察觉）。"""
    out = await analyze_jobs_batch(make_ctx(tmp_path), jobs="[]")
    assert out["job_count"] == 0


@pytest.mark.asyncio
async def test_analyze_jobs_batch_from_source_path(tmp_path: Path) -> None:
    f = tmp_path / "jobs.json"
    f.write_text(json.dumps([{"title": "A", "company": "C"}, {"title": "B"}]), encoding="utf-8")
    out = await analyze_jobs_batch(make_ctx(tmp_path), source_path=str(f))
    assert out["job_count"] == 2


@pytest.mark.asyncio
async def test_analyze_jobs_batch_requires_input(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="jobs 或 source_path"):
        await analyze_jobs_batch(make_ctx(tmp_path))


def test_profile_roundtrip(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    assert get_profile(ctx)["profile"] == ""
    save_profile(ctx, "name: 我\n目标岗位: Agent 开发")
    assert "Agent 开发" in get_profile(ctx)["profile"]


def test_profile_file_is_0600(tmp_path: Path) -> None:
    """画像属个人信息，落盘按敏感文件对待。"""
    ctx = make_ctx(tmp_path)
    save_profile(ctx, "secret")
    assert oct((tmp_path / "profile.txt").stat().st_mode)[-3:] == "600"


def test_list_prompt_packs(tmp_path: Path) -> None:
    out = list_prompt_packs(make_ctx(tmp_path))
    assert {p["name"] for p in out["packs"]} >= {"presales", "product", "engineering"}
    assert out["local_files"] == []


def test_sync_prompts_writes_and_is_idempotent(tmp_path: Path) -> None:
    """离线模式（remote=False）：直接用包内提示词，测试自洽不依赖网络。"""
    ctx = make_ctx(tmp_path)
    first = sync_prompts(ctx, pack="presales", remote=False)
    assert first["written"] and not first["skipped"]
    assert (tmp_path / "prompts" / "批量职位分析.md").exists()

    second = sync_prompts(ctx, pack="presales", remote=False)
    assert not second["written"] and second["skipped"]


def test_sync_prompts_unknown_pack(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="未知 pack"):
        sync_prompts(make_ctx(tmp_path), pack="不存在", remote=False)


def test_sync_prompts_remote_falls_back_to_package(tmp_path: Path) -> None:
    """远端不可达时自动回落包内，并在结果里说明实际生效的源。"""
    ctx = make_ctx(tmp_path)
    out = sync_prompts(ctx, pack="presales", source=(tmp_path / "无此源").as_uri())
    assert out["source"] == "package"
    assert out["used_fallback"] is True
    assert out["errors"] and out["note"]
    assert (tmp_path / "prompts" / "批量职位分析.md").exists()


def test_sync_prompts_output_is_usable(tmp_path: Path) -> None:
    """同步到本地的提示词必须是完整可用的（含 JSON 骨架）。"""
    ctx = make_ctx(tmp_path)
    sync_prompts(ctx, pack="presales", remote=False)
    text = (tmp_path / "prompts" / "批量职位分析.md").read_text(encoding="utf-8")
    assert "年包" in text and "track_heatmap" in text


@pytest.mark.asyncio
async def test_analyze_job_per_request_profile_beats_saved(tmp_path: Path) -> None:
    """按请求传入的画像必须**压过**已保存的全局画像。

    多用户宿主（SEKB）每个请求的用户不同，而 ``save_profile`` 是进程级全局状态——
    没有这个参数，SEKB 切到 MCP 后就会把 A 的画像用到 B 的分析上。
    """
    captured: list[str] = []

    class SpyLLM:
        async def complete(self, role, messages):
            captured.extend(m.content for m in messages)
            return '{"ok": 1}'

    cfg = ServerConfig(
        prompts_dir=tmp_path / "prompts", data_dir=tmp_path, profile_path=tmp_path / "p.txt"
    )
    ctx = ToolContext(llm=SpyLLM(), config=cfg)
    save_profile(ctx, "全局画像-不应该出现")

    await analyze_job(ctx, jd_text="JD", user_profile="请求画像-应该出现")
    assert any("请求画像-应该出现" in c for c in captured)
    assert not any("全局画像-不应该出现" in c for c in captured)


@pytest.mark.asyncio
async def test_analyze_job_falls_back_to_saved_profile(tmp_path: Path) -> None:
    """不传 user_profile 时仍用已保存的画像（单用户场景）。"""
    captured: list[str] = []

    class SpyLLM:
        async def complete(self, role, messages):
            captured.extend(m.content for m in messages)
            return '{"ok": 1}'

    cfg = ServerConfig(
        prompts_dir=tmp_path / "prompts", data_dir=tmp_path, profile_path=tmp_path / "p.txt"
    )
    ctx = ToolContext(llm=SpyLLM(), config=cfg)
    save_profile(ctx, "已保存画像-应出现")
    await analyze_job(ctx, jd_text="JD")
    assert any("已保存画像-应出现" in c for c in captured)


@pytest.mark.asyncio
async def test_analyze_jobs_batch_per_request_profile(tmp_path: Path) -> None:
    """批量分析同样支持按请求注入画像。"""
    captured: list[str] = []

    class SpyLLM:
        async def complete(self, role, messages):
            captured.extend(m.content for m in messages)
            return '{"track_heatmap": []}'

    cfg = ServerConfig(
        prompts_dir=tmp_path / "prompts", data_dir=tmp_path, profile_path=tmp_path / "p.txt"
    )
    ctx = ToolContext(llm=SpyLLM(), config=cfg)
    await analyze_jobs_batch(ctx, jobs=[{"title": "A"}], user_profile="批量请求画像")
    assert any("批量请求画像" in c for c in captured)


def test_prompts_dir_env_is_honored(monkeypatch) -> None:
    """`JOBCOPILOT_PROMPTS_DIR` 必须生效。

    宿主（SEKB）靠它把自身的提示词目录传给内核；漏读会让内核悄悄改用包内 base，
    宿主的「提示词热改」能力**静默失效**（本地目录 bind mount 白挂了）。
    """
    from jobcopilot.mcp.config import ServerConfig, default_prompts_dir

    monkeypatch.setenv("JOBCOPILOT_PROMPTS_DIR", "/tmp/host-prompts")
    assert str(default_prompts_dir()) == "/tmp/host-prompts"
    assert str(ServerConfig.from_env().prompts_dir) == "/tmp/host-prompts"


def test_prompts_dir_falls_back_to_data_dir(monkeypatch) -> None:
    """未设置时回落到 <data_dir>/prompts。"""
    from jobcopilot.mcp.config import default_prompts_dir

    monkeypatch.delenv("JOBCOPILOT_PROMPTS_DIR", raising=False)
    monkeypatch.setenv("JOBCOPILOT_DATA_DIR", "/tmp/jc-data")
    assert str(default_prompts_dir()) == "/tmp/jc-data/prompts"


@pytest.mark.asyncio
async def test_tool_reports_per_call_usage(tmp_path: Path) -> None:
    """内核必须把**本次调用**的 token 用量报回宿主。

    走 MCP 后是内核自己调 LLM，宿主的 LLM 工厂看不到这些调用——
    没有这个字段，宿主的费用统计会出现黑洞。
    """

    class UsageLLM:
        def __init__(self) -> None:
            self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

        async def complete(self, role, messages):
            self.usage["calls"] += 1
            self.usage["prompt_tokens"] += 100
            self.usage["completion_tokens"] += 20
            return '{"ok": 1}'

    llm = UsageLLM()
    cfg = ServerConfig(
        prompts_dir=tmp_path / "prompts", data_dir=tmp_path, profile_path=tmp_path / "p"
    )
    ctx = ToolContext(llm=llm, config=cfg)

    out = await analyze_job(ctx, jd_text="JD")
    assert out["usage"]["calls"] == 7          # 单职位 7 步
    assert out["usage"]["prompt_tokens"] == 700

    # 第二次调用只应报**增量**，不是累计
    out2 = await analyze_job(ctx, jd_text="JD")
    assert out2["usage"]["calls"] == 7
    assert out2["usage"]["prompt_tokens"] == 700


@pytest.mark.asyncio
async def test_batch_reports_usage(tmp_path: Path) -> None:
    """批量分析同样回报用量（两路 LLM）。"""

    class UsageLLM:
        def __init__(self) -> None:
            self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

        async def complete(self, role, messages):
            self.usage["calls"] += 1
            self.usage["prompt_tokens"] += 50
            return '{"track_heatmap": []}'

    cfg = ServerConfig(
        prompts_dir=tmp_path / "prompts", data_dir=tmp_path, profile_path=tmp_path / "p"
    )
    ctx = ToolContext(llm=UsageLLM(), config=cfg)
    out = await analyze_jobs_batch(ctx, jobs=[{"title": "A"}])
    assert out["usage"]["calls"] == 2
    assert out["usage"]["prompt_tokens"] == 100
