"""
待办任务服务：提醒对账（持久化派单）、认领/指派/延期/完成、交班。

设计要点
--------
1. 幂等派单：提醒引擎每次算出的事项都带稳定指纹 dedup_key。
   reconcile 时同指纹只建一次任务；刷新页面或重启服务绝不重复派出。
2. 源记录变更：对比 source_hash，内容变化标记 source_state=updated（“已更新”）；
   源记录删除/牛只离场/事项已在业务模块办结，标记 source_state=invalid（“已失效”），
   任务留痕不再提醒，而不是悄悄消失。
3. 并发认领：所有写操作带 version 乐观锁，两人同时认领时后提交者得到 409，
   并返回先认领的人，前端可立即看到冲突。
4. 安全红线：续用药/孕检的“完成”必须落到真实业务登记（treated / 孕检结果），
   仅在待办上点完成不允许；休药警告（withdrawal）在有效期内始终有效、
   不可被“完成”隐藏，到期由对账自动结案。
"""
import json
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from . import models, services
from .models import (
    ReminderTask, TaskEvent, Person, Handover,
)

TYPE_LABEL = {
    "estrus": "发情配种", "return_estrus": "返情观察", "preg_check": "妊娠检查",
    "open_cow": "长期空怀", "medication_dose": "续用药", "withdrawal": "休药期",
    "health_followup": "健康复查", "calving": "待产", "first_insemination": "产后首配",
}
# 需要真实业务登记才能办结的事项：type -> 说明
REGISTER_GUARD = {
    "medication_dose": "请先在「健康与用药-用药记录」执行本次用药（已执行/登记用药），"
                       "不能用待办完成代替真实用药登记",
    "preg_check": "请先在「发情与配种」回填孕检结果（已孕/未孕），"
                  "不能用待办完成代替真实孕检登记",
}
# 休药警告：只读安全事项，不允许认领后“完成”，有效期内始终展示
READONLY_TYPES = {"withdrawal"}
ACTIVE_STATUSES = (
    models.TASK_STATUS_OPEN, models.TASK_STATUS_CLAIMED,
    models.TASK_STATUS_POSTPONED, models.TASK_STATUS_CHANGED,
)


