"""
记录版本化审计：更正 / 作废 / 恢复 / 撤销更正

设计原则：
- 审计日志只追加（append-only），任何操作都不修改或删除历史日志
- 业务表保留“当前版本”，历史内容完整保存在 audit_logs.before/after 快照中
- 作废是软删除（is_void），可恢复；恢复走冲突校验且生成新版本
- 撤销更正 = 以更正前快照生成一个新版本（不是删除历史）
- 所有写操作在单一事务内完成：先做全部校验，最后一次 commit；出错整体回滚
- 不接入账号体系：操作人由调用方手填
- 系统启用前的存量数据以 baseline（历史起点）标记，operator 留空，不虚构操作人
"""
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from . import models
from .database import SessionLocal, engine

# 各实体受审计的业务字段（JSON 快照中的 date 字段统一转 ISO 字符串）
ENTITY_FIELDS = {
    "milking": {
        "model": models.MilkingRecord,
        "date_fields": ["date"],
        "fields": ["cow_id", "date", "session", "yield_kg", "scc", "discarded", "note"],
        "labels": {
            "date": "日期", "session": "班次", "yield_kg": "产奶量(kg)",
            "scc": "体细胞数", "discarded": "废弃", "note": "备注",
        },
        "name": "挤奶记录",
    },
    "health": {
        "model": models.HealthRecord,
        "date_fields": ["date", "follow_up_date"],
        "fields": ["cow_id", "date", "record_type", "diagnosis", "temperature",
                   "severity", "follow_up_date", "result", "note"],
        "labels": {
            "date": "日期", "record_type": "类型", "diagnosis": "诊断/项目",
            "temperature": "体温(℃)", "severity": "严重程度",
            "follow_up_date": "复查日期", "result": "处置状态", "note": "备注",
        },
        "name": "健康记录",
    },
    "medication": {
        "model": models.Medication,
        "date_fields": ["date", "withdrawal_end", "next_dose_date"],
        "fields": ["cow_id", "drug_id", "drug_name", "date", "dose", "route",
                   "reason", "withdrawal_days", "withdrawal_end",
                   "next_dose_date", "treated", "operator", "note"],
        "labels": {
            "drug_name": "药品", "date": "用药日期", "dose": "剂量",
            "route": "给药途径", "reason": "用药原因", "withdrawal_days": "休药期(天)",
            "withdrawal_end": "休药截止日", "next_dose_date": "下次用药日",
            "treated": "续用药已执行", "operator": "兽医", "note": "备注",
        },
        "name": "用药记录",
    },
}

ACTION_LABELS = {
    "create": "录入",
    "correct": "更正",
    "void": "作废",
    "restore": "恢复",
    "undo": "撤销更正",
    "baseline": "历史起点",
}

BASELINE_REASON = "系统启用版本管理前的存量记录，作为历史起点导入"


# ---------------- 快照 ----------------
def snapshot(obj, entity_type: str) -> dict:
    """导出业务字段快照（date -> ISO 字符串，可直接 JSON 序列化）"""
    cfg = ENTITY_FIELDS[entity_type]
    out = {}
    for f in cfg["fields"]:
        v = getattr(obj, f, None)
        out[f] = v.isoformat() if isinstance(v, (date, datetime)) else v
    return out


def apply_snapshot(obj, entity_type: str, data: dict) -> None:
    """将快照写回 ORM 对象（ISO 字符串 -> date）"""
    cfg = ENTITY_FIELDS[entity_type]
    date_fields = set(cfg["date_fields"])
    for f in cfg["fields"]:
        if f not in data:
            continue
        v = data[f]
        if f in date_fields and v is not None:
            v = date.fromisoformat(v)
        setattr(obj, f, v)


def get_entity(db: Session, entity_type: str, entity_id: int, allow_void: bool = False):
    cfg = ENTITY_FIELDS[entity_type]
    obj = db.get(cfg["model"], entity_id)
    if not obj:
        raise HTTPException(404, f"未找到该{cfg['name']}")
    if obj.is_void and not allow_void:
        raise HTTPException(409, f"该{cfg['name']}已作废，请先恢复后再操作")
    return obj


