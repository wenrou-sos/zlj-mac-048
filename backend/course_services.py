"""用药疗程领域：围绕病历的多药、多次、多班给药

关键口径：
- 休药期只由「已实际给药」的记录产生：疗程中仅 planned/delayed（未执行）的计划
  不算已用药；missed/cancelled 也不算。每次实际给药按当日 + 休药天数独立顺延。
- 结束疗程不会缩短或解除已经生效的休药限制（休药窗口挂在每一次实际给药上）。
- 旧的零散用药记录（medications 表）继续保留、继续参与休药校验，仅作为历史来源。
"""
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy import and_, exists, or_
from sqlalchemy.orm import Session

from . import models
from .models import (
    DOSE_ADMINISTERED,
    DOSE_CANCELLED,
    DOSE_DELAYED,
    DOSE_MISSED,
    DOSE_PLANNED,
)

SESSION_LABEL = {"morning": "早班", "noon": "午班", "evening": "晚班", None: ""}
DOSE_STATUS_LABEL = {
    DOSE_PLANNED: "待给药",
    DOSE_ADMINISTERED: "已给药",
    DOSE_MISSED: "漏用",
    DOSE_DELAYED: "已延期",
    DOSE_CANCELLED: "已取消",
}
DOSE_STATUS_BADGE = {
    DOSE_PLANNED: "blue",
    DOSE_ADMINISTERED: "green",
    DOSE_MISSED: "red",
    DOSE_DELAYED: "amber",
    DOSE_CANCELLED: "gray",
}
LINE_STATUS_LABEL = {"active": "进行中", "switched": "已换药停用", "stopped": "已停药"}


# ============================ 统一休药窗口 ============================
def withdrawal_violation_clause(on_column=None, cow_column=None):
    """某挤奶记录当日处于休药期内且未废弃的 SQL 条件（旧零散用药 + 疗程实际给药）。

    注意：planned/delayed 等未执行的计划不产生休药窗口；窗口只来自已给药剂量。
    """
    on_column = on_column or models.MilkingRecord.date
    cow_column = cow_column or models.MilkingRecord.cow_id
    med = models.Medication
    dose = models.CourseDose
    legacy = and_(
        med.cow_id == cow_column,
        med.withdrawal_days > 0,
        med.date <= on_column,
        med.withdrawal_end >= on_column,
    )
    course = and_(
        dose.cow_id == cow_column,
        dose.status == DOSE_ADMINISTERED,
        dose.withdrawal_days > 0,
        dose.administered_date <= on_column,
        dose.withdrawal_end >= on_column,
    )
    return and_(
        models.MilkingRecord.discarded.is_(False),
        or_(exists().where(legacy), exists().where(course)),
    )


def _legacy_windows(db: Session, cow_ids, min_d: date, max_d: date) -> Dict[int, list]:
    rows = (
        db.query(models.Medication)
        .filter(
            models.Medication.cow_id.in_(cow_ids),
            models.Medication.withdrawal_days > 0,
            models.Medication.date <= max_d,
            models.Medication.withdrawal_end >= min_d,
        )
        .all()
    )
    out: Dict[int, list] = {}
    for m in rows:
        out.setdefault(m.cow_id, []).append({
            "start": m.date, "end": m.withdrawal_end,
            "drug_name": m.drug_name, "source": "legacy",
        })
    return out


def _course_windows(db: Session, cow_ids, min_d: date, max_d: date) -> Dict[int, list]:
    rows = (
        db.query(models.CourseDose)
        .filter(
            models.CourseDose.cow_id.in_(cow_ids),
            models.CourseDose.status == DOSE_ADMINISTERED,
            models.CourseDose.withdrawal_days > 0,
            models.CourseDose.administered_date <= max_d,
            models.CourseDose.withdrawal_end >= min_d,
        )
        .all()
    )
    line_ids = {r.course_drug_id for r in rows}
    lines = {
        x.id: x for x in
        db.query(models.CourseDrug).filter(models.CourseDrug.id.in_(line_ids)).all()
    } if line_ids else {}
    out: Dict[int, list] = {}
    for r in rows:
        line = lines.get(r.course_drug_id)
        out.setdefault(r.cow_id, []).append({
            "start": r.administered_date, "end": r.withdrawal_end,
            "drug_name": line.drug_name if line else "用药", "source": "course",
        })
    return out


