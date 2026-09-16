"""
繁殖周期领域服务。

设计要点：
- 事件溯源：发情/配种/孕检/妊娠终止/产犊均为只追加的 ReproEvent，档案上的
  最近产犊日、预产期、阶段全部由“有效事件”重算，任何旧周期结果都无法直接覆盖当前状态。
- 周期不可变：事件按时间形成一个个产犊周期，历史周期永久保留，作废使用 voided 标记。
- 日期精度：day 精确到日才参与状态/预产期/提醒计算；month、unknown 只归档展示，
  绝不猜测为已完成事件。
- 补录/更正：每次写入前后做状态快照对比，生成“对后续事件与当前状态的影响”说明并留痕。
"""
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from . import models, schemas

GESTATION_DAYS = 280          # 配种到平均产犊
DRY_OFF_BEFORE = 60           # 预产期前 60 天干奶
ESTRUS_LINK_DAYS = 3          # 配种向前关联发情事件的窗口
CHECK_FROM = 35               # 孕检窗口起
CHECK_DUE = 42                # 孕检计划日
OPEN_DAYS = 60                # 配种后超期未确认
FIRST_INSEM_MIN, FIRST_INSEM_MAX = 40, 80

EVENT_LABELS = {
    "estrus": "发情",
    "insemination": "配种输精",
    "pregnancy_check": "妊娠检查",
    "pregnancy_end": "妊娠终止",
    "calving": "产犊",
}
STAGE_LABELS = {
    "estrus": "发情待配",
    "bred_pending": "已配待检",
    "pregnant": "妊娠中",
    "postpartum_open": "产后空怀",
    "open": "空怀待配",
    "no_record": "暂无繁殖记录",
}
PRECISION_LABELS = {"day": "精确到日", "month": "仅年月", "unknown": "日期缺失"}
CHECK_LABELS = {"pregnant": "妊娠阳性", "negative": "未孕", "recheck": "疑似·需复查"}
END_REASON_LABELS = {
    "abortion": "流产", "stillbirth": "死产终止",
    "cull_pregnant": "孕牛淘汰", "other": "其他原因",
}
DETECTION_LABELS = {"observed": "人工观察", "activity": "计步器", "detector": "尾根蜡笔"}
CALF_SEX_LABELS = {"male": "公", "female": "母", "mixed": "雌雄均有", "unknown": "未知"}
CALF_STATUS_LABELS = {"alive": "存活", "dead": "死亡", "mixed": "部分存活"}


# ---------------------------------------------------------------- 基础工具
def event_date(ev: models.ReproEvent) -> Optional[date]:
    return ev.event_date if ev.date_precision == "day" else None


def event_sort_date(ev: models.ReproEvent) -> Optional[date]:
    """排序/分组用日期；不精确事件用月中占位，绝不用于业务计算"""
    if ev.event_date:
        return ev.event_date
    if ev.event_year and ev.event_month:
        return date(ev.event_year, ev.event_month, 15)
    return None


def iso(d: Optional[date]) -> Optional[str]:
    return str(d) if d else None


def _events_for_cow(db: Session, cow_id: int, *, include_void: bool = False):
    q = db.query(models.ReproEvent).filter_by(cow_id=cow_id)
    if not include_void:
        q = q.filter(models.ReproEvent.voided.is_(False))
    return q.order_by(
        models.ReproEvent.event_date.asc().nullslast(),
        models.ReproEvent.event_year.asc(),
        models.ReproEvent.event_month.asc(),
        models.ReproEvent.id.asc(),
    ).all()


def _precise_events(events: List[models.ReproEvent]) -> List[models.ReproEvent]:
    """只有精确日期的事件才能进入状态机/周期边界计算"""
    out = [e for e in events if not e.voided and e.date_precision == "day" and e.event_date]
    out.sort(key=lambda e: (e.event_date, e.id))
    return out