def _next_seq(db: Session, entity_type: str, entity_id: int) -> int:
    last = (
        db.query(models.AuditLog)
        .filter_by(entity_type=entity_type, entity_id=entity_id)
        .order_by(models.AuditLog.seq.desc())
        .first()
    )
    return (last.seq + 1) if last else 1


def _add_log(db, entity_type, obj, action, operator, reason,
             before: Optional[dict], after: Optional[dict],
             v_before: Optional[int], v_after: Optional[int]) -> models.AuditLog:
    log = models.AuditLog(
        entity_type=entity_type,
        entity_id=obj.id,
        cow_id=obj.cow_id,
        seq=_next_seq(db, entity_type, obj.id),
        action=action,
        version_before=v_before,
        version_after=v_after,
        operator=(operator or "").strip() or None,
        reason=(reason or "").strip() or None,
        before_data=before,
        after_data=after,
    )
    db.add(log)
    return log


def check_version(obj, expected_version: Optional[int]) -> None:
    """乐观锁：前端基于某个版本操作，期间已被他人改动则拒绝，避免覆盖"""
    if expected_version is not None and obj.version != expected_version:
        raise HTTPException(
            409,
            f"记录已被其他人改动（当前 v{obj.version}，你看到的是 v{expected_version}），"
            f"请刷新查看最新版本后再操作",
        )


def require_operator_reason(operator: str, reason: str) -> None:
    if not (operator or "").strip():
        raise HTTPException(400, "请填写操作人")
    if not (reason or "").strip():
        raise HTTPException(400, "请填写操作原因")


# ---------------- 业务派生字段重算 ----------------
def recompute_derived(entity_type: str, obj, changes: dict) -> None:
    """更正后重算派生字段（用药休药截止日）"""
    if entity_type == "medication":
        if "withdrawal_days" in changes or "date" in changes:
            obj.withdrawal_end = obj.date + timedelta(days=obj.withdrawal_days or 0)


# ---------------- 操作：录入 ----------------
def log_create(db: Session, entity_type: str, obj, operator: Optional[str] = None) -> None:
    """新建记录成功后调用（obj 已 flush 拿到 id）"""
    db.flush()
    _add_log(db, entity_type, obj, "create", operator, None,
             before=None, after=snapshot(obj, entity_type),
             v_before=None, v_after=obj.version)


# ---------------- 操作：更正 ----------------
def correct(db: Session, entity_type: str, entity_id: int, changes: dict,
            operator: str, reason: str, expected_version: Optional[int] = None):
    """
    更正记录：
    - 仅允许白名单字段；作废记录不可更正
    - 保存更正前完整快照，version +1
    - 调用方需在此之前完成业务校验（唯一性、日期合法性等）
    """
    require_operator_reason(operator, reason)
    obj = get_entity(db, entity_type, entity_id)
    check_version(obj, expected_version)

    allowed = set(ENTITY_FIELDS[entity_type]["fields"]) - {"cow_id"}
    changes = {k: v for k, v in changes.items() if k in allowed}
    if not changes:
        raise HTTPException(400, "没有需要更正的字段")

    before = snapshot(obj, entity_type)
    try:
        for k, v in changes.items():
            setattr(obj, k, v)
        recompute_derived(entity_type, obj, changes)
        obj.version += 1
        after = snapshot(obj, entity_type)
        _add_log(db, entity_type, obj, "correct", operator, reason,
                 before, after, obj.version - 1, obj.version)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    db.refresh(obj)
    return obj


# ---------------- 操作：作废（软删除） ----------------
def void_record(db: Session, entity_type: str, entity_id: int,
                operator: str, reason: str, expected_version: Optional[int] = None):
    require_operator_reason(operator, reason)
    obj = get_entity(db, entity_type, entity_id)
    check_version(obj, expected_version)
    before = snapshot(obj, entity_type)
    try:
        obj.is_void = True
        obj.voided_at = datetime.utcnow()
        # 作废不改内容版本号，但记录一次状态流转
        _add_log(db, entity_type, obj, "void", operator, reason,
                 before, before, obj.version, obj.version)
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(obj)
    return obj


