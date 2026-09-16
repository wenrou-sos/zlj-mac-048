"""
牛舍与批量转群业务规则。

设计要点：
- stays 是“居住日历”：半开区间 [start_date, end_date)，end_date 为空表示至今/至未来；
  已确认的未来转群、隔离也立即写入 stays，因此任何时点的占栏量都可以直接由 stays 算出。
- 转群计划(transfer_plans)是草稿/安排，确认时原子化地裁剪 stays、更新 cows.group；
  两个安排争用最后一个栏位时，先确认者成功，后确认者在容量重算 + SQLite 触发器两层拦截下失败。
- 同一头牛的居住区间不允许重叠：服务层先校验给出可读错误，数据库触发器兜底。
"""
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import or_, update as sa_update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from . import models

FAR_FUTURE = date(9999, 12, 31)
PURPOSE_LABEL = {
    "lactating": "泌乳牛舍",
    "dry": "干奶牛舍",
    "maternity": "产房/待产",
    "isolation": "隔离舍",
    "other": "其他",
}
PURPOSE_BY_GROUP_KEYWORD = {
    "隔离": "isolation",
    "干奶": "dry",
    "待产": "maternity",
    "产后": "maternity",
    "产房": "maternity",
}


class HousingError(Exception):
    """业务规则错误，消息可直接展示给用户"""


# ---------------- 基础查询 ----------------
def _end(stay: models.Stay) -> date:
    return stay.end_date or FAR_FUTURE


def stays_overlap(s1: models.Stay, start: date, end: Optional[date],
                  exclude_id: Optional[int] = None) -> bool:
    """半开区间重叠判断：[start, end)"""
    if s1.id == exclude_id:
        return False
    return s1.start_date < (end or FAR_FUTURE) and _end(s1) > start


def find_overlap(db: Session, cow_id: int, start: date, end: Optional[date],
                 exclude_id: Optional[int] = None) -> Optional[models.Stay]:
    for s in db.query(models.Stay).filter_by(cow_id=cow_id).all():
        if stays_overlap(s, start, end, exclude_id):
            return s
    return None


def pen_at(db: Session, cow_id: int, on_date: date) -> Optional[models.Pen]:
    """某头牛在指定日期所在栏位（含已确认的未来安排）"""
    stay = (
        db.query(models.Stay)
        .filter(
            models.Stay.cow_id == cow_id,
            models.Stay.start_date <= on_date,
            or_(models.Stay.end_date.is_(None), models.Stay.end_date > on_date),
        )
        .order_by(models.Stay.start_date.desc())
        .first()
    )
    return stay.pen if stay else None


def open_stay(db: Session, cow_id: int) -> Optional[models.Stay]:
    """该牛当前开放（end_date 为空）的居住记录"""
    return (
        db.query(models.Stay)
        .filter(models.Stay.cow_id == cow_id, models.Stay.end_date.is_(None))
        .order_by(models.Stay.start_date.desc())
        .first()
    )


def occupancy_on(db: Session, on_date: date,
                 extra_stays: Optional[List[dict]] = None,
                 exclude_item_ids: Optional[set] = None) -> Dict[int, dict]:
    """
    计算各栏位在 on_date 当天的占栏量。
    extra_stays：尚未落库的“假设居住区间”（如其他待确认草稿），
    形如 {pen_id, cow_id, start_date, end_date}，用于提前查看冲突。
    exclude_item_ids：模拟延期时，先把该计划已写入的旧区间排除。
    返回 pen_id -> {occupied, cows:set, extra:set}
    """
    result: Dict[int, dict] = {}

    def slot(pen_id: int) -> dict:
        return result.setdefault(pen_id, {"occupied": 0, "cows": set(), "extra": set()})

    for s in db.query(models.Stay).all():
        if exclude_item_ids and s.transfer_item_id in exclude_item_ids:
            continue
        if s.start_date <= on_date < _end(s):
            cur = slot(s.pen_id)
            cur["occupied"] += 1
            cur["cows"].add(s.cow_id)

    for ex in extra_stays or []:
        if ex["start_date"] <= on_date < (ex.get("end_date") or FAR_FUTURE):
            cur = slot(ex["pen_id"])
            if ex["cow_id"] not in cur["cows"]:  # 同一头牛不在同栏重复计数
                cur["occupied"] += 1
                cur["extra"].add(ex["cow_id"])
    return result


def pen_dict(pen: models.Pen, db: Session, on_date: Optional[date] = None) -> dict:
    on_date = on_date or date.today()
    occ = occupancy_on(db, on_date).get(pen.id)
    occupied = occ["occupied"] if occ else 0
    return {
        "id": pen.id,
        "name": pen.name,
        "purpose": pen.purpose,
        "purpose_label": PURPOSE_LABEL.get(pen.purpose, pen.purpose),
        "capacity": pen.capacity,
        "active": pen.active,
        "note": pen.note,
        "occupied": occupied,
        "free": max(0, pen.capacity - occupied) if pen.active else 0,
        "over": max(0, occupied - pen.capacity),
    }


