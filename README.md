# JobCopilot

> 把「一份 JD / 一批职位」变成结构化求职情报的 Python 内核。
> **零宿主耦合、零第三方依赖**，可嵌入任意应用，也可独立运行。

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](./LICENSE)

---

## 它解决什么问题

求职者面对的信息是**非结构化**的：几十份 JD 散落在各平台，看不出赛道冷热、
不知道自己差在哪、更不知道该优先补什么。

JobCopilot 做三件事：

| 能力 | 说明 |
|---|---|
| **单职位深度分析** | 一个 JD → 7 个维度（核心要求 / 知识优先级 / 面试 Q&A / 差距分析 / 简历建议 / 项目迭代 / 求职策略） |
| **批量赛道分析** | 一批职位 → 程序化统计（公司分布 / 方向分布 / 热点关键词）+ 市场行情 + 知识迭代建议 |
| **投递作战计划** | 记录投递进度，按大厂「冷冻期」自动计算可再投日期，避免盲目海投浪费机会 |

---

## 设计原则

1. **内核不碰 IO** —— 职位从哪来（爬虫 / 粘贴 / 文件导入）、报告存到哪，都是宿主的职责。
   内核只接受「一批职位 dict」，返回「一份报告 dict」。
2. **零第三方依赖** —— `core` 只用标准库，不依赖 langchain / openai。
   消息用中性的 `Message` 表达，由宿主适配成自家类型。需要自带模型调用时再装
   `jobcopilot[providers]`。
3. **提示词多级回退** —— `请求级 override → 宿主本地目录 → 包内 packs/<职能族> → 包内 base`。
   宿主可以完全接管提示词（比如现网热改），也可以零配置直接用包内通用版本。
4. **逐步降级** —— 任何一步 LLM 调用失败或返回脏 JSON，都只让该步产出空结构，
   整条分析链不崩、报告不缺块。

---

## 快速开始

```bash
pip install jobcopilot              # 仅内核（零依赖）
pip install "jobcopilot[providers]" # 需要自带模型调用时
```

### 独立运行（自带模型）

```python
import asyncio
from jobcopilot import SingleJobAnalyzer
from jobcopilot.core.providers import OpenAICompatLLM

async def main():
    # Key 从环境变量读：DEEPSEEK_API_KEY / DASHSCOPE_API_KEY / MOONSHOT_API_KEY ...
    llm = OpenAICompatLLM(preset="deepseek")
    analyzer = SingleJobAnalyzer(llm)          # 零配置即用包内提示词

    report = await analyzer.analyze(
        jd_text=open("jd.txt", encoding="utf-8").read(),
        user_profile="name: 我\ntarget_role: Agent 开发\ncity: 北京",
    )
    print(report["gap_analysis"])

asyncio.run(main())
```

支持的预设（全部走 OpenAI 兼容协议）：

| preset | 厂商 | 环境变量 |
|---|---|---|
| `deepseek` | DeepSeek | `DEEPSEEK_API_KEY` |
| `qwen` | 阿里百炼 · 通义千问 | `DASHSCOPE_API_KEY` |
| `kimi` | 月之暗面 | `MOONSHOT_API_KEY` |
| `doubao` | 火山方舟 · 豆包 | `ARK_API_KEY` |
| `zhipu` | 智谱 GLM | `ZHIPUAI_API_KEY` |
| `openai` | OpenAI | `OPENAI_API_KEY` |

### 嵌入宿主（自管模型调用）

宿主只需实现 `LLMPort`，把调用转给自家的模型路由 / 计费 / 重试层：

```python
from jobcopilot import SingleJobAnalyzer

class MyLLM:                      # 实现 LLMPort
    async def complete(self, role, messages):
        return await my_model_router.call(role, messages)   # 返回 str 或带 .content 的对象

analyzer = SingleJobAnalyzer(MyLLM(), prompt_dir="/my/prompts")  # 本地提示词优先
```

宿主还可以用 `set_logger_factory()` 把内核日志接进自己的日志管道：

```python
from jobcopilot.core.logging import set_logger_factory
set_logger_factory(my_structlog_get_logger)
```

---

## 目录结构

```
src/jobcopilot/
├── core/
│   ├── analyzers/          # single（单职位流水线）/ batch（批量）/ apply_plan（投递计划）
│   ├── prompts/            # resolver（多级回退）+ base/（内置通用提示词）+ packs/（职能族）
│   ├── providers/          # OpenAI 兼容客户端 + 国内厂商预设
│   ├── messages.py         # 中性 Message + LLMPort
│   ├── ports.py            # ProfilePort / KVStorePort
│   ├── stats.py            # 程序化统计（零 LLM，可精确断言）
│   ├── models.py           # TypedDict 领域模型
│   └── logging.py          # 可注入的 logger 工厂
└── storage/                # JSON 文件存储默认实现
```

---

## 提示词分层

分层轴是**职能**而非行业——同一职能（售前 / 产品 / 研发）在不同行业面对的 JD
结构高度相似，而不同职能即便同行业也差异巨大。

```
packs/<职能族>/<提示词文件名>.md     # 只放与 base 的差异部分
base/<提示词文件名>.md               # 通用版本，随包发布
```

---

## 开发

```bash
pip install -e ".[dev]"
pytest -q          # 78 passed
ruff check src tests
mypy src           # strict，零错误
```

---

## 许可

Apache-2.0，见 [LICENSE](./LICENSE)。
