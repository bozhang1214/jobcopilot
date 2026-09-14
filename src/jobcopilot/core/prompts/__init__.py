"""提示词解析与内置提示词包。"""
from jobcopilot.core.prompts.resolver import (
    PromptResolver,
    base_dir,
    packs_dir,
    prompt_source_of,
)

__all__ = ["PromptResolver", "base_dir", "packs_dir", "prompt_source_of"]