# ---------------- 转群预检（模拟，不落库） ----------------
def _planned_intervals(db: Session, plan: Optional[models.TransferPlan],
                       effective: date, kind: str,
                       items: List[dict]) -> List[dict]:
    """一批明细若确认产生的假设居住区间（隔离含返回段），用于容量模拟"""
    out = []
    for it in items:
        if kind == "isolation":
            out.append({
                "pen_id": it["to_pen_id"], "cow_id": it["cow_id"],
                "start_date": effective,
                "end_date": it.get("return_date"),
            })
            if it.get("return_date"):
                back = it.get("return_pen_id") or it.get("from_pen_id")
                if back:
                    out.append({
                        "pen_id": back, "cow_id": it["cow_id"],
                        "start_date": it["return_date"], "end_date": None,
                    })
        else:
            out.append({
                "pen_id": it["to_pen_id"], "cow_id": it["cow_id"],
                "start_date": effective, "end_date": None,
            })
    return out


def other_pending_plans(db: Session, exclude_id: Optional[int]) -> List[models.TransferPlan]:
    """其他待确认（draft）安排：预检时把它们也视为未来占用，提前暴露两个安排争位"""
    q = db.query(models.TransferPlan).filter(models.TransferPlan.status == "draft")
    if exclude_id:
        q = q.filter(models.TransferPlan.id != exclude_id)
    return q.all()


def analyze_moves(db: Session, *, effective: date, kind: str, items: List[dict],
                  plan_id: Optional[int] = None) -> dict:
    """
    逐项校验并模拟容量。返回：
    {
      ok: bool（无 error 级冲突）,
      item_reports: [{cow_id, cow_tag, from_pen_id, from_pen_name, to_pen_id,...,
                      errors:[], warnings:[]}],
      pen_conflicts: [{pen_id, name, capacity, baseline, incoming, outgoing, projected, free}],
      errors/warnings: 全局消息
    }
    """
    today = date.today()
    errors: List[str] = []
    warnings: List[str] = []

    if not items:
        errors.append("至少添加一头牛")

    pens = {p.id: p for p in db.query(models.Pen).all()}
    cows = {c.id: c for c in db.query(models.Cow).all()}

    # 同一批次内牛重复
    seen: set = set()
    for it in items:
        if it["cow_id"] in seen:
            errors.append("同一批次内存在重复牛只")
        seen.add(it["cow_id"])

    # 其他待确认草稿在各自生效日的假设区间（提前查看冲突）
    pending_extra: List[dict] = []
    for p in other_pending_plans(db, plan_id):
        for it in p.items:
            if p.kind == "isolation":
                end = it.return_date
            else:
                end = None
            pending_extra.append({
                "pen_id": it.to_pen_id, "cow_id": it.cow_id,
                "start_date": p.effective_date, "end_date": end,
            })
            # 隔离的返回段也要占位
            if p.kind == "isolation":
                ret_pen = it.return_pen_id or it.from_pen_id
                if it.return_date and ret_pen:
                    pending_extra.append({
                        "pen_id": ret_pen, "cow_id": it.cow_id,
                        "start_date": it.return_date, "end_date": None,
                    })

    # 牛级校验
    item_reports = []
    # 每头牛当前/生效日所在栏：以 stays 为准，stays 缺失时回退 group 文本归并
    def current_pen_for(cow: models.Cow) -> Optional[models.Pen]:
        p = pen_at(db, cow.id, today)
        if p:
            return p
        if cow.group:
            return next((x for x in pens.values() if x.name == cow.group), None)
        return None

    for it in items:
        cow = cows.get(it["cow_id"])
        to_pen = pens.get(it["to_pen_id"])
        cur = current_pen_for(cow) if cow else None
        from_pen_id = it.get("from_pen_id") or (cur.id if cur else None)
        ierr: List[str] = []
        iwarn: List[str] = []

        if not cow:
            ierr.append("牛只不存在")
        elif cow.status == "sold":
            ierr.append("该牛已离场，不能安排转群")
        if not to_pen:
            ierr.append("目标栏位不存在")
        elif not to_pen.active or to_pen.capacity <= 0:
            ierr.append(f"目标栏位「{to_pen.name}」已停用")

        # 该牛是否已有尚未确认、生效日相同/更早且会在 effective 当天占住它的安排
        if cow:
            for p in other_pending_plans(db, plan_id):
                other_it = next((x for x in p.items if x.cow_id == cow.id), None)
                if other_it and p.effective_date <= effective and \
                        (p.kind != "isolation" or
                         not other_it.return_date or other_it.return_date > effective):
                    ierr.append(
                        f"已有待确认安排「{p.title}」（{p.effective_date} 生效）涉及该牛，"
                        f"请先确认或取消该安排")
                    break

        if cow and to_pen and cur and cur.id == to_pen.id and kind != "isolation":
            iwarn.append("该牛已在目标栏位，无需转群")

        if cow and to_pen and to_pen.purpose == "isolation" and kind != "isolation":
            iwarn.append("目标栏位是隔离舍，建议使用“临时隔离”安排")
        if cow and to_pen and kind == "group" and to_pen.purpose in ("dry", "maternity") \
                and cow.status not in ("dry", "pregnant"):
            iwarn.append(f"牛只状态与栏位用途「{PURPOSE_LABEL[to_pen.purpose]}」不符")
        if cow and to_pen and kind == "isolation" and to_pen.purpose != "isolation":
            iwarn.append("临时隔离建议安排到用途为“隔离舍”的栏位")

        if kind == "isolation" and it.get("return_date") and it["return_date"] <= effective:
            ierr.append("隔离返回日期必须晚于隔离生效日期")
        if kind == "group" and it.get("return_date"):
            iwarn.append("普通转群不使用返回日期")

        item_reports.append({
            "cow_id": it["cow_id"],
            "cow_tag": cow.ear_tag if cow else None,
            "cow_name": cow.name if cow else None,
            "from_pen_id": from_pen_id,
            "from_pen_name": next((p.name for p in pens.values() if p.id == from_pen_id), None),
            "to_pen_id": it["to_pen_id"],
            "to_pen_name": to_pen.name if to_pen else None,
            "return_pen_id": it.get("return_pen_id"),
            "return_date": str(it["return_date"]) if it.get("return_date") else None,
            "errors": ierr,
            "warnings": iwarn,
        })
        errors.extend(ierr)
        warnings.extend(iwarn)

    # 容量模拟：stays（含已确认安排）+ 其他草稿 + 本批
    projected = occupancy_on(db, effective, extra_stays=pending_extra + _planned_intervals(
        db, None, effective, kind, items))
    baseline = occupancy_on(db, effective)

    pen_conflicts = []
    incoming: Dict[int, set] = {}
    outgoing: Dict[int, set] = {}
    for rep, it in zip(item_reports, items):
        incoming.setdefault(it["to_pen_id"], set()).add(it["cow_id"])
        if rep["from_pen_id"]:
            outgoing.setdefault(rep["from_pen_id"], set()).add(it["cow_id"])

    touched = set(incoming) | set(outgoing)
    for pen_id in touched:
        pen = pens.get(pen_id)
        if not pen:
            continue
        proj = projected.get(pen_id, {"occupied": 0})["occupied"]
        base = baseline.get(pen_id, {"occupied": 0})["occupied"]
        ins = len(incoming.get(pen_id, set()))
        outs = len(outgoing.get(pen_id, set()))
        conflict = pen.active and pen.capacity > 0 and proj > pen.capacity
        over_now = base > pen.capacity
        if conflict or over_now:
            pen_conflicts.append({
                "pen_id": pen_id,
                "name": pen.name,
                "capacity": pen.capacity,
                "baseline": base,
                "incoming": ins,
                "outgoing": outs,
                "projected": proj,
                "free_after": pen.capacity - proj,
            })
        if conflict:
            errors.append(
                f"栏位「{pen.name}」容量 {pen.capacity}，生效日预计在栏 {proj} 头"
                f"（现有 {base} + 迁入 {ins} - 迁出 {outs}），超出 {-pen.capacity + proj} 头")

    return {
        "ok": not errors,
        "effective_date": str(effective),
        "kind": kind,
        "item_reports": item_reports,
        "pen_conflicts": pen_conflicts,
        "errors": errors,
        "warnings": sorted(set(warnings)),
    }