# ---------------- 操作：恢复作废记录 ----------------
def restore_record(db: Session, entity_type: str, entity_id: int,
                   operator: str, reason: str, expected_version: Optional[int] = None,
                   conflicts: Optional[list] = None):
    """
    恢复作废记录：
    - 调用方先做冲突校验（如挤奶班次唯一），冲突信息通过 conflicts 传入则直接拒绝
    - 恢复后生成新版本（内容可能已与现状冲突，需显式确认）
    """
    require_operator_reason(operator, reason)
    obj = get_entity(db, entity_type, entity_id, allow_void=True)
    if not obj.is_void:
        raise HTTPException(409, "该记录未作废，无需恢复")
    check_version(obj, expected_version)
    if conflicts:
        raise HTTPException(409, {"message": "恢复后与现有记录冲突", "conflicts": conflicts})

    before = snapshot(obj, entity_type)
    try:
        obj.is_void = False
        obj.voided_at = None
        obj.version += 1
        after = snapshot(obj, entity_type)
        _add_log(db, entity_type, obj, "restore", operator, reason,
                 before, after, obj.version - 1, obj.version)
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(obj)
    return obj


# ---------------- 操作：撤销更正 / 回到历史版本 ----------------
def revert_to_version(db: Session, entity_type: str, entity_id: int,
                      target_version: int, operator: str, reason: str,
                      expected_version: Optional[int] = None):
    """
    以某历史版本的内容生成新版本（用于“撤销更正”或时间线上“按此版本恢复内容”）。
    target_version 指向某次 create/baseline/correct/restore/undo 之后的内容版本。
    """
    require_operator_reason(operator, reason)
    obj = get_entity(db, entity_type, entity_id)
    check_version(obj, expected_version)

    target_log = (
        db.query(models.AuditLog)
        .filter(
            models.AuditLog.entity_type == entity_type,
            models.AuditLog.entity_id == entity_id,
            models.AuditLog.version_after == target_version,
            models.AuditLog.action.in_(["create", "baseline", "correct", "restore", "undo"]),
        )
        .order_by(models.AuditLog.seq.desc())
        .first()
    )
    if not target_log or not target_log.after_data:
        raise HTTPException(404, f"未找到版本 v{target_version} 的内容快照")
    if target_version == obj.version:
        raise HTTPException(400, "所选版本与当前版本一致，无需撤销")

    # 撤销到的内容若重新引入冲突（如同班次已有记录），交由调用方提前校验
    before = snapshot(obj, entity_type)
    try:
        apply_snapshot(obj, entity_type, target_log.after_data)
        # 撤销只回退业务内容，保持当前未作废状态
        obj.is_void = False
        obj.voided_at = None
        recompute_derived(entity_type, obj, {f: True for f in ENTITY_FIELDS[entity_type]["fields"]})
        obj.version += 1
        after = snapshot(obj, entity_type)
        _add_log(db, entity_type, obj, "undo", operator, reason,
                 before, after, obj.version - 1, obj.version, )
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(obj)
    return obj


# ---------------- 查询：版本历史 ----------------
def history(db: Session, entity_type: str, entity_id: int) -> list:
    logs = (
        db.query(models.AuditLog)
        .filter_by(entity_type=entity_type, entity_id=entity_id)
        .order_by(models.AuditLog.seq.asc())
        .all()
    )
    return [_log_dict(log) for log in logs]