# ---------------------------------------------------------------- 状态机
def compute_state(events: List[models.ReproEvent], today: date) -> dict:
    """
    按时间顺序回放有效事件，得到当前繁殖状态。
    关键：以“事件发生日期”而非录入顺序推进，旧周期事件天然不可能覆盖更新的当前状态。
    """
    phase = "open"          # open / bred / pregnant
    current_insem = None    # 未结案的最近一次配种
    pregnant_insem = None
    positive_check = None
    last_calving = None
    last_end = None
    last_negative = None
    last_estrus = None
    warnings: List[str] = []

    for ev in _precise_events(events):
        t = ev.event_type
        if t == "estrus":
            if phase == "open":
                last_estrus = ev
        elif t == "insemination":
            if phase == "pregnant":
                warnings.append(
                    f"{ev.event_date} 的配种发生在已确认妊娠之后，未改变妊娠状态，"
                    f"如属妊娠终止后复配，请先补录妊娠终止事件")
                continue
            phase, current_insem = "bred", ev
            last_estrus = None
        elif t == "pregnancy_check":
            r = ev.check_result
            if r == "pregnant":
                phase = "pregnant"
                pregnant_insem = ev.linked_event if (
                    ev.linked_event and ev.linked_event.event_type == "insemination"
                ) else current_insem
                positive_check = ev
                current_insem = None
                last_estrus = None
            elif r == "negative":
                if phase == "pregnant":
                    warnings.append(
                        f"{ev.event_date} 孕检为阴性，但此前已有阳性孕检；"
                        f"系统按妊娠终止处理，建议补录一条“妊娠终止”事件说明原因")
                phase = "open"
                last_negative = ev
                current_insem = pregnant_insem = positive_check = None
            # recheck：维持当前阶段（已配待检或妊娠疑似），等待下次孕检
        elif t == "pregnancy_end":
            phase = "open"
            last_end = ev
            current_insem = pregnant_insem = positive_check = None
            last_estrus = None
        elif t == "calving":
            phase = "open"
            last_calving = ev
            last_end = None
            current_insem = pregnant_insem = positive_check = None
            last_estrus = None

    expected = None
    days_pregnant = None
    if phase == "pregnant":
        expected = positive_check.expected_calving_date if positive_check else None
        if not expected and pregnant_insem and pregnant_insem.event_date:
            expected = pregnant_insem.event_date + timedelta(days=GESTATION_DAYS)
        if pregnant_insem and pregnant_insem.event_date:
            days_pregnant = (today - pregnant_insem.event_date).days

    if last_calving and last_calving.event_date:
        dim = (today - last_calving.event_date).days
    else:
        dim = None

    if phase == "pregnant":
        stage = "pregnant"
    elif phase == "bred":
        stage = "bred_pending"
    elif last_estrus and 0 <= (today - last_estrus.event_date).days <= 2:
        stage = "estrus"
    elif last_calving:
        stage = "postpartum_open"
    elif events:
        stage = "open"
    else:
        stage = "no_record"

    failed_insem = None
    if phase == "open":
        src = last_negative or last_end
        if src and src.linked_event and src.linked_event.event_type == "insemination":
            failed_insem = src.linked_event
        elif src:
            prior = [
                e for e in _precise_events(events)
                if e.event_type == "insemination" and e.event_date <= src.event_date
            ]
            failed_insem = prior[-1] if prior else None

    return {
        "phase": phase,
        "stage": stage,
        "stage_label": STAGE_LABELS[stage],
        "current_insem": current_insem,
        "pregnant_insem": pregnant_insem,
        "positive_check": positive_check,
        "last_calving": last_calving,
        "last_end": last_end,
        "last_negative": last_negative,
        "last_estrus": last_estrus,
        "failed_insem": failed_insem,
        "expected_calving_date": expected,
        "days_pregnant": days_pregnant,
        "days_in_milk": dim,
        "warnings": warnings,
    }


# ---------------------------------------------------------------- 档案同步
def snapshot(state: dict) -> dict:
    """状态中会回写到奶牛档案的部分，用于变更前后影响对比"""
    return {
        "stage": state["stage"],
        "expected_calving_date": state["expected_calving_date"],
        "calving_date": state["last_calving"].event_date if state["last_calving"] else None,
    }


def _snapshot_diff(before: dict, after: dict) -> List[str]:
    impacts: List[str] = []
    if before["stage"] is not None and before["stage"] != after["stage"]:
        impacts.append(
            f"当前繁殖阶段：{STAGE_LABELS.get(before['stage'], before['stage'])} → "
            f"{STAGE_LABELS.get(after['stage'], after['stage'])}，后续提醒已按新阶段重算")
    b_edd, a_edd = before["expected_calving_date"], after["expected_calving_date"]
    if b_edd != a_edd:
        if a_edd and not b_edd:
            impacts.append(f"档案预产期已设定为 {a_edd}，将据此产生干奶/待产提醒")
        elif b_edd and not a_edd:
            impacts.append(f"原预产期 {b_edd} 已清除（妊娠不再成立），待产/干奶提醒同步取消")
        else:
            impacts.append(f"档案预产期由 {b_edd} 更正为 {a_edd}，干奶/待产提醒同步推移")
    b_c, a_c = before["calving_date"], after["calving_date"]
    if b_c != a_c:
        if a_c and not b_c:
            impacts.append(f"档案最近产犊日已设定为 {a_c}，产后首配窗口与泌乳天数以此为准")
        elif b_c and not a_c:
            impacts.append(f"原最近产犊日 {b_c} 已随事件作废而回退")
        else:
            impacts.append(f"档案最近产犊日由 {b_c} 更正为 {a_c}，泌乳天数与首配窗口同步重算")
    return impacts


def sync_cow_state(db: Session, cow: models.Cow, events: List[models.ReproEvent],
                   today: date) -> Tuple[dict, List[str]]:
    """按事件重算并回写档案字段；返回 (状态, 档案变化说明)"""
    state = compute_state(events, today)
    before = {
        "stage": getattr(cow, "_repro_stage_cache", None),
        "expected_calving_date": cow.expected_calving_date,
        "calving_date": cow.calving_date,
    }
    cow.expected_calving_date = state["expected_calving_date"]
    if state["last_calving"]:
        cow.calving_date = state["last_calving"].event_date
    # 无产犊事件时不主动清空（可能是新建档案手填），迁移后历史都有事件

    # 阶段衔接的牛群状态：产犊/妊娠终止后回到泌乳；妊娠中不强行改状态（孕中期仍在挤奶）
    status_impacts: List[str] = []
    if cow.status != "sold":
        calving = state["last_calving"]
        ended = state["last_end"]
        anchor = calving.event_date if calving else None
        if (calving or ended) and cow.status in ("dry", "pregnant"):
            anchor_date = calving.event_date if calving else ended.event_date
            # 仅当该终止/产犊是牛只最新的有效事件时才衔接泌乳阶段
            latest = _precise_events(events)[-1] if events else None
            if latest and (latest.event_type in ("calving", "pregnancy_end")
                           or latest.event_date <= anchor_date):
                old = cow.status
                cow.status = "lactating"
                status_impacts.append(
                    f"牛群状态由{'干奶' if old == 'dry' else '待产'}衔接为“泌乳中”，"
                    f"可登记新泌乳期挤奶记录")
    db.flush()
    impacts = _snapshot_diff(before, snapshot(state)) + status_impacts
    cow._repro_stage_cache = state["stage"]
    return state, impacts


