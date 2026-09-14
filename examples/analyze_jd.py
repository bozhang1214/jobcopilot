"""最小示例：用自带 provider 分析一份 JD。

用法：
    export DEEPSEEK_API_KEY=sk-xxx
    python examples/analyze_jd.py

内核零配置即用包内通用提示词；想覆盖提示词就传 prompt_dir。
"""
from __future__ import annotations

import asyncio
import json
import os

from jobcopilot import SingleJobAnalyzer
from jobcopilot.core.providers import OpenAICompatLLM

JD = """【AI Agent 平台开发工程师】某大厂 | 60-90K | 北京
职责：负责 Agent 编排框架、工具调用协议与评测体系的设计与落地。
要求：3 年以上后端经验，熟悉 Python / LangGraph / RAG，有 LLM 应用落地经验者优先。
"""

PROFILE = """name: 示例用户
target_role: Agent 开发（应用侧）
city: 北京
skill_stack:
  strong: [技术管理, 客户端架构]
  weak: [RAG 工程化, LLM 应用后端]
"""


async def main() -> None:
    preset = os.environ.get("JOBCOPILOT_PRESET", "deepseek")
    analyzer = SingleJobAnalyzer(OpenAICompatLLM(preset=preset))
    print(f"使用 {preset}，已加载步骤：{analyzer.available_steps}\n")

    result = await analyzer.analyze(jd_text=JD, user_profile=PROFILE)
    for step, payload in result.items():
        print(f"===== {step} =====")
        print(json.dumps(payload, ensure_ascii=False, indent=2)[:800])
        print()


if __name__ == "__main__":
    asyncio.run(main())