def all_withdrawal_windows(db: Session, cow_ids, min_d: date, max_d: date) -> Dict[int, list]:
    """合并旧零散用药与疗程实际给药的休药窗口（按截止日倒序）"""
    if not cow_ids:
        return {}
    merged: Dict[int, list] = {}
    for src in (_legacy_windows(db, cow_ids, min_d, max_d),
                _course_windows(db, cow_ids, min_d, max_d)):
        for cid, wins in src.items():
            merged.setdefault(cid, []).extend(wins)
    for wins in merged.values():
        wins.sort(key=lambda w: w["end"], reverse=True)
    return merged


def active_withdrawal_window(db: Session, cow_id: int, on_date: date) -> Optional[dict]:
    """该牛该日生效的休药窗口（取截止日最晚者）。未执行计划不算。"""
    windows = all_withdrawal_windows(db, [cow_id], on_date, on_date).get(cow_id, [])
    return windows[0] if windows else None


def check_withdrawal(db: Session, cow_id: int, on_date: date) -> dict:
    """统一休药校验：疗程实际给药与旧零散用药合并判断"""
    win = active_withdrawal_window(db, cow_id, on_date)
    if not win:
        return {"in_withdrawal": False, "medication": None, "window": None}
    return {
        "in_withdrawal": True,
        "medication": None,
        "window": win,
        "withdrawal_end": win["end"],
        "drug_name": win["drug_name"],
        "message": f"该牛自 {win['start']} 使用 {win['drug_name']}，"
                   f"牛奶休药期至 {win['end']}（含当天），当日鲜奶应废弃",
    }


def cows_in_withdrawal(db: Session, on_date: date) -> set:
    """当日处于休药期内的牛只集合（旧 + 疗程）"""
    legacy = {
        cid for (cid,) in db.query(models.Medication.cow_id).filter(
            models.Medication.withdrawal_days > 0,
            models.Medication.date <= on_date,
            models.Medication.withdrawal_end >= on_date,
        ).distinct().all()
    }
    course = {
        cid for (cid,) in db.query(models.CourseDose.cow_id).filter(
            models.CourseDose.status == DOSE_ADMINISTERED,
            models.CourseDose.withdrawal_days > 0,
            models.CourseDose.administered_date <= on_date,
            models.CourseDose.withdrawal_end >= on_date,
        ).distinct().all()
    }
    return legacy | course


# ============================ 疗程事件 ============================
def _log(db: Session, course: models.TreatmentCourse, event_type: str,
         summary: str, detail: Optional[str] = None, operator: Optional[str] = None):
    db.add(models.CourseEvent(
        course_id=course.id, event_type=event_type, summary=summary,
        detail=detail, operator=operator))


def _resolve_drug(db: Session, drug_id: Optional[int],
                  drug_name: Optional[str], withdrawal_days: Optional[int]):
    drug = None
    if drug_id:
        drug = db.get(models.DrugCatalog, drug_id)
        if not drug:
            raise HTTPException(404, "未找到该药品")
        drug_name = drug.name
        if withdrawal_days is None:
            withdrawal_days = drug.default_withdrawal_days
    if not drug_name:
        raise HTTPException(400, "请选择药品或填写药品名称")
    if withdrawal_days is None:
        withdrawal_days = 0
    return drug, drug_name, withdrawal_days