# ---------------------------------------------------------------- 写入校验
def _normalize_date_fields(
    payload, today: date
) -> Tuple[Optional[date], str, Optional[int], Optional[int], List[str]]:
    precision = payload.get("date_precision", "day")
    event_date_v = payload.get("event_date")
    year, month = payload.get("event_year"), payload.get("event_month")
    warnings: List[str] = []
    if precision == "day":
        if not event_date_v:
            raise HTTPException(400, "精确日期事件必须填写事件日期")
        if event_date_v > today:
            raise HTTPException(400, "繁殖事件是已发生事实，事件日期不能晚于今天")
        year, month = event_date_v.year, event_date_v.month
    elif precision == "month":
        if not year or not month:
            raise HTTPException(400, "仅年月的记录必须填写年份和月份")
        if year > today.year or (year == today.year and month > today.month):
            raise HTTPException(400, "事件月份不能晚于当前月份")
        event_date_v = None
        warnings.append("该记录仅精确到月份，只归档展示，不参与当前阶段、预产期与提醒计算")
    else:
        event_date_v, year, month = None, None, None
        warnings.append("该记录日期缺失，只归档展示，不会被当作已完成事件参与任何计算")
    return event_date_v, precision, year, month, warnings


REQUIRED_FIELDS = {
    "pregnancy_check": ("check_result", "请选择孕检结果"),
    "pregnancy_end": ("end_reason", "请选择妊娠终止原因"),
}


def _validate_type_fields(t: str, data: dict):
    if t in REQUIRED_FIELDS:
        field, msg = REQUIRED_FIELDS[t]
        if not data.get(field):
            raise HTTPException(400, msg)
    if t == "pregnancy_check" and data.get("check_result") not in CHECK_LABELS:
        raise HTTPException(400, "孕检结果非法")
    if t == "pregnancy_end" and data.get("end_reason") not in END_REASON_LABELS:
        raise HTTPException(400, "妊娠终止原因非法")


def _auto_link(db: Session, cow_id: int, ev: models.ReproEvent,
               precise: List[models.ReproEvent]) -> List[str]:
    """按事件类型自动关联前置事件；返回提示"""
    warnings: List[str] = []
    if ev.date_precision != "day":
        return warnings
    t = ev.event_type
    linked_ids = {
        x.linked_event_id for x in precise
        if x.linked_event_id and x.event_type == "insemination"
    }
    if t == "insemination":
        candidates = [
            x for x in precise
            if x.event_type == "estrus" and x.id not in linked_ids
            and ev.event_date - timedelta(days=ESTRUS_LINK_DAYS) <= x.event_date <= ev.event_date
        ]
        if candidates:
            ev.linked_event_id = candidates[-1].id
    elif t == "pregnancy_check":
        prior = [x for x in precise if x.event_type == "insemination"
                 and x.event_date <= ev.event_date]
        if prior:
            ev.linked_event_id = prior[-1].id
            gap = (ev.event_date - prior[-1].event_date).days
            if gap < 0:
                warnings.append("孕检日期早于所关联配种日期，请核对")
            elif gap < CHECK_FROM:
                warnings.append(f"距配种仅 {gap} 天，早孕检结果可能不准确，建议安排复查")
    elif t in ("pregnancy_end", "calving"):
        # 找到最近一次阳性孕检所确认的配种
        checks = [x for x in precise if x.event_type == "pregnancy_check"
                  and x.check_result == "pregnant" and x.event_date <= ev.event_date]
        target_insem = None
        if checks:
            chk = checks[-1]
            target_insem = chk.linked_event if chk.linked_event_id else None
        if not target_insem:
            prior = [x for x in precise if x.event_type == "insemination"
                     and x.event_date <= ev.event_date]
            target_insem = prior[-1] if prior else None
        if target_insem:
            ev.linked_event_id = target_insem.id
            if t == "calving":
                gl = (ev.event_date - target_insem.event_date).days
                if gl < 260:
                    warnings.append(f"距配种仅 {gl} 天，短于正常妊娠期（约280天），已标注早产请核对")
                elif gl > 300:
                    warnings.append(f"距配种 {gl} 天，超出正常妊娠期较多，请核对日期")
        else:
            warnings.append("未找到可关联的配种记录，该事件将独立归档")
    return warnings


# ---------------------------------------------------------------- 变更留痕
def _log(db: Session, ev: models.ReproEvent, action: str,
         summary: str, impact: str):
    db.add(models.ReproEventChange(
        event_id=ev.id, cow_id=ev.cow_id, action=action,
        change_summary=summary, impact=impact or "无后续影响"))