# ---------------- 计划落库 / 确认 ----------------
def _get_plan(db: Session, plan_id: int) -> models.TransferPlan:
    plan = db.get(models.TransferPlan, plan_id)
    if not plan:
        raise HousingError("未找到该转群安排")
    return plan


def _add_event(db: Session, plan: models.TransferPlan, action: str,
               detail: str = "", cow_id: Optional[int] = None) -> None:
    db.add(models.TransferEvent(
        plan_id=plan.id, cow_id=cow_id, action=action, detail=detail, at=datetime.utcnow()))


def create_plan(db: Session, *, title: str, effective_date: date, kind: str,
                items: List[dict], operator: Optional[str] = None,
                note: Optional[str] = None, auto_confirm: bool = False) -> Tuple[models.TransferPlan, dict]:
    analysis = analyze_moves(db, effective=effective_date, kind=kind, items=items)
    if not analysis["ok"]:
        raise HousingError("；".join(analysis["errors"]))

    plan = models.TransferPlan(
        title=title, effective_date=effective_date, kind=kind,
        status="draft", operator=operator, note=note)
    db.add(plan)
    db.flush()

    cows = {c.id: c for c in db.query(models.Cow).all()}
    for rep, it in zip(analysis["item_reports"], items):
        cow = cows[it["cow_id"]]
        db.add(models.TransferItem(
            plan_id=plan.id, cow_id=it["cow_id"],
            from_pen_id=rep["from_pen_id"],
            to_pen_id=it["to_pen_id"],
            return_pen_id=it.get("return_pen_id"),
            return_date=it.get("return_date"),
        ))
    _add_event(db, plan, "created", f"创建安排，{len(items)} 头牛，{effective_date} 生效")
    db.flush()

    if auto_confirm:
        if effective_date < date.today():
            raise HousingError("生效日早于今天的安排不能直接确认；请使用“补录历史转群”")
        _confirm_locked(db, plan)
    return plan, analysis


