"""输出长度约束：把「本次输出不超过 N 字符」的要求**交给 LLM**（不是事后裁剪）。

为什么这样做（2026-09-15 与 owner 确认）：云端平台对单次工具响应有硬上限
（Dify 约 68000 字符、千帆 API 节点 1M），超了会被截断成空。事后按字节裁剪会产出
**非法 JSON**，模型必然解析失败；所以改成在提示词里给出长度预算，由模型自己把内容
写短。

⚠️ 已知局限（务必知情）：模型对「精确字符数」并不精确，通常会有上下浮动。
因此调用方不应把 `max_chars` 当成硬保证，而应当成**显著降低超限概率**的手段；
真正的硬边界仍由平台侧决定（超了就换更小的预算或减少输入）。

设计要点：**JSON 键结构必须完整保留**，长度只能通过「把文本值写短 / 减少数组条数」
来压缩 —— 这样 L1 的骨架断言仍然守得住，调用方也不会拿到缺字段的结果。
"""
from __future__ import annotations

#: 单段最小预算（再小模型就只能放弃内容而不是压缩表达，收益为负）
MIN_PART_BUDGET = 200

#: 安全系数：**告诉模型的数字比调用方的预算小一档**。
#:
#: 为什么需要它（实测依据）：模型对精确字符数并不精确。用 DeepSeek 实测
#: `max_chars=3000`（两路各 1500）时实际输出 **3291 字符 —— 超出约 10%**。
#: 直接照原数告诉模型，等于把「会不会超」赌在模型的自觉上；打折后实测可落回预算内。
#: 代价是内容再少约两成，但 `max_chars` 的意义本来就是「宁短不超」。
SAFETY_FACTOR = 0.8


def per_part_budget(total: int, parts: int, floor: int = MIN_PART_BUDGET) -> int:
    """把总预算按段数均摊（不低于 ``floor``）。

    Args:
        total: 调用方给出的总字符预算。
        parts: 本次分析会产生的响应段数（单职位 7 段、批量 2 段）。
        floor: 单段下限，避免总预算很小时算出不可用的数字。

    Returns:
        每一段应当遵守的字符预算（**已计入 SAFETY_FACTOR**）。
    """
    target = int(total * SAFETY_FACTOR)
    if parts <= 0:
        return max(floor, target)
    return max(floor, target // parts)


def budget_instruction(limit: int) -> str:
    """生成追加到系统提示词末尾的长度约束块。

    Args:
        limit: 本段响应的字符上限。

    Returns:
        可直接拼在提示词后面的说明文本。
    """
    return (
        "\n\n---\n"
        f"【输出长度约束】本次响应总长度**不得超过 {limit} 个字符**（含 JSON 结构符号）。\n"
        "硬性要求：\n"
        "1. **JSON 的键结构必须完整保留**：不得删键、不得改成非 JSON、不得用省略号占位；\n"
        "2. 压缩只能通过**把文本值写短**实现：每条要点写一句话，去掉套话与重复表述；\n"
        "3. 数组类字段可以减少条数（留最重要的几条），但不要输出未闭合的结构；\n"
        "4. 宁可少写内容，也不要截断 JSON —— 输出必须是能被直接解析的完整 JSON。\n"
    )


def with_budget(prompt: str, limit: int | None) -> str:
    """给提示词追加长度约束（``limit`` 为空时原样返回）。"""
    if not limit or limit <= 0:
        return prompt
    return prompt + budget_instruction(limit)