def _field_summary(ev: models.ReproEvent) -> str:
    parts = []
    if ev.detection:
        parts.append(f"发现方式 {DETECTION_LABELS.get(ev.detection, ev.detection)}")
    if ev.score:
        parts.append(f"强度 {ev.score} 级")
    if ev.semen:
        parts.append(f"冻精 {ev.semen}")
    if ev.technician:
        parts.append(f"配种员 {ev.technician}")
    if ev.check_result:
        parts.append(f"结果 {CHECK_LABELS.get(ev.check_result, ev.check_result)}")
    if ev.expected_calving_date:
        parts.append(f"预产期 {ev.expected_calving_date}")
    if ev.end_reason:
        parts.append(f"终止原因 {END_REASON_LABELS.get(ev.end_reason, ev.end_reason)}")
    if ev.event_type == "calving":
        if ev.calf_count is not None:
            parts.append(f"犊牛 {ev.calf_count} 头")
        if ev.calf_status:
            parts.append(CALF_STATUS_LABELS.get(ev.calf_status, ev.calf_status))
    return "，".join(parts) or "无补充字段"


# ---------------------------------------------------------------- CRUD
def create_event(db: Session, payload: schemas.ReproEventCreate, today: date) -> dict:
    cow = db.get(models.Cow, payload.cow_id)
    if not cow:
        raise HTTPException(404, "未找到该牛")
    data = payload.model_dump()
    _validate_type_fields(payload.event_type, data)
    ev_date, precision, yr, mo, warnings = _normalize_date_fields(data, today)

    old_events = _events_for_cow(db, cow.id)
    before = compute_state(old_events, today)

    # 配种时可同步补建一条发情事件
    paired = None
    if payload.event_type == "insemination" and payload.create_paired_estrus and precision == "day":
        paired = models.ReproEvent(
            cow_id=cow.id, event_type="estrus", event_date=ev_date,
            date_precision="day", event_year=yr, event_month=mo,
            detection=payload.detection or "observed", score=payload.score,
            note="配种时补登的发情事件",
        )
        db.add(paired)
        db.flush()

    ev = models.ReproEvent(
        cow_id=cow.id, event_type=payload.event_type, event_date=ev_date,
        date_precision=precision, event_year=yr, event_month=mo,
        detection=payload.detection, score=payload.score,
        semen=payload.semen, technician=payload.technician,
        check_result=payload.check_result,
        expected_calving_date=payload.expected_calving_date,
        end_reason=payload.end_reason,
        calf_count=payload.calf_count, calf_sex=payload.calf_sex, calf_status=payload.calf_status,
        updates_parity=payload.updates_parity,
        linked_event_id=payload.linked_event_id, note=payload.note,
        source="manual",
    )
    db.add(ev)
    db.flush()

    work_events = old_events + ([paired] if paired else []) + [ev]
    precise = _precise_events(work_events)
    if not ev.linked_event_id:
        warnings += _auto_link(db, cow.id, ev, precise)
    if ev.linked_event and ev.linked_event.cow_id != cow.id:
        raise HTTPException(400, "关联事件必须属于同一头牛")
    if ev.event_type == "pregnancy_check" and ev.check_result == "pregnant":
        if ev.expected_calving_date:
            ev.edd_manual = True
        elif ev.linked_event_id and ev.linked_event.event_date:
            ev.expected_calving_date = ev.linked_event.event_date + timedelta(days=GESTATION_DAYS)
            ev.edd_manual = False
            warnings.append(f"未手工指定预产期，已按配种日 +{GESTATION_DAYS} 天推算为 "
                            f"{ev.expected_calving_date}，可在编辑中校正")

    impacts: List[str] = []
    # 胎次推进（仅在明确勾选中发生，可逆：作废时回退）
    if ev.event_type == "calving" and ev.updates_parity:
        cow.parity = (cow.parity or 0) + 1
        impacts.append(f"胎次已推进为第 {cow.parity} 胎")

    _, state_impacts = sync_cow_state(db, cow, work_events, today)
    impacts += state_impacts
    after = compute_state(work_events, today)
    impacts += [w for w in after["warnings"] if w not in impacts]

    # 判断是否落入历史周期：早于当前最近产犊（重算后的最近产犊若不是本次事件）
    if precision == "day":
        cur_calving = after["last_calving"]
        if cur_calving and cur_calving.id != ev.id and ev.event_date < cur_calving.event_date:
            unchanged = ("当前阶段、预产期与后续提醒均不变，事件已归档到历史周期")
            impacts = [m for m in impacts if "阶段" not in m and "预产期" not in m]
            impacts.append(f"该事件发生在最近产犊（{cur_calving.event_date}）之前，"
                           f"属于上一繁殖周期；" + unchanged)
    if paired:
        _log(db, paired, "create", f"补登发情（{paired.event_date}）",
             "作为配种的前置发情事件，不单独改变阶段")
    _log(db, ev, "create",
         f"{EVENT_LABELS[ev.event_type]}（{_date_text(ev)}）：{_field_summary(ev)}",
         "；".join(dict.fromkeys(impacts)) or "当前阶段与档案无变化")

    db.commit()
    db.refresh(ev)
    return {
        "event": event_json(ev),
        "warnings": list(dict.fromkeys(warnings)),
        "impacts": list(dict.fromkeys(impacts)),
    }