def _planned_slots(start: date, times_per_day: int, interval_days: int,
                   total_doses: Optional[int], days: Optional[int],
                   planned_times: Optional[List[str]]):
    """生成 (日期, 班次) 计划槽位。total_doses 优先；否则按连用天数 × 每日次数。"""
    default_times = ["morning", "noon", "evening"]
    times = planned_times or default_times[:max(1, times_per_day)]
    if len(times) != times_per_day:
        raise HTTPException(400, "每日班次数与每日次数不一致")
    if total_doses is None:
        n_days = days or 1
        total_doses = n_days * times_per_day
    slots = []
    day_index = 0
    while len(slots) < total_doses:
        d = start + timedelta(days=day_index * interval_days)
        for t in times:
            slots.append((d, t))
            if len(slots) >= total_doses:
                break
        day_index += 1
    return slots


# ============================ 疗程序列化 ============================
def dose_to_dict(d: models.CourseDose, line: Optional[models.CourseDrug] = None,
                 today: Optional[date] = None) -> dict:
    today = today or date.today()
    # 延期未执行：显示改期后的目标日
    effective_date = d.delayed_to if d.status == DOSE_DELAYED else d.planned_date
    overdue = (
        d.status in (DOSE_PLANNED, DOSE_DELAYED)
        and effective_date < today
    )
    due_today = d.status in (DOSE_PLANNED, DOSE_DELAYED) and effective_date == today
    return {
        "id": d.id, "course_id": d.course_id, "course_drug_id": d.course_drug_id,
        "dose_no": d.dose_no,
        "planned_date": str(d.planned_date),
        "planned_time": d.planned_time,
        "planned_time_label": SESSION_LABEL.get(d.planned_time, ""),
        "planned_dose": d.planned_dose,
        "status": d.status,
        "status_label": DOSE_STATUS_LABEL[d.status],
        "status_badge": DOSE_STATUS_BADGE[d.status],
        "effective_date": str(effective_date),
        "administered_date": str(d.administered_date) if d.administered_date else None,
        "administered_time": d.administered_time,
        "administered_time_label": SESSION_LABEL.get(d.administered_time, ""),
        "administered_dose": d.administered_dose,
        "withdrawal_days": d.withdrawal_days,
        "withdrawal_end": str(d.withdrawal_end) if d.withdrawal_end else None,
        "sale_date": str(d.withdrawal_end + timedelta(days=1)) if d.withdrawal_end else None,
        "operator": d.operator,
        "reason": d.reason,
        "delayed_to": str(d.delayed_to) if d.delayed_to else None,
        "cancel_scope": d.cancel_scope,
        "note": d.note,
        "recorded_at": str(d.recorded_at) if d.recorded_at else None,
        "overdue": overdue,
        "due_today": due_today,
        "drug_name": line.drug_name if line else None,
        "route": line.route if line else None,
        "line_status": line.status if line else None,
    }


