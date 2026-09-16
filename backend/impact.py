"""
操作影响预览：在真正更正 / 作废 / 恢复 / 撤销之前，
向用户展示该操作对【奶量统计、休药期校验、提醒】的影响。

实现方式：在独立数据库会话中开启事务 -> 套用变更 -> 计算指标 -> ROLLBACK，
不写入任何数据；正式提交仍由 audit 模块在单事务中完成，杜绝“只恢复一半”。
"""
from datetime import date, timedelta
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from . import audit, models, services
from .database import SessionLocal

# 统计回看窗口（天）：覆盖仪表盘与异常检测关心的近期数据
WINDOW_DAYS = 21


def preview(db: Session, entity_type: str, entity_id: int, action: str,
            changes: Optional[dict] = None, target_version: Optional[int] = None) -> dict:
    """
    action: correct / void / restore / undo
    返回 {"before": {...}, "after": {...}, "deltas": [...], "conflicts": [...]}
    before/after 均包含 milk 统计、withdrawal 区间、violations 数、reminders 列表
    """
    changes = changes or {}
    cow_id = _require_cow(db, entity_type, entity_id)
    today = date.today()

    before_sess = SessionLocal()
    try:
        before = _metrics(before_sess, cow_id, today)
    finally:
        before_sess.close()
    probe = SessionLocal()
    try:
        # 独立会话内试写后整体回滚；probe 不影响主请求会话
        _apply_probe_change(probe, entity_type, entity_id, action, changes, target_version)
        conflicts = _detect_conflicts(probe, entity_type, entity_id, action, cow_id)
        after = _metrics(probe, cow_id, today)
        probe.rollback()
    except HTTPException:
        probe.rollback()
        raise
    except Exception:
        probe.rollback()
        raise
    finally:
        probe.close()

    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "action": action,
        "action_label": audit.ACTION_LABELS.get(action, action),
        "cow_id": cow_id,
        "before": before,
        "after": after,
        "deltas": _build_deltas(entity_type, before, after),
        "conflicts": conflicts,
    }


def _require_cow(db: Session, entity_type: str, entity_id: int) -> int:
    obj = db.get(audit.ENTITY_FIELDS[entity_type]["model"], entity_id)
    if not obj:
        raise HTTPException(404, "未找到该记录")
    return obj.cow_id


def _apply_probe_change(probe: Session, entity_type: str, entity_id: int,
                        action: str, changes: dict, target_version: Optional[int]) -> None:
    """在探针会话中执行“即将发生”的变更（不走审计日志，最终会回滚）"""
    obj = probe.get(audit.ENTITY_FIELDS[entity_type]["model"], entity_id)
    if action == "correct":
        allowed = set(audit.ENTITY_FIELDS[entity_type]["fields"]) - {"cow_id"}
        for k, v in changes.items():
            if k in allowed:
                setattr(obj, k, v)
        audit.recompute_derived(entity_type, obj, changes)
    elif action == "void":
        obj.is_void = True
        obj.voided_at = None
    elif action == "restore":
        obj.is_void = False
        obj.voided_at = None
    elif action == "undo":
        log = (
            probe.query(models.AuditLog)
            .filter(
                models.AuditLog.entity_type == entity_type,
                models.AuditLog.entity_id == entity_id,
                models.AuditLog.version_after == target_version,
            )
            .order_by(models.AuditLog.seq.desc()).first()
        )
        if not log or not log.after_data:
            raise HTTPException(404, f"未找到版本 v{target_version}")
        audit.apply_snapshot(obj, entity_type, log.after_data)
        obj.is_void = False
        obj.voided_at = None
        audit.recompute_derived(entity_type, obj,
                                {f: True for f in audit.ENTITY_FIELDS[entity_type]["fields"]})
    else:
        raise HTTPException(400, f"不支持的预览操作：{action}")
    probe.flush()


def _metrics(db: Session, cow_id: int, today: date) -> dict:
    """计算该牛在预览变更下的关键指标快照"""
    start = today - timedelta(days=WINDOW_DAYS)
    rows = (
        db.query(models.MilkingRecord)
        .filter(
            models.MilkingRecord.cow_id == cow_id,
            models.MilkingRecord.date >= start,
            models.MilkingRecord.is_void.is_(False),
        ).all()
    )
    market_kg = round(sum(r.yield_kg for r in rows if not r.discarded), 1)
    discard_kg = round(sum(r.yield_kg for r in rows if r.discarded), 1)

    # 违规混装：休药期内（基于未作废用药）却未废弃、且记录本身未作废
    violations = []
    meds = [
        m for m in db.query(models.Medication)
        .filter_by(cow_id=cow_id).all()
        if not m.is_void and m.withdrawal_days > 0
    ]
    for r in rows:
        in_w = any(m.date <= r.date <= m.withdrawal_end for m in meds)
        if in_w and not r.discarded:
            violations.append({
                "milking_id": r.id, "date": str(r.date),
                "session": r.session,
                "session_label": services.SESSION_LABEL.get(r.session, r.session),
                "yield_kg": r.yield_kg,
            })

    # 休药期窗口（近窗口 + 未来 14 天内的有效区间）
    future_end = today + timedelta(days=14)
    windows = [{
        "medication_id": m.id,
        "drug_name": m.drug_name,
        "date": str(m.date),
        "withdrawal_end": str(m.withdrawal_end),
        "active_today": m.date <= today <= m.withdrawal_end,
    } for m in meds if m.withdrawal_end >= start and m.date <= future_end]

    reminders = [
        {"type": r["type"], "title": r["title"], "level": r["level"],
         "due_date": r["due_date"]}
        for r in services.build_reminders(db, today) if r["cow_id"] == cow_id
    ]
    return {
        "window": {"from": str(start), "to": str(today)},
        "market_kg": market_kg,
        "discard_kg": discard_kg,
        "violation_count": len(violations),
        "violations": sorted(violations, key=lambda v: v["date"], reverse=True)[:10],
        "withdrawal_windows": sorted(windows, key=lambda w: w["date"], reverse=True),
        "reminders": reminders,
    }


