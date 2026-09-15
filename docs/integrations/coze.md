# 接入扣子（Coze / coze.cn）

> ## ⚠️ 隐私提示（接入前必读）
>
> 用扣子调用 JobCopilot 时，**JD 正文会先到扣子的服务器，再由扣子调用你的 MCP 端点**。
> 数据流向为：`你的浏览器/扣子 Agent → 扣子服务器 → 你的 MCP 端点 → LLM 厂商`。
>
> 扣子隐私政策明确：智能体会「按照 MCP 扩展定义的接口规范，向你选择的 MCP 扩展服务
> 提供方提供请求信息」，且第三方 MCP **不适用**扣子隐私政策，需你与第三方另行约定。
>
> 因此**隐私等级低于本地 stdio 模式**——后者 JD 完全不离开你的机器。分析公司内部
> 未公开职位请改用本地模式。完整对照见 [`PRIVACY.md`](PRIVACY.md)。

---

## 1. 支持情况

| 项 | 结论 |
|---|---|
| 是否支持 MCP | ✅ 支持 |
| 传输方式 | **Streamable HTTP / SSE**；**不支持 stdio** |
| 配置入口 | 左侧导航 **扩展 > MCP** → 右上角**添加自定义 MCP** → 粘贴 MCP JSON |
| 管理与绑定 | 管理在 **我的 > 我的 MCP**；给 Agent 启用：**Agent 设置 > 扩展能力 > MCP**（下一轮对话生效） |
| 端侧限制 | 仅**网页端 / 桌面端**可创建自定义 MCP；建议每个 Agent 启用 **≤ 10 个** MCP（工具描述占上下文与积分） |

## 2. 配置

```json
{
  "mcpServers": {
    "jobcopilot": {
      "url": "https://<你的域名>/mcp",
      "headers": {
        "Authorization": "Bearer <你的 LLM Key 或访问令牌>",
        "X-JobCopilot-Provider": "deepseek"
      }
    }
  }
}
```

**鉴权头怎么填**（取决于你的服务端怎么部署）：

| 你的服务端 | `Authorization` 填什么 | 说明 |
|---|---|---|
| 自用（未设 `JOBCOPILOT_HTTP_TOKEN`） | **你的 LLM Key**，如 `Bearer sk-xxx` | 服务端会顺平台惯例把 `Authorization` 当 BYOK Key |
| 团队共用（设了 `JOBCOPILOT_HTTP_TOKEN`） | **服务访问令牌** | 此时 LLM Key 必须另放 `X-JobCopilot-Api-Key` 头 |

> `url` **必须精确写到 `/mcp`** —— MCP Streamable HTTP 是单端点，平台按你填的地址直接
> POST，不会替你补路径。
>
> `headers` 字段有官方旁证：扣子「查询插件详情」API 返回的 `mcp_json` 就是
> `{"url": "...", "headers": {"Authorization": "Bearer ${COZE_API_TOKEN}"}}` 这一形态。
> 但扣子官方 MCP 文档页未展开字段表，若粘贴后识别失败，改用下面的插件（OpenAPI）路线。

## 3. 本平台特有的坑

1. **扣子不接受 IP 地址，必须是公网域名**（插件导入文档明确「必须为域名格式，暂不支持
   IP 格式的 URL」，FAQ 也说明用 IP 会被网络安全策略拦）。所以 `http://<IP>:8765/mcp`
   **一定不行**，需要 `https://your-domain/mcp`。
2. **内网服务接不进来**：只有企业旗舰版的**私网模式插件**（要求服务部署在火山引擎华北 2，
   经 PrivateLink 打通）或申请**固定 IP 访问白名单**，企业能力，普通账号没有。
3. **调用来源是动态 IP**；要精准放行需申请固定 IP 白名单。
4. **无官方超时说明**。批量分析（`analyze_jobs_batch`）耗时可能较长，建议先用少量职位试；
   若频繁超时，请把批量任务拆小（见 `PRIVACY.md` 同目录下的性能说明）。
5. **工具描述占上下文与积分**：JobCopilot 暴露 6 个工具，若同时挂多个 MCP 注意额度。

## 4. 降级方案：用插件（OpenAPI）接入

MCP 走不通时才考虑。扣子的插件路线**导入限制**较多（官方文档 + 社区反馈）：

- 只认 **OpenAPI 3.0.x / Swagger 2.0 / Postman**（社区反馈 3.1 常失败）；
- **一次导入的多个接口必须共享同一 URL 路径前缀**，否则整批导入失败；
- 插件 URL 同样必须是域名；
- 入参要逐条声明传入方法（Body / Path / Query / Header），**嵌套对象如何处理官方未说明**
  （推测会退化成单个字符串参数）；
- 配额：自定义插件 QPS **≤ 50**、单插件 ≤ 100 工具。

> 本项目**没有**为插件路线单独生成 OpenAPI 文件；`analyze_jobs_batch` 的 `jobs` 参数
> 已额外支持**传 JSON 字符串**，正是为了兼容这类「无法声明嵌套数组」的平台。

## 5. 相关链接

- MCP 文档：<https://docs.coze.cn/mcp>
- 导入插件（域名/前缀限制）：<https://docs.coze.cn/guides_import>
- 插件常见问题（IP 被拦）：<https://docs.coze.cn/guides_plugin_faq>
- 私网模式插件：<https://docs.coze.cn/guides_private_plugin>
- 固定 IP 访问插件：<https://docs.coze.cn/guides_fixed_ip_access_plugin>
- 隐私政策（§一.3(5) MCP 数据流向、§十.2 第三方不适用）：<https://docs.coze.cn/guides_privacy>
