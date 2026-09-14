"""国内主流大模型接入预设。

全部走 **OpenAI 兼容** 协议（``/chat/completions``），所以一个
:class:`~jobcopilot.core.providers.openai_compat.OpenAICompatLLM` 就能覆盖。

**BYOK（Bring Your Own Key）**：Key 只从环境变量读，绝不写进代码或配置仓库。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderPreset:
    """一个模型的接入参数。

    Attributes:
        name: 预设名（供 CLI / MCP 里 ``--provider`` 使用）。
        base_url: OpenAI 兼容端点（**不含** ``/chat/completions``）。
        default_model: 默认模型名。
        env_key: 读取 API Key 的环境变量名。
        label: 中文展示名。
    """

    name: str
    base_url: str
    default_model: str
    env_key: str
    label: str


#: 预设表。顺序即推荐顺序。
PRESETS: dict[str, ProviderPreset] = {
    p.name: p
    for p in [
        ProviderPreset(
            name="deepseek",
            base_url="https://api.deepseek.com/v1",
            default_model="deepseek-chat",
            env_key="DEEPSEEK_API_KEY",
            label="DeepSeek",
        ),
        ProviderPreset(
            name="qwen",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            default_model="qwen-plus",
            env_key="DASHSCOPE_API_KEY",
            label="阿里百炼 · 通义千问",
        ),
        ProviderPreset(
            name="kimi",
            base_url="https://api.moonshot.cn/v1",
            default_model="moonshot-v1-32k",
            env_key="MOONSHOT_API_KEY",
            label="月之暗面 · Kimi",
        ),
        ProviderPreset(
            name="doubao",
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            default_model="doubao-seed-1-6",
            env_key="ARK_API_KEY",
            label="火山方舟 · 豆包",
        ),
        ProviderPreset(
            name="zhipu",
            base_url="https://open.bigmodel.cn/api/paas/v4",
            default_model="glm-4-plus",
            env_key="ZHIPUAI_API_KEY",
            label="智谱 · GLM",
        ),
        ProviderPreset(
            name="openai",
            base_url="https://api.openai.com/v1",
            default_model="gpt-4o-mini",
            env_key="OPENAI_API_KEY",
            label="OpenAI",
        ),
    ]
}


def get_preset(name: str) -> ProviderPreset:
    """按名取预设。

    Raises:
        KeyError: 预设不存在（附上可用清单，便于 CLI 直接展示给用户）。
    """
    try:
        return PRESETS[name]
    except KeyError:
        available = ", ".join(PRESETS)
        raise KeyError(f"未知 provider: {name!r}；可用: {available}") from None
