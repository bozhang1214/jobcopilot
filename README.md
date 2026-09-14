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
packs/<职能族>/<提示词文件名>.md     # 只放与 base 的差异章节
base/<提示词文件名>.md               # 通用版本，随包发布
```

内置 pack：`presales`（售前：年包口径 / 客户与云厂商赛道）、
`product`（产品：产品线赛道 / 端云协同 / 评测体系）、
`engineering`（研发：技术职能赛道 / 框架源码深度）。

### pack 只写差异章节（章节级合并）

pack 里写到的**标题**覆盖 base 的同名标题，没写到的部分原样继承——
尤其是 JSON 输出骨架，永远来自 base，避免 schema 漂移。

匹配按**标题序号**（`### 2.` ↔ `###:2`），所以 pack 可以自由改写标题文案；
pack 新增的章节插在最后一个被覆盖章节之后。整文件替换用
`<!-- override: full -->` 开头。

### 命令

```bash
jobcopilot pack                    # 列出内置职能 pack
jobcopilot pack presales           # 看该 pack 覆盖了哪些章节
jobcopilot pull --pack presales    # 把「合并后的完整提示词」拉到本地目录（可直接改）
jobcopilot doctor                  # 自检：提示词 / pack / provider Key / 数据集
```

`pull` 默认写入 `~/.jobcopilot/prompts`（可用 `--dest` 或 `JOBCOPILOT_PROMPTS_DIR` 改），
该目录优先级最高，改完直接生效。它写出的是**合并后的完整提示词**而非 pack 片段——
因为本地目录是整文件优先，只拉片段会丢掉 base 的 JSON 骨架。

**优先级**：请求级 override → 本地目录 → `packs/<职能族>` → `base`。

---

## 评估（Eval）

| 层 | 需要 LLM | 成本 | 跑什么 |
|---|---|---|---|
| **L1** 程序化断言 | 否 | 0 | 结构 / 统计口径精确比对 / 段落非空 / **输出与提示词声明的 JSON 骨架一致** |
| **L2** 基线回归 | 否 | 0 | **提示词指纹** + 各指标不得低于基线 |
| **L3** LLM-as-Judge | 是 | ~1 元/全量 | JD 覆盖度 / 如实性（防编造）/ 可执行性 / 赛道合理性 / 薪资有据 |

```bash
jobcopilot eval --level 12                        # CI 用：零成本、离线、无需 Key
jobcopilot eval --level 3 --provider deepseek     # 发版前本地跑（有成本）
jobcopilot eval --level 12 --update-baseline      # 改完提示词后重新基线化
```

**守门员机制**（方案 B）：CI 只跑 L1+L2（不需要 Key、不花钱）；L3 由改提示词的作者本地跑。

L2 的关键设计是**提示词指纹**：提示词变了而基线没更新就失败。用零成本的方式强制
「改了提示词就必须重新基线化」，而重新基线化前必须先在本地跑一次 L3——既守住质量，
又不把 LLM Key 放进 CI。

> ⚠️ **骨架解析容忍伪 JSON**：提示词骨架常用裸词占位（`"job_count": 招聘量`），
> 严格 `json.loads` 必然失败。早期版本因此让两条最重要的批量提示词**静默跳过校验**
> （断言假绿）。现在用深度感知扫描，只取「字段 → 粗类型」。

### 可追溯性

每份批量报告都带 `prompt_meta`，记录产出它的提示词版本：

```json
{"pack": "presales", "digest": "f5d9deac8cdeaf5f",
 "prompts": {"批量职位分析.md": {"source": "pack+base", "pack": "presales",
              "overridden_sections": ["###:1", "###:2", "..."]}}}
```

事后可从缓存/存档的报告反查提示词版本，不必依赖外部日志。

---

## 开发

> **⚠️ 本仓库也可作为 SEKB 的 git 子模块使用**（SEKB 侧用 Docker 命名构建上下文
> 把它装进镜像）。克隆 SEKB 时记得 `--recurse-submodules`。

```bash
pip install -e ".[dev]"
pytest -q          # 143 passed
ruff check src tests
mypy src           # strict，零错误
```

---

## 许可

Apache-2.0，见 [LICENSE](./LICENSE)。