def _pending_cow_conflicts(db: Session, plan: models.TransferPlan) -> List[str]:
    """该计划涉及的牛是否同时出现在其他待确认安排中（同一头牛不能被两个安排占用）"""
    msgs = []
    cow_ids = {it.cow_id for it in plan.items}
    for other in other_pending_plans(db, plan.id):
        for it in other.items:
            if it.cow_id in cow_ids:
                cow = db.get(models.Cow, it.cow_id)
                msgs.append(
                    f"{cow.ear_tag} 已在待确认安排「{other.title}」"
                    f"（{other.effective_date} 生效）中，请先处理该安排")
    return msgs


def _hard_capacity_errors(db: Session, effective: date, kind: str, items: List[dict],
                          exclude_item_ids: Optional[set] = None) -> List[str]:
    """按已落库 stays + 假设区间，在各关键日期逐栏检查容量，返回硬冲突消息。"""
    extra = _planned_intervals(db, None, effective, kind, items)
    check_dates = {effective}
    for x in extra:
        if x["end_date"]:
            check_dates.add(x["end_date"])
    errs: List[str] = []
    for d in check_dates:
        occ = occupancy_on(db, d, extra_stays=extra, exclude_item_ids=exclude_item_ids)
        for x in extra:
            if x["start_date"] > d:
                continue  # 该区间在这个检查日尚未开始
            pen = db.get(models.Pen, x["pen_id"])
            n = occ.get(x["pen_id"], {"occupied": 0})["occupied"]
            if pen and pen.active and pen.capacity > 0 and n > pen.capacity:
                msg = f"栏位「{pen.name}」容量 {pen.capacity}，{d} 将在栏 {n} 头，容量不足"
                if msg not in errs:
                    errs.append(msg)
    return errs


def confirm_plan(db: Session, plan_id: int) -> models.TransferPlan:
    plan = _get_plan(db, plan_id)
    if plan.status == "cancelled":
        raise HousingError("该安排已取消，不能确认")
    if plan.status == "confirmed":
        raise HousingError("该安排已确认")
    if plan.effective_date < date.today():
        raise HousingError(
            "生效日已过，不能再确认未来安排；如需补录请使用“补录历史转群”")
    _confirm_locked(db, plan)
    return plan


def _confirm_locked(db: Session, plan: models.TransferPlan) -> None:
    """确认：重新做容量校验（防止创建后被别的安排抢先），再原子改写居住历史。"""
    today = date.today()
    eff = plan.effective_date

    # 重新模拟（此时 stays 可能已被其他已确认计划改变；其他草稿只用于逐牛冲突提示，
    # 容量硬性拦截一律走下面只看“已落库 stays + 本批”的 _hard_capacity_errors）
    items = [{
        "cow_id": it.cow_id, "to_pen_id": it.to_pen_id,
        "return_pen_id": it.return_pen_id, "from_pen_id": it.from_pen_id,
        "return_date": it.return_date,
    } for it in plan.items]
    analyze_moves(db, effective=eff, kind=plan.kind,
                  items=items, plan_id=plan.id)
    hard_errors = _pending_cow_conflicts(db, plan)
    hard_errors.extend(_hard_capacity_errors(
        db, eff, plan.kind, items,
        exclude_item_ids={it.id for it in plan.items}))

    if hard_errors:
        raise HousingError("；".join(dict.fromkeys(hard_errors)))

    try:
        if plan.kind == "isolation":
            _confirm_isolation(db, plan, eff)
        else:
            _confirm_group_move(db, plan, eff)
        plan.status = "confirmed"
        plan.confirmed_at = datetime.utcnow()
        _add_event(db, plan, "confirmed",
                   f"已确认，{eff} 生效" + ("（已即时占栏）" if eff == today else
                                          f"（{eff} 起占栏）"))
        # 生效日已到：同步 cows.group 文本快照（隔离可能已跨过返回日，取当天实际所在栏）
        if eff <= today:
            for item in plan.items:
                cow = db.get(models.Cow, item.cow_id)
                pen = pen_at(db, cow.id, today)
                if pen:
                    cow.group = pen.name
                else:
                    cow.group = item.to_pen.name
        db.flush()
        # 触发器是最后的防线：任何漏判的超栏/重叠在此回滚整个事务
        db.commit()
    except (IntegrityError, OperationalError) as exc:
        db.rollback()
        raise HousingError(f"确认被数据库完整性规则拒绝：{exc.orig}")


def _confirm_group_move(db: Session, plan: models.TransferPlan, eff: date) -> None:
    for item in plan.items:
        cur = open_stay(db, item.cow_id)
        if cur and cur.pen_id == item.to_pen_id and cur.start_date <= eff:
            continue  # 已在目标栏
        if cur:
            # 正常顺序：先关旧区间（仅收缩，触发器不会报重叠/超栏）
            if cur.start_date > eff:
                # 旧区间本身在未来才开始：直接丢弃（此前的未来安排被本计划取代）
                db.delete(cur)
            else:
                cur.end_date = eff
        new = models.Stay(
            cow_id=item.cow_id, pen_id=item.to_pen_id, start_date=eff,
            end_date=None, source="transfer", transfer_item_id=item.id,
            note=plan.title)
        db.add(new)
        db.flush()
        _add_event(db, plan, "confirmed",
                   f"{eff} 迁入「{item.to_pen.name}」", cow_id=item.cow_id)