class Conflict(Exception):
    """乐观锁冲突（HTTP 409）"""

    def __init__(self, message: str, current: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.current = current


# ---------------------------------------------------------------- 序列化
def task_to_dict(t: ReminderTask) -> dict:
    today = date.today()
    days_overdue = 0
    if t.due_date and t.status not in (models.TASK_STATUS_DONE, models.TASK_STATUS_INVALID):
        days_overdue = max(0, (today - t.due_date).days)
    return {
        "id": t.id,
        "dedup_key": t.dedup_key,
        "type": t.type,
        "type_label": TYPE_LABEL.get(t.type, t.type),
        "cow_id": t.cow_id,
        "title": t.title,
        "detail": t.detail,
        "level": t.level,
        "due_date": str(t.due_date) if t.due_date else None,
        "days_overdue": days_overdue,
        "ref_type": t.ref_type,
        "ref_id": t.ref_id,
        "status": t.status,
        "source_state": t.source_state,
        "readonly": t.type in READONLY_TYPES,
        "register_required": t.type in REGISTER_GUARD and not t.registered,
        "registered": t.registered,
        "owner_id": t.owner_id,
        "owner_name": t.owner_name,
        "postpone_reason": t.postpone_reason,
        "result_note": t.result_note,
        "shift_code": t.shift_code,
        "shift_label": t.shift_label,
        "carry_count": t.carry_count,
        "version": t.version,
        "claimed_at": t.claimed_at.strftime("%Y-%m-%d %H:%M") if t.claimed_at else None,
        "postponed_at": t.postponed_at.strftime("%Y-%m-%d %H:%M") if t.postponed_at else None,
        "done_at": t.done_at.strftime("%Y-%m-%d %H:%M") if t.done_at else None,
        "closed_at": t.closed_at.strftime("%Y-%m-%d %H:%M") if t.closed_at else None,
        "created_at": t.created_at.strftime("%Y-%m-%d %H:%M") if t.created_at else None,
        "events": [
            {
                "event": e.event, "actor": e.actor, "detail": e.detail,
                "shift": e.shift_code,
                "at": e.created_at.strftime("%m-%d %H:%M") if e.created_at else None,
            } for e in t.events
        ],
    }


# ---------------------------------------------------------------- 人员
def get_or_create_person(db: Session, name: str, role: Optional[str] = None) -> Person:
    name = (name or "").strip()
    if not name:
        raise ValueError("请填写负责人姓名")
    p = db.query(Person).filter(Person.name == name).first()
    if not p:
        p = Person(name=name, role=role, active=True)
        db.add(p)
        db.flush()
    elif not p.active:
        p.active = True
    return p


def _apply_owner(t: ReminderTask, person: Optional[Person], name: Optional[str]) -> None:
    t.owner_id = person.id if person else None
    t.owner_name = person.name if person else (name or None)


# ---------------------------------------------------------------- 源核对
def _resolve_stale(db: Session, t: ReminderTask, today: date) -> None:
    """
    引擎本次没有产出该任务（窗口滑出/源被改或删/业务已办结）。
    回到源记录核对：
      - 源删除/牛只离场 -> 失效（invalid，留痕）
      - 在业务模块真实办结（用药已执行/已孕/已配/康复）-> 完成（done）
      - 只是滑出提醒窗口（逾期、窗口滑过）-> 保留待办继续跟，不丢单
    """
    invalid_reason = None
    done_reason = None
    cow = db.get(models.Cow, t.cow_id) if t.cow_id else None
    if t.cow_id and not cow:
        invalid_reason = "牛只档案已删除，待办失效"
    elif cow and cow.status == "sold":
        invalid_reason = f"牛只 {cow.ear_tag} 已离场，待办失效"

    if invalid_reason is None:
        if t.ref_type == "estrus":
            e = db.get(models.EstrusRecord, t.ref_id) if t.ref_id else None
            if e is None:
                invalid_reason = "发情/配种源记录已删除，待办失效"
            elif e.result == "pregnant":
                done_reason = "已确认妊娠，该事项办结"
            elif t.type == "estrus" and e.inseminated:
                done_reason = "已完成配种，发情待配事项办结"
            elif t.type == "open_cow" and e.result == "negative":
                # 判未孕本身正是长期空怀事项要跟进的状态，不办结
                pass
        elif t.ref_type == "medication":
            m = db.get(models.Medication, t.ref_id) if t.ref_id else None
            if m is None:
                invalid_reason = "用药源记录已删除，待办失效"
            elif t.type == "medication_dose" and m.treated:
                done_reason = "本次用药已在用药管理中执行登记，续用药事项办结"
            elif t.type == "withdrawal":
                # 休药警告到期由引擎自然消失：到期自动安全结案；记录被删则失效
                if today > m.withdrawal_end:
                    t.status = models.TASK_STATUS_DONE
                    t.source_state = models.SOURCE_STATE_ACTIVE
                    t.result_note = f"休药期已于 {m.withdrawal_end} 到期，鲜奶可恢复上市"
                    t.done_at = datetime.utcnow()
                    t.closed_at = datetime.utcnow()
                    t.version += 1
                    db.add(TaskEvent(task_id=t.id, event="done",
                                     detail="休药期到期，警告自动解除",
                                     shift_code=services.current_shift()["code"]))
                    return
                invalid_reason = "休药期用药记录已删除，警告失效"
        elif t.ref_type == "health":
            h = db.get(models.HealthRecord, t.ref_id) if t.ref_id else None
            if h is None:
                invalid_reason = "健康源记录已删除，待办失效"
            elif h.result == "recovered":
                done_reason = "病历已结案（康复），复查事项办结"
        elif t.ref_type == "cow":
            if t.type == "first_insemination" and cow:
                insem = (
                    db.query(models.EstrusRecord)
                    .filter(models.EstrusRecord.cow_id == cow.id)
                    .filter(models.EstrusRecord.inseminated)
                    .order_by(models.EstrusRecord.insemination_date.desc())
                    .first()
                )
                if insem and insem.result == "pregnant":
                    done_reason = "已确认妊娠，首配窗口事项办结"
                elif insem and insem.insemination_date and cow.calving_date \
                        and insem.insemination_date >= cow.calving_date:
                    done_reason = "产后已完成配种，首配窗口事项办结"
            # calving 超窗：是否已产犊仍需人工确认，保留待办（不判失效也不办结）

    shift = services.current_shift()
    if invalid_reason:
        t.status = models.TASK_STATUS_INVALID
        t.source_state = models.SOURCE_STATE_INVALID
        t.closed_at = datetime.utcnow()
        t.version += 1
        db.add(TaskEvent(task_id=t.id, event="invalid",
                         detail=invalid_reason, shift_code=shift["code"]))
    elif done_reason:
        t.status = models.TASK_STATUS_DONE
        t.source_state = models.SOURCE_STATE_REGISTERED
        t.registered = True
        t.result_note = done_reason
        t.done_at = datetime.utcnow()
        t.closed_at = datetime.utcnow()
        t.version += 1
        db.add(TaskEvent(task_id=t.id, event="done",
                         detail=done_reason + "（业务登记自动结案）",
                         shift_code=shift["code"]))
    # 两者皆无：仅滑出提醒窗口（如孕检逾期、产后窗口滑过），待办保留，
    # 继续随班续传并在列表中以“逾期/窗口外”可见。


# ---------------------------------------------------------------- 对账（幂等派单）
def reconcile(db: Session, today: Optional[date] = None) -> List[ReminderTask]:
    """
    用引擎最新提醒与持久化任务对账：
    - 新指纹 -> 建单（每个业务事项全局只派一次）
    - 同指纹且源内容变化 -> 更新内容并标记“已更新”
    - 引擎不再产出 -> 回源核对，办结/删除则“失效”留痕，滑窗则保留
    - 跨班未完成 -> 记 carried 事件、carry_count+1，带到下一班
    """
    today = today or date.today()
    raw = services.build_reminders(db, today)
    shift = services.current_shift()

    existing: Dict[str, ReminderTask] = {
        t.dedup_key: t for t in db.query(ReminderTask).all()
    }
    raw_keys = {r["dedup_key"] for r in raw}

    # 1) 引擎本次没产出的活跃任务 -> 回源核对
    for key, t in existing.items():
        if key in raw_keys:
            continue
        if t.status in (models.TASK_STATUS_DONE, models.TASK_STATUS_INVALID):
            continue
        _resolve_stale(db, t, today)

    # 2) 引擎产出的事项 -> 建单或更新
    for r in raw:
        t = existing.get(r["dedup_key"])
        if t is None:
            t = ReminderTask(
                dedup_key=r["dedup_key"], type=r["type"], cow_id=r.get("cow_id"),
                ref_type=r.get("ref_type"), ref_id=r.get("ref_id"),
                title=r["title"], detail=r["detail"], level=r["level"],
                due_date=_parse_date(r.get("due_date")),
                source_hash=r["source_hash"],
                status=models.TASK_STATUS_OPEN,
                source_state=models.SOURCE_STATE_ACTIVE,
                shift_code=shift["code"], shift_label=shift["label"],
                last_seen_at=datetime.utcnow(),
            )
            db.add(t)
            db.flush()
            db.add(TaskEvent(task_id=t.id, event="created",
                             detail=f"{shift['label']} 系统派单", shift_code=shift["code"]))
            continue

        # 同步最新展示内容（仅活跃任务；已结案任务的标题等快照保留，避免历史被改写）
        t.last_seen_at = datetime.utcnow()

        if t.status == models.TASK_STATUS_INVALID:
            # 已判失效（源删除/牛离场）的任务即便又撞到同指纹也不复活，
            # 需要时由人工“重新打开”
            continue

        if t.status == models.TASK_STATUS_DONE:
            if t.source_hash == r["source_hash"]:
                # 同一事项、内容未变：尊重此前的办结，不自动重开
                continue
            # 办结后源记录确有新变化（如未孕后继续跟进、改期复发）：重新打开，
            # 原处理记录保留，并据新内容展示
            t.title, t.detail, t.level = r["title"], r["detail"], r["level"]
            t.cow_id = r.get("cow_id")
            t.due_date = _parse_date(r.get("due_date"))
            t.status = models.TASK_STATUS_OPEN
            t.source_state = models.SOURCE_STATE_UPDATED
            t.source_hash = r["source_hash"]
            t.closed_at = t.done_at = None
            t.version += 1
            db.add(TaskEvent(task_id=t.id, event="reopened",
                             detail="办结后源记录再次变化，待办重新打开待确认",
                             shift_code=shift["code"]))
            continue

        # 活跃任务：同步最新内容
        t.title, t.detail, t.level = r["title"], r["detail"], r["level"]
        t.cow_id = r.get("cow_id")
        t.due_date = _parse_date(r.get("due_date"))

        if t.source_hash != r["source_hash"]:
            # 源记录被修改：标记“已更新”，等负责人确认；负责人/延期记录保留
            t.source_hash = r["source_hash"]
            t.source_state = models.SOURCE_STATE_UPDATED
            if t.status != models.TASK_STATUS_CHANGED:
                t.status = models.TASK_STATUS_CHANGED
                t.version += 1
            db.add(TaskEvent(task_id=t.id, event="updated",
                             detail="源记录已变更，待办内容已更新，请确认",
                             shift_code=shift["code"]))

    db.flush()

    # 3) 跨班续传：活跃任务进入新班次时记录一次 carried（每跨一班仅一次）
    for t in db.query(ReminderTask).all():
        if t.status not in ACTIVE_STATUSES:
            continue
        last_carried_shift = (
            db.query(TaskEvent.shift_code)
            .filter(TaskEvent.task_id == t.id, TaskEvent.event == "carried")
            .order_by(TaskEvent.id.desc()).limit(1).scalar()
        )
        origin_shift = last_carried_shift or t.shift_code
        # 本任务不是本班产生，且本班尚未记过 carried
        if origin_shift != shift["code"]:
            t.carry_count = (t.carry_count or 0) + 1
            db.add(TaskEvent(task_id=t.id, event="carried",
                             detail=f"由 {t.shift_label} 交班续传（第 {t.carry_count} 次跨班）",
                             shift_code=shift["code"]))

    db.commit()
    return db.query(ReminderTask).all()


def _parse_date(s: Optional[str]):
    return date.fromisoformat(s) if s else None


# ---------------------------------------------------------------- 查询
def list_tasks(db: Session, scope: str = "active") -> List[ReminderTask]:
    q = db.query(ReminderTask)
    if scope == "active":
        q = q.filter(ReminderTask.status.in_(
            [models.TASK_STATUS_OPEN, models.TASK_STATUS_CLAIMED,
             models.TASK_STATUS_POSTPONED, models.TASK_STATUS_CHANGED]))
    elif scope == "open":
        q = q.filter(ReminderTask.status.in_(
            [models.TASK_STATUS_OPEN, models.TASK_STATUS_CHANGED]))
    elif scope == "mine":
        q = q.filter(ReminderTask.status.in_(
            [models.TASK_STATUS_CLAIMED, models.TASK_STATUS_POSTPONED]))
    elif scope == "closed":
        q = q.filter(ReminderTask.status.in_(
            [models.TASK_STATUS_DONE, models.TASK_STATUS_INVALID]))
    return q.order_by(
        # 休药安全警告始终最前
        (ReminderTask.type == "withdrawal").desc(),
        ReminderTask.status.asc(),
        ReminderTask.due_date.is_(None),
        ReminderTask.due_date.asc(),
        ReminderTask.id.asc(),
    ).all()


def get_task(db: Session, task_id: int) -> ReminderTask:
    t = db.get(ReminderTask, task_id)
    if not t:
        raise LookupError("未找到该待办")
    return t


def _check_version(t: ReminderTask, version: Optional[int]) -> None:
    if version is not None and int(version) != t.version:
        raise Conflict(
            f"该待办刚被其他人操作（当前负责人：{t.owner_name or '未认领'}，"
            f"状态：{t.status}），请刷新后查看最新情况",
            current=task_to_dict(t),
        )


# ---------------------------------------------------------------- 写操作
def claim(db: Session, task_id: int, person_name: str,
          version: Optional[int] = None) -> ReminderTask:
    t = get_task(db, task_id)
    _check_version(t, version)
    if t.type in READONLY_TYPES:
        raise ValueError("休药警告为安全提示，无需认领处理；请在业务上保证该牛鲜奶废弃")
    if t.status in (models.TASK_STATUS_DONE, models.TASK_STATUS_INVALID):
        raise ValueError("该待办已结束，不能认领")
    person = get_or_create_person(db, person_name)
    if t.owner_id and t.owner_id != person.id and t.status == models.TASK_STATUS_CLAIMED:
        # 已被他人认领 -> 冲突（即便前端没带版本也强校验业务状态）
        raise Conflict(
            f"该待办已被 {t.owner_name} 认领，请与其协商或由其转交",
            current=task_to_dict(t),
        )
    _apply_owner(t, person, person.name)
    t.status = models.TASK_STATUS_CLAIMED
    t.claimed_at = datetime.utcnow()
    t.version += 1
    db.add(TaskEvent(task_id=t.id, event="claimed", actor=person.name,
                     detail=f"{person.name} 认领", shift_code=services.current_shift()["code"]))
    db.commit()
    db.refresh(t)
    return t


def assign(db: Session, task_id: int, person_name: str, actor: Optional[str],
           version: Optional[int] = None) -> ReminderTask:
    t = get_task(db, task_id)
    _check_version(t, version)
    if t.type in READONLY_TYPES:
        raise ValueError("休药警告为安全提示，不支持指派负责人")
    if t.status in (models.TASK_STATUS_DONE, models.TASK_STATUS_INVALID):
        raise ValueError("该待办已结束，不能指派")
    person = get_or_create_person(db, person_name)
    old = t.owner_name or "未认领"
    _apply_owner(t, person, person.name)
    if t.status == models.TASK_STATUS_OPEN:
        t.status = models.TASK_STATUS_CLAIMED
        t.claimed_at = datetime.utcnow()
    t.version += 1
    db.add(TaskEvent(task_id=t.id, event="assigned", actor=actor,
                     detail=f"由 {old} 指派给 {person.name}",
                     shift_code=services.current_shift()["code"]))
    db.commit()
    db.refresh(t)
    return t


def postpone(db: Session, task_id: int, reason: str, new_due: Optional[date],
             actor: Optional[str], version: Optional[int] = None) -> ReminderTask:
    t = get_task(db, task_id)
    _check_version(t, version)
    if t.type in READONLY_TYPES:
        raise ValueError("休药警告为安全提示，不能延期，有效期内必须持续执行废弃")
    if t.status in (models.TASK_STATUS_DONE, models.TASK_STATUS_INVALID):
        raise ValueError("该待办已结束，不能延期")
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("请填写延期原因")
    if new_due:
        t.due_date = new_due
    t.postpone_reason = reason
    t.postponed_at = datetime.utcnow()
    t.status = models.TASK_STATUS_POSTPONED
    if not t.owner_name and actor:
        person = get_or_create_person(db, actor)
        _apply_owner(t, person, person.name)
    t.version += 1
    db.add(TaskEvent(task_id=t.id, event="postponed", actor=actor or t.owner_name,
                     detail=f"延期至 {t.due_date or '待定'}，原因：{reason}",
                     shift_code=services.current_shift()["code"]))
    db.commit()
    db.refresh(t)
    return t


def complete(
    db: Session, task_id: int, result_note: str, actor: Optional[str],
    version: Optional[int] = None,
    register_action: Optional[dict] = None,
) -> ReminderTask:
    """
    办结待办。
    - medication_dose / preg_check：必须带 register_action 完成真实业务登记，
      否则拒绝（完成待办不能代替用药/孕检登记）。
    - withdrawal：禁止办结（休药警告有效期内不可隐藏）。
    """
    t = get_task(db, task_id)
    _check_version(t, version)
    if t.type in READONLY_TYPES:
        raise ValueError("休药警告不能手动办结：有效期内必须持续展示，到期后系统自动解除")
    if t.status == models.TASK_STATUS_DONE:
        raise ValueError("该待办已完成")
    if t.status == models.TASK_STATUS_INVALID:
        raise ValueError("该待办已失效，无需完成")

    note = (result_note or "").strip()
    shift = services.current_shift()

    # 红线：需要真实登记的事项
    keep_open = False
    if t.type in REGISTER_GUARD:
        if not register_action:
            raise ValueError(REGISTER_GUARD[t.type])
        if t.type == "medication_dose":
            _register_medication(db, t, register_action, actor, shift)
        elif t.type == "preg_check":
            keep_open = _register_preg_check(db, t, register_action, actor, shift)
        t.registered = True
        if keep_open:
            # 孕检结果为未孕/未确认：真实登记已完成并留痕，但繁殖事项需继续跟进，
            # 待办保持打开（后续引擎会按长期空怀重新对账为同指纹任务）
            if not t.owner_name and actor:
                person = get_or_create_person(db, actor)
                _apply_owner(t, person, person.name)
            t.result_note = (note or "") + "（孕检未孕/未确认，转继续跟进）"
            t.status = models.TASK_STATUS_CLAIMED if t.owner_name else models.TASK_STATUS_OPEN
            t.version += 1
            db.commit()
            db.refresh(t)
            return t
        if not note:
            note = "已完成真实业务登记"

    if not t.owner_name and actor:
        person = get_or_create_person(db, actor)
        _apply_owner(t, person, person.name)
    t.result_note = note or "已处理"
    t.status = models.TASK_STATUS_DONE
    t.done_at = datetime.utcnow()
    t.closed_at = datetime.utcnow()
    t.version += 1
    done_detail = f"处理结果：{t.result_note}" + ("（已完成真实业务登记）" if t.registered else "")
    db.add(TaskEvent(task_id=t.id, event="done", actor=actor or t.owner_name,
                     detail=done_detail, shift_code=shift["code"]))
    db.commit()
    db.refresh(t)
    return t


def _register_medication(db: Session, t: ReminderTask, action: dict,
                         actor: Optional[str], shift: dict) -> None:
    """真实用药登记：把对应用药记录标记为已执行（与用药列表“已执行”同一字段）"""
    m = db.get(models.Medication, t.ref_id) if t.ref_type == "medication" else None
    if not m:
        raise ValueError("对应的用药记录不存在，请到用药管理核实后再处理")
    if m.treated:
        return  # 已登记过，幂等
    m.treated = True
    if action.get("note"):
        m.note = (m.note or "") + f"｜{action['note']}"
    if actor and not m.operator:
        m.operator = actor
    db.add(TaskEvent(task_id=t.id, event="registered", actor=actor,
                     detail=f"用药执行登记：{m.drug_name}（{m.next_dose_date or m.date}）",
                     shift_code=shift["code"]))


def _register_preg_check(db: Session, t: ReminderTask, action: dict,
                         actor: Optional[str], shift: dict) -> bool:
    """真实孕检登记：回填发情/配种记录的孕检结果。返回是否需要继续跟进（未孕）。"""
    e = db.get(models.EstrusRecord, t.ref_id) if t.ref_type == "estrus" else None
    if not e:
        raise ValueError("对应的配种记录不存在，请到发情与配种核实后再处理")
    result = action.get("result")
    if result not in ("pregnant", "negative", "unknown"):
        raise ValueError("请选择孕检结果：已孕 / 未孕 / 未确认")
    e.result = result
    result_date = _parse_date(action.get("result_date")) or date.today()
    e.result_date = result_date
    if action.get("note"):
        e.note = (e.note or "") + f"｜{action['note']}"
    label = {"pregnant": "已孕", "negative": "未孕", "unknown": "未确认"}[result]
    db.add(TaskEvent(task_id=t.id, event="registered", actor=actor,
                     detail=f"孕检结果登记：{label}（{result_date}）",
                     shift_code=shift["code"]))
    return result != "pregnant"


def acknowledge_update(db: Session, task_id: int, actor: Optional[str],
                       version: Optional[int] = None) -> ReminderTask:
    """确认“源记录已更新”提示：source_state 回到 active（恢复其原处理状态）"""
    t = get_task(db, task_id)
    _check_version(t, version)
    if t.source_state != models.SOURCE_STATE_UPDATED:
        raise ValueError("该待办当前没有待确认的更新")
    t.source_state = models.SOURCE_STATE_ACTIVE
    if t.status == models.TASK_STATUS_CHANGED:
        t.status = (models.TASK_STATUS_CLAIMED if t.owner_name
                    else models.TASK_STATUS_OPEN)
    t.version += 1
    db.add(TaskEvent(task_id=t.id, event="acknowledged", actor=actor,
                     detail="已确认源记录变更，按最新内容继续处理",
                     shift_code=services.current_shift()["code"]))
    db.commit()
    db.refresh(t)
    return t


def acknowledge_invalid(db: Session, task_id: int, actor: Optional[str]) -> ReminderTask:
    """确认已知悉失效（仅记录流水，状态保持失效留痕）"""
    t = get_task(db, task_id)
    if t.status != models.TASK_STATUS_INVALID:
        raise ValueError("该待办未失效")
    db.add(TaskEvent(task_id=t.id, event="acknowledged", actor=actor,
                     detail="已知悉该待办失效",
                     shift_code=services.current_shift()["code"]))
    db.commit()
    db.refresh(t)
    return t


def reopen(db: Session, task_id: int, actor: Optional[str]) -> ReminderTask:
    t = get_task(db, task_id)
    if t.status not in (models.TASK_STATUS_DONE, models.TASK_STATUS_INVALID):
        raise ValueError("只能重新打开已完成或已失效的待办")
    if t.type in READONLY_TYPES:
        raise ValueError("休药警告由系统按休药期自动管理")
    t.status = models.TASK_STATUS_OPEN
    t.source_state = models.SOURCE_STATE_ACTIVE
    t.result_note = None
    t.closed_at = t.done_at = None
    t.version += 1
    db.add(TaskEvent(task_id=t.id, event="reopened", actor=actor,
                     detail="人工重新打开待办",
                     shift_code=services.current_shift()["code"]))
    db.commit()
    db.refresh(t)
    return t


# ---------------------------------------------------------------- 交班
def build_handover(db: Session, from_person: Optional[str], to_person: str,
                   note: Optional[str]) -> Handover:
    """
    生成交班单：快照当前所有未完成事项带到下一班。
    未完成事项本就持久化且按指纹续传，交班单固化“交给谁、交了什么”。
    """
    to_person = (to_person or "").strip()
    if not to_person:
        raise ValueError("请填写接班人姓名")
    shift = services.current_shift()
    open_tasks = [t for t in list_tasks(db, "active")]
    snapshot = [
        {
            "id": t.id, "title": t.title, "type": t.type,
            "type_label": TYPE_LABEL.get(t.type, t.type),
            "owner_name": t.owner_name, "status": t.status,
            "due_date": str(t.due_date) if t.due_date else None,
            "carry_count": t.carry_count,
            "postpone_reason": t.postpone_reason,
            "level": t.level,
        } for t in open_tasks
    ]
    h = Handover(
        shift_code=shift["code"], shift_label=shift["label"],
        from_person=from_person, to_person=to_person, note=note,
        open_count=len(snapshot), items_json=json.dumps(snapshot, ensure_ascii=False),
    )
    db.add(h)
    for t in open_tasks:
        db.add(TaskEvent(task_id=t.id, event="assigned", actor=from_person,
                         detail=f"交班移交给 {to_person}", shift_code=shift["code"]))
    db.commit()
    db.refresh(h)
    return h


def handover_to_dict(h: Handover) -> dict:
    return {
        "id": h.id, "shift_code": h.shift_code, "shift_label": h.shift_label,
        "from_person": h.from_person, "to_person": h.to_person, "note": h.note,
        "open_count": h.open_count,
        "items": json.loads(h.items_json) if h.items_json else [],
        "created_at": h.created_at.strftime("%Y-%m-%d %H:%M") if h.created_at else None,
    }
