"""FastAPI 入口：牧场管理系统 API"""
import json
import logging
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import case as sql_case
from sqlalchemy.orm import Session

from . import models, schemas, services
from .database import Base, engine, get_db
from .seed import init_db

logger = logging.getLogger("dairy.anomaly")

Base.metadata.create_all(bind=engine)
init_db()

app = FastAPI(title="牧场管理系统 API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

VALID_STATUS = {"lactating", "dry", "pregnant", "sold"}
SESSION_LABEL = {"morning": "早班", "noon": "午班", "evening": "晚班"}


def case_status_order(column):
    """调查单排序：调查中/重开续跟 → 已恢复关闭 → 误报关闭"""
    return sql_case(
        (column.in_(("open", "reopened")), 0),
        (column == "resolved", 1),
        (column == "false_positive", 2),
        else_=9,
    )


# ---------------- 序列化 ----------------
def milking_to_dict(r: models.MilkingRecord, db: Session, check_date: bool = True) -> dict:
    cow = db.get(models.Cow, r.cow_id)
    in_w, until = False, None
    if check_date:
        chk = services.check_withdrawal(db, r.cow_id, r.date)
        if chk["in_withdrawal"]:
            in_w, until = True, chk["withdrawal_end"]
    return {
        "id": r.id, "cow_id": r.cow_id, "date": str(r.date), "session": r.session,
        "session_label": SESSION_LABEL.get(r.session, r.session),
        "yield_kg": r.yield_kg, "scc": r.scc, "discarded": r.discarded,
        "note": r.note, "created_at": str(r.created_at) if r.created_at else None,
        "cow_ear_tag": cow.ear_tag if cow else None,
        "cow_name": cow.name if cow else None,
        "in_withdrawal": in_w,
        "withdrawal_until": str(until) if until else None,
        "violation": bool(in_w and not r.discarded),
    }


def med_to_dict(m: models.Medication, today: date) -> dict:
    return {
        "id": m.id, "cow_id": m.cow_id, "drug_id": m.drug_id,
        "drug_name": m.drug_name, "date": str(m.date), "dose": m.dose,
        "route": m.route, "reason": m.reason, "withdrawal_days": m.withdrawal_days,
        "withdrawal_end": str(m.withdrawal_end), "next_dose_date": str(m.next_dose_date) if m.next_dose_date else None,
        "treated": m.treated, "operator": m.operator, "note": m.note,
        "active_withdrawal": m.date <= today <= m.withdrawal_end,
    }


# ---------------- 奶牛档案 ----------------
@app.get("/api/cows", response_model=List[schemas.CowOut])
def list_cows(
    q: Optional[str] = None,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
):
    query = db.query(models.Cow)
    if q:
        like = f"%{q}%"
        query = query.filter(
            (models.Cow.ear_tag.like(like))
            | (models.Cow.name.like(like))
            | (models.Cow.group.like(like))
        )
    if status:
        if status not in VALID_STATUS:
            raise HTTPException(400, "非法状态")
        query = query.filter(models.Cow.status == status)
    return query.order_by(models.Cow.ear_tag.asc()).all()


@app.post("/api/cows", response_model=schemas.CowOut, status_code=201)
def create_cow(payload: schemas.CowCreate, db: Session = Depends(get_db)):
    if payload.status not in VALID_STATUS:
        raise HTTPException(400, "非法状态")
    if db.query(models.Cow).filter_by(ear_tag=payload.ear_tag).first():
        raise HTTPException(409, f"耳标号 {payload.ear_tag} 已存在")
    if payload.expected_calving_date and payload.calving_date and \
            payload.expected_calving_date <= payload.calving_date:
        raise HTTPException(400, "预产期应晚于产犊日期")
    cow = models.Cow(**payload.model_dump())
    db.add(cow)
    db.commit()
    db.refresh(cow)
    return cow


@app.get("/api/cows/{cow_id}")
def get_cow(cow_id: int, db: Session = Depends(get_db)):
    cow = db.get(models.Cow, cow_id)
    if not cow:
        raise HTTPException(404, "未找到该牛")
    return cow_detail(cow_id, db)


@app.patch("/api/cows/{cow_id}", response_model=schemas.CowOut)
def update_cow(cow_id: int, payload: schemas.CowUpdate, db: Session = Depends(get_db)):
    cow = db.get(models.Cow, cow_id)
    if not cow:
        raise HTTPException(404, "未找到该牛")
    data = payload.model_dump(exclude_unset=True)
    if "status" in data and data["status"] not in VALID_STATUS:
        raise HTTPException(400, "非法状态")
    for k, v in data.items():
        setattr(cow, k, v)
    db.commit()
    db.refresh(cow)
    return cow


@app.delete("/api/cows/{cow_id}", status_code=204)
def delete_cow(cow_id: int, db: Session = Depends(get_db)):
    cow = db.get(models.Cow, cow_id)
    if not cow:
        raise HTTPException(404, "未找到该牛")
    db.delete(cow)
    db.commit()


def cow_detail(cow_id: int, db: Session) -> dict:
    cow = db.get(models.Cow, cow_id)
    today = date.today()
    milkings = (
        db.query(models.MilkingRecord).filter_by(cow_id=cow_id)
        .order_by(models.MilkingRecord.date.desc(), models.MilkingRecord.id.desc())
        .limit(30).all()
    )
    health = (
        db.query(models.HealthRecord).filter_by(cow_id=cow_id)
        .order_by(models.HealthRecord.date.desc()).limit(20).all()
    )
    meds = (
        db.query(models.Medication).filter_by(cow_id=cow_id)
        .order_by(models.Medication.date.desc()).limit(20).all()
    )
    estruses = (
        db.query(models.EstrusRecord).filter_by(cow_id=cow_id)
        .order_by(models.EstrusRecord.date.desc()).limit(20).all()
    )
    anomaly_cases = (
        db.query(models.AnomalyCase).filter_by(cow_id=cow_id)
        .order_by(models.AnomalyCase.id.desc()).limit(10).all()
    )
    wd = services.check_withdrawal(db, cow_id, today)
    # 近7天每日产奶量（含废弃标记）
    trend = []
    for off in range(6, -1, -1):
        d = today - timedelta(days=off)
        rows = db.query(models.MilkingRecord).filter_by(cow_id=cow_id, date=d).all()
        trend.append({
            "date": str(d),
            "yield_kg": round(sum(r.yield_kg for r in rows if not r.discarded), 1),
            "discarded_kg": round(sum(r.yield_kg for r in rows if r.discarded), 1),
        })
    return {
        "id": cow.id, "ear_tag": cow.ear_tag, "name": cow.name, "breed": cow.breed,
        "birth_date": str(cow.birth_date), "parity": cow.parity, "status": cow.status,
        "status_label": services.STATUS_LABEL.get(cow.status, cow.status),
        "group": cow.group,
        "calving_date": str(cow.calving_date) if cow.calving_date else None,
        "expected_calving_date": str(cow.expected_calving_date) if cow.expected_calving_date else None,
        "days_in_milk": (today - cow.calving_date).days if cow.calving_date else None,
        "avg_yield_kg": cow.avg_yield_kg, "note": cow.note,
        "in_withdrawal": wd["in_withdrawal"],
        "withdrawal": {
            "drug_name": wd.get("drug_name"),
            "withdrawal_end": str(wd["withdrawal_end"]) if wd.get("withdrawal_end") else None,
            "message": wd.get("message"),
        } if wd["in_withdrawal"] else None,
        "milkings": [milking_to_dict(r, db) for r in milkings],
        "health": [{
            "id": h.id, "date": str(h.date), "record_type": h.record_type,
            "diagnosis": h.diagnosis, "temperature": h.temperature,
            "severity": h.severity, "follow_up_date": str(h.follow_up_date) if h.follow_up_date else None,
            "result": h.result, "note": h.note,
        } for h in health],
        "medications": [med_to_dict(m, today) for m in meds],
        "estruses": [{
            "id": e.id, "date": str(e.date), "detection": e.detection, "score": e.score,
            "inseminated": e.inseminated,
            "insemination_date": str(e.insemination_date) if e.insemination_date else None,
            "semen": e.semen, "technician": e.technician, "result": e.result,
            "result_date": str(e.result_date) if e.result_date else None, "note": e.note,
        } for e in estruses],
        "anomaly_cases": [services.case_to_dict(db, c) for c in anomaly_cases],
        "yield_trend": trend,
    }


# ---------------- 挤奶记录 ----------------
@app.get("/api/milkings")
def list_milkings(
    cow_id: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    only_violations: bool = False,
    limit: int = Query(200, le=1000),
    db: Session = Depends(get_db),
):
    query = db.query(models.MilkingRecord)
    if cow_id:
        query = query.filter_by(cow_id=cow_id)
    if date_from:
        query = query.filter(models.MilkingRecord.date >= date_from)
    if date_to:
        query = query.filter(models.MilkingRecord.date <= date_to)
    if only_violations:
        # 违规判定必须在 SQL 层过滤：若先 limit 再在内存筛，较早的违规会被漏掉
        query = query.filter(services.withdrawal_violation_clause())
    rows = query.order_by(
        models.MilkingRecord.date.desc(), models.MilkingRecord.id.desc()
    ).limit(limit).all()
    return annotate_milkings(rows, db)


def annotate_milkings(rows: list, db: Session) -> list:
    """批量补充牛只与休药期标注，避免逐行 N+1 查询"""
    if not rows:
        return []
    cow_ids = {r.cow_id for r in rows}
    cows = {c.id: c for c in db.query(models.Cow).filter(models.Cow.id.in_(cow_ids)).all()}

    min_d, max_d = min(r.date for r in rows), max(r.date for r in rows)
    meds = (
        db.query(models.Medication)
        .filter(
            models.Medication.cow_id.in_(cow_ids),
            models.Medication.withdrawal_days > 0,
            models.Medication.date <= max_d,
            models.Medication.withdrawal_end >= min_d,
        )
        .order_by(models.Medication.withdrawal_end.desc())
        .all()
    )
    # (cow_id) -> 与本批记录日期范围相交的用药区间
    by_cow: dict = {}
    for m in meds:
        by_cow.setdefault(m.cow_id, []).append(m)

    out = []
    for r in rows:
        cow = cows.get(r.cow_id)
        med = next(
            (m for m in by_cow.get(r.cow_id, [])
             if m.date <= r.date <= m.withdrawal_end),
            None,
        )
        in_w = med is not None
        out.append({
            "id": r.id, "cow_id": r.cow_id, "date": str(r.date), "session": r.session,
            "session_label": SESSION_LABEL.get(r.session, r.session),
            "yield_kg": r.yield_kg, "scc": r.scc, "discarded": r.discarded,
            "note": r.note, "created_at": str(r.created_at) if r.created_at else None,
            "cow_ear_tag": cow.ear_tag if cow else None,
            "cow_name": cow.name if cow else None,
            "in_withdrawal": in_w,
            "withdrawal_until": str(med.withdrawal_end) if med else None,
            "violation": bool(in_w and not r.discarded),
        })
    return out


@app.post("/api/milkings/check-withdrawal")
def milking_check(payload: dict, db: Session = Depends(get_db)):
    """录入前休药期校验"""
    try:
        cid, on = int(payload["cow_id"]), date.fromisoformat(payload["date"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "需要 cow_id 与 date(YYYY-MM-DD)")
    if not db.get(models.Cow, cid):
        raise HTTPException(404, "未找到该牛")
    chk = services.check_withdrawal(db, cid, on)
    if not chk["in_withdrawal"]:
        return {"in_withdrawal": False, "message": "该牛当日不在休药期内，可正常挤奶"}
    med = chk["medication"]
    return {
        "in_withdrawal": True,
        "withdrawal_end": str(chk["withdrawal_end"]),
        "drug_name": chk["drug_name"],
        "message": chk["message"],
        "medication_id": med.id,
    }


@app.post("/api/milkings", status_code=201)
def create_milking(payload: schemas.MilkingCreate, db: Session = Depends(get_db)):
    cow = db.get(models.Cow, payload.cow_id)
    if not cow:
        raise HTTPException(404, "未找到该牛")
    if cow.status != "lactating":
        raise HTTPException(400, f"该牛当前状态为{cow.status}，不能登记挤奶")
    dup = db.query(models.MilkingRecord).filter_by(
        cow_id=payload.cow_id, date=payload.date, session=payload.session
    ).first()
    if dup:
        raise HTTPException(409, "该牛当日该班次已有挤奶记录")

    chk = services.check_withdrawal(db, payload.cow_id, payload.date)
    warnings = []
    discarded = payload.discarded
    if chk["in_withdrawal"]:
        if not discarded:
            discarded = True  # 安全默认：休药期自动按废弃处理
        if not payload.discarded and discarded:
            warnings.append({
                "type": "auto_discard",
                "message": f"当日处于 {chk['drug_name']} 休药期（至 {chk['withdrawal_end']}），"
                           f"已自动标记为废弃奶",
            })
        else:
            warnings.append({"type": "withdrawal", "message": chk["message"]})
    rec = models.MilkingRecord(
        **payload.model_dump(exclude={"discarded"}), discarded=discarded
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    # 新奶量数据进入：立即对账异常调查单（可能触发开单/跟踪/消退证据）
    try:
        services.reconcile_anomaly_cases(db, source="milk_change")
    except Exception:
        logger.exception("挤奶登记后异常调查对账失败（不影响挤奶数据）")
    result = milking_to_dict(rec, db)
    result["warnings"] = warnings
    return result


@app.patch("/api/milkings/{rec_id}")
def update_milking(rec_id: int, payload: schemas.MilkingUpdate, db: Session = Depends(get_db)):
    rec = db.get(models.MilkingRecord, rec_id)
    if not rec:
        raise HTTPException(404, "未找到该记录")
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(rec, k, v)
    db.commit()
    db.refresh(rec)
    # 补改历史奶量：重新判断并以新证据记录，旧证据快照保持不变
    try:
        services.reconcile_anomaly_cases(db, source="milk_change")
    except Exception:
        logger.exception("补改挤奶记录后异常调查对账失败（不影响挤奶数据）")
    return milking_to_dict(rec, db)


@app.delete("/api/milkings/{rec_id}", status_code=204)
def delete_milking(rec_id: int, db: Session = Depends(get_db)):
    rec = db.get(models.MilkingRecord, rec_id)
    if not rec:
        raise HTTPException(404, "未找到该记录")
    db.delete(rec)
    db.commit()
    # 删除同样属于“补改”，重新对账（只追加新判断，不抹旧证据）
    try:
        services.reconcile_anomaly_cases(db, source="milk_change")
    except Exception:
        logger.exception("删除挤奶记录后异常调查对账失败（不影响挤奶数据）")


# ---------------- 健康记录 ----------------
@app.get("/api/health")
def list_health(cow_id: Optional[int] = None, db: Session = Depends(get_db)):
    query = db.query(models.HealthRecord)
    if cow_id:
        query = query.filter_by(cow_id=cow_id)
    rows = query.order_by(models.HealthRecord.date.desc()).limit(300).all()
    cows = {c.id: c for c in db.query(models.Cow).all()}
    return [{
        "id": h.id, "cow_id": h.cow_id, "date": str(h.date),
        "record_type": h.record_type, "diagnosis": h.diagnosis,
        "temperature": h.temperature, "severity": h.severity,
        "follow_up_date": str(h.follow_up_date) if h.follow_up_date else None,
        "result": h.result, "note": h.note,
        "cow_ear_tag": cows[h.cow_id].ear_tag if h.cow_id in cows else None,
        "cow_name": cows[h.cow_id].name if h.cow_id in cows else None,
    } for h in rows]


@app.post("/api/health", response_model=schemas.HealthOut, status_code=201)
def create_health(payload: schemas.HealthCreate, db: Session = Depends(get_db)):
    if not db.get(models.Cow, payload.cow_id):
        raise HTTPException(404, "未找到该牛")
    h = models.HealthRecord(**payload.model_dump())
    db.add(h)
    db.commit()
    db.refresh(h)
    return h


@app.patch("/api/health/{rec_id}", response_model=schemas.HealthOut)
def update_health(rec_id: int, payload: schemas.HealthUpdate, db: Session = Depends(get_db)):
    h = db.get(models.HealthRecord, rec_id)
    if not h:
        raise HTTPException(404, "未找到该记录")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(h, k, v)
    db.commit()
    db.refresh(h)
    return h


@app.delete("/api/health/{rec_id}", status_code=204)
def delete_health(rec_id: int, db: Session = Depends(get_db)):
    h = db.get(models.HealthRecord, rec_id)
    if not h:
        raise HTTPException(404, "未找到该记录")
    # 显式解除调查单关联（SQLite 默认不强制外键级联，避免悬挂关联）
    db.query(models.CaseHealthLink).filter_by(health_id=rec_id).delete()
    db.delete(h)
    db.commit()


# ---------------- 药品目录 ----------------
@app.get("/api/drugs", response_model=List[schemas.DrugOut])
def list_drugs(db: Session = Depends(get_db)):
    return db.query(models.DrugCatalog).filter_by(active=True).order_by(
        models.DrugCatalog.name.asc()
    ).all()


@app.post("/api/drugs", response_model=schemas.DrugOut, status_code=201)
def create_drug(payload: schemas.DrugCreate, db: Session = Depends(get_db)):
    if db.query(models.DrugCatalog).filter_by(name=payload.name).first():
        raise HTTPException(409, "药品已存在")
    d = models.DrugCatalog(**payload.model_dump())
    db.add(d)
    db.commit()
    db.refresh(d)
    return d


# ---------------- 用药记录 ----------------
@app.get("/api/medications")
def list_medications(cow_id: Optional[int] = None, db: Session = Depends(get_db)):
    query = db.query(models.Medication)
    if cow_id:
        query = query.filter_by(cow_id=cow_id)
    rows = query.order_by(models.Medication.date.desc()).limit(300).all()
    return [med_to_dict(m, date.today()) for m in rows]


@app.post("/api/medications", status_code=201)
def create_medication(payload: schemas.MedicationCreate, db: Session = Depends(get_db)):
    cow = db.get(models.Cow, payload.cow_id)
    if not cow:
        raise HTTPException(404, "未找到该牛")

    drug = None
    drug_name = payload.drug_name
    wd = payload.withdrawal_days
    if payload.drug_id:
        drug = db.get(models.DrugCatalog, payload.drug_id)
        if not drug:
            raise HTTPException(404, "未找到该药品")
        drug_name = drug.name
        if wd is None:
            wd = drug.default_withdrawal_days
    if not drug_name:
        raise HTTPException(400, "请选择药品或填写药品名称")
    if wd is None:
        wd = 0
    if payload.next_dose_date and payload.next_dose_date < payload.date:
        raise HTTPException(400, "下次用药日期不能早于本次用药日期")

    m = models.Medication(
        cow_id=payload.cow_id, drug_id=drug.id if drug else None,
        drug_name=drug_name, date=payload.date, dose=payload.dose,
        route=payload.route, reason=payload.reason, withdrawal_days=wd,
        withdrawal_end=payload.date + timedelta(days=wd),
        next_dose_date=payload.next_dose_date,
        treated=payload.next_dose_date is None,
        operator=payload.operator, note=payload.note,
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return med_to_dict(m, date.today())


@app.patch("/api/medications/{med_id}")
def update_medication(med_id: int, payload: schemas.MedicationUpdate, db: Session = Depends(get_db)):
    m = db.get(models.Medication, med_id)
    if not m:
        raise HTTPException(404, "未找到该记录")
    data = payload.model_dump(exclude_unset=True)
    if "withdrawal_days" in data:
        m.withdrawal_days = data.pop("withdrawal_days")
        m.withdrawal_end = m.date + timedelta(days=m.withdrawal_days)
    for k, v in data.items():
        setattr(m, k, v)
    db.commit()
    db.refresh(m)
    return med_to_dict(m, date.today())


@app.delete("/api/medications/{med_id}", status_code=204)
def delete_medication(med_id: int, db: Session = Depends(get_db)):
    m = db.get(models.Medication, med_id)
    if not m:
        raise HTTPException(404, "未找到该记录")
    db.delete(m)
    db.commit()


# ---------------- 发情/配种 ----------------
@app.get("/api/estruses")
def list_estruses(cow_id: Optional[int] = None, db: Session = Depends(get_db)):
    query = db.query(models.EstrusRecord)
    if cow_id:
        query = query.filter_by(cow_id=cow_id)
    rows = query.order_by(models.EstrusRecord.date.desc()).limit(300).all()
    cows = {c.id: c for c in db.query(models.Cow).all()}
    return [{
        "id": e.id, "cow_id": e.cow_id, "date": str(e.date),
        "detection": e.detection, "score": e.score,
        "inseminated": e.inseminated,
        "insemination_date": str(e.insemination_date) if e.insemination_date else None,
        "semen": e.semen, "technician": e.technician, "result": e.result,
        "result_date": str(e.result_date) if e.result_date else None, "note": e.note,
        "cow_ear_tag": cows[e.cow_id].ear_tag if e.cow_id in cows else None,
        "cow_name": cows[e.cow_id].name if e.cow_id in cows else None,
    } for e in rows]


@app.post("/api/estruses", response_model=schemas.EstrusOut, status_code=201)
def create_estrus(payload: schemas.EstrusCreate, db: Session = Depends(get_db)):
    if not db.get(models.Cow, payload.cow_id):
        raise HTTPException(404, "未找到该牛")
    if payload.insemination_date and payload.insemination_date < payload.date:
        raise HTTPException(400, "配种日期不能早于发情日期")
    e = models.EstrusRecord(**payload.model_dump())
    if e.inseminated and not e.result:
        e.result = "pending"
    db.add(e)
    db.commit()
    db.refresh(e)
    return e


@app.patch("/api/estruses/{rec_id}", response_model=schemas.EstrusOut)
def update_estrus(rec_id: int, payload: schemas.EstrusUpdate, db: Session = Depends(get_db)):
    e = db.get(models.EstrusRecord, rec_id)
    if not e:
        raise HTTPException(404, "未找到该记录")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(e, k, v)
    db.commit()
    db.refresh(e)
    return e


@app.delete("/api/estruses/{rec_id}", status_code=204)
def delete_estrus(rec_id: int, db: Session = Depends(get_db)):
    e = db.get(models.EstrusRecord, rec_id)
    if not e:
        raise HTTPException(404, "未找到该记录")
    db.delete(e)
    db.commit()


# ---------------- 提醒 / 异常 / 仪表盘 ----------------
@app.get("/api/reminders")
def get_reminders(db: Session = Depends(get_db)):
    return services.build_reminders(db)


@app.get("/api/anomalies")
def get_anomalies(days: int = Query(7, ge=1, le=30), db: Session = Depends(get_db)):
    return services.detect_yield_anomalies(db, days=days)


# ---------------- 奶量异常调查单 ----------------
def _get_case_or_404(db: Session, case_id: int) -> models.AnomalyCase:
    case = db.get(models.AnomalyCase, case_id)
    if not case:
        raise HTTPException(404, "未找到该调查单")
    return case


@app.get("/api/anomaly-cases")
def list_anomaly_cases(
    status: Optional[str] = None,
    cow_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    """异常调查单列表；先例行对账（发现新异常/跟踪变严重），保证状态最新"""
    services.reconcile_anomaly_cases(db)
    query = db.query(models.AnomalyCase)
    if status == "open":
        query = query.filter(models.AnomalyCase.status.in_(services.OPEN_STATUSES))
    elif status in services.CLOSED_STATUSES:
        query = query.filter(models.AnomalyCase.status == status)
    elif status:
        raise HTTPException(400, "非法状态筛选")
    if cow_id:
        query = query.filter_by(cow_id=cow_id)
    rows = query.order_by(
        # 进行中（open/reopened）排在已关闭之前
        case_status_order(models.AnomalyCase.status),
        models.AnomalyCase.latest_evidence_date.desc(),
        models.AnomalyCase.id.desc(),
    ).all()
    return [services.case_to_dict(db, c) for c in rows]


@app.get("/api/anomaly-cases/{case_id}")
def get_anomaly_case(case_id: int, db: Session = Depends(get_db)):
    case = _get_case_or_404(db, case_id)
    out = services.case_to_dict(db, case, detail=True)
    # 详情页同时给出当前最新判断（基于最新挤奶数据实时计算，供与冻结证据对照）
    cur = {
        a["cow_id"]: a for a in services.detect_yield_anomalies(db, days=7)
    }.get(case.cow_id)
    out["current"] = cur
    return out


@app.post("/api/anomaly-cases", status_code=201)
def create_anomaly_case(payload: dict, db: Session = Depends(get_db)):
    """手动开单（如外部检查发现、系统窗口之外的异常）"""
    cow_id = payload.get("cow_id")
    cow = db.get(models.Cow, cow_id) if cow_id else None
    if not cow:
        raise HTTPException(404, "未找到该牛")
    existing = (
        db.query(models.AnomalyCase)
        .filter_by(cow_id=cow_id)
        .filter(models.AnomalyCase.status.in_(services.OPEN_STATUSES))
        .first()
    )
    if existing:
        raise HTTPException(409, f"该牛已有进行中的调查单 #{existing.id}，请直接跟踪处理")
    today = date.today()
    cur = {a["cow_id"]: a for a in services.detect_yield_anomalies(db, days=7)}.get(cow_id)
    if cur:
        case = services._create_case(db, cow_id, cur, today)
    else:
        note = payload.get("note") or "人工开单（当前窗口内未触发自动检测）"
        level = payload.get("level") if payload.get("level") in ("danger", "warning", "info") else "info"
        tags = payload.get("tags") if isinstance(payload.get("tags"), list) else []
        tags = [t for t in tags if t in services.TAG_LABELS]
        case = models.AnomalyCase(
            cow_id=cow_id, status="open", detected_on=today,
            first_level=level, first_tags=",".join(tags),
            latest_level=level, latest_tags=",".join(tags),
            latest_evidence_date=today,
        )
        db.add(case)
        db.flush()
        db.add(models.AnomalyEvidence(
            case_id=case.id, kind="detect", eval_date=today, level=level,
            tags=",".join(tags), note=note, operator=payload.get("operator"),
            snapshot=json.dumps({"manual": True}, ensure_ascii=False),
        ))
        services._auto_link_health(db, case, today)
    db.commit()
    db.refresh(case)
    return services.case_to_dict(db, case, detail=True)


@app.patch("/api/anomaly-cases/{case_id}")
def update_anomaly_case(case_id: int, payload: schemas.CaseUpdate, db: Session = Depends(get_db)):
    case = _get_case_or_404(db, case_id)
    data = payload.model_dump(exclude_unset=True)
    if "follow_up_date" in data and data["follow_up_date"] is not None \
            and data["follow_up_date"] < case.detected_on:
        raise HTTPException(400, "复查日期不能早于发现日期")
    for k, v in data.items():
        setattr(case, k, v)
    db.commit()
    db.refresh(case)
    return services.case_to_dict(db, case)


@app.post("/api/anomaly-cases/{case_id}/recheck")
def recheck_anomaly_case(case_id: int, payload: schemas.CaseRecheck,
                         db: Session = Depends(get_db)):
    """人工复查：记录复查结论（正常/仍异常/继续观察），这是关闭前必须留下的依据"""
    case = _get_case_or_404(db, case_id)
    if case.status not in services.OPEN_STATUSES:
        raise HTTPException(400, "调查单已关闭，如需继续跟踪请先重开")
    eval_date = payload.eval_date or date.today()
    if payload.result == "normal":
        level = "ok"
    elif payload.level:
        level = payload.level
    else:
        level = case.latest_level if case.latest_level != "ok" else "info"
    tags = case.latest_tags if payload.result == "abnormal" else None
    cur = {a["cow_id"]: a for a in services.detect_yield_anomalies(db, days=7)}.get(case.cow_id)
    snapshot = None
    if payload.result == "abnormal" and cur:
        snapshot = json.dumps(cur, ensure_ascii=False)
    result_notes = {"normal": "复查结果：已恢复正常", "abnormal": "复查结果：异常仍存在",
                    "observed": "复查结果：继续观察"}
    case.latest_level = level
    case.latest_tags = tags
    case.latest_evidence_date = eval_date
    if payload.follow_up_date:
        case.follow_up_date = payload.follow_up_date
    db.add(models.AnomalyEvidence(
        case_id=case.id, kind="recheck", eval_date=eval_date, level=level,
        tags=tags, snapshot=snapshot,
        note=result_notes[payload.result] + ("——" + payload.note if payload.note else ""),
        operator=payload.operator,
    ))
    db.commit()
    db.refresh(case)
    return services.case_to_dict(db, case, detail=True)


@app.post("/api/anomaly-cases/{case_id}/close")
def close_anomaly_case(case_id: int, payload: schemas.CaseClose, db: Session = Depends(get_db)):
    """关闭调查：必须先有人工复查记录；误报必须注明原因"""
    case = _get_case_or_404(db, case_id)
    if case.status not in services.OPEN_STATUSES:
        raise HTTPException(400, "调查单已关闭")
    has_manual = (
        db.query(models.AnomalyEvidence)
        .filter_by(case_id=case.id, kind="recheck")
        .count()
    )
    if not has_manual:
        raise HTTPException(400, "请先提交一次人工复查，再关闭调查单")
    reason = (payload.false_reason or "").strip()
    if payload.outcome == "false_positive" and not reason:
        raise HTTPException(400, "判定为误报时必须注明误报原因")
    today = date.today()
    case.status = payload.outcome
    case.false_reason = reason or None
    case.close_note = payload.close_note
    case.closed_at = today
    case.latest_level = "ok"
    case.latest_tags = None
    case.latest_evidence_date = today
    label = "确认恢复，结案" if payload.outcome == "resolved" else "判定为误报，结案"
    db.add(models.AnomalyEvidence(
        case_id=case.id, kind="close", eval_date=today, level="ok",
        note=label + (f"：{payload.close_note}" if payload.close_note else "")
             + (f"；误报原因：{reason}" if reason else ""),
        operator=payload.operator,
    ))
    db.commit()
    db.refresh(case)
    return services.case_to_dict(db, case, detail=True)


@app.post("/api/anomaly-cases/{case_id}/reopen")
def reopen_anomaly_case(case_id: int, payload: schemas.CaseReopen,
                        db: Session = Depends(get_db)):
    """误关/关早了：重开原单继续跟踪（区别于恢复后新异常另开新单）"""
    case = _get_case_or_404(db, case_id)
    if case.status in services.OPEN_STATUSES:
        raise HTTPException(400, "调查单进行中，无需重开")
    today = date.today()
    case.status = "reopened"
    case.closed_at = None
    case.close_note = None
    case.false_reason = None
    if payload.follow_up_date:
        case.follow_up_date = payload.follow_up_date
    cur = {a["cow_id"]: a for a in services.detect_yield_anomalies(db, days=7)}.get(case.cow_id)
    if cur:
        case.latest_level = cur["level"]
        case.latest_tags = ",".join(cur["tags"])
        snapshot = json.dumps(cur, ensure_ascii=False)
        detail = "；".join(cur["reasons"])
    else:
        case.latest_level = "ok"
        case.latest_tags = None
        snapshot = None
        detail = "当前窗口未触发自动检测"
    case.latest_evidence_date = today
    db.add(models.AnomalyEvidence(
        case_id=case.id, kind="reopen", eval_date=today,
        level=case.latest_level, tags=case.latest_tags,
        snapshot=snapshot,
        note="调查单重新开启" + ("——" + payload.note if payload.note else "——") + detail,
    ))
    db.commit()
    db.refresh(case)
    return services.case_to_dict(db, case, detail=True)


@app.post("/api/anomaly-cases/{case_id}/health-links", status_code=201)
def link_anomaly_health(case_id: int, payload: schemas.HealthLink,
                        db: Session = Depends(get_db)):
    case = _get_case_or_404(db, case_id)
    h = db.get(models.HealthRecord, payload.health_id)
    if not h:
        raise HTTPException(404, "未找到该健康记录")
    if h.cow_id != case.cow_id:
        raise HTTPException(400, "只能关联同一头牛的健康记录")
    exists_link = (
        db.query(models.CaseHealthLink)
        .filter_by(case_id=case.id, health_id=h.id)
        .first()
    )
    if exists_link:
        raise HTTPException(409, "该健康记录已关联")
    db.add(models.CaseHealthLink(case_id=case.id, health_id=h.id))
    db.commit()
    return services.case_to_dict(db, case, detail=True)


@app.delete("/api/anomaly-cases/{case_id}/health-links/{health_id}", status_code=204)
def unlink_anomaly_health(case_id: int, health_id: int, db: Session = Depends(get_db)):
    case = _get_case_or_404(db, case_id)
    link = (
        db.query(models.CaseHealthLink)
        .filter_by(case_id=case.id, health_id=health_id)
        .first()
    )
    if not link:
        raise HTTPException(404, "未找到该关联")
    db.delete(link)
    db.commit()


@app.get("/api/dashboard")
def dashboard(db: Session = Depends(get_db)):
    today = date.today()
    yesterday = today - timedelta(days=1)
    cows = db.query(models.Cow).all()
    active = [c for c in cows if c.status != "sold"]
    by_status = defaultdict(int)
    for c in cows:
        by_status[c.status] += 1

    def day_milk(d: date):
        rows = db.query(models.MilkingRecord).filter_by(date=d).all()
        valid = [r for r in rows if not r.discarded]
        discard = [r for r in rows if r.discarded]
        cows_milked = len({r.cow_id for r in valid})
        total = sum(r.yield_kg for r in valid)
        return {
            "total_kg": round(total, 1),
            "avg_kg": round(total / cows_milked, 1) if cows_milked else 0,
            "cows_milked": cows_milked,
            "discarded_kg": round(sum(r.yield_kg for r in discard), 1),
        }

    trend = []
    for off in range(13, -1, -1):
        d = today - timedelta(days=off)
        trend.append({"date": str(d), **day_milk(d)})

    reminders = services.build_reminders(db, today)
    # 工作台异常以“调查单”为准：例行对账后统计进行中/高风险/待复查
    services.reconcile_anomaly_cases(db, today=today)
    open_cases = db.query(models.AnomalyCase).filter(
        models.AnomalyCase.status.in_(services.OPEN_STATUSES)
    ).all()
    anomaly_open = len(open_cases)
    anomaly_danger = sum(1 for c in open_cases if c.latest_level == "danger")
    anomaly_waiting_close = sum(
        1 for c in open_cases
        if (c.follow_up_date and c.follow_up_date <= today + timedelta(days=3))
        or c.latest_level == "ok"
    )
    violation_count = (
        db.query(models.MilkingRecord)
        .filter(services.withdrawal_violation_clause())
        .count()
    )
    cows_in_withdrawal = (
        db.query(models.Medication.cow_id)
        .filter(
            models.Medication.withdrawal_days > 0,
            models.Medication.date <= today,
            models.Medication.withdrawal_end >= today,
        )
        .distinct().count()
    )

    return {
        "today": str(today),
        "cows_total": len(cows),
        "cows_active": len(active),
        "by_status": dict(by_status),
        "today_milk": day_milk(today),
        "yesterday_milk": day_milk(yesterday),
        "trend_14d": trend,
        "reminder_count": len(reminders),
        "reminder_danger": sum(1 for r in reminders if r["level"] == "danger"),
        "anomaly_count": anomaly_open,
        "anomaly_danger": anomaly_danger,
        "anomaly_waiting_close": anomaly_waiting_close,
        "violation_count": violation_count,
        "cows_in_withdrawal": cows_in_withdrawal,
    }


# ---------------- 静态前端 ----------------
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