def _confirm_isolation(db: Session, plan: models.TransferPlan, eff: date) -> None:
    for item in plan.items:
        cur = open_stay(db, item.cow_id)
        if cur:
            if cur.start_date > eff:
                db.delete(cur)
            else:
                cur.end_date = eff
        # 隔离段
        iso = models.Stay(
            cow_id=item.cow_id, pen_id=item.to_pen_id, start_date=eff,
            end_date=item.return_date,  # 有返回日：半开到返回日；无：开放
            source="transfer", transfer_item_id=item.id, note=f"隔离：{plan.title}")
        db.add(iso)
        db.flush()
        _add_event(db, plan, "confirmed",
                   f"{eff} 临时隔离至「{item.to_pen.name}」"
                   + (f"，计划 {item.return_date} 返回" if item.return_date else ""),
                   cow_id=item.cow_id)
        # 返回段
        if item.return_date:
            back_pen_id = item.return_pen_id or item.from_pen_id
            if back_pen_id:
                back_pen = db.get(models.Pen, back_pen_id)
                ret = models.Stay(
                    cow_id=item.cow_id, pen_id=back_pen_id,
                    start_date=item.return_date, end_date=None,
                    source="transfer", transfer_item_id=item.id,
                    note=f"隔离返回：{plan.title}")
                db.add(ret)
                db.flush()
                _add_event(db, plan, "confirmed",
                           f"{item.return_date} 隔离期满返回「{back_pen.name}」",
                           cow_id=item.cow_id)


# ---------------- 延期 ----------------
def postpone_plan(db: Session, plan_id: int, new_date: date,
                  new_return_date: Optional[date] = None) -> models.TransferPlan:
    plan = _get_plan(db, plan_id)
    today = date.today()
    if new_date < today:
        raise HousingError("新的生效日期不能早于今天")
    if plan.status == "cancelled":
        raise HousingError("已取消的安排不能延期")

    if plan.status == "draft":
        old = plan.effective_date
        plan.effective_date = new_date
        if new_return_date is not None:
            for it in plan.items:
                it.return_date = new_return_date or None
        _add_event(db, plan, "postponed", f"待确认安排生效日 {old} → {new_date}")
        db.commit()
        return plan

    # 已确认：移动 stays，并重新做容量校验
    if plan.effective_date <= today:
        raise HousingError("已生效的安排不能延期；如需调整请直接办理新的转群")
    old_eff = plan.effective_date
    items_payload = [{
        "cow_id": it.cow_id, "to_pen_id": it.to_pen_id,
        "return_pen_id": it.return_pen_id, "from_pen_id": it.from_pen_id,
        "return_date": new_return_date if new_return_date is not None else it.return_date,
    } for it in plan.items]
    # 软预检（含其他草稿的预警信息）
    analysis = analyze_moves(db, effective=new_date, kind=plan.kind,
                             items=items_payload, plan_id=plan.id)
    # 硬校验：排除本计划已写入的旧区间，用新区间在新日期上模拟
    blocking = _hard_capacity_errors(
        db, new_date, plan.kind, items_payload,
        exclude_item_ids={it.id for it in plan.items})
    if blocking:
        raise HousingError("；".join(blocking))

    try:
        _shift_confirmed_stays(db, plan, old_eff, new_date, new_return_date)
        plan.effective_date = new_date
        _add_event(db, plan, "postponed",
                   f"已确认安排延期：生效日 {old_eff} → {new_date}"
                   + (f"，返回日调整为 {new_return_date}" if new_return_date else ""))
        db.commit()
    except (IntegrityError, OperationalError) as exc:
        db.rollback()
        raise HousingError(f"延期被完整性规则拒绝：{exc.orig}")
    return plan


def _shift_confirmed_stays(db: Session, plan: models.TransferPlan, old_eff: date,
                           new_eff: date, new_return_date: Optional[date]) -> None:
    """移动已确认计划产生的居住区间。

    居住区间之间首尾相接，任何中间态重叠都会被 SQLite 触发器拒绝，而 ORM 工作单元
    会把 UPDATE 与 INSERT 合并到同一 flush 批处理、打乱语句顺序。因此这里全部通过
    Core 连接按严格顺序执行：删旧 -> 截旧栏 -> 插隔离段 -> 延旧栏 -> 插返回段。
    """
    conn = db.connection()
    item_ids = [it.id for it in plan.items]
    for item in plan.items:
        linked = (
            db.query(models.Stay)
            .filter(models.Stay.transfer_item_id == item.id)
            .order_by(models.Stay.start_date.asc())
            .all()
        )
        if not linked:
            continue
        first = linked[0]
        prev = (
            db.query(models.Stay)
            .filter(models.Stay.cow_id == item.cow_id,
                    models.Stay.end_date == old_eff,
                    models.Stay.transfer_item_id.is_(None))
            .first()
        )
        old_return = linked[1].start_date if len(linked) > 1 else None
        linked_ids = [s.id for s in linked]
        ret_date = new_return_date if new_return_date is not None else old_return

        # 1) 删除本计划写入的旧区间
        conn.execute(
            models.Stay.__table__.delete().where(models.Stay.id.in_(linked_ids)))

        if plan.kind == "isolation":
            # 最终链条：原栏[..new_eff) → 隔离段[new_eff,ret) → 返回段[ret,∞)
            # 2) 原栏截到新隔离生效日
            if prev:
                conn.execute(
                    models.Stay.__table__.update()
                    .where(models.Stay.id == prev.id)
                    .values(end_date=new_eff))
            # 3) 插隔离段（中间无空洞，故原栏不能延伸到返回日）
            conn.execute(models.Stay.__table__.insert().values(
                cow_id=item.cow_id, pen_id=item.to_pen_id, start_date=new_eff,
                end_date=ret_date, source="transfer", transfer_item_id=item.id,
                note=f"隔离（已延期）：{plan.title}"))
            back_pen_id = item.return_pen_id or item.from_pen_id
            # 4) 插返回段
            if back_pen_id:
                conn.execute(models.Stay.__table__.insert().values(
                    cow_id=item.cow_id, pen_id=back_pen_id, start_date=ret_date,
                    end_date=None, source="transfer", transfer_item_id=item.id,
                    note=f"隔离返回（已延期）：{plan.title}"))
        else:
            if prev:
                conn.execute(
                    models.Stay.__table__.update()
                    .where(models.Stay.id == prev.id)
                    .values(end_date=new_eff))
            conn.execute(models.Stay.__table__.insert().values(
                cow_id=item.cow_id, pen_id=item.to_pen_id, start_date=new_eff,
                end_date=None, source="transfer", transfer_item_id=item.id,
                note=f"转群（已延期）：{plan.title}"))
    db.expire_all()