def update_event(db: Session, ev_id: int, payload: schemas.ReproEventUpdate,
                 today: date) -> dict:
    ev = db.get(models.ReproEvent, ev_id)
    if not ev or ev.voided:
        raise HTTPException(404, "事件不存在或已作废")
    cow = db.get(models.Cow, ev.cow_id)
    data = payload.model_dump(exclude_unset=True)
    old_events = _events_for_cow(db, cow.id)
    before = compute_state(old_events, today)
    old_summary = f"{_date_text(ev)}：{_field_summary(ev)}"
    old_parity_flag = ev.updates_parity

    if "date_precision" in data or "event_date" in data or \
            "event_year" in data or "event_month" in data:
        merged = {
            "event_date": data.get("event_date", ev.event_date),
            "date_precision": data.get("date_precision", ev.date_precision),
            "event_year": data.get("event_year", ev.event_year),
            "event_month": data.get("event_month", ev.event_month),
        }
        ev_date, precision, yr, mo, date_warnings = _normalize_date_fields(merged, today)
        ev.event_date, ev.date_precision = ev_date, precision
        ev.event_year, ev.event_month = yr, mo
    for k in ("detection", "score", "semen", "technician", "check_result",
              "expected_calving_date", "end_reason", "calf_count", "calf_sex",
              "calf_status", "updates_parity", "linked_event_id", "note"):
        if k in data:
            setattr(ev, k, data[k])
    _validate_type_fields(ev.event_type, {
        "check_result": ev.check_result, "end_reason": ev.end_reason})
    if ev.linked_event and ev.linked_event.cow_id != cow.id:
        raise HTTPException(400, "关联事件必须属于同一头牛")
    if ev.event_type == "pregnancy_check" and ev.check_result == "pregnant":
        if ev.expected_calving_date:
            if "expected_calving_date" in data:
                ev.edd_manual = True
        elif ev.linked_event_id and ev.linked_event.event_date:
            ev.expected_calving_date = ev.linked_event.event_date + timedelta(days=GESTATION_DAYS)
            ev.edd_manual = False

    # 更正配种日期：级联重算所有“自动推算”的阳性孕检预产期（手工校正的不动）
    if ev.event_type == "insemination" and ev.event_date:
        dependents = (
            db.query(models.ReproEvent)
            .filter_by(cow_id=cow.id, event_type="pregnancy_check",
                       check_result="pregnant", linked_event_id=ev.id, voided=False)
            .all()
        )
        for chk in dependents:
            if not chk.edd_manual:
                old_edd = chk.expected_calving_date
                chk.expected_calving_date = ev.event_date + timedelta(days=GESTATION_DAYS)
                if old_edd and old_edd != chk.expected_calving_date:
                    _log(db, chk, "update",
                         f"因关联配种日期更正，预产期 {old_edd} → {chk.expected_calving_date}",
                         "随配种日期自动重算（非手工校正）")

    impacts: List[str] = []
    if ev.event_type == "calving" and ev.updates_parity != old_parity_flag:
        cow.parity = max(0, (cow.parity or 0) + (1 if ev.updates_parity else -1))
        impacts.append(f"胎次已相应{'推进' if ev.updates_parity else '回退'}为第 {cow.parity} 胎")

    new_events = _events_for_cow(db, cow.id)
    _, state_impacts = sync_cow_state(db, cow, new_events, today)
    impacts += state_impacts
    after = compute_state(new_events, today)
    # 更正早期事件但当前状态不变时给出明确说明
    if not state_impacts and ev.date_precision == "day":
        impacts.append("本次更正未改变当前阶段、预产期与后续提醒（仅历史周期内容更新）")

    _log(db, ev, "update", f"由 [{old_summary}] 更正为 [{_date_text(ev)}：{_field_summary(ev)}]",
         "；".join(dict.fromkeys(impacts)) or "当前阶段与档案无变化")
    db.commit()
    db.refresh(ev)
    return {
        "event": event_json(ev),
        "warnings": [],
        "impacts": list(dict.fromkeys(impacts)),
    }


def void_event(db: Session, ev_id: int, reason: str, today: date) -> dict:
    ev = db.get(models.ReproEvent, ev_id)
    if not ev or ev.voided:
        raise HTTPException(404, "事件不存在或已作废")
    cow = db.get(models.Cow, ev.cow_id)
    old_events = _events_for_cow(db, cow.id)
    ev.voided, ev.void_reason = True, reason

    impacts: List[str] = []
    if ev.event_type == "calving" and ev.updates_parity:
        cow.parity = max(0, (cow.parity or 0) - 1)
        impacts.append(f"胎次已回退为第 {cow.parity} 胎")
    new_events = _events_for_cow(db, cow.id)
    _, state_impacts = sync_cow_state(db, cow, new_events, today)
    impacts += state_impacts
    if not state_impacts:
        impacts.append("作废的是历史周期事件，当前阶段、预产期与提醒不变")
    _log(db, ev, "void",
         f"作废 {EVENT_LABELS[ev.event_type]}（{_date_text(ev)}），原因：{reason}",
         "；".join(impacts))
    db.commit()
    return {"impacts": impacts}


def delete_event(db: Session, ev_id: int, today: date) -> dict:
    ev = db.get(models.ReproEvent, ev_id)
    if not ev:
        raise HTTPException(404, "事件不存在")
    if ev.source == "legacy":
        raise HTTPException(400, "旧表迁移事件请用作废处理，保留历史痕迹")
    cow = db.get(models.Cow, ev.cow_id)
    if ev.event_type == "calving" and ev.updates_parity and not ev.voided:
        cow.parity = max(0, (cow.parity or 0) - 1)
    db.delete(ev)
    remaining = _events_for_cow(db, cow.id)
    sync_cow_state(db, cow, remaining, today)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- 序列化
