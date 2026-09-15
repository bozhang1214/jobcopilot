"""MCP Server 的运行时配置（全部来自环境变量，BYOK）。

**不读配置文件、不写死 Key** —— MCP Server 常被客户端以子进程方式拉起，
环境变量是各客户端（DSH / Claude Desktop / 云端平台）唯一通用的传参方式。

| 变量 | 默认 | 说明 |
|---|---|---|
| ``JOBCOPILOT_LLM_PROVIDER`` | ``deepseek`` | 预设名（deepseek/qwen/kimi/doubao/zhipu/openai） |
| ``JOBCOPILOT_LLM_API_KEY`` | — | BYOK；不给则回落到预设自带的 ``DEEPSEEK_API_KEY`` 等 |
| ``JOBCOPILOT_LLM_MODEL`` | 预设默认 | 覆盖模型名 |
| ``JOBCOPILOT_PROMPTS_DIR`` | ``~/.jobcopilot/prompts`` | 本地提示词目录（优先级最高） |
| ``JOBCOPILOT_DATA_DIR`` | ``~/.jobcopilot`` | 画像等数据目录 |
| ``JOBCOPILOT_PROFILE`` | ``<data>/profile.txt`` | 画像文件路径 |
| ``JOBCOPILOT_ALLOW_SOURCE_PATH`` | stdio=true / http=false | 是否允许服务端读本地文件 |
| ``JOBCOPILOT_SOURCE_ROOT`` | 空（不限制） | 限定 ``source_path`` 只能落在该目录下 |
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

#: 环境变量名
ENV_PROVIDER = "JOBCOPILOT_LLM_PROVIDER"
ENV_API_KEY = "JOBCOPILOT_LLM_API_KEY"
ENV_MODEL = "JOBCOPILOT_LLM_MODEL"
ENV_PROMPTS_DIR = "JOBCOPILOT_PROMPTS_DIR"
ENV_DATA_DIR = "JOBCOPILOT_DATA_DIR"
ENV_PROFILE = "JOBCOPILOT_PROFILE"
ENV_ALLOW_SOURCE_PATH = "JOBCOPILOT_ALLOW_SOURCE_PATH"
ENV_SOURCE_ROOT = "JOBCOPILOT_SOURCE_ROOT"
#: HTTP 形态的访问令牌（``Authorization: Bearer <token>``）；非回环绑定必填
ENV_HTTP_TOKEN = "JOBCOPILOT_HTTP_TOKEN"
#: HTTP 形态允许的 Host 头（逗号分隔）。必须显式列出公网域名——SDK 默认只放行回环，
#: 不配会让云端平台收到 421 Invalid Host header。
ENV_HTTP_ALLOWED_HOSTS = "JOBCOPILOT_HTTP_ALLOWED_HOSTS"
#: HTTP 形态允许的 Origin 头（逗号分隔）；不配则拒绝带 Origin 的浏览器请求
ENV_HTTP_ALLOWED_ORIGINS = "JOBCOPILOT_HTTP_ALLOWED_ORIGINS"
#: 显式放弃「非回环必须带令牌」的保护（仅限可信内网；会打印醒目警告）
ENV_ALLOW_PUBLIC_BIND = "JOBCOPILOT_ALLOW_PUBLIC_BIND"

DEFAULT_PROVIDER = "deepseek"

#: 视为「本机」的绑定地址（这些地址不需要令牌 / Host 白名单）
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def _env_bool(name: str, default: bool) -> bool:
    """读布尔环境变量（``1/true/yes/on`` 为真）。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str) -> list[str]:
    """读逗号（或空白）分隔的列表环境变量，去空项。"""
    raw = os.environ.get(name, "")
    return [x.strip() for x in raw.replace(",", " ").split() if x.strip()]


def is_loopback_host(host: str) -> bool:
    """判断绑定地址是否只对本机可见。

    Args:
        host: 监听地址，如 ``127.0.0.1`` / ``0.0.0.0`` / ``::``。

    Returns:
        仅 ``127.0.0.1`` / ``localhost`` / ``::1`` 这类回环地址才为 ``True``；
        ``0.0.0.0`` 等通配地址一律视为「对外」。
    """
    return host.strip().lower() in LOOPBACK_HOSTS


