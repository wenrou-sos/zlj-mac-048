"""FastAPI 入口：牧场管理系统 API"""
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from . import audit, impact, models, schemas, services
from .database import Base, engine, get_db
from .seed import init_db

Base.metadata.create_all(bind=engine)
audit.run_migrations()
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
        "version": r.version, "is_void": r.is_void,
        "voided_at": r.voided_at.isoformat() if r.voided_at else None,
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
        "active_withdrawal": (not m.is_void) and m.date <= today <= m.withdrawal_end,
        "version": m.version, "is_void": m.is_void,
        "voided_at": m.voided_at.isoformat() if m.voided_at else None,
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
        db.query(models.MilkingRecord)
        .filter_by(cow_id=cow_id)
        .filter(models.MilkingRecord.is_void.is_(False))
        .order_by(models.MilkingRecord.date.desc(), models.MilkingRecord.id.desc())
        .limit(30).all()
    )
    health = (
        db.query(models.HealthRecord)
        .filter_by(cow_id=cow_id)
        .filter(models.HealthRecord.is_void.is_(False))
        .order_by(models.HealthRecord.date.desc()).limit(20).all()
    )
    meds = (
        db.query(models.Medication)
        .filter_by(cow_id=cow_id)
        .filter(models.Medication.is_void.is_(False))
        .order_by(models.Medication.date.desc()).limit(20).all()
    )
    estruses = (
        db.query(models.EstrusRecord).filter_by(cow_id=cow_id)
        .order_by(models.EstrusRecord.date.desc()).limit(20).all()
    )
    wd = services.check_withdrawal(db, cow_id, today)
    # 近7天每日产奶量（含废弃标记）
    trend = []
    for off in range(6, -1, -1):
        d = today - timedelta(days=off)
        rows = (db.query(models.MilkingRecord)
                .filter_by(cow_id=cow_id, date=d)
                .filter(models.MilkingRecord.is_void.is_(False)).all())
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
    query = db.query(models.MilkingRecord).filter(models.MilkingRecord.is_void.is_(False))
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
            models.Medication.is_void.is_(False),
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
            "version": r.version, "is_void": r.is_void,
            "voided_at": r.voided_at.isoformat() if r.voided_at else None,
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
    dup = db.query(models.MilkingRecord).filter(
        models.MilkingRecord.cow_id == payload.cow_id,
        models.MilkingRecord.date == payload.date,
        models.MilkingRecord.session == payload.session,
        models.MilkingRecord.is_void.is_(False),
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
    db.flush()
    audit.log_create(db, "milking", rec)
    db.commit()
    db.refresh(rec)
    result = milking_to_dict(rec, db)
    result["warnings"] = warnings
    return result


@app.patch("/api/milkings/{rec_id}")
def update_milking(rec_id: int, payload: schemas.MilkingCorrect, db: Session = Depends(get_db)):
    """更正挤奶记录：记录操作人、原因及前后内容，版本号+1"""
    rec = db.get(models.MilkingRecord, rec_id)
    if not rec:
        raise HTTPException(404, "未找到该记录")
    changes = payload.model_dump(exclude={"operator", "reason", "expected_version"},
                                 exclude_unset=True)
    # 日期/班次不允许通过更正修改（会改变业务归属），需作废后重新登记
    changes.pop("date", None)
    changes.pop("cow_id", None)
    audit.correct(db, "milking", rec_id, changes,
                  payload.operator, payload.reason, payload.expected_version)
    db.refresh(rec)
    return milking_to_dict(rec, db)


@app.delete("/api/milkings/{rec_id}", status_code=204)
def delete_milking(rec_id: int, payload: schemas.AuditAction, db: Session = Depends(get_db)):
    """作废挤奶记录（软删除，可在版本历史中恢复）"""
    audit.void_record(db, "milking", rec_id, payload.operator, payload.reason,
                      payload.expected_version)


# ---------------- 健康记录 ----------------
@app.get("/api/health")
def list_health(cow_id: Optional[int] = None, include_void: bool = False,
                db: Session = Depends(get_db)):
    query = db.query(models.HealthRecord)
    if cow_id:
        query = query.filter_by(cow_id=cow_id)
    if not include_void:
        query = query.filter(models.HealthRecord.is_void.is_(False))
    rows = query.order_by(models.HealthRecord.date.desc()).limit(300).all()
    cows = {c.id: c for c in db.query(models.Cow).all()}
    return [{
        "id": h.id, "cow_id": h.cow_id, "date": str(h.date),
        "record_type": h.record_type, "diagnosis": h.diagnosis,
        "temperature": h.temperature, "severity": h.severity,
        "follow_up_date": str(h.follow_up_date) if h.follow_up_date else None,
        "result": h.result, "note": h.note,
        "version": h.version, "is_void": h.is_void,
        "voided_at": h.voided_at.isoformat() if h.voided_at else None,
        "cow_ear_tag": cows[h.cow_id].ear_tag if h.cow_id in cows else None,
        "cow_name": cows[h.cow_id].name if h.cow_id in cows else None,
    } for h in rows]


@app.post("/api/health", response_model=schemas.HealthOut, status_code=201)
def create_health(payload: schemas.HealthCreate, db: Session = Depends(get_db)):
    if not db.get(models.Cow, payload.cow_id):
        raise HTTPException(404, "未找到该牛")
    h = models.HealthRecord(**payload.model_dump())
    db.add(h)
    db.flush()
    audit.log_create(db, "health", h)
    db.commit()
    db.refresh(h)
    return h


@app.patch("/api/health/{rec_id}")
def update_health(rec_id: int, payload: schemas.HealthCorrect, db: Session = Depends(get_db)):
    h = db.get(models.HealthRecord, rec_id)
    if not h:
        raise HTTPException(404, "未找到该记录")
    changes = payload.model_dump(exclude={"operator", "reason", "expected_version"},
                                 exclude_unset=True)
    audit.correct(db, "health", rec_id, changes,
                  payload.operator, payload.reason, payload.expected_version)
    db.refresh(h)
    return {
        "id": h.id, "cow_id": h.cow_id, "date": str(h.date),
        "record_type": h.record_type, "diagnosis": h.diagnosis,
        "temperature": h.temperature, "severity": h.severity,
        "follow_up_date": str(h.follow_up_date) if h.follow_up_date else None,
        "result": h.result, "note": h.note,
        "version": h.version, "is_void": h.is_void,
    }


@app.delete("/api/health/{rec_id}", status_code=204)
def delete_health(rec_id: int, payload: schemas.AuditAction, db: Session = Depends(get_db)):
    audit.void_record(db, "health", rec_id, payload.operator, payload.reason,
                      payload.expected_version)


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
def list_medications(cow_id: Optional[int] = None, include_void: bool = False,
                     db: Session = Depends(get_db)):
    query = db.query(models.Medication)
    if cow_id:
        query = query.filter_by(cow_id=cow_id)
    if not include_void:
        query = query.filter(models.Medication.is_void.is_(False))
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
    db.flush()
    audit.log_create(db, "medication", m, payload.operator)
    db.commit()
    db.refresh(m)
    return med_to_dict(m, date.today())


@app.patch("/api/medications/{med_id}")
def update_medication(med_id: int, payload: schemas.MedicationCorrect,
                      db: Session = Depends(get_db)):
    """更正用药记录：休药截止日随用药日/休药天数自动重算"""
    m = db.get(models.Medication, med_id)
    if not m:
        raise HTTPException(404, "未找到该记录")
    raw = payload.model_dump(exclude_unset=True)
    data = {k: v for k, v in raw.items()
            if k not in ("audit_operator", "audit_reason", "expected_version")}

    # 改选药品目录时同步药名与默认休药期
    if data.get("drug_id") is not None:
        drug = db.get(models.DrugCatalog, data["drug_id"])
        if not drug:
            raise HTTPException(404, "未找到该药品")
        data["drug_name"] = drug.name
        data.setdefault("withdrawal_days", drug.default_withdrawal_days)
    if "drug_name" in data and not data["drug_name"]:
        raise HTTPException(400, "药品名称不能为空")

    new_date = data.get("date") or m.date
    new_next = data.get("next_dose_date", m.next_dose_date)
    if new_next and new_next < new_date:
        raise HTTPException(400, "下次用药日期不能早于本次用药日期")

    audit.correct(db, "medication", med_id, data,
                  payload.audit_operator, payload.audit_reason,
                  payload.expected_version)
    db.refresh(m)
    return med_to_dict(m, date.today())


@app.delete("/api/medications/{med_id}", status_code=204)
def delete_medication(med_id: int, payload: schemas.AuditAction, db: Session = Depends(get_db)):
    audit.void_record(db, "medication", med_id, payload.operator, payload.reason,
                      payload.expected_version)


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


# ---------------- 版本化审计：历史 / 作废 / 恢复 / 撤销 / 影响预览 ----------------
VALID_ENTITY = set(audit.ENTITY_FIELDS.keys())


def _validate_entity(entity_type: str):
    if entity_type not in VALID_ENTITY:
        raise HTTPException(400, f"不支持的记录类型：{entity_type}")


def _milking_restore_conflicts(db: Session, entity_id: int) -> list:
    """恢复/撤销挤奶记录时，检查同牛同日同班是否已存在另一条有效记录"""
    rec = db.get(models.MilkingRecord, entity_id)
    if not rec:
        raise HTTPException(404, "未找到该记录")
    clash = (
        db.query(models.MilkingRecord)
        .filter(
            models.MilkingRecord.cow_id == rec.cow_id,
            models.MilkingRecord.date == rec.date,
            models.MilkingRecord.session == rec.session,
            models.MilkingRecord.is_void.is_(False),
            models.MilkingRecord.id != rec.id,
        ).first()
    )
    if clash:
        return [{
            "type": "duplicate_milking",
            "message": f"{rec.date} {SESSION_LABEL.get(rec.session, rec.session)}"
                       f"已有另一条有效记录（#{clash.id}，{clash.yield_kg}kg），"
                       f"不能直接恢复，请先处理现有记录",
            "conflict_id": clash.id,
        }]
    return []


@app.get("/api/audit/{entity_type}/{entity_id}/history")
def get_history(entity_type: str, entity_id: int, db: Session = Depends(get_db)):
    _validate_entity(entity_type)
    obj = db.get(audit.ENTITY_FIELDS[entity_type]["model"], entity_id)
    if not obj:
        raise HTTPException(404, "未找到该记录")
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "current_version": obj.version,
        "is_void": obj.is_void,
        "voided_at": obj.voided_at.isoformat() if obj.voided_at else None,
        "logs": audit.history(db, entity_type, entity_id),
    }