def _date_text(ev: models.ReproEvent) -> str:
    if ev.date_precision == "day":
        return str(ev.event_date)
    if ev.date_precision == "month":
        return f"{ev.event_year}-{ev.event_month:02d}（仅年月）"
    return "日期缺失"


def event_json(ev: models.ReproEvent) -> dict:
    return {
        "id": ev.id,
        "cow_id": ev.cow_id,
        "event_type": ev.event_type,
        "event_type_label": EVENT_LABELS[ev.event_type],
        "event_date": iso(ev.event_date),
        "date_precision": ev.date_precision,
        "date_precision_label": PRECISION_LABELS[ev.date_precision],
        "date_text": _date_text(ev),
        "event_year": ev.event_year,
        "event_month": ev.event_month,
        "detection": ev.detection,
        "detection_label": DETECTION_LABELS.get(ev.detection) if ev.detection else None,
        "score": ev.score,
        "semen": ev.semen,
        "technician": ev.technician,
        "check_result": ev.check_result,
        "check_result_label": CHECK_LABELS.get(ev.check_result) if ev.check_result else None,
        "expected_calving_date": iso(ev.expected_calving_date),
        "edd_manual": bool(ev.edd_manual),
        "end_reason": ev.end_reason,
        "end_reason_label": END_REASON_LABELS.get(ev.end_reason) if ev.end_reason else None,
        "calf_count": ev.calf_count,
        "calf_sex": ev.calf_sex,
        "calf_sex_label": CALF_SEX_LABELS.get(ev.calf_sex) if ev.calf_sex else None,
        "calf_status": ev.calf_status,
        "calf_status_label": CALF_STATUS_LABELS.get(ev.calf_status) if ev.calf_status else None,
        "updates_parity": ev.updates_parity,
        "linked_event_id": ev.linked_event_id,
        "source": ev.source,
        "note": ev.note,
        "voided": ev.voided,
        "void_reason": ev.void_reason,
    }


# ---------------------------------------------------------------- 周期重建
def _cycles_from_events(precise: List[models.ReproEvent]) -> List[dict]:
    """按产犊事件切分周期：两次产犊之间为一个闭合周期，最近产犊之后为当前周期"""
    cycles: List[dict] = []
    cur: List[models.ReproEvent] = []
    prev_calving = None
    for ev in precise:
        if ev.event_type == "calving":
            cur.append(ev)
            cycles.append({
                "closed": True,
                "start_event": prev_calving,
                "end_event": ev,
                "events": cur,
            })
            prev_calving, cur = ev, []
        else:
            cur.append(ev)
    if cur or prev_calving is not None:
        cycles.append({
            "closed": False,
            "start_event": prev_calving,
            "end_event": None,
            "events": cur,
        })
    return cycles


def _cycle_summary(c: dict, state: dict) -> dict:
    evs = c["events"]
    insems = [e for e in evs if e.event_type == "insemination"]
    checks = [e for e in evs if e.event_type == "pregnancy_check"]
    ends = [e for e in evs if e.event_type == "pregnancy_end"]
    estruses = [e for e in evs if e.event_type == "estrus"]
    result = "已产犊"
    if not c["closed"]:
        if state["phase"] == "pregnant":
            result = "妊娠中"
        elif state["phase"] == "bred":
            result = "已配待检"
        elif estruses and state["stage"] == "estrus":
            result = "发情待配"
        elif ends:
            result = "妊娠终止后空怀"
        elif insems:
            result = "复配中/空怀"
        else:
            result = "空怀待配"
    return {
        "insemination_count": len(insems),
        "check_count": len(checks),
        "end_count": len(ends),
        "estrus_count": len(estruses),
        "result": result,
    }


def build_profile(db: Session, cow: models.Cow, today: date) -> dict:
    all_events = _events_for_cow(db, cow.id)
    precise = _precise_events(all_events)
    incomplete = [e for e in all_events if e.date_precision != "day"]
    state = compute_state(all_events, today)

    raw_cycles = _cycles_from_events(precise) if precise else []
    total_closed = sum(1 for c in raw_cycles if c["closed"])
    cycles = []
    for idx, c in enumerate(raw_cycles):
        num = idx + 1
        is_current = not c["closed"]
        cycles.append({
            "index": num,
            "name": (f"第 {num} 周期" if not is_current or total_closed == 0
                     else f"第 {num} 周期·当前"),
            "current": is_current,
            "closed": c["closed"],
            "start_date": iso(c["start_event"].event_date) if c["start_event"] else None,
            "end_date": iso(c["end_event"].event_date) if c["end_event"] else None,
            "summary": _cycle_summary(c, state if is_current else
                                      {"phase": "closed", "stage": "open"}),
            "events": [event_json(e) for e in c["events"]],
        })

    return {
        "cow_id": cow.id,
        "ear_tag": cow.ear_tag,
        "name": cow.name,
        "parity": cow.parity,
        "status": cow.status,
        "stage": state["stage"],
        "stage_label": state["stage_label"],
        "phase": state["phase"],
        "days_pregnant": state["days_pregnant"],
        "days_in_milk": state["days_in_milk"],
        "expected_calving_date": iso(state["expected_calving_date"]),
        "dry_off_date": iso(state["expected_calving_date"] - timedelta(days=DRY_OFF_BEFORE))
        if state["expected_calving_date"] else None,
        "calving_date": iso(state["last_calving"].event_date) if state["last_calving"] else None,
        "current_insemination_id": state["current_insem"].id if state["current_insem"] else None,
        "current_conception_id": state["pregnant_insem"].id if state["pregnant_insem"] else None,
        "warnings": state["warnings"],
        "cycles": list(reversed(cycles)),  # 最近的周期在最前
        "incomplete_events": [event_json(e) for e in incomplete],
        "incomplete_count": len(incomplete),
        "changes": [
            {
                "id": ch.id,
                "action": ch.action,
                "summary": ch.change_summary,
                "impact": ch.impact,
                "created_at": ch.created_at.strftime("%Y-%m-%d %H:%M") if ch.created_at else None,
            }
            for ch in db.query(models.ReproEventChange)
            .filter_by(cow_id=cow.id)
            .order_by(models.ReproEventChange.id.desc()).limit(20).all()
        ],
    }


