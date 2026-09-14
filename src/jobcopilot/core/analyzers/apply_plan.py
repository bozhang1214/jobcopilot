"""投递作战计划（按用户隔离）。

记录每一次投递（公司 / 岗位 / 分层 / 状态 / 日期），并在**面试失败**后按
冷却月数自动计算「可再投日期」——避免盲目重复投递触发大厂冷冻期。

设计原则：简单易用。一张表 + 一个表单，不做复杂流程。

- 状态：``planned``（计划投）| ``applied``（已投）| ``interview``（面试中）
  | ``rejected``（已挂）| ``offer``（已拿 offer）
- 冷却期：仅当 ``status=rejected`` 且填了 ``result_at`` + ``cooldown_months`` 时计算

存储通过 :class:`~jobcopilot.storage.json_store.JsonFileStore` 注入，
默认落在 ``$JOBCOPILOT_DATA_DIR/job_apply_plan.json``（默认 ``data/``）。
"""
from __future__ import annotations

import time
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from jobcopilot.storage.json_store import JsonFileStore, data_dir

#: 单用户最多保留的投递记录数
MAX_ITEMS = 500

#: 默认存储文件名
DEFAULT_FILENAME = "job_apply_plan.json"

# 状态枚举（与前端对齐）
STATUS_PLANNED = "planned"
STATUS_APPLIED = "applied"
STATUS_INTERVIEW = "interview"
STATUS_REJECTED = "rejected"
STATUS_OFFER = "offer"
STATUSES = (STATUS_PLANNED, STATUS_APPLIED, STATUS_INTERVIEW, STATUS_REJECTED, STATUS_OFFER)

# 分层枚举（1=长期主攻 / 2=中期过渡 / 3=短期保底）
TIERS = (1, 2, 3)


def default_store() -> JsonFileStore:
    """返回默认存储（路径受 ``JOBCOPILOT_DATA_DIR`` 控制）。"""
    return JsonFileStore(data_dir() / DEFAULT_FILENAME)


# ============================================================
# 冷却期计算
# ============================================================

def _add_months(day: date, months: int) -> date:
    """在日期上加 N 个月（处理月末溢出，如 1/31 + 1 月 → 2/28）。"""
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    # 定位该月最后一天，避免 31 号在 2 月溢出
    if month == 12:
        last_day = 31
    else:
        last_day = (date(year, month + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(day.day, last_day))


def _parse_date(value: Any) -> date | None:
    """解析 YYYY-MM-DD；失败返回 None（宽松，不因脏数据报错）。"""
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def enrich(item: dict[str, Any]) -> dict[str, Any]:
    """补齐冷却期派生字段（供前端直接展示，无需前端算日期）。

    新增字段：
        cooldown_until: 可再投日期（YYYY-MM-DD），无冷却时为空串
        cooling:        当前是否在冷却期
        days_left:      距解冻剩余天数（冷却中才有意义）
        can_apply:      现在是否可以投（不含冷却）
    """
    out = dict(item)
    until: date | None = None

    if item.get("status") == STATUS_REJECTED:
        result_at = _parse_date(item.get("result_at"))
        months = int(item.get("cooldown_months") or 0)
        if result_at and months > 0:
            until = _add_months(result_at, months)

    if until is not None:
        today = date.today()
        days_left = (until - today).days
        out["cooldown_until"] = until.isoformat()
        out["cooling"] = days_left > 0
        out["days_left"] = max(days_left, 0)
    else:
        out["cooldown_until"] = ""
        out["cooling"] = False
        out["days_left"] = 0

    out["can_apply"] = not out["cooling"]
    return out


# ============================================================
# 对外接口
# ============================================================

def list_plans(
    user_id: str, store: JsonFileStore | None = None
) -> list[dict[str, Any]]:
    """返回某用户的投递计划列表（按创建时间倒序，已补冷却期字段）。"""
    items = (store or default_store()).load().get(user_id) or []
    return [enrich(i) for i in items]


def compute_stats(items: list[dict[str, Any]]) -> dict[str, int]:
    """统计投递进度（供顶部卡片展示）。"""
    stats = {
        "total": len(items),
        "planned": 0,
        "applied": 0,
        "interview": 0,
        "rejected": 0,
        "offer": 0,
        "cooling": 0,
    }
    for i in items:
        status = i.get("status")
        if status in stats:
            stats[status] += 1
        if i.get("cooling"):
            stats["cooling"] += 1
    return stats


def upsert_plan(
    user_id: str, payload: dict[str, Any], store: JsonFileStore | None = None
) -> dict[str, Any]:
    """新增或更新一条投递记录（payload 含 id 则更新，否则新增）。

    只接受白名单字段，避免前端传入脏字段污染存储。
    """
    st = store or default_store()
    data = st.load()
    items: list[dict[str, Any]] = data.get(user_id) or []
    plan_id = str(payload.get("id") or "").strip()

    record = {
        "company": str(payload.get("company") or "").strip(),
        "title": str(payload.get("title") or "").strip(),
        "tier": _normalize_tier(payload.get("tier")),
        "status": _normalize_status(payload.get("status")),
        "applied_at": str(payload.get("applied_at") or "").strip()[:10],
        "result_at": str(payload.get("result_at") or "").strip()[:10],
        "cooldown_months": _normalize_cooldown(payload.get("cooldown_months")),
        "url": str(payload.get("url") or "").strip(),
        "note": str(payload.get("note") or "").strip()[:500],
        "updated_at": time.time(),
    }

    if plan_id:
        for idx, item in enumerate(items):
            if item.get("id") == plan_id:
                record["id"] = plan_id
                record["created_at"] = item.get("created_at") or time.time()
                items[idx] = record
                break
        else:
            # 指定了 id 但没找到：按新增处理（避免静默丢失）
            record["id"] = plan_id
            record["created_at"] = time.time()
            items.insert(0, record)
    else:
        record["id"] = uuid.uuid4().hex[:12]
        record["created_at"] = time.time()
        items.insert(0, record)

    data[user_id] = items[:MAX_ITEMS]
    st.save(data)
    return enrich(record)


def delete_plan(
    user_id: str, plan_id: str, store: JsonFileStore | None = None
) -> bool:
    """删除一条投递记录，返回是否真的删了。"""
    st = store or default_store()
    data = st.load()
    items: list[dict[str, Any]] = data.get(user_id) or []
    remaining = [i for i in items if i.get("id") != plan_id]
    if len(remaining) == len(items):
        return False
    data[user_id] = remaining
    st.save(data)
    return True


# ============================================================
# 字段规范化（宽松：脏值一律回退默认，不报错）
# ============================================================

def _normalize_tier(value: Any) -> int:
    try:
        tier = int(value)
    except (TypeError, ValueError):
        return 1
    return tier if tier in TIERS else 1


def _normalize_status(value: Any) -> str:
    status = str(value or "").strip()
    return status if status in STATUSES else STATUS_PLANNED


def _normalize_cooldown(value: Any) -> int:
    """冷却月数：0~24 之间，非法回退 0。"""
    try:
        months = int(value)
    except (TypeError, ValueError):
        return 0
    return months if 0 <= months <= 24 else 0