@app.post("/api/audit/{entity_type}/{entity_id}/void", status_code=204)
def void_entity(entity_type: str, entity_id: int, payload: schemas.AuditAction,
                db: Session = Depends(get_db)):
    _validate_entity(entity_type)
    audit.void_record(db, entity_type, entity_id, payload.operator, payload.reason,
                      payload.expected_version)


@app.post("/api/audit/{entity_type}/{entity_id}/restore")
def restore_entity(entity_type: str, entity_id: int, payload: schemas.AuditAction,
                   db: Session = Depends(get_db)):
    """恢复已作废记录。挤奶记录恢复前做班次唯一冲突校验，冲突则整单失败。"""
    _validate_entity(entity_type)
    conflicts = []
    if entity_type == "milking":
        conflicts = _milking_restore_conflicts(db, entity_id)
    obj = audit.restore_record(db, entity_type, entity_id,
                               payload.operator, payload.reason,
                               payload.expected_version, conflicts)
    return {"id": obj.id, "version": obj.version, "is_void": obj.is_void, "conflicts": []}


@app.post("/api/audit/{entity_type}/{entity_id}/revert")
def revert_entity(entity_type: str, entity_id: int, payload: schemas.RevertRequest,
                  db: Session = Depends(get_db)):
    """
    撤销更正：以 target_version 的内容生成新版本。
    后续已有改动（版本号不匹配）或缺奶班次冲突时拒绝，不做任何部分写入。
    """
    _validate_entity(entity_type)
    # 撤销后会覆盖当前内容，先用目标版本快照在探针事务里做冲突预检
    prev = impact.preview(db, entity_type, entity_id, "undo",
                          target_version=payload.target_version)
    if prev["conflicts"]:
        raise HTTPException(409, {"message": "撤销后与现有记录冲突",
                                  "conflicts": prev["conflicts"]})
    obj = audit.revert_to_version(db, entity_type, entity_id,
                                  payload.target_version, payload.operator,
                                  payload.reason, payload.expected_version)
    return {"id": obj.id, "version": obj.version, "is_void": obj.is_void,
            "impact": prev["deltas"]}