def course_to_dict(c: models.TreatmentCourse, today: Optional[date] = None) -> dict:
    today = today or date.today()
    lines_map = {ln.id: ln for ln in c.drug_lines}
    doses = sorted(c.doses, key=lambda d: (d.planned_date, d.id))

    counts = {"planned": 0, "administered": 0, "missed": 0,
              "delayed": 0, "cancelled": 0}
    remaining: List[dict] = []
    latest_end = None
    for d in doses:
        counts[d.status] = counts.get(d.status, 0) + 1
        ln = lines_map.get(d.course_drug_id)
        if d.status == DOSE_ADMINISTERED and d.withdrawal_end:
            if latest_end is None or d.withdrawal_end > latest_end:
                latest_end = d.withdrawal_end
        if d.status in (DOSE_PLANNED, DOSE_DELAYED) and ln and ln.status == "active":
            remaining.append(dose_to_dict(d, ln, today))

    # 下一次待办：延期按新日期，普通按计划日，取最早
    def _due_key(x):
        return date.fromisoformat(x["effective_date"])
    remaining.sort(key=_due_key)
    next_due = remaining[0] if remaining else None
    in_withdrawal = latest_end is not None and latest_end >= today

    return {
        "id": c.id, "cow_id": c.cow_id,
        "health_record_id": c.health_record_id,
        "title": c.title,
        "start_date": str(c.start_date),
        "planned_end_date": str(c.planned_end_date) if c.planned_end_date else None,
        "end_date": str(c.end_date) if c.end_date else None,
        "status": c.status,
        "status_label": "进行中" if c.status == "active" else "已结束",
        "end_reason": c.end_reason,
        "veterinarian": c.veterinarian,
        "note": c.note,
        "created_at": str(c.created_at) if c.created_at else None,
        "drug_lines": [{
            "id": ln.id, "drug_id": ln.drug_id, "drug_name": ln.drug_name,
            "planned_dose": ln.planned_dose, "route": ln.route,
            "times_per_day": ln.times_per_day, "interval_days": ln.interval_days,
            "withdrawal_days": ln.withdrawal_days, "seq": ln.seq,
            "status": ln.status, "status_label": LINE_STATUS_LABEL[ln.status],
            "change_reason": ln.change_reason,
        } for ln in c.drug_lines],
        "doses": [dose_to_dict(d, lines_map.get(d.course_drug_id), today) for d in doses],
        "events": [{
            "id": e.id, "event_type": e.event_type, "summary": e.summary,
            "detail": e.detail, "operator": e.operator,
            "occurred_at": str(e.occurred_at) if e.occurred_at else None,
        } for e in c.events],
        "counts": counts,
        "remaining_count": len(remaining),
        "remaining": remaining,
        "next_due": next_due,
        "withdrawal_end": str(latest_end) if latest_end else None,
        "in_withdrawal": in_withdrawal,
    }


# ============================ 疗程创建 ============================
def create_course(db: Session, payload) -> models.TreatmentCourse:
    cow = db.get(models.Cow, payload.cow_id)
    if not cow:
        raise HTTPException(404, "未找到该牛")
    if not payload.drugs:
        raise HTTPException(400, "至少安排一种药")
    if payload.planned_end_date and payload.planned_end_date < payload.start_date:
        raise HTTPException(400, "计划结束日期不能早于开始日期")
    if payload.health_record_id:
        hr = db.get(models.HealthRecord, payload.health_record_id)
        if not hr or hr.cow_id != cow.id:
            raise HTTPException(400, "病历不属于该牛，无法关联")

    course = models.TreatmentCourse(
        cow_id=cow.id, health_record_id=payload.health_record_id,
        title=payload.title, start_date=payload.start_date,
        planned_end_date=payload.planned_end_date,
        veterinarian=payload.veterinarian, note=payload.note)
    db.add(course)
    db.flush()

    for idx, item in enumerate(payload.drugs, start=1):
        drug, drug_name, wd = _resolve_drug(
            db, item.drug_id, item.drug_name, item.withdrawal_days)
        line = models.CourseDrug(
            course_id=course.id, drug_id=drug.id if drug else None,
            drug_name=drug_name, planned_dose=item.planned_dose, route=item.route,
            times_per_day=item.times_per_day, interval_days=item.interval_days,
            withdrawal_days=wd, seq=1, status="active")
        db.add(line)
        db.flush()
        slots = _planned_slots(
            payload.start_date, item.times_per_day, item.interval_days,
            item.total_doses, item.days, item.planned_times)
        for no, (d, t) in enumerate(slots, start=1):
            db.add(models.CourseDose(
                course_id=course.id, course_drug_id=line.id, cow_id=cow.id,
                dose_no=no, planned_date=d, planned_time=t,
                planned_dose=item.planned_dose, status=DOSE_PLANNED))

    _log(db, course, "created",
         f"建立疗程「{course.title}」，安排 {len(payload.drugs)} 种药",
         detail=course.note, operator=payload.veterinarian)
    db.commit()
    db.refresh(course)
    return course


