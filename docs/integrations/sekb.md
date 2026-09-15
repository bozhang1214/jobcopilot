# 接入 SEKB（嵌入宿主，宿主自管模型调用）

SEKB（Self-Evolving Knowledge Base）是最早的宿主：它的「求职助理」模块把 JobCopilot
当**内核**用，自己负责爬取职位、存库、出报告、暴露给前端。

本文记录**宿主侧**要做的接线与踩过的坑，供其他想嵌入 JobCopilot 的项目参考。

---

## 1. 接入方式：MCP over stdio

```
SEKB backend（FastAPI，容器内）
   └─ 子进程：jobcopilot-mcp（stdio）
         └─ 读提示词 / 调 LLM / 返回结构化报告
```

为什么走 MCP 而不是直接 `import jobcopilot`：

- **进程隔离**：内核崩溃/卡死不会带倒宿主；升级内核不必改宿主代码；
- **协议是公开的**：同一套内核能力也能被 DSH、云端平台复用；
- 宿主仍然可以**回退**到直接 import（见 §4）。

## 2. 配置

```yaml
job:
  transport: mcp                     # mcp（默认）| direct（应急回退）
  mcp_command: jobcopilot-mcp        # 容器内 PATH 上的入口
  mcp_timeout_s: 180.0               # 批量分析耗时长，默认 60s 会超时
  mcp_connect_timeout_s: 30.0
```

宿主用 `MCPClient` 维护**一条长连接**（不要每次请求都拉起子进程——启动开销约 1 秒）。

## 3. 三个必须处理的点

### 3.1 提示词热改不能被内核默认值覆盖

宿主的 `prompt/job` 目录是 bind mount，运营可以直接改提示词。内核按
「请求级 override → **宿主本地目录** → packs → 包内 base」的顺序解析，因此宿主**必须**
把目录路径通过环境变量传进去：

```bash
JOBCOPILOT_PROMPTS_DIR=/app/prompt/job
```

> ⚠️ 这个变量曾「文档里写了、代码里没读」，导致宿主的提示词被**静默**换成内核自带的
> base——热改能力悄悄失效，且没有任何报错。宿主侧请用
> `jobcopilot doctor` / 健康检查确认**实际生效的提示词来源**（见 §3.3）。

### 3.2 缺 Key 时内核的行为

内核在**没有 API Key 时照常启动**，只在需要 LLM 的工具被调用时返回可操作的错误。
这是刻意的：如果内核启动即退出，MCP 客户端（DSH / Claude Desktop）只会显示「没有工具」，
用户完全看不出是缺 Key，而且连 `list_prompt_packs` 这类**不需要 LLM** 的工具也用不了。

宿主侧应当在内核预热阶段（`bootstrap`）做一次探活，把「内核缺 Key」变成一条明确的
启动日志，而不是等用户点分析才报错。

### 3.3 把内核版本暴露进健康检查

```json
GET /api/v1/health/  →  {
  "kernel": {
    "name": "jobcopilot", "version": "0.1.0",
    "commit": "9bc394e…",            // 构建时注入的内核 commit
    "prompts": 12,                    // 包内 base 提示词数
    "prompt_source": "local",         // 本次实际生效的来源
    "prompt_dir": "/app/prompt/job",  // 宿主热改目录
    "healthy": true
  }
}
```

`prompt_source` 是关键：`local` 表示用的是宿主目录（符合预期）；若变成 `base`，
说明宿主的提示词**没被读到**，热改已经失效——这是一个必须能立刻看出来的信号。

## 4. 应急回退：direct 模式

`transport: direct` 时宿主直接 import 内核（同一进程）。用于排查「是内核的问题还是
MCP 接线的问题」：

```yaml
job:
  transport: direct
```

两条路径共用同一套内核实现，所以行为差异只可能来自传输层（超时、序列化、环境变量传递）。

## 5. 宿主侧的已知约束

| 约束 | 原因 |
|---|---|
| `source_path` 在宿主容器内**默认可用**（stdio 传输），但宿主应把可读范围限制在职位数据目录 | 内核的路径安全检查用 `JOBCOPILOT_SOURCE_ROOT` 限定目录 |
| 批量分析要设 ≥ 180s 超时 | 88 个职位常超过 MCP 客户端默认的 60s |
| 内核升级要**同步更新子模块指针** | 否则「仓库钉的版本」与「容器里跑的版本」不一致 |
| 多用户场景**不要**用 `save_profile` | 画像会串；改用按请求注入的 `user_profile` 参数 |

## 6. 相关文件（SEKB 侧）

| 文件 | 作用 |
|---|---|
| `backend/app/agents/job/mcp_client.py` | 共享 MCP 连接、命令解析、清晰的失败信息 |
| `backend/app/agents/job/generator.py` | 单职位分析走 MCP |
| `backend/app/agents/job/market.py` | 批量分析走 MCP |
| `backend/app/core/kernel_info.py` | 把内核版本/提示词来源暴露进 `/health` |
| `backend/app/core/bootstrap.py` | 启动时预热内核并打印内核信息 |
| `scripts/check_kernel.sh` | 子模块版本与仓库钉住的 commit 是否一致 |
