"""投递作战计划的测试（含冷却期计算与存储注入）。"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from jobcopilot.core.analyzers import apply_plan
from jobcopilot.storage.json_store import JsonFileStore


@pytest.fixture
def store(tmp_path: Path) -> JsonFileStore:
    """每个用例独立的临时存储。"""
    return JsonFileStore(tmp_path / "job_apply_plan.json")


def test_upsert_and_list(store: JsonFileStore) -> None:
    """新增后可列出，字段落库正确。"""
    rec = apply_plan.upsert_plan("u1", {"company": "字节", "title": "Agent 工程师"}, store)
    assert rec["company"] == "字节"
    assert rec["status"] == apply_plan.STATUS_PLANNED
    assert rec["tier"] == 1
    items = apply_plan.list_plans("u1", store)
    assert len(items) == 1
    assert items[0]["id"] == rec["id"]


def test_user_isolation(store: JsonFileStore) -> None:
    """不同用户的计划互相隔离。"""
    apply_plan.upsert_plan("u1", {"company": "A"}, store)
    apply_plan.upsert_plan("u2", {"company": "B"}, store)
    assert len(apply_plan.list_plans("u1", store)) == 1
    assert apply_plan.list_plans("u2", store)[0]["company"] == "B"


def test_update_by_id_keeps_created_at(store: JsonFileStore) -> None:
    """带 id 更新时保留原 created_at。"""
    rec = apply_plan.upsert_plan("u1", {"company": "A"}, store)
    updated = apply_plan.upsert_plan("u1", {"id": rec["id"], "company": "A2"}, store)
    assert updated["company"] == "A2"
    assert updated["created_at"] == rec["created_at"]
    assert len(apply_plan.list_plans("u1", store)) == 1


def test_update_unknown_id_inserts(store: JsonFileStore) -> None:
    """指定了不存在的 id 时按新增处理，不静默丢失。"""
    rec = apply_plan.upsert_plan("u1", {"id": "not-exist", "company": "A"}, store)
    assert rec["company"] == "A"
    assert len(apply_plan.list_plans("u1", store)) == 1


def test_delete_plan(store: JsonFileStore) -> None:
    """删除成功返回 True，重复删除返回 False。"""
    rec = apply_plan.upsert_plan("u1", {"company": "A"}, store)
    assert apply_plan.delete_plan("u1", rec["id"], store) is True
    assert apply_plan.delete_plan("u1", rec["id"], store) is False
    assert apply_plan.list_plans("u1", store) == []


def test_normalize_bad_values(store: JsonFileStore) -> None:
    """脏值一律回退默认，不报错。"""
    rec = apply_plan.upsert_plan(
        "u1",
        {"tier": "abc", "status": "不存在的状态", "cooldown_months": 999},
        store,
    )
    assert rec["tier"] == 1
    assert rec["status"] == apply_plan.STATUS_PLANNED
    assert rec["cooldown_months"] == 0


def test_cooldown_computed_on_rejection() -> None:
    """已挂 + 结果日期 + 冷却月数 → 算出可再投日期。"""
    result_at = date.today() - timedelta(days=10)
    out = apply_plan.enrich(
        {
            "status": apply_plan.STATUS_REJECTED,
            "result_at": result_at.isoformat(),
            "cooldown_months": 6,
        }
    )
    assert out["cooling"] is True
    assert out["can_apply"] is False
    assert out["days_left"] > 0
    assert out["cooldown_until"]


def test_cooldown_expired() -> None:
    """冷却期已过 → cooling=False，days_left=0。"""
    result_at = date.today() - timedelta(days=200)
    out = apply_plan.enrich(
        {"status": apply_plan.STATUS_REJECTED, "result_at": result_at.isoformat(),
         "cooldown_months": 1}
    )
    assert out["cooling"] is False
    assert out["days_left"] == 0
    assert out["can_apply"] is True


def test_cooldown_not_applied_for_other_status() -> None:
    """非「已挂」状态即使填了日期也不进冷却。"""
    out = apply_plan.enrich(
        {"status": apply_plan.STATUS_APPLIED, "result_at": date.today().isoformat(),
         "cooldown_months": 6}
    )
    assert out["cooling"] is False
    assert out["cooldown_until"] == ""


def test_cooldown_month_end_overflow() -> None:
    """月末溢出：1/31 + 1 月 → 2/28（非闰年 2/29 也算对）。"""
    assert apply_plan._add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert apply_plan._add_months(date(2026, 3, 31), 1) == date(2026, 4, 30)
    assert apply_plan._add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)
    assert apply_plan._add_months(date(2026, 12, 31), 12) == date(2027, 12, 31)


def test_parse_date_lenient() -> None:
    """非法日期返回 None，不抛异常。"""
    assert apply_plan._parse_date("") is None
    assert apply_plan._parse_date("不是日期") is None
    assert apply_plan._parse_date("2026-09-14") == date(2026, 9, 14)


def test_compute_stats_counts_statuses() -> None:
    """统计各状态数量与冷却数。"""
    items = [
        {"status": "applied", "cooling": False},
        {"status": "rejected", "cooling": True},
        {"status": "rejected", "cooling": False},
    ]
    stats = apply_plan.compute_stats(items)
    assert stats["total"] == 3
    assert stats["applied"] == 1
    assert stats["rejected"] == 2
    assert stats["cooling"] == 1


def test_store_survives_corrupted_file(tmp_path: Path) -> None:
    """存储文件损坏时按空数据兜底，不抛异常。"""
    f = tmp_path / "bad.json"
    f.write_text("{坏 JSON", encoding="utf-8")
    assert JsonFileStore(f).load() == {}


def test_note_truncated(store: JsonFileStore) -> None:
    """备注超长截断到 500 字，避免脏数据撑爆存储。"""
    rec = apply_plan.upsert_plan("u1", {"note": "x" * 800}, store)
    assert len(rec["note"]) == 500