def ensure_course_deletable(db: Session, course: models.TreatmentCourse) -> None:
    """删除疗程前的安全校验。

    只要存在已实际给药的记录，就禁止删除：实际给药是既成医疗事实，
    其休药窗口仍可能有效，删除会让休药校验/废弃奶拦截与历史追溯同时失效。
    这类疗程应“结束疗程”而不是删除。
    """
    given = (
        db.query(models.CourseDose)
        .filter(
            models.CourseDose.course_id == course.id,
            models.CourseDose.status == DOSE_ADMINISTERED,
        )
        .count()
    )
    if given:
        raise HTTPException(
            409,
            f"该疗程已有 {given} 次实际给药记录，不能删除（实际给药与其休药限制须保留）；"
            "如治疗已完成请改为“结束疗程”。")


def update_note(db: Session, course: models.TreatmentCourse, note: str):
    course.note = note
    _log(db, course, "note", "更新交班备注", detail=note)
    db.commit()
    db.refresh(course)
    return course


# ============================ 加药 / 换药 ============================
def add_drug_line(db: Session, course: models.TreatmentCourse, payload):
    if course.status != "active":
        raise HTTPException(400, "疗程已结束，不能再加药")
    start = payload.start_date or date.today()
    drug, drug_name, wd = _resolve_drug(
        db, payload.drug_id, payload.drug_name, payload.withdrawal_days)

    replaced = None
    if payload.replace_line_id:
        replaced = db.get(models.CourseDrug, payload.replace_line_id)
        if not replaced or replaced.course_id != course.id:
            raise HTTPException(404, "未找到被替换的用药行")
        if replaced.status != "active":
            raise HTTPException(400, "该用药已停用，不能再次换药")
        if not payload.reason:
            raise HTTPException(400, "换药必须注明原因")
    if replaced and not payload.reason:
        raise HTTPException(400, "换药必须注明原因")

    new_seq = (max((ln.seq for ln in course.drug_lines), default=0) + 1) if replaced else 1
    line = models.CourseDrug(
        course_id=course.id, drug_id=drug.id if drug else None,
        drug_name=drug_name, planned_dose=payload.planned_dose, route=payload.route,
        times_per_day=payload.times_per_day, interval_days=payload.interval_days,
        withdrawal_days=wd, seq=new_seq, status="active")
    db.add(line)
    db.flush()
    slots = _planned_slots(
        start, payload.times_per_day, payload.interval_days,
        payload.total_doses, payload.days, payload.planned_times)
    for no, (d, t) in enumerate(slots, start=1):
        db.add(models.CourseDose(
            course_id=course.id, course_drug_id=line.id, cow_id=course.cow_id,
            dose_no=no, planned_date=d, planned_time=t,
            planned_dose=payload.planned_dose, status=DOSE_PLANNED))

    if replaced:
        _cancel_pending(db, course, replaced,
                        reason=f"换药为 {drug_name}：{payload.reason}",
                        event_type="switched", operator=None,
                        scope="switch")
        replaced.status = "switched"
        replaced.change_reason = f"换为 {drug_name}：{payload.reason}"
        replaced.changed_at = datetime.utcnow()
        _log(db, course, "switched",
             f"{replaced.drug_name} 换药为 {drug_name}",
             detail=payload.reason)
    else:
        _log(db, course, "added_drug",
             f"疗程中加药 {drug_name}（自 {start} 起）",
             detail=payload.reason)
    db.commit()
    db.refresh(course)
    return course


def stop_line(db: Session, course: models.TreatmentCourse,
              line: models.CourseDrug, reason: str):
    if course.status != "active":
        raise HTTPException(400, "疗程已结束")
    if line.status != "active":
        raise HTTPException(400, "该用药行已停用")
    _cancel_pending(db, course, line, reason=f"停药：{reason}",
                    event_type="stopped", operator=None, scope="line_stop")
    line.status = "stopped"
    line.change_reason = reason
    line.changed_at = datetime.utcnow()
    _log(db, course, "stopped", f"{line.drug_name} 提前停药", detail=reason)
    db.commit()
    db.refresh(course)
    return course