@app.post("/api/audit/{entity_type}/{entity_id}/impact/{action}")
def impact_preview(entity_type: str, entity_id: int, action: str,
                   payload: Optional[schemas.ImpactCorrectRequest] = None,
                   db: Session = Depends(get_db)):
    """操作前影响预览（不写库）：奶量统计 / 休药校验 / 提醒变化"""
    _validate_entity(entity_type)
    if action not in ("correct", "void", "restore", "undo"):
        raise HTTPException(400, "action 仅支持 correct/void/restore/undo")
    changes = (payload.changes if payload else {}) or {}
    target_version = changes.pop("target_version", None)
    if not target_version and action == "undo":
        raise HTTPException(400, "撤销更正预览需提供 target_version")
    return impact.preview(db, entity_type, entity_id, action,
                          changes=changes, target_version=target_version)


@app.get("/api/cows/{cow_id}/timeline")
def get_cow_timeline(cow_id: int, db: Session = Depends(get_db)):
    """沿牛只时间线查看挤奶/健康/用药记录的历次版本（含已作废）"""
    if not db.get(models.Cow, cow_id):
        raise HTTPException(404, "未找到该牛")
    return {"cow_id": cow_id, "items": audit.cow_timeline(db, cow_id)}


# ---------------- 提醒 / 异常 / 仪表盘 ----------------
@app.get("/api/reminders")
def get_reminders(db: Session = Depends(get_db)):
    return services.build_reminders(db)


@app.get("/api/anomalies")
def get_anomalies(days: int = Query(7, ge=1, le=30), db: Session = Depends(get_db)):
    return services.detect_yield_anomalies(db, days=days)


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
        rows = (db.query(models.MilkingRecord)
                .filter_by(date=d)
                .filter(models.MilkingRecord.is_void.is_(False)).all())
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
    anomalies = services.detect_yield_anomalies(db, days=7, today=today)
    violation_count = (
        db.query(models.MilkingRecord)
        .filter(services.withdrawal_violation_clause())
        .count()
    )
    cows_in_withdrawal = (
        db.query(models.Medication.cow_id)
        .filter(
            models.Medication.withdrawal_days > 0,
            models.Medication.is_void.is_(False),
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
        "anomaly_count": len(anomalies),
        "violation_count": violation_count,
        "cows_in_withdrawal": cows_in_withdrawal,
    }


# ---------------- 静态前端 ----------------
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