def _display_value(entity_type: str, field: str, value):
    if value is None:
        return "空"
    if field == "session":
        return {"morning": "早班", "noon": "午班", "evening": "晚班"}.get(value, value)
    if field == "discarded" or field == "treated":
        return "是" if value else "否"
    if field == "record_type":
        return {"checkup": "常规体检", "diagnosis": "疾病诊断",
                "vaccination": "免疫接种"}.get(value, value)
    if field == "severity":
        return {"mild": "轻度", "moderate": "中度", "severe": "重度"}.get(value, value)
    if field == "result":
        return {"recovered": "已康复", "ongoing": "治疗中",
                "observed": "观察中"}.get(value, "未结案" if value is None else value)
    if field == "cow_id":
        return f"牛#{value}"
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def _diff_fields(entity_type: str, before: Optional[dict], after: Optional[dict]) -> list:
    """对比两个快照，返回字段级差异"""
    out = []
    if not after:
        return out
    keys = ENTITY_FIELDS[entity_type]["fields"]
    labels = ENTITY_FIELDS[entity_type]["labels"]
    # drug_id 是内部关联键，其信息已体现在“药品名称”，不在版本差异中展示
    hidden = {"cow_id", "drug_id"}
    for f in keys:
        if f in hidden:
            continue
        old = before.get(f) if before else None
        new = after.get(f)
        if old == new:
            continue
        if not before:
            continue
        out.append({
            "field": f,
            "label": labels.get(f, f),
            "old": _display_value(entity_type, f, old) if before else None,
            "new": _display_value(entity_type, f, new),
        })
    return out


def _log_dict(log: models.AuditLog) -> dict:
    entity_type = log.entity_type
    changes = _diff_fields(entity_type, log.before_data, log.after_data)
    # 作废 / 恢复属于状态流转，在差异列表中显式展示，避免历史看起来“什么都没变”
    if log.action == "void":
        changes.insert(0, {"field": "__status__", "label": "记录状态",
                           "old": "有效", "new": "已作废"})
    elif log.action == "restore":
        changes.insert(0, {"field": "__status__", "label": "记录状态",
                           "old": "已作废", "new": "已恢复"})
    return {
        "id": log.id,
        "seq": log.seq,
        "action": log.action,
        "action_label": ACTION_LABELS.get(log.action, log.action),
        "operator": log.operator,
        "operator_display": (
            "（历史起点，无操作人记录）" if log.action == "baseline"
            else (log.operator or "（未记录操作人）")
        ),
        "reason": log.reason,
        "version_before": log.version_before,
        "version_after": log.version_after,
        "operated_at": log.operated_at.isoformat() if log.operated_at else None,
        "changes": changes,
        "snapshot": log.after_data,
        "is_baseline": log.action == "baseline",
    }


# ---------------- 牛只时间线 ----------------
def cow_timeline(db: Session, cow_id: int) -> list:
    """沿牛只聚合三类记录的当前状态与历次版本，按业务日期倒序"""
    logs = (
        db.query(models.AuditLog)
        .filter_by(cow_id=cow_id)
        .order_by(models.AuditLog.operated_at.asc(), models.AuditLog.seq.asc())
        .all()
    )
    # entity -> 聚合条目（以首次出现的日志为准）
    entries: dict = {}
    for log in logs:
        key = (log.entity_type, log.entity_id)
        ent = entries.get(key)
        if ent is None:
            cfg = ENTITY_FIELDS[log.entity_type]
            obj = db.get(cfg["model"], log.entity_id)
            ent = {
                "entity_type": log.entity_type,
                "entity_type_label": cfg["name"],
                "entity_id": log.entity_id,
                "cow_id": cow_id,
                "business_date": (log.after_data or {}).get("date"),
                "version": obj.version if obj else log.version_after,
                "is_void": obj.is_void if obj else False,
                "voided_at": obj.voided_at.isoformat() if obj and obj.voided_at else None,
                "summary": _entity_summary(log.entity_type, log.after_data),
                "logs": [],
            }
            entries[key] = ent
        # 作废/恢复后业务日期不变；始终用最新快照更新摘要
        if log.after_data and log.action != "void":
            ent["business_date"] = log.after_data.get("date", ent["business_date"])
            ent["summary"] = _entity_summary(log.entity_type, log.after_data)
        ent["logs"].append(_log_dict(log))
        ent["version"] = log.version_after or ent["version"]

    out = list(entries.values())
    for ent in out:
        ent["logs"].reverse()  # 最近操作在前
        ent["current"] = ent["logs"][0] if ent["logs"] else None
    out.sort(key=lambda e: (e["business_date"] or "", e["entity_id"]), reverse=True)
    return out