def default_data_dir() -> Path:
    """默认数据目录（``$JOBCOPILOT_DATA_DIR`` 或 ``~/.jobcopilot``）。"""
    env = os.environ.get(ENV_DATA_DIR)
    return Path(env) if env else Path.home() / ".jobcopilot"


def default_prompts_dir() -> Path:
    """本地提示词目录：``$JOBCOPILOT_PROMPTS_DIR`` 优先，否则 ``<data_dir>/prompts``。

    ⚠️ 必须读这个环境变量：宿主（如 SEKB）靠它把**自己的**提示词目录传给内核
    （SEKB 的 ``prompt/job`` 是 bind mount，运营可热改）。漏读会导致内核悄悄
    改用包内 base——宿主的热改能力静默失效。
    """
    env = os.environ.get(ENV_PROMPTS_DIR, "").strip()
    return Path(env) if env else default_data_dir() / "prompts"


@dataclass(frozen=True)
class TransportSecurity:
    """DNS-rebinding 保护的展开结果（与 MCP SDK 的字段一一对应）。

    单独定义而不直接返回 ``dict``：``**dict[str, list[str] | bool]`` 无法满足
    mypy strict（异构 dict 展开后类型对不上），结构化返回也更难写错。
    """

    enable: bool
    allowed_hosts: list[str]
    allowed_origins: list[str]