def overview(db: Session, today: date) -> List[dict]:
    cows = db.query(models.Cow).order_by(models.Cow.ear_tag.asc()).all()
    out = []
    for cow in cows:
        events = _events_for_cow(db, cow.id)
        state = compute_state(events, today)
        out.append({
            "cow_id": cow.id,
            "ear_tag": cow.ear_tag,
            "name": cow.name,
            "status": cow.status,
            "parity": cow.parity,
            "group": cow.group,
            "stage": state["stage"],
            "stage_label": state["stage_label"],
            "days_pregnant": state["days_pregnant"],
            "days_in_milk": state["days_in_milk"],
            "expected_calving_date": iso(state["expected_calving_date"]),
            "calving_date": iso(state["last_calving"].event_date) if state["last_calving"] else None,
            "incomplete_count":
                sum(1 for e in events if e.date_precision != "day"),
            "sold": cow.status == "sold",
        })
    return out


# ---------------------------------------------------------------- 提醒
def build_repro_reminders(db: Session, today: date) -> List[dict]:
    """阶段驱动的繁殖提醒；不完整日期事件一律不产生提醒"""
    out: List[dict] = []
    for cow in db.query(models.Cow).all():
        if cow.status == "sold":
            continue
        events = _events_for_cow(db, cow.id)
        st = compute_state(events, today)

        def add(t, level, title, detail, due, overdue=0):
            out.append({
                "type": t, "level": level, "cow_id": cow.id, "cow_tag": cow.ear_tag,
                "title": f"{cow.ear_tag}（{cow.name}）{title}" if cow.name
                else f"{cow.ear_tag} {title}",
                "detail": detail, "due_date": str(due), "days_overdue": overdue,
            })

        # 发情待配：发情后 48 小时窗口，且之后没有配种（进入 bred 阶段时不会命中）
        if st["stage"] == "estrus" and st["last_estrus"]:
            e = st["last_estrus"]
            overdue = max(0, (today - e.event_date - timedelta(days=1)).days)
            add("estrus", "danger" if overdue else "warning", "发情待配种",
                f"{e.event_date} 通过{DETECTION_LABELS.get(e.detection, '观察')}发现发情"
                f"{f'，强度{e.score}级' if e.score else ''}，请适时输精",
                e.event_date + timedelta(days=1), overdue)

        # 已配待检：返情观察 → 孕检 → 长期空怀，全部围绕“当前有效配种”
        if st["phase"] == "bred" and st["current_insem"]:
            ins = st["current_insem"]
            days = (today - ins.event_date).days
            if 18 <= days <= 24:
                add("return_estrus", "warning", "进入返情观察期",
                    f"配种已 {days} 天（冻精 {ins.semen or '-'}），注意观察返情，必要时复配",
                    ins.event_date + timedelta(days=24))
            elif CHECK_FROM <= days <= CHECK_DUE:
                add("preg_check", "warning", "待妊娠检查",
                    f"配种已 {days} 天，已进入孕检窗口，请尽快做直肠/B超孕检并回填结果",
                    ins.event_date + timedelta(days=CHECK_DUE))
            elif CHECK_DUE < days <= OPEN_DAYS:
                add("preg_check", "danger", "妊娠检查已超期",
                    f"配种已 {days} 天，孕检窗口已过 {days - CHECK_DUE} 天，请尽快补检",
                    ins.event_date + timedelta(days=CHECK_DUE), days - CHECK_DUE)
            elif days > OPEN_DAYS:
                add("open_cow", "danger", "长期空怀需处理",
                    f"配种后 {days} 天仍无孕检结论，请评估卵巢与子宫状态，安排复配或淘汰",
                    ins.event_date + timedelta(days=OPEN_DAYS), days - OPEN_DAYS)

        # 空怀：阴性孕检/妊娠终止对应的配种超 60 天且之后未再配
        if st["phase"] == "open" and st["failed_insem"]:
            days = (today - st["failed_insem"].event_date).days
            if days > OPEN_DAYS:
                src = "孕检未孕" if st["last_negative"] else "妊娠终止"
                add("open_cow", "danger", "长期空怀需处理",
                    f"上次配种（{st['failed_insem'].event_date}）{src}距今 {days} 天，"
                    f"尚未复配，请安排同期发情或淘汰评估",
                    st["failed_insem"].event_date + timedelta(days=OPEN_DAYS),
                    days - OPEN_DAYS)

        # 产后首配窗口
        if st["phase"] == "open" and st["days_in_milk"] is not None:
            dim = st["days_in_milk"]
            if FIRST_INSEM_MIN <= dim <= FIRST_INSEM_MAX:
                add("first_insemination",
                    "info" if dim < 55 else "warning", "进入产后首配窗口",
                    f"产后已 {dim} 天，建议加强发情监测并安排首次配种",
                    st["last_calving"].event_date + timedelta(days=60),
                    max(0, dim - 60))

        # 妊娠中：干奶提醒 → 待产预警
        if st["phase"] == "pregnant" and st["expected_calving_date"]:
            edd = st["expected_calving_date"]
            delta = (edd - today).days
            dry = edd - timedelta(days=DRY_OFF_BEFORE)
            dry_delta = (dry - today).days
            if cow.status == "lactating" and -3 <= dry_delta <= 7:
                add("dry_off", "info", "临近干奶期",
                    f"妊娠 {st['days_pregnant']} 天，预产期 {edd}，计划干奶日 {dry}"
                    f"（{'剩' if dry_delta >= 0 else '超'}{abs(dry_delta)}天），请安排干奶操作",
                    dry)
            if -3 <= delta <= 10:
                add("calving", "danger" if delta <= 2 else "info", "临近预产期",
                    f"预产期 {edd}（{'剩' if delta >= 0 else '超'} {abs(delta)} 天），"
                    f"做好产房与接产准备", edd, max(0, -delta))
    return out