def _entity_summary(entity_type: str, data: Optional[dict]) -> str:
    if not data:
        return ""
    if entity_type == "milking":
        sess = {"morning": "早班", "noon": "午班", "evening": "晚班"}.get(
            data.get("session"), data.get("session"))
        disc = "（废弃）" if data.get("discarded") else ""
        scc = f"，SCC {data['scc']}" if data.get("scc") is not None else ""
        return f"{data.get('date')} {sess} {data.get('yield_kg')}kg{disc}{scc}"
    if entity_type == "health":
        t = {"checkup": "常规体检", "diagnosis": "疾病诊断",
             "vaccination": "免疫接种"}.get(data.get("record_type"), data.get("record_type"))
        return f"{data.get('date')} {t} {data.get('diagnosis') or ''}".strip()
    if entity_type == "medication":
        return (f"{data.get('date')} {data.get('drug_name')}，"
                f"休药 {data.get('withdrawal_days', 0)} 天至 {data.get('withdrawal_end')}")
    return ""


# ---------------- 轻量迁移 + 存量数据基线 ----------------
def _has_column(table_name: str, column: str) -> bool:
    insp = inspect(engine)
    if table_name not in insp.get_table_names():
        return False
    return any(c["name"] == column for c in insp.get_columns(table_name))


def run_migrations() -> None:
    """为已存在的旧库补审计列（SQLite 支持 ADD COLUMN），并回填历史起点"""
    alter_cols = {
        "milking_records": [
            ("version", "INTEGER NOT NULL DEFAULT 1"),
            ("is_void", "BOOLEAN NOT NULL DEFAULT 0"),
            ("voided_at", "DATETIME"),
        ],
        "health_records": [
            ("version", "INTEGER NOT NULL DEFAULT 1"),
            ("is_void", "BOOLEAN NOT NULL DEFAULT 0"),
            ("voided_at", "DATETIME"),
        ],
        "medications": [
            ("version", "INTEGER NOT NULL DEFAULT 1"),
            ("is_void", "BOOLEAN NOT NULL DEFAULT 0"),
            ("voided_at", "DATETIME"),
        ],
    }
    with engine.begin() as conn:
        for table, cols in alter_cols.items():
            for name, ddl in cols:
                if not _has_column(table, name):
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    # create_all 负责建 audit_logs（新库直接建表）
    models.Base.metadata.create_all(bind=engine)
    _backfill_baselines()


def _backfill_baselines() -> None:
    """存量记录补一条 baseline 审计日志；operator 留空，明确标注历史起点"""
    db = SessionLocal()
    try:
        for entity_type, cfg in ENTITY_FIELDS.items():
            objs = db.query(cfg["model"]).all()
            existing = {
                row[0] for row in
                db.query(models.AuditLog.entity_id)
                .filter(models.AuditLog.entity_type == entity_type).all()
            }
            for obj in objs:
                if obj.id in existing:
                    continue
                db.add(models.AuditLog(
                    entity_type=entity_type,
                    entity_id=obj.id,
                    cow_id=obj.cow_id,
                    seq=1,
                    action="baseline",
                    version_before=obj.version,
                    version_after=obj.version,
                    operator=None,
                    reason=BASELINE_REASON,
                    before_data=None,
                    after_data=snapshot(obj, entity_type),
                    operated_at=obj.created_at or datetime.utcnow(),
                ))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# SQLite 对 SAVEPOINT 的支持开关（影响预览使用独立连接，不依赖此设置）
@event.listens_for(engine, "connect")
def _sqlite_pragma(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()
