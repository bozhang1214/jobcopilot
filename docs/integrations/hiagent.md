# 接入火山引擎 HiAgent / AgentKit

> ## ⚠️ 隐私提示（接入前必读）
>
> HiAgent **没有公开的专属隐私条款**，可引用同生态的火山引擎条款：
>
> - 火山引擎 [数据处理协议（DPA）](https://www.volcengine.com/docs/6391/67493)：原则上在
>   **中国大陆**存储，除客户指定否则不出境；
> - [方舟专用条款](https://www.volcengine.com/docs/82379/1104498) **3.7.7**「未经您的
>   单独同意，不会存储和使用您的数据来训练或优化模型」；
> - 同条款 **3.7.9**「使用官方插件服务时，**您的数据将被发送至插件内进行处理**」
>   ——这句正好可以用来向使用者解释工具调用链路。
>
> 数据流向：`HiAgent → 你的 MCP 端点 → LLM 厂商`。完整对照见 [`PRIVACY.md`](PRIVACY.md)。

---

## 1. 支持情况

HiAgent **在火山文档中心没有公开库**（其产品页也无文档入口），但**官方开源 SDK
`volcengine/hiagent-go-sdk` 的 `hibot` 包**（HiAgent 私有化版，代号 Hibot）给出了字段级证据：

| 项 | 结论 |
|---|---|
| 是否支持 MCP | ✅ 支持（README：`MCP \| 外部 Streamable HTTP / Stdio MCP Server`） |
| 传输方式 | **Streamable HTTP**（+ stdio） |
| 配置方式 | 控制台菜单**未见公开文档**；可绕开控制台走 **TOP API**（`ServerService = "hibot-server"`）/ CLI / SDK |
| 鉴权 | `Headers map[string]string`（**任意 header 名**），另有 `AuthType` / `CredentialProviderID` |
| 其他字段 | `Name` / `Description` / `Endpoint`（服务端字段名 `URL`）/ `ToolAllowlist` / `ToolDenylist` / `ToolPrefix` / `Timeout` |

```
https://<你的域名>/mcp
```

URL 官方示例一律以 `/mcp` 结尾（**无强制后缀**），与我们的端点一致。

> ❓ **未查到**：控制台 MCP 菜单路径、HiAgent 专属隐私条款、插件 OpenAPI 规范。
> ⚠️ **推测**：不强制公网 HTTPS（SDK 测试用 `http://mcp.local/mcp`）、可能支持内网
> ——均未证实，接入前请以实际控制台为准。

## 2. 同生态更推荐：AgentKit（有完整公开文档）

若你不确定用 HiAgent 还是 AgentKit，**AgentKit 的文档最全**：
入口「网关 > MCP > MCP 服务 > 创建」，三种方式（部署 / 导入 / HTTP 转 MCP）。

**三条硬限制**：

1. **仅 Streamable HTTP**（协议 ≥ 2025-03-26；更低版本需用 `mcp-proxy` 包一层）；
   官方原文明确**不支持 stdio 与 SSE-only 的 MCP Server**；
2. **后端不支持私网 IP，必须用域名**；
3. **MCP 工具集不接受「仅私网访问」的服务**。

关键 API 字段：`ProtocolType=MCP`、`BackendType=Domain`、`Path=/mcp`（**可自定义**）、
`TlsMode=DISABLE|SIMPLE`（后端用 http 也行）、`EnablePublicNetwork` /
`EnablePrivateNetwork` + `VpcId`、出站 ApiKey `ApiKeyLocation=HEADER` +
**`Parameter` 可自定义**（即可以用 `X-JobCopilot-Api-Key`）。

另有 **DataAgent** 的 MCP 服务管理：支持 StreamableHTTP / SSE / OpenAPI + Header 鉴权，
但**要求协议版本 ≥ 2025-06-18**。

## 3. 本平台特有的坑

1. **AgentKit 不支持 SSE-only** —— 而我们两条传输都有，正好兼容；反过来千帆只吃 SSE。
   这就是必须同时暴露 `/mcp` 与 `/sse` 的原因。
2. **AgentKit 无状态轮询计费减半**（24 次/时 vs 60 次/时）——我们的工具是**无状态**的，
   天然符合。
3. **OpenAPI 路线（AgentKit 作官方答案）**：仅 Swagger 2.0 / OpenAPI 3.0（**无 3.1**）、
   文件 ≤ 5 MiB、**`operationId` 建议设置且会映射为 MCP 工具名（缺失会导致工具列表为空）**、
   方法仅 GET/POST/PUT/DELETE/PATCH、Content-Type 仅 `application/json` 与
   `x-www-form-urlencoded`、响应只解析 200/201/default 取 1 个、array 序列化为 CSV、
   **不支持 `file`**。
4. **HiAgent 侧无公开文档**：字段以 SDK 为准，接入时建议先用 SDK/TOP API 跑通再谈控制台。

## 4. 相关链接

- HiAgent Go SDK（`hibot` 包，MCP 字段证据）：<https://github.com/volcengine/hiagent-go-sdk>
- 火山引擎数据处理协议：<https://www.volcengine.com/docs/6391/67493>
- 方舟专用条款（3.7.7 / 3.7.9）：<https://www.volcengine.com/docs/82379/1104498>
