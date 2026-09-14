"""JobCopilot —— 求职分析内核（可嵌入任何宿主，也可独立运行）。

一句话：把「一份 JD / 一批职位 → 结构化求职情报」这件事，做成一个
**零宿主耦合、零第三方依赖**的 Python 包。

用法::

    from jobcopilot import SingleJobAnalyzer, OpenAICompatLLM

    llm = OpenAICompatLLM(preset="deepseek")      # Key 从 DEEPSEEK_API_KEY 读
    analyzer = SingleJobAnalyzer(llm)
    report = await analyzer.analyze(jd_text="...", user_profile="...")

宿主（如 SEKB）也可以不装 provider，自己实现 :class:`~jobcopilot.core.messages.LLMPort`
把调用转给自家的模型路由与计费层。
"""
from jobcopilot.core.analyzers import (
    SingleJobAnalyzer,
    analyze_jobs_batch,
    build_job_summaries,
)
from jobcopilot.core.messages import LLMPort, Message, system, user
from jobcopilot.core.models import BatchReport, JobStats, normalize_job
from jobcopilot.core.prompts import PromptResolver
from jobcopilot.core.stats import classify_role, compute_stats

__version__ = "0.0.1"

__all__ = [
    "__version__",
    # 分析器
    "SingleJobAnalyzer",
    "analyze_jobs_batch",
    "build_job_summaries",
    # 消息与端口
    "LLMPort",
    "Message",
    "system",
    "user",
    # 统计与模型
    "compute_stats",
    "classify_role",
    "normalize_job",
    "BatchReport",
    "JobStats",
    # 提示词
    "PromptResolver",
]
