# 接入 Dify

> ## ⚠️ 隐私提示（接入前必读）
>
> 用 Dify 调用 JobCopilot 时，JD 正文会经 Dify 服务器转发到你的 MCP 端点，再到 LLM 厂商：
> `Dify 应用 → （Dify 服务器）→ 你的 MCP 端点 → LLM 厂商`。
>
> - **Dify Cloud（SaaS）**：JD 会经过 Dify 的服务器；
> - **自托管 Dify**：转发发生在你自己的 `api` 容器里，数据不出你的基础设施
>   （但**仍会发给 LLM 厂商**。
>
> 无论哪种，隐私等级都**低于**本地 stdio 模式。完整对照见 [`PRIVACY.md`](PRIVACY.md)。

---

## 1. 支持情况

| 项 | 结论 |
|---|---|
| 是否支持 MCP | ✅ 支持（作为 MCP **客户端**） |
| 传输方式 | **仅 HTTP 传输** = Streamable HTTP 或 HTTP+SSE；**不支持 stdio** |
| 配置入口 | 3.12.x：**集成 > 工具 > MCP** → **添加 MCP 服务器 (HTTP)**；3.5.x 及更早：**工具 > MCP** |

## 2. 配置

| 字段 | 说明 |
|---|---|
| 服务器 URL | 精确填 `https://<你的域名>/mcp`（无后缀重写） |
| 名称 / 图标 | 任意 |
| 服务器 ID | 小写字母、数字、下划线、连字符，**≤ 24 字符**；**改了会让已引用它的应用失效** |
| 自定义请求头 | 填鉴权：`Authorization: Bearer <Key 或令牌>`；也可加 `X-JobCopilot-Provider` |
| 请求超时 / SSE 读取超时 | **务必调大**（见下） |
| 动态客户端注册（DCR） | **静态令牌场景请关掉** |

**鉴权头**同扣子那张表：服务端未设 `JOBCOPILOT_HTTP_TOKEN` 时 `Authorization` 可放
LLM Key；设了令牌时它放令牌，LLM Key 另放 `X-JobCopilot-Api-Key`。

### 两个必须注意的开关

1. **关掉 DCR**：Dify 默认用「动态客户端注册」走 OAuth 发现流程；面对只认静态
   `Authorization: Bearer` 的服务端，先走 OAuth 会得到非预期行为（此点为基于官方
   「支持就保持开启」表述的反推，建议实际接入时验证一次）。
2. **调大超时**：默认很可能不足以完成 `analyze_jobs_batch`。这是**选 MCP 而不是
   OpenAPI 路线的最实际理由**——MCP 路线的超时在界面里可调，OpenAPI 路线难调。

## 3. 本平台特有的坑

1. **不要用 OpenAPI（Swagger API）路线**，除非 MCP 确实走不通。其原因有源码级证据：
   - 解析器**完全没有 `oneOf` / `anyOf` 处理**；
   - 嵌套 `object` 属性的类型会**退化成 STRING** → 模型必须自己产出 JSON 文本，极易出错；
     而 `analyze_jobs_batch` 的 `jobs` 正是嵌套数组；
   - `requestBody.content` 会被**逐个 media type** 展开，声明多种会产生重复参数；
   - 缺 `operationId` 时自动生成，根路径会生成含 `<` / `>` 的非法值（Dify issue #19887）；
     显式 `operationId` 需匹配 `^[a-zA-Z0-9_-]{1,64}$`；
   - 必须有不空的 `servers`（只取 `servers[0]`），Swagger 2.0 形态会 KeyError 而非优雅回退；
   - `$ref` 只支持文档内 `#/` 本地引用；list/dict 默认值会被丢弃。
2. **OpenAPI 路线默认 60 秒超时**且历史上难以调整（Dify issue #6304），批量分析很容易撞上。
3. **返回体积**：社区 issue（#18731）报告响应超过约 **68000 字符**时，Agent 节点的
   `tool_response` 会变成空字符串。批量市场报告的完整 JSON 有超过这个量的风险——
   建议减少单次职位数量，或只取报告中的关键字段。
4. **`{{request.headers.X}}` 占位符**：把调用方 header 透传给 MCP Server。注意在**没有源
   HTTP 请求**的运行方式下（如定时任务），占位符会被**原样发出**而不是留空。
5. **自托管可访问内网**（转发在你的 `api` 容器里），但工具 HTTP 调用经过 `ssrf_proxy`，
   内网地址可能需要在部署侧放行。

## 4. 相关链接

- 工具 / MCP（3.12.x）：<https://enterprise-docs.dify.ai/zh/3.12.x/use/workspace/tools#mcp>
- 使用 MCP 工具（3.5.x）：<https://enterprise-docs.dify.ai/zh/3.5.x/use/build/mcp>
- OpenAPI 解析器源码：<https://github.com/langgenius/dify/blob/main/api/core/tools/utils/parser.py>
- 根路径 operationId issue：<https://github.com/langgenius/dify/issues/19887>
- OpenAPI 60s 超时 issue：<https://github.com/langgenius/dify/issues/6304>
- 大返回变空 issue：<https://github.com/langgenius/dify/issues/18731>
- 插件隐私政策指南：<https://enterprise-docs.dify.ai/zh/3.12.x/develop/plugins/publishing/standards/privacy-protection-guidelines>