# ---------------------------------------------------------------- 旧表迁移
def migrate_legacy(db: Session, today: date) -> int:
    """
    将旧 estrus_records 与档案上的 calving_date 一次性迁移为繁殖事件（幂等）。
    迁移只做一次：之后的真相源是 reproductive_events，旧表仅保留只读。
    """
    flag = db.get(models.AppMeta, "legacy_repro_migrated")
    if flag and flag.value == "1":
        return 0

    created = 0
    cows = db.query(models.Cow).all()
    for cow in cows:
        if db.query(models.ReproEvent).filter_by(cow_id=cow.id).first():
            continue
        old_rows = (
            db.query(models.EstrusRecord)
            .filter_by(cow_id=cow.id)
            .order_by(models.EstrusRecord.date.asc(), models.EstrusRecord.id.asc())
            .all()
        )
        # 仅把“最近一次阳性孕检”的预产期取档案值，历史孕检预产期留空/推算
        last_positive = None
        for row in old_rows:
            if row.result == "pregnant":
                last_positive = row

        new_events: List[models.ReproEvent] = []
        for row in old_rows:
            estrus = models.ReproEvent(
                cow_id=cow.id, event_type="estrus", event_date=row.date,
                date_precision="day", event_year=row.date.year, event_month=row.date.month,
                detection=row.detection, score=row.score,
                note=row.note or "由旧发情记录表迁移", source="legacy",
            )
            db.add(estrus)
            new_events.append(estrus)
            if row.inseminated and row.insemination_date:
                ins = models.ReproEvent(
                    cow_id=cow.id, event_type="insemination",
                    event_date=row.insemination_date,
                    date_precision="day",
                    event_year=row.insemination_date.year,
                    event_month=row.insemination_date.month,
                    semen=row.semen, technician=row.technician,
                    note="由旧发情记录表迁移", source="legacy",
                )
                db.add(ins)
                new_events.append(ins)
            if row.result in ("pregnant", "negative", "unknown") and row.result_date:
                chk = models.ReproEvent(
                    cow_id=cow.id, event_type="pregnancy_check",
                    event_date=row.result_date,
                    date_precision="day",
                    event_year=row.result_date.year,
                    event_month=row.result_date.month,
                    check_result={"pregnant": "pregnant", "negative": "negative",
                                  "unknown": "recheck"}[row.result],
                    expected_calving_date=(
                        cow.expected_calving_date if row is last_positive else None
                    ),
                    edd_manual=bool(
                        row is last_positive and cow.expected_calving_date
                    ),
                    note="由旧发情记录表迁移", source="legacy",
                )
                db.add(chk)
                new_events.append(chk)

        # 档案上的最近产犊日迁移为一条历史产犊事件（不重复推进胎次）
        if cow.calving_date:
            calv = models.ReproEvent(
                cow_id=cow.id, event_type="calving", event_date=cow.calving_date,
                date_precision="day", event_year=cow.calving_date.year,
                event_month=cow.calving_date.month,
                calf_status=None, updates_parity=False,
                note="由奶牛档案最近产犊日迁移", source="legacy",
            )
            db.add(calv)
            new_events.append(calv)

        if not new_events:
            continue
        db.flush()
        precise = _precise_events(new_events)
        # 按时间顺序补关联（配种→发情、孕检/产犊→配种）
        for ev in sorted(new_events, key=lambda e: (e.event_date or date.min, e.id)):
            if ev.event_type != "estrus" and not ev.linked_event_id:
                _auto_link(db, cow.id, ev, precise)
        created += len(new_events)
        sync_cow_state(db, cow, _events_for_cow(db, cow.id), today)

    db.add(models.AppMeta(key="legacy_repro_migrated", value="1"))
    db.commit()
    return created

