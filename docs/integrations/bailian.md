# 接入阿里云百炼（Bailian）

> ## ⚠️ 隐私提示（接入前必读）
>
> 百炼官方表述：「阿里云严格保护数据隐私，**绝不会将您的数据用于模型训练**」，传输
> AES-256 加密；但同时明确**「将存储模型与应用调用时产生的数据」**。
>
> 数据流向：`百炼 → 你的 MCP 端点 → LLM 厂商`。隐私等级**低于**本地 stdio 模式。
> 完整对照见 [`PRIVACY.md`](PRIVACY.md)。

---

## 1. 支持情况

| 项 | 结论 |
|---|---|
| 是否支持 MCP | ✅ 支持 |
| 传输方式 | **stdio + SSE + Streamable HTTP**（三选一） |
| 配置入口 | `https://bailian.console.aliyun.com/?tab=app#/mcp-manage` → 创建 MCP 服务 |
| 可选路径 | 官方 MCP（一键开通）/ **自定义 MCP →「使用脚本部署」→ `http`** / 从 AI 网关导入 / 从阿里云 OpenAPI 导入 |

## 2. 配置

```json
{
  "mcpServers": {
    "jobcopilot": {
      "type": "streamableHttp",
      "url": "https://<你的域名>/mcp",
      "headers": { "Authorization": "Bearer <你的 Key 或访问令牌>" }
    }
  }
}
```

- **`/mcp` 是硬约定路径**：官方 FAQ 明确「`"sse"` 对应 GET `/sse`，
  `"streamableHttp"` 对应 POST `/mcp`」，填错报 404/405。
- **支持自定义 header 键值对**，所以既可以用 `Authorization: Bearer`，也可以用
  `X-JobCopilot-Api-Key`（团队共用场景：`Authorization` 放服务令牌、Key 放这个头）。
- 百炼**不会**把平台自己的 key 注入你的服务，鉴权由你的服务端负责。

## 3. 本平台特有的坑

1. **必须公网可达 + 受信任 CA 的证书**：自签名证书会报 **`MCP_SSL_ERROR`**。
   用 Let's Encrypt 之类即可。
2. **不支持内网地址**，且函数计算托管**没有固定出口 IP**——想按来源 IP 限制访问
   需要自己配白名单/VPC 打通。
3. **智能体最多挂 5 个 MCP**；**工作流 MCP 节点 1 个节点只能选 1 个工具**
   （JobCopilot 有 6 个工具，工作流里要按需多节点或用智能体模式）。
4. 部署后**只能改名称/描述**，改配置要重建。
5. **「插件」通道挂不了已写好的 MCP Server**（社区实测），必须走
   「MCP 管理 → 使用脚本部署 → `http`」。别在插件页浪费时间。

## 4. 降级方案：OpenAPI

百炼**控制台不支持上传 OpenAPI 文件**（插件是表单式：插件 URL + 工具路径 + 入参/出参
+ 鉴权 Header/Query）。已知限制：仅 `application/json` 或 `x-www-form-urlencoded`；
GET 入参不支持 Object；参数必须有描述。**要导 OpenAPI 文件得走「AI 网关」**。

> 注意：`analyze_jobs_batch` 的 `jobs` 参数是嵌套数组，表单式配置里会退化成字符串
> ——本项目已支持传 **JSON 字符串**，正是为这类情况准备的。

## 5. 相关链接

- 创建 MCP 服务 / MCP 管理：<https://bailian.console.aliyun.com/?tab=app#/mcp-manage>
- 阿里云 MCP 文档（`streamableHttp` ↔ `POST /mcp` 的 FAQ 出处）：<https://help.aliyun.com/>
