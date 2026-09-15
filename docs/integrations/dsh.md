# 接入 DSH（本地 MCP 客户端）

DSH（DeepSeek Harness）是本地开发环境。把 JobCopilot 接进去后，可以在对话里直接
贴 JD 出七段分析、或读本地职位文件出市场报告。

**隐私等级最高的一条路**：走 stdio，JD **不离开你的机器**（只发给 LLM 厂商）。

---

## 1. 安装

```bash
pip install "jobcopilot[mcp]"
jobcopilot-mcp --version      # 确认可执行文件在 PATH 上
```

## 2. 配置 DSH

DSH 用 MCP 客户端插件来连。配置文件（profile 的补丁层，如
`~/.dsh/profiles/<profile>/cordis.patch.yml`）：

```yaml
- insert:                      # ⚠️ 必须用 insert: 包裹，见 §4 坑 1
    - id: mcp-jobcopilot
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: jobcopilot
        transport: stdio
        command: jobcopilot-mcp
        env:
          JOBCOPILOT_LLM_PROVIDER: deepseek
          JOBCOPILOT_LLM_API_KEY: !!js process.env.JOBCOPILOT_LLM_API_KEY
        toolCallTimeoutMs: 180000    # ⚠️ 默认 60000 会让批量分析超时，见 §4 坑 2
        failOnStartupError: true
```

`@deepseek-ai/dsh-mcp-client` 不在默认 bundle 里，需要先装：

```bash
dsh plugin --profile <profile> add @deepseek-ai/dsh-mcp-client
```

> 已经把它打包成一个可直接用的插件仓库（README + 补丁模板）：
> `jobcopilot-dsh-plugin`。里面的 `README.md` 就是给最终用户的安装说明。

## 3. 怎么用

工具会以 `mcp__jobcopilot__<工具名>` 出现（6 个）：

| 工具 | 用途 |
|---|---|
| `analyze_job` | 单职位七段深度分析 |
| `analyze_jobs_batch` | 批量市场报告（赛道/技能门槛/薪资锚点 + 知识迭代） |
| `get_profile` / `save_profile` | 读写求职者画像 |
| `list_prompt_packs` | 列出职能包 |
| `sync_prompts` | 把提示词同步到本地目录（之后可直接改） |

典型对话：

> 用 `mcp__jobcopilot__analyze_jobs_batch` 分析 `/Users/me/jobs.json`（用 `source_path` 传路径），
> 关键词「Agent」，城市「北京」。

**优先用 `source_path`**：几十上百个职位当参数传会烧掉大量 token，让服务端自己读文件
（stdio 模式下服务端就是你的机器，安全）。

## 4. 两个会让人白折腾半天的坑

### 坑 1：补丁层新增插件必须用 `insert:` 包裹

DSH 的 profile 补丁层是**按 id 覆盖**已有条目的。直接写

```yaml
- id: mcp-jobcopilot
  name: '@deepseek-ai/dsh-mcp-client'
```

会被当成「覆盖一个已有条目」，由于该 id 不存在，DSH 只留一句
`patch: entry "mcp-jobcopilot" not found` 后**静默跳过**——插件根本没加载。
正确写法是外层包一个 `insert:`（见 §2）。

### 坑 2：`toolCallTimeoutMs` 默认 60 秒

批量分析（几十个职位）经常超过 60 秒，超时后工具调用会被中断，看起来像「内核挂了」。
调到 `180000`。

### 验证是否真的生效

**不要**用 `dsh --dump-config | grep mcp-jobcopilot` 判断——那句
`not found` 警告里**也含**这个字符串，会给出假阳性。正确做法是直接问模型：

> 把你可用的、名字以 `mcp__jobcopilot__` 开头的工具名列出来。

列得出 6 个才算接上了。

## 5. 本机用 HTTP 形态（可选）

需要让局域网内其他设备或云端平台用时才需要（见
[`DEPLOY-HTTP.md`](DEPLOY-HTTP.md)）。本机用 stdio 最简单，也最私密。