# ---------------- 取消 ----------------
def cancel_plan(db: Session, plan_id: int, reason: Optional[str] = None) -> models.TransferPlan:
    plan = _get_plan(db, plan_id)
    today = date.today()
    if plan.status == "cancelled":
        raise HousingError("该安排已经是取消状态")
    if plan.status == "confirmed" and plan.effective_date <= today:
        raise HousingError(
            "安排已经生效，不能直接取消；如牛只未实际转群，请办理新的转群/回迁安排")

    if plan.status == "confirmed":
        # 回收该计划写入的未来居住区间，并恢复原栏开放区间
        item_ids = {it.id for it in plan.items}
        try:
            for item in plan.items:
                linked = (
                    db.query(models.Stay)
                    .filter(models.Stay.transfer_item_id == item.id)
                    .order_by(models.Stay.start_date.asc())
                    .all()
                )
                if not linked:
                    continue
                first = linked[0]
                prev = (
                    db.query(models.Stay)
                    .filter(models.Stay.cow_id == item.cow_id,
                            models.Stay.end_date == first.start_date,
                            models.Stay.id.notin_(
                                db.query(models.Stay.id).filter(
                                    models.Stay.transfer_item_id.in_(item_ids))))
                    .first()
                )
                linked_ids = [s.id for s in linked]
                db.query(models.Stay).filter(models.Stay.id.in_(linked_ids)).delete(
                    synchronize_session=False)
                db.flush()
                if prev:
                    prev.end_date = None
                    db.flush()
        except (IntegrityError, OperationalError) as exc:
            db.rollback()
            raise HousingError(f"取消失败：{exc.orig}")

    plan.status = "cancelled"
    _add_event(db, plan, "cancelled",
               f"安排已取消" + (f"：{reason}" if reason else ""))
    db.commit()
    return plan


# ---------------- 隔离提前回迁 ----------------
def release_isolation(db: Session, plan_id: int, return_date: date,
                      return_pen_id: Optional[int] = None) -> models.TransferPlan:
    """隔离中的牛提前/按期回迁：截断隔离段，自 return_date 起住回原栏或指定栏。"""
    plan = _get_plan(db, plan_id)
    if plan.kind != "isolation":
        raise HousingError("该安排不是隔离安排")
    if plan.status != "confirmed":
        raise HousingError("只有已确认的隔离安排可以办理回迁")
    today = date.today()
    if return_date < today:
        raise HousingError("回迁日期不能早于今天")

    try:
        for item in plan.items:
            linked = (
                db.query(models.Stay)
                .filter_by(transfer_item_id=item.id)
                .order_by(models.Stay.start_date.asc())
                .all()
            )
            iso_stay = next((s for s in linked if s.pen_id == item.to_pen_id), None)
            if not iso_stay:
                continue
            future_return = next(
                (s for s in linked if s.id != iso_stay.id and s.start_date >= today), None)
            back_pen_id = return_pen_id or item.return_pen_id or item.from_pen_id
            if not back_pen_id:
                raise HousingError("缺少回迁栏位，请指定返回栏")
            back_pen = db.get(models.Pen, back_pen_id)

            # 容量预检：return_date 当天返回栏是否有位
            occ = occupancy_on(db, return_date, extra_stays=[{
                "pen_id": back_pen_id, "cow_id": item.cow_id,
                "start_date": return_date, "end_date": None}])
            n = occ.get(back_pen_id, {"occupied": 0})["occupied"]
            if back_pen.active and back_pen.capacity > 0 and n > back_pen.capacity:
                raise HousingError(
                    f"回迁栏「{back_pen.name}」{return_date} 容量不足（在栏 {n}/{back_pen.capacity}）")

            if future_return:
                db.query(models.Stay).filter_by(id=future_return.id).delete(
                    synchronize_session=False)
            # 先把隔离段截到回迁日（Core UPDATE 立即执行，避免与新 INSERT 同批 flush）
            db.execute(sa_update(models.Stay).where(models.Stay.id == iso_stay.id)
                       .values(end_date=return_date))
            db.expire_all()
            item.return_date = return_date
            item.return_pen_id = back_pen_id
            db.flush()
            db.add(models.Stay(
                cow_id=item.cow_id, pen_id=back_pen_id, start_date=return_date,
                end_date=None, source="transfer", transfer_item_id=item.id,
                note=f"提前回迁：{plan.title}"))
            db.flush()
            _add_event(db, plan, "released",
                       f"{return_date} 提前回迁至「{back_pen.name}」", cow_id=item.cow_id)
            if return_date <= today:
                cow = db.get(models.Cow, item.cow_id)
                cow.group = back_pen.name
        _add_event(db, plan, "returned", f"隔离回迁办理完成（{return_date}）")
        db.commit()
    except (IntegrityError, OperationalError) as exc:
        db.rollback()
        raise HousingError(f"回迁失败：{exc.orig}")
    return plan