def _cancel_pending(db: Session, course: models.TreatmentCourse,
                    line: models.CourseDrug, reason: str,
                    event_type: str, operator: Optional[str],
                    scope: str = "manual"):
    """取消某用药行全部尚未执行（含延期）的计划；已给药/漏用/已取消保持不变。

    scope 标记取消来源，决定之后能否逐次恢复：
    manual（单次取消）可逐次恢复；line_stop/switch/course_end 为批量取消，
    只能随用药行恢复 / 疗程重开整批恢复。
    """
    pending = [d for d in line.doses
               if d.status in (DOSE_PLANNED, DOSE_DELAYED)]
    for d in pending:
        d.status = DOSE_CANCELLED
        d.reason = reason
        d.cancel_scope = scope
        d.delayed_to = None
        d.recorded_at = datetime.utcnow()
    if pending:
        db.flush()


# ============================ 逐次给药 / 漏用 / 延期 / 取消 ============================
def _get_dose(db: Session, dose_id: int):
    d = db.get(models.CourseDose, dose_id)
    if not d:
        raise HTTPException(404, "未找到该次给药计划")
    course = db.get(models.TreatmentCourse, d.course_id)
    line = db.get(models.CourseDrug, d.course_drug_id)
    return d, course, line


def administer_dose(db: Session, dose_id: int, payload):
    d, course, line = _get_dose(db, dose_id)
    if d.status == DOSE_ADMINISTERED:
        raise HTTPException(400, "该次已记录实际给药，请勿重复执行")
    if d.status == DOSE_CANCELLED:
        if d.cancel_scope in ("line_stop", "course_end"):
            raise HTTPException(
                400, "该次已随停药/结束疗程批量取消，不能直接给药；"
                     "请先“恢复用药”或重新打开疗程")
        if d.cancel_scope == "switch":
            raise HTTPException(400, "该次已随换药取消，不能给药；需要时请在疗程中加药")
        raise HTTPException(400, "该次已取消，不能给药；如需补做请先恢复为待给药")
    if course.status != "active":
        raise HTTPException(400, "疗程已结束，不能再给药；如需继续请先重新打开疗程")
    if line.status != "active":
        raise HTTPException(
            400, f"「{line.drug_name}」已{'换药停用' if line.status == 'switched' else '停药'}，"
                 "不能给药；请先恢复该用药或在疗程中加药")

    admin_date = payload.administered_date or date.today()
    admin_time = payload.administered_time or d.planned_time
    # 休药期：默认取本用药行快照，可在本次执行时覆盖；随实际给药日顺延
    wd = payload.withdrawal_days
    if wd is None:
        wd = line.withdrawal_days

    planned_label = f"{d.planned_date} {SESSION_LABEL.get(d.planned_time, '')}".strip()
    delayed_note = ""
    if d.status == DOSE_DELAYED:
        delayed_note = f"（由 {planned_label} 延期）"
    makeup_note = ""
    if d.status == DOSE_MISSED:
        makeup_note = "（漏用后补做）"
    dev_note = ""
    if admin_date != d.planned_date or (
            d.planned_time and admin_time != d.planned_time):
        if d.status not in (DOSE_DELAYED, DOSE_MISSED):
            dev_note = f"，实际偏离计划 {planned_label}"
    if payload.administered_dose and d.planned_dose and \
            payload.administered_dose != d.planned_dose:
        dev_note += "，剂量有调整"

    d.status = DOSE_ADMINISTERED
    d.administered_date = admin_date
    d.administered_time = admin_time
    d.administered_dose = payload.administered_dose or d.planned_dose
    d.withdrawal_days = wd
    d.withdrawal_end = admin_date + timedelta(days=wd)
    d.operator = payload.operator
    d.recorded_by = payload.operator
    d.recorded_at = datetime.utcnow()
    if payload.note:
        d.note = payload.note

    time_label = SESSION_LABEL.get(admin_time, "")
    _log(db, course, "administered",
         f"第 {d.dose_no} 次 {line.drug_name} 已给药：{admin_date} {time_label}".strip()
         + makeup_note + delayed_note + dev_note,
         detail=(f"剂量 {d.administered_dose or '-'}；休药 {wd} 天，至 {d.withdrawal_end}"
                 + (f"；{payload.note}" if payload.note else "")),
         operator=payload.operator)
    db.commit()
    db.refresh(d)
    return d