@dataclass
class ServerConfig:
    """MCP Server 配置。

    Attributes:
        provider: LLM 预设名。
        api_key: BYOK Key（空则交给 provider 从预设环境变量读）。
        model: 覆盖模型名。
        prompts_dir: 本地提示词目录。
        data_dir: 数据目录（画像等）。
        profile_path: 画像文件路径。
        transport: ``stdio`` / ``streamable-http``。
        host / port: HTTP 传输时的监听地址。
        allow_source_path: 是否允许 ``source_path`` 读本地文件。
        source_root: 若非空，``source_path`` 必须落在该目录内。
    """

    provider: str = DEFAULT_PROVIDER
    api_key: str = ""
    model: str = ""
    prompts_dir: Path = field(default_factory=default_prompts_dir)
    data_dir: Path = field(default_factory=default_data_dir)
    profile_path: Path | None = None
    transport: str = "stdio"
    host: str = "127.0.0.1"
    port: int = 8765
    allow_source_path: bool = True
    source_root: Path | None = None
    remote_timeout: float = 20.0
    #: HTTP 形态的访问令牌（空=不校验；非回环绑定时必须非空，除非显式 allow_public_bind）
    http_token: str = ""
    #: HTTP 形态允许的 Host 头列表（云端平台必须显式加公网域名，否则 421）
    allowed_hosts: list[str] = field(default_factory=list)
    #: HTTP 形态允许的 Origin 头列表
    allowed_origins: list[str] = field(default_factory=list)
    #: 显式允许「非回环 + 无令牌」（仅限可信内网）
    allow_public_bind: bool = False

    def __post_init__(self) -> None:
        """补默认值：画像路径、HTTP 下的 source_path 默认关闭。"""
        if self.profile_path is None:
            self.profile_path = Path(
                os.environ.get(ENV_PROFILE) or (self.data_dir / "profile.txt")
            )
        if os.environ.get(ENV_ALLOW_SOURCE_PATH) is None and self.transport != "stdio":
            # ⚠️ HTTP 传输面向的是「别人的服务器 + 多用户」，开放任意路径读取
            #    等于把宿主机的文件系统暴露给调用方。默认关闭，要开就显式开。
            self.allow_source_path = False
        root = os.environ.get(ENV_SOURCE_ROOT)
        if root:
            self.source_root = Path(root)

    @property
    def is_http(self) -> bool:
        """是否走 HTTP 传输。"""
        return self.transport != "stdio"

    @property
    def is_loopback_bind(self) -> bool:
        """监听地址是否只对本机可见。"""
        return is_loopback_host(self.host)

    def http_bind_error(self) -> str | None:
        """HTTP 对外绑定前的安全自检。

        为什么要有这道闸：MCP 的 HTTP 端点一旦暴露到公网就等于把
        **服务器上的 LLM Key** 开放给任何人（无鉴权时）。启动时直接拒绝，
        比事后发现账单异常要好。

        Returns:
            ``None`` 表示可以启动；否则返回**可操作**的中文错误说明。
        """
        if not self.is_http or self.is_loopback_bind or self.allow_public_bind:
            return None
        if not self.http_token:
            return (
                f"拒绝在 {self.host} 上无鉴权启动（任何能访问该端口的人都会用掉你的 LLM Key）。\n"
                "请任选其一：\n"
                f"  1) 设置访问令牌：export {ENV_HTTP_TOKEN}=<一段足够长的随机串>\n"
                "     客户端请求时带 Authorization: Bearer <该串>\n"
                "  2) 只监听本机并由反向代理（nginx）终结 TLS 与鉴权：--host 127.0.0.1\n"
                f"  3) 可信内网且明确接受风险：export {ENV_ALLOW_PUBLIC_BIND}=1"
            )
        if not self.allowed_hosts:
            return (
                f"已在 {self.host} 上启用令牌校验，但没有配置 Host 白名单，"
                "云端平台会收到 421 Invalid Host header（MCP SDK 默认只放行回环地址）。\n"
                "请设置公网域名（逗号分隔），例如：\n"
                f"  export {ENV_HTTP_ALLOWED_HOSTS}=jobcopilot.example.com"
            )
        return None

    def transport_security(self) -> TransportSecurity:
        """展开 MCP SDK 的 DNS-rebinding 保护配置。

        ⚠️ SDK 的匹配规则很窄：**只支持精确匹配或 ``host:*`` 端口通配**，
        没有 ``*.example.com`` 这种域名通配。因此这里对「裸主机名」自动补一份
        ``host:*``，否则 HTTPS 默认端口下客户端常带的 ``host:443`` 会对不上。

        Returns:
            展开后的 Host / Origin 白名单（回环地址始终保留，便于本机自检）。
        """
        hosts: list[str] = ["127.0.0.1:*", "localhost:*", "[::1]:*", "127.0.0.1", "localhost"]
        for raw in self.allowed_hosts:
            h = raw.strip()
            if not h:
                continue
            hosts.append(h)
            if ":" not in h:
                # 裸主机名 → 补端口通配形式
                hosts.append(f"{h}:*")
        origins: list[str] = [
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
        ]
        origins.extend(o.strip() for o in self.allowed_origins if o.strip())
        # 去重但保序
        return TransportSecurity(
            enable=True,
            allowed_hosts=list(dict.fromkeys(hosts)),
            allowed_origins=list(dict.fromkeys(origins)),
        )

    @classmethod
    def from_env(cls, transport: str = "stdio") -> "ServerConfig":
        """从环境变量构造配置。"""
        return cls(
            provider=os.environ.get(ENV_PROVIDER, DEFAULT_PROVIDER),
            api_key=os.environ.get(ENV_API_KEY, ""),
            model=os.environ.get(ENV_MODEL, ""),
            prompts_dir=default_prompts_dir(),
            data_dir=default_data_dir(),
            transport=transport,
            host=os.environ.get("JOBCOPILOT_HOST", "127.0.0.1"),
            port=int(os.environ.get("JOBCOPILOT_PORT", "8765")),
            allow_source_path=_env_bool(ENV_ALLOW_SOURCE_PATH, True),
            http_token=os.environ.get(ENV_HTTP_TOKEN, "").strip(),
            allowed_hosts=_env_list(ENV_HTTP_ALLOWED_HOSTS),
            allowed_origins=_env_list(ENV_HTTP_ALLOWED_ORIGINS),
            allow_public_bind=_env_bool(ENV_ALLOW_PUBLIC_BIND, False),
        )
