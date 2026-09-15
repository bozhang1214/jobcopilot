# 贡献指南

感谢你有兴趣参与。**改提示词和改代码的门槛不一样**，请先读完第 2 节。

---

## 1. 开发环境

```bash
git clone https://github.com/bozhang1214/jobcopilot.git
cd jobcopilot
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'          # 含 pytest / ruff / mypy / mcp
```

跑一遍基线，确认环境是好的：

```bash
pytest -q
jobcopilot eval --level 12       # L1+L2，零 LLM 成本
```

## 2. 门禁（CI 会卡这些，请本地先跑）

| 门禁 | 命令 | 说明 |
|---|---|---|
| 单测 | `pytest -q` | 全绿 |
| Lint | `ruff check src tests` | `select = E,F,I,N,W` |
| 类型 | `mypy src` | **strict**，必须零错误 |
| **评估 L1+L2** | `jobcopilot eval --level 12` | **必须与基线一致**（见下） |

### 为什么评估是硬门禁

提示词是这个项目的核心资产，而**提示词退化是不报错的**——改一个章节号、动一句措辞，
7 段分析照样「成功」返回，只是变差了。所以：

- **L1（程序化断言，零 LLM）**：JSON 骨架、字段齐全、`stats` 与程序计算结果**完全一致**、
  7 段非空、枚举合法。`stats` 是程序算的，LLM 一旦篡改就是 bug。
- **L2（基线回归，零 LLM）**：对比 `evals/baselines/v1.json`，**核心是提示词指纹
  （prompt digest）**——改了提示词内容，指纹必然变化，L2 会明确告诉你「动了什么」。

**CI 只跑 L1+L2（零成本、零 Key）**。这是刻意的：把 LLM Key 放进 CI 既有泄露风险，
费用也不可控。

### 改了提示词怎么办

1. 本地跑 **L3**（需要 Key，成本很低，全量约 180 次调用、不到 1 元）：
   ```bash
   export DEEPSEEK_API_KEY=sk-xxx
   jobcopilot eval --level 123 --provider deepseek
   ```
   > ⚠️ **`--provider` 默认是 `stub`**（零成本离线骨架，用于跑 L1/L2）。不显式指定
   > `--provider deepseek` 时，L3 的打分会由 stub 产生，**结果没有意义**。
2. **在 PR 描述里贴上 L3 报告与你的判断**（哪几项升了/降了、为什么可接受）；
3. 如果提示词改动是**有意的**，同时更新基线：
   ```bash
   jobcopilot eval --level 12 --update-baseline
   ```
   并在 PR 里说明「基线为何要动」。**没有说明的基线更新一律不接受。**

> 维护者看 PR 时，先看 `prompt_digests` 的 diff：它精确告诉你哪几个提示词变了。

## 3. 代码约定

- **内核零第三方依赖**：`src/jobcopilot/core/**` 只能用标准库。需要 HTTP 之类的能力时，
  通过端口接口（如 `LLMPort`）注入，不要 `import httpx`。
- **纯实现与协议分层**：`mcp/tools.py` 是**纯实现**（不 import MCP SDK，可独立单测）；
  协议接线在 `mcp/server.py`。加工具时按这个分层写。
- **失败必须显式**：分析器内部逐步降级是对的，但到 API 边界必须让调用方看得出失败
  （结构化错误 + `warnings`），不能把「7 段全空」当成功返回。
- 类型注解要齐全（mypy strict）；公开函数写 docstring，说明**为什么**这么设计而不只是做什么。
- 注释写「为什么」：这个仓库里好几处注释记录了踩过的坑（例如只合并 routes 不合并
  lifespan 会得到「连得上但没有会话」的服务），请保持这个习惯。

## 4. 提交与 PR

- 提交信息用约定式前缀：`feat(scope):` / `fix(scope):` / `docs:` / `test:` / `chore:`，
  正文写清**背景 → 改动 → 验证**。
- **一次改动一个提交**，别攒着。
- PR 检查清单：
  - [ ] `pytest -q` 全绿
  - [ ] `ruff check src tests` 干净
  - [ ] `mypy src` 零错误
  - [ ] `jobcopilot eval --level 12` 与基线一致（或已说明为何更新基线）
  - [ ] 改了提示词 → 附 L3 报告
  - [ ] 改了对外行为 → 更新 README / `docs/integrations/`
- 新功能请带测试。**聚合/统计类逻辑必须有精确断言**（不能只断言「不报错」）。

## 5. 报告问题 / 提需求

Issue 里请带上：

```bash
jobcopilot --version
python -V
python -c "import jobcopilot, sys; print(jobcopilot.__file__)"
```

以及复现命令与完整报错。**不要贴 API Key**（贴了请立刻去厂商后台吊销）。

## 6. 发布流程（维护者）

```bash
# 1) 版本号：pyproject.toml 与 src/jobcopilot/__init__.py 必须一致
# 2) 全量门禁
pytest -q && ruff check src tests && mypy src && jobcopilot eval --level 12
# 3) 构建并检查
rm -rf dist && python -m build
python -m twine check dist/*
# 4) 干净环境冒烟（很重要：验证新模块/提示词真的随包发布）
python -m venv /tmp/jc-rel && /tmp/jc-rel/bin/pip install dist/*.whl
/tmp/jc-rel/bin/jobcopilot --version && /tmp/jc-rel/bin/jobcopilot eval --level 12
# 5) 上传
python -m twine upload dist/*
```

> ⚠️ 第 4 步不能省：曾经出现过「本地测试全过、但新模块没进 wheel」的情况。
> 提交前请确认 `python -m zipfile -l dist/*.whl | grep <新模块>` 有输出。

## 7. 许可

贡献即表示你同意以 [Apache-2.0](LICENSE) 许可发布你的贡献。
