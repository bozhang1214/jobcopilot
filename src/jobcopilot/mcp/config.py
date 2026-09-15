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

DEFAULT_PROVIDER = "deepseek"


def _env_bool(name: str, default: bool) -> bool:
    """读布尔环境变量（``1/true/yes/on`` 为真）。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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
        )