def _detect_conflicts(probe: Session, entity_type: str, entity_id: int,
                      action: str, cow_id: int) -> list:
    """恢复 / 撤销时检查业务冲突（同一牛同日同班挤奶记录唯一）"""
    conflicts = []
    if entity_type != "milking" or action not in ("restore", "undo"):
        return conflicts
    obj = probe.get(models.MilkingRecord, entity_id)
    clash = (
        probe.query(models.MilkingRecord)
        .filter(
            models.MilkingRecord.cow_id == cow_id,
            models.MilkingRecord.date == obj.date,
            models.MilkingRecord.session == obj.session,
            models.MilkingRecord.is_void.is_(False),
            models.MilkingRecord.id != entity_id,
        ).first()
    )
    if clash:
        conflicts.append({
            "type": "duplicate_milking",
            "message": f"{obj.date} {services.SESSION_LABEL.get(obj.session, obj.session)}"
                       f"已有另一条有效挤奶记录（#{clash.id}，{clash.yield_kg}kg），"
                       f"恢复会造成同班次重复",
            "conflict_id": clash.id,
        })
    return conflicts


def _build_deltas(entity_type: str, before: dict, after: dict) -> list:
    """人类可读的影响条目列表"""
    deltas = []

    d_market = round(after["market_kg"] - before["market_kg"], 1)
    d_discard = round(after["discard_kg"] - before["discard_kg"], 1)
    if abs(d_market) >= 0.05:
        deltas.append({
            "kind": "market_milk",
            "level": "warn" if d_market < 0 else "info",
            "message": f"近{WINDOW_DAYS}天上市奶量 {before['market_kg']}kg → "
                       f"{after['market_kg']}kg（{d_market:+.1f}kg）",
        })
    if abs(d_discard) >= 0.05:
        deltas.append({
            "kind": "discard_milk",
            "level": "info",
            "message": f"近{WINDOW_DAYS}天废弃奶量 {before['discard_kg']}kg → "
                       f"{after['discard_kg']}kg（{d_discard:+.1f}kg）",
        })

    d_v = after["violation_count"] - before["violation_count"]
    if d_v > 0:
        deltas.append({
            "kind": "violation",
            "level": "danger",
            "message": f"将新增 {d_v} 条休药期违规混装记录（休药期内未标记废弃）",
            "violations": after["violations"],
        })
    elif d_v < 0:
        deltas.append({
            "kind": "violation",
            "level": "ok",
            "message": f"将消除 {abs(d_v)} 条休药期违规混装记录",
        })

    # 休药窗口差异（仅用药相关操作会触发）
    wb = {w["medication_id"]: w for w in before["withdrawal_windows"]}
    wa = {w["medication_id"]: w for w in after["withdrawal_windows"]}
    for mid, w in wa.items():
        old = wb.get(mid)
        if old is None:
            deltas.append({
                "kind": "withdrawal",
                "level": "warn",
                "message": f"恢复休药期校验：{w['drug_name']} {w['date']} 至 "
                           f"{w['withdrawal_end']} 期间鲜奶须废弃",
            })
        elif old["withdrawal_end"] != w["withdrawal_end"]:
            deltas.append({
                "kind": "withdrawal",
                "level": "warn",
                "message": f"休药期截止日 {old['withdrawal_end']} → {w['withdrawal_end']}"
                           f"（{w['drug_name']}）",
            })
    for mid, w in wb.items():
        if mid not in wa:
            deltas.append({
                "kind": "withdrawal",
                "level": "warn",
                "message": f"休药期校验将失效：{w['drug_name']} {w['date']} 至 "
                           f"{w['withdrawal_end']} 的区间不再参与拦截",
            })

    # 提醒差异
    rb = {(r["type"], r["title"]) for r in before["reminders"]}
    ra = {(r["type"], r["title"]) for r in after["reminders"]}
    for key in sorted(ra - rb):
        item = next(r for r in after["reminders"] if (r["type"], r["title"]) == key)
        deltas.append({
            "kind": "reminder_add",
            "level": "danger" if item["level"] == "danger" else "info",
            "message": f"将新增提醒：{item['title']}（{item['due_date']}）",
        })
    for key in sorted(rb - ra):
        item = next(r for r in before["reminders"] if (r["type"], r["title"]) == key)
        deltas.append({
            "kind": "reminder_remove",
            "level": "ok",
            "message": f"将移除提醒：{item['title']}",
        })

    if not deltas:
        deltas.append({"kind": "none", "level": "ok", "message": "对奶量统计、休药校验与提醒均无影响"})
    return deltas
