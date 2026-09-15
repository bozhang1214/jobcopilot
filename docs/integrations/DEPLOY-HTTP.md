# 把 MCP HTTP 端点暴露给云端平台

云端平台（扣子 / 百炼 / 千帆 / HiAgent / Dify）都要求**公网可达**的地址，
扣子还不接受 IP、必须是域名。本文给出从零到可用的完整步骤。

> 只有一个平台可以走内网：**自托管 Dify**（转发发生在你自己的 `api` 容器里）。
> 其余平台都必须公网 HTTPS。

---

## 0. 前置条件

| 需要 | 说明 |
|---|---|
| 一台能出网的服务器 | 你已有的机器即可 |
| 一个域名 + HTTPS 证书 | 扣子明确要求域名；MCP 客户端普遍要求 `https://` |
| 24 字节以上的随机令牌 | `openssl rand -hex 24` |

## 1. 先在本机跑通（不暴露）

```bash
pip install "jobcopilot[mcp]"
export JOBCOPILOT_LLM_API_KEY=sk-xxx
jobcopilot-mcp --http --port 8765      # 默认 127.0.0.1，仅本机可访问
```

自检（返回 `400 Missing session ID` 就说明端点活着——这是没有 `Mcp-Session-Id` 时的
正常响应，不是错误）：

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8765/mcp \
  -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# → 400
```

## 2. 起成常驻服务

用 systemd 托管（推荐），`/etc/systemd/system/jobcopilot-mcp.service`：

```ini
[Unit]
Description=JobCopilot MCP Server (streamable HTTP)
After=network-online.target

[Service]
User=jobcopilot
WorkingDirectory=/opt/jobcopilot
EnvironmentFile=/opt/jobcopilot/env            # 权限设 600，放 Key 与令牌
ExecStart=/opt/jobcopilot/.venv/bin/jobcopilot-mcp --http --host 127.0.0.1 --port 8765
Restart=always
RestartSec=3
# 只监听回环，由 nginx 终结 TLS —— 这样端口本身不对公网暴露
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

`/opt/jobcopilot/env`（`chmod 600`）：

```bash
JOBCOPILOT_HTTP_TOKEN=<openssl rand -hex 24 的输出>
JOBCOPILOT_HTTP_ALLOWED_HOSTS=mcp.example.com
JOBCOPILOT_LLM_PROVIDER=deepseek
# 服务端默认 Key 可以不配 —— 让每个调用方带自己的（BYOK）
```

> ⚠️ **`JOBCOPILOT_HTTP_ALLOWED_HOSTS` 必须填 nginx 传给后端的那个 Host**，
> 也就是**对外域名**（见第 3 步的 `proxy_set_header Host $host;`）。
> 漏配或不一致 → 平台收到 `421 Invalid Host header`。这是最容易踩的一步。
>
> 该变量只支持**精确域名**或 `域名:*`；不支持 `*.example.com` 通配
> （裸域名会自动补 `域名:*`，所以 `mcp.example.com` 与 `mcp.example.com:443` 都能匹配）。

`--host 127.0.0.1` 时程序不做令牌强制校验（本机自己用不需要），但既然要经 nginx
对外服务，**令牌仍应配上**——nginx 会把外部的 `Authorization` 头透传进来。

## 3. nginx 反向代理（streamable HTTP / SSE 的关键设置）

```nginx
server {
    listen 443 ssl;
    server_name mcp.example.com;

    # 证书略：certbot --nginx -d mcp.example.com

    location /mcp {
        proxy_pass http://127.0.0.1:8765/mcp;
        proxy_http_version 1.1;

        # 必须转发真实域名：后端用它做 Host 白名单校验
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE / 流式响应必须关缓冲，否则响应会被攒着不发
        proxy_buffering off;
        proxy_cache off;
        chunked_transfer_encoding on;

        # 批量分析耗时较长，超时要放大（默认 60s 会把长任务切断）
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
```

`nginx -t && systemctl reload nginx` 后，从外部验证：

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://mcp.example.com/mcp \
  -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -H 'authorization: Bearer <你的令牌>' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# → 400（端点活着）；401 = 令牌不对；421 = Host 白名单没配对
```

## 4. 安全清单

- [ ] `JOBCOPILOT_HTTP_TOKEN` 已设为**足够长**的随机串（不是 `123456`）；
- [ ] `--host 127.0.0.1`，由 nginx 终结 TLS（不要把 8765 直接暴露）；
- [ ] `JOBCOPILOT_HTTP_ALLOWED_HOSTS` 与对外域名一致；
- [ ] **不要**设 `JOBCOPILOT_ALLOW_SOURCE_PATH=1`（公网端点开放任意文件读取）；
- [ ] 不要用 `--allow-public-bind` 绕过令牌校验（除非完全在内网且清楚代价）；
- [ ] 服务端**不配**默认 LLM Key，让调用方 BYOK —— 这样别人用不会花你的钱；
- [ ] 反向代理访问日志里不要记 `Authorization` 头（默认不记，确认一下）；
- [ ] 定期看服务的 `usage` 字段与日志量，异常流量能及早发现。

## 5. 平台侧的地址怎么填

| 平台 | 填什么 |
|---|---|
| 扣子 | MCP JSON 的 `url` = `https://mcp.example.com/mcp`；`headers.Authorization` = `Bearer <令牌或 Key>` |
| Dify | 服务器 URL = `https://mcp.example.com/mcp`；自定义请求头同上；**关掉 DCR**、调大超时 |

各平台的细节与坑见同目录 `coze.md` / `dify.md` / `bailian.md` / `qianfan.md` / `hiagent.md`。
