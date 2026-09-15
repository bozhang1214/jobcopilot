# 云端平台接入文档

JobCopilot 的 MCP Server 可以接到各云端平台。**先读 [`PRIVACY.md`](PRIVACY.md)**——
云端接入意味着 JD 会经过平台方服务器，隐私等级低于本地 stdio 模式。

## 我该走哪条？

| 场景 | 走哪条 |
|---|---|
| 只在自己电脑上用 | 不用看这里，直接用 **stdio**（DSH / Claude Desktop / Cursor） |
| 自托管 Dify（能访问内网） | `dify.md`，可直接填内网地址 |
| 其他云端平台 | 先按 [`DEPLOY-HTTP.md`](DEPLOY-HTTP.md) 把端点暴露成**公网 HTTPS 域名** |

## 平台对照

| 平台 | MCP 传输 | `url` 填什么 | 鉴权怎么放 | 主要限制 |
|---|---|---|---|---|
| [扣子 Coze](coze.md) | Streamable HTTP / SSE | `https://域名/mcp` | `headers.Authorization`，或 `X-JobCopilot-Api-Key` | **不接受 IP**，必须公网域名；内网需企业旗舰版私网插件 |
| [阿里百炼](bailian.md) | stdio / SSE / Streamable HTTP | `https://域名/mcp`（`streamableHttp` ↔ `POST /mcp`） | 自定义 header | 自签名证书报 `MCP_SSL_ERROR`；工作流 1 节点 1 工具 |
| [百度千帆](qianfan.md) | 🔴 **仅 SSE** | `https://域名/sse?token=<令牌>` | **只能放查询串**（配置里没有 headers 字段） | 返回 ≤ 1M；CFC 节点超时约 10 秒 |
| [火山 HiAgent](hiagent.md) | Streamable HTTP（+ stdio） | `https://域名/mcp` | `Headers`（任意名） | 无公开文档；AgentKit 不支持 SSE-only |
| [Dify](dify.md) | HTTP（Streamable HTTP / SSE） | `https://域名/mcp` | 自定义请求头 | 必须关掉 DCR；默认超时 60 秒需调大 |

## 三平台共同的最小可行配置

调研（2026-02）得到的硬性条件，JobCopilot 已全部满足：

| # | 条件 | 状态 |
|---|---|---|
| 1 | 同时提供 `POST /mcp` 与 `GET /sse`（千帆只吃 SSE，AgentKit 只吃 Streamable HTTP） | ✅ 两条并存 |
| 2 | 鉴权 `Authorization: Bearer` 优先 + `?token=` 兜底（千帆没有 headers 字段） | ✅ |
| 3 | 公网可达 + 受信任 CA 的 HTTPS（自签名会被百炼拒） | ⚠️ 部署侧要求，见 `DEPLOY-HTTP.md` |
| 4 | 客户端发 `Accept: application/json, text/event-stream` | ✅ SDK 处理 |
| 5 | 工具名纯 `[a-z0-9_]` 无中文、入参标准 JSON Schema、避免 `oneOf`/`anyOf` | ✅ 有测试守着 |
| 6 | 调用无状态、可独立调用、返回体积可控 | ⚠️ 批量分析返回较大，见下 |
| 7 | 协议版本 ≥ 2025-06-18（火山 DataAgent 要求） | ✅ 随 mcp SDK |

## 已知的落地风险

1. **返回体积**：Dify 社区报告响应超过约 **68000 字符**时 Agent 节点的 `tool_response`
   会变成空字符串；千帆 API 节点硬限 **1M**。批量市场分析的完整 JSON 有超限风险 →
   **控制单次职位数量**，或只取报告里的关键字段。
2. **时延**：`analyze_jobs_batch` 在少量职位时约十几秒，职位多时会显著变长；千帆的
   函数计算节点超时口径只有约 10 秒，**基本不可用于批量**。批量场景建议用扣子 /
   百炼 / Dify（超时可调），或把批量分析放在你自建的应用里跑。
3. **数据合规**：`analyze_jobs_batch` 会把整批职位数据（可能含联系人、薪资等个人信息）
   送进模型上下文。各平台条款差异很大（千帆明确「不得提供保密信息、我们没有保密义务」），
   对外产品请自行完成告知同意与合规评估。