def mark_dose(db: Session, dose_id: int, new_status: str, payload):
    """漏用 / 取消：必须注明原因；二者都不产生休药窗口。

    只能对“进行中用药行 + 进行中疗程”里尚未执行的次数做单次操作；
    停药/换药/结束疗程导致的批量取消不能在这里改动。
    """
    d, course, line = _get_dose(db, dose_id)
    if course.status != "active":
        raise HTTPException(400, "疗程已结束，不能逐次操作")
    if line.status != "active":
        raise HTTPException(400, "该用药行已停药/换药，不能逐次操作")
    if d.status == DOSE_ADMINISTERED:
        raise HTTPException(400, "已实际给药的次数不能标记为漏用/取消")
    if d.status == DOSE_CANCELLED:
        raise HTTPException(400, "该次已取消")
    if not payload.reason.strip():
        raise HTTPException(400, "必须注明原因")

    d.status = new_status
    d.reason = payload.reason
    d.recorded_at = datetime.utcnow()
    if payload.note:
        d.note = payload.note
    if new_status == DOSE_CANCELLED:
        d.cancel_scope = "manual"
        d.delayed_to = None
    planned = f"{d.planned_date} {SESSION_LABEL.get(d.planned_time, '')}".strip()
    etype = "missed" if new_status == DOSE_MISSED else "cancelled"
    _log(db, course, etype,
         f"第 {d.dose_no} 次 {line.drug_name}（{planned}）"
         f"{'漏用' if new_status == DOSE_MISSED else '取消'}：{payload.reason}",
         detail=payload.note)
    db.commit()
    db.refresh(d)
    return d


def delay_dose(db: Session, dose_id: int, payload):
    """延期：计划保留，另记新目标日；在实际执行前不产生休药窗口。"""
    d, course, line = _get_dose(db, dose_id)
    if course.status != "active":
        raise HTTPException(400, "疗程已结束，不能延期")
    if line.status != "active":
        raise HTTPException(400, "该用药行已停药/换药，不能延期")
    if d.status not in (DOSE_PLANNED, DOSE_DELAYED):
        raise HTTPException(400, "只有待给药/已延期的次数可以改期")
    base = d.delayed_to if d.status == DOSE_DELAYED else d.planned_date
    if payload.delayed_to <= base:
        raise HTTPException(400, "新日期必须晚于当前计划日期")
    if not payload.reason.strip():
        raise HTTPException(400, "延期必须注明原因")

    prev = f"{base} {SESSION_LABEL.get(d.planned_time, '')}".strip()
    d.status = DOSE_DELAYED
    d.delayed_to = payload.delayed_to
    d.reason = payload.reason
    d.recorded_at = datetime.utcnow()
    if payload.note:
        d.note = payload.note
    _log(db, course, "delayed",
         f"第 {d.dose_no} 次 {line.drug_name} 由 {prev} 延期至 {payload.delayed_to}",
         detail=payload.reason)
    db.commit()
    db.refresh(d)
    return d


def reset_dose(db: Session, dose_id: int):
    """把单次漏用/延期/手动取消恢复为待给药。

    已给药不可撤销；因停药/换药/结束疗程被批量取消的次数不能逐次恢复，
    必须通过“恢复用药（行级）”或“重新打开疗程”整批恢复，保证用药行状态一致。
    """
    d, course, line = _get_dose(db, dose_id)
    if d.status == DOSE_ADMINISTERED:
        raise HTTPException(400, "已实际给药的记录不能撤销")
    if d.status == DOSE_CANCELLED and d.cancel_scope in ("line_stop", "course_end"):
        raise HTTPException(400, "该次随停药/结束疗程批量取消，请使用“恢复用药”或重新打开疗程")
    if d.status == DOSE_CANCELLED and d.cancel_scope == "switch":
        raise HTTPException(400, "该次随换药取消，不能恢复；需要时请在疗程中加药")
    if d.status == DOSE_PLANNED:
        return d
    old = DOSE_STATUS_LABEL[d.status]
    d.status = DOSE_PLANNED
    d.delayed_to = None
    d.reason = None
    d.cancel_scope = None
    d.recorded_at = None
    _log(db, course, "note",
         f"第 {d.dose_no} 次 {line.drug_name} 由「{old}」恢复为待给药")
    db.commit()
    db.refresh(d)
    return d