# ---------------- 补录历史转群 ----------------
def backfill_stay(db: Session, *, cow_id: int, pen_id: int, start_date: date,
                  end_date: Optional[date] = None, note: Optional[str] = None) -> models.Stay:
    """
    补录历史居住段。规则：
    - 与该牛任何已有居住区间重叠一律拒绝（同一头牛不能同时住两栏，补录也不行）；
    - end_date 留空表示“从 start_date 住到现在”：仅当迁入日早于当前开放区间起点时
      允许，自动把当前开放区间截到 start_date（即补记更早的迁入）；迁入日更晚则与
      现有在栏事实冲突，需先为原栏补录迁出日；
    - 相邻同日且同栏的区间自动归并，不产生零天碎片。
    """
    cow = db.get(models.Cow, cow_id)
    if not cow:
        raise HousingError("未找到该牛")
    pen = db.get(models.Pen, pen_id)
    if not pen:
        raise HousingError("未找到该栏位")
    if end_date and end_date <= start_date:
        raise HousingError("迁出日期必须晚于迁入日期")
    today = date.today()
    if start_date > today:
        raise HousingError("补录日期不能晚于今天；未来安排请使用转群计划")

    cur_open = open_stay(db, cow_id)
    overlap = find_overlap(db, cow_id, start_date, end_date)

    # 特殊情形：补录的开放区间与当前开放区间同栏，且迁入日更早——
    # 相当于该栏实际入住时间更早，直接把当前区间起点前移（中间空段并入在栏期）
    extend_open_same_pen = (
        end_date is None and cur_open is not None and cur_open.pen_id == pen_id
        and start_date < cur_open.start_date
    )

    # 唯一允许的重叠：开放补录 + 重叠的正是当前开放（异栏）+ 迁入日更早 -> 截断旧区间
    truncate_open = (
        end_date is None and cur_open is not None
        and overlap is not None and overlap.id == cur_open.id
        and start_date < cur_open.start_date
        and not extend_open_same_pen
    )
    if overlap is not None and not truncate_open and not extend_open_same_pen:
        other = db.get(models.Pen, overlap.pen_id)
        raise HousingError(
            f"与已有居住记录冲突：{overlap.start_date} ~ "
            f"{overlap.end_date or '至今'} 在「{other.name if other else overlap.pen_id}」，"
            f"同一头牛不能同时住两栏")

    prev_touch = (
        db.query(models.Stay)
        .filter(models.Stay.cow_id == cow_id, models.Stay.end_date == start_date)
        .first()
    )
    next_touch = (
        db.query(models.Stay)
        .filter(models.Stay.cow_id == cow_id, models.Stay.start_date == end_date)
        .first() if end_date else None
    )

    try:
        tbl = models.Stay.__table__
        # 把本牛全部居住对象逐出 ORM 身份映射，防止 Core 改写后过期对象在
        # 后续 flush 时用旧 end_date 回写（SQLite 触发器会因此看到错乱区间）
        for s in db.query(models.Stay).filter_by(cow_id=cow_id).all():
            db.expunge(s)

        conn = db.connection()
        if extend_open_same_pen:
            # 同栏补录更早的入住日：把当前开放区间起点前移，不新增记录
            conn.execute(tbl.update().where(tbl.c.id == cur_open.id)
                         .values(start_date=start_date))
            stay_id = cur_open.id
        elif prev_touch and prev_touch.pen_id == pen_id:
            new_end = next_touch.end_date if (
                next_touch and next_touch.pen_id == pen_id) else end_date
            conn.execute(tbl.update().where(tbl.c.id == prev_touch.id)
                         .values(end_date=new_end))
            if next_touch and next_touch.pen_id == pen_id:
                conn.execute(tbl.delete().where(tbl.c.id == next_touch.id))
            stay_id = prev_touch.id
        elif end_date is None:
            if truncate_open:
                conn.execute(tbl.update().where(tbl.c.id == cur_open.id)
                             .values(end_date=start_date))
            res = conn.execute(tbl.insert().values(
                cow_id=cow_id, pen_id=pen_id, start_date=start_date,
                end_date=None, source="history", note=note))
            stay_id = res.inserted_primary_key[0]
        else:
            res = conn.execute(tbl.insert().values(
                cow_id=cow_id, pen_id=pen_id, start_date=start_date,
                end_date=end_date, source="history", note=note))
            stay_id = res.inserted_primary_key[0]

        stay = db.get(models.Stay, stay_id)
        cur_pen = pen_at(db, cow_id, today)
        if cur_pen:
            cow.group = cur_pen.name
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HousingError(f"补录被拒绝：{exc.orig}")
    return stay


