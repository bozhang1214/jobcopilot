"""提示词解析、章节级合并与内置提示词包。"""
from jobcopilot.core.prompts.compose import compose, digest, split_blocks
from jobcopilot.core.prompts.resolver import (
    PromptMeta,
    PromptResolver,
    available_packs,
    base_dir,
    packs_dir,
    prompt_source_of,
)

__all__ = [
    "PromptResolver",
    "PromptMeta",
    "compose",
    "digest",
    "split_blocks",
    "base_dir",
    "packs_dir",
    "available_packs",
    "prompt_source_of",
]