# ============================ 结束疗程 ============================
def end_course(db: Session, course: models.TreatmentCourse, payload):
    if course.status != "active":
        raise HTTPException(400, "疗程已结束")
    end_d = payload.end_date or date.today()
    course.status = "ended"
    course.end_date = end_d
    course.end_reason = payload.end_reason

    # 剩余计划一律取消并注明“结束疗程”，但不动任何已生效的休药窗口
    n = 0
    for line in course.drug_lines:
        if line.status == "active":
            line.status = "stopped"
            line.change_reason = f"结束疗程：{payload.end_reason}"
            line.changed_at = datetime.utcnow()
        for d in line.doses:
            if d.status in (DOSE_PLANNED, DOSE_DELAYED):
                d.status = DOSE_CANCELLED
                d.reason = f"结束疗程：{payload.end_reason}"
                d.cancel_scope = "course_end"
                d.delayed_to = None
                d.recorded_at = datetime.utcnow()
                n += 1
    _log(db, course, "ended",
         f"结束疗程：{payload.end_reason}",
         detail=f"{n} 次未执行计划已取消；既有休药限制继续有效")
    db.commit()
    db.refresh(course)
    return course


def reopen_course(db: Session, course: models.TreatmentCourse):
    """重开误结束的疗程：恢复疗程、用药行，以及“随结束疗程批量取消”的计划。

    单次手动取消、漏用、换药停用的次数不在此恢复（换药本身仍是既定医嘱）。
    """
    if course.status == "active":
        return course
    course.status = "active"
    course.end_date = None
    course.end_reason = None
    restored = 0
    for line in course.drug_lines:
        if line.change_reason and line.change_reason.startswith("结束疗程："):
            line.status = "active"
            line.change_reason = None
            line.changed_at = None
        for d in line.doses:
            if d.status == DOSE_CANCELLED and d.cancel_scope == "course_end":
                d.status = DOSE_PLANNED
                d.reason = None
                d.cancel_scope = None
                d.recorded_at = None
                restored += 1
    _log(db, course, "note",
         f"疗程重新打开，恢复 {restored} 次随结束取消的待给药计划")
    db.commit()
    db.refresh(course)
    return course


def resume_line(db: Session, course: models.TreatmentCourse,
                line: models.CourseDrug, note: Optional[str] = None):
    """撤销“提前停药”：恢复用药行，并把随停药批量取消的计划整批复为待给药。

    换药停用（switched）不允许恢复——换药是确定的医嘱变更，需要继续请重新加药。
    """
    if line.status == "active":
        return course
    if line.status == "switched":
        raise HTTPException(400, "换药停用不能撤销；如仍需该药请在疗程中加药")
    if course.status != "active":
        raise HTTPException(400, "疗程已结束，请先重新打开疗程")
    restored = 0
    for d in line.doses:
        if d.status == DOSE_CANCELLED and d.cancel_scope == "line_stop":
            d.status = DOSE_PLANNED
            d.reason = None
            d.cancel_scope = None
            d.recorded_at = None
            restored += 1
    line.status = "active"
    line.change_reason = None
    line.changed_at = None
    _log(db, course, "note",
         f"恢复用药 {line.drug_name}，恢复 {restored} 次待给药计划", detail=note)
    db.commit()
    db.refresh(course)
    return course