# ---------------- 计划/历史序列化 ----------------
def plan_to_dict(plan: models.TransferPlan, db: Session) -> dict:
    today = date.today()

    def pen_name(pid: Optional[int]) -> Optional[str]:
        if not pid:
            return None
        p = db.get(models.Pen, pid)
        return p.name if p else None

    return {
        "id": plan.id,
        "title": plan.title,
        "effective_date": str(plan.effective_date),
        "kind": plan.kind,
        "kind_label": "临时隔离" if plan.kind == "isolation" else "批量转群",
        "status": plan.status,
        "status_label": {"draft": "待确认", "confirmed": "已确认",
                         "cancelled": "已取消"}[plan.status],
        "operator": plan.operator,
        "note": plan.note,
        "confirmed_at": plan.confirmed_at.isoformat() if plan.confirmed_at else None,
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
        "is_effective_today": plan.status == "confirmed" and plan.effective_date <= today,
        "can_confirm": plan.status == "draft" and plan.effective_date >= today,
        "can_postpone": plan.status != "cancelled" and not (
            plan.status == "confirmed" and plan.effective_date <= today),
        "can_cancel": plan.status != "cancelled" and not (
            plan.status == "confirmed" and plan.effective_date <= today),
        "can_release": plan.kind == "isolation" and plan.status == "confirmed",
        "items": [{
            "id": it.id,
            "cow_id": it.cow_id,
            "cow_tag": (db.get(models.Cow, it.cow_id).ear_tag
                        if db.get(models.Cow, it.cow_id) else None),
            "cow_name": (db.get(models.Cow, it.cow_id).name
                         if db.get(models.Cow, it.cow_id) else None),
            "from_pen_id": it.from_pen_id,
            "from_pen_name": pen_name(it.from_pen_id),
            "to_pen_id": it.to_pen_id,
            "to_pen_name": pen_name(it.to_pen_id),
            "return_pen_id": it.return_pen_id,
            "return_pen_name": pen_name(it.return_pen_id or it.from_pen_id),
            "return_date": str(it.return_date) if it.return_date else None,
        } for it in plan.items],
        "events": [{
            "id": e.id, "action": e.action, "detail": e.detail,
            "at": e.at.isoformat(timespec="seconds") if e.at else None,
            "cow_id": e.cow_id,
        } for e in plan.events],
    }


def stay_to_dict(stay: models.Stay, db: Session) -> dict:
    cow = db.get(models.Cow, stay.cow_id)
    return {
        "id": stay.id,
        "cow_id": stay.cow_id,
        "cow_tag": cow.ear_tag if cow else None,
        "cow_name": cow.name if cow else None,
        "pen_id": stay.pen_id,
        "pen_name": stay.pen.name,
        "start_date": str(stay.start_date),
        "end_date": str(stay.end_date) if stay.end_date else None,
        "source": stay.source,
        "note": stay.note,
    }


def reconcile_group_text(db: Session) -> dict:
    """
    把旧的 cows.group 自由文本对照归并到 pens：
    - 同名栏位已存在 -> 直接关联；
    - 不存在 -> 按文本关键字推断用途并新建栏位（容量默认 10）；
    - 每头在场牛建立一条开放居住记录（已有 stays 的牛跳过），实现可对照归并。
    """
    created, linked, skipped = [], 0, 0
    existing = {p.name: p for p in db.query(models.Pen).all()}
    cows = db.query(models.Cow).order_by(models.Cow.id.asc()).all()
    for cow in cows:
        text = (cow.group or "").strip()
        if not text:
            skipped += 1
            continue
        pen = existing.get(text)
        if not pen:
            purpose = "other"
            for kw, code in PURPOSE_BY_GROUP_KEYWORD.items():
                if kw in text:
                    purpose = code
                    break
            if cow.status == "sold":
                purpose = "other"
            pen = models.Pen(
                name=text, purpose=purpose,
                capacity=0 if cow.status == "sold" else 10,
                active=cow.status != "sold",
                note="由原牛舍文本归并生成")
            db.add(pen)
            db.flush()
            existing[text] = pen
            created.append(pen.name)
        if cow.status != "sold" and not db.query(models.Stay).filter_by(cow_id=cow.id).first():
            db.add(models.Stay(
                cow_id=cow.id, pen_id=pen.id,
                start_date=date.today(), end_date=None,
                source="seed", note="原有牛舍信息归并建账"))
        linked += 1
    db.commit()
    return {"created_pens": created, "linked": linked, "skipped": skipped}


# 让 pen_at 内的 or_ 可用
from sqlalchemy import or_  # noqa: E402
