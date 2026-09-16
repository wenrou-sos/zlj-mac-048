"""FastAPI 入口：牧场管理系统 API"""
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from . import models, schemas, services
from .database import Base, engine, get_db
from .inventory import StockError, VOUCHER_LABEL
from . import inventory as inv
from .seed import init_db

Base.metadata.create_all(bind=engine)
init_db()

app = FastAPI(title="牧场管理系统 API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(StockError)
def stock_error_handler(request, exc: StockError):
    """库存规则冲突（过期/不足/超退等）统一 400 返回，事务已随依赖回滚"""
    return JSONResponse(status_code=400, content={"detail": str(exc)})

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
        "cow_ear_tag": cow.ear_tag if cow else None,
        "cow_name": cow.name if cow else None,
        "in_withdrawal": in_w,
        "withdrawal_until": str(until) if until else None,
        "violation": bool(in_w and not r.discarded),
    }


def med_to_dict(m: models.Medication, today: date, db: Optional[Session] = None) -> dict:
    issue_batches = None
    if m.voucher_id and db is not None:
        v = db.get(models.StockVoucher, m.voucher_id)
        if v:
            issue_batches = [{
                "batch_no": ln.batch.batch_no,
                "expiry_date": str(ln.batch.expiry_date),
                "qty": ln.qty,
            } for ln in v.lines]
    return {
        "id": m.id, "cow_id": m.cow_id, "drug_id": m.drug_id,
        "drug_name": m.drug_name, "date": str(m.date), "dose": m.dose,
        "route": m.route, "reason": m.reason, "withdrawal_days": m.withdrawal_days,
        "withdrawal_end": str(m.withdrawal_end), "next_dose_date": str(m.next_dose_date) if m.next_dose_date else None,
        "treated": m.treated, "operator": m.operator, "note": m.note,
        "voucher_id": m.voucher_id,
        "voucher_no": m.voucher.voucher_no if m.voucher_id and m.voucher else None,
        "issue_batches": issue_batches,
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
        "medications": [med_to_dict(m, today, db) for m in meds],
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
    return milking_to_dict(rec, db)


@app.delete("/api/milkings/{rec_id}", status_code=204)
def delete_milking(rec_id: int, db: Session = Depends(get_db)):
    rec = db.get(models.MilkingRecord, rec_id)
    if not rec:
        raise HTTPException(404, "未找到该记录")
    db.delete(rec)
    db.commit()


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


# ---------------- 药品库存（批号/效期） ----------------
@app.get("/api/inventory/batches")
def list_batches(
    drug_id: Optional[int] = None,
    status_filter: str = Query("", alias="status"),
    include_empty: bool = True,
    db: Session = Depends(get_db),
):
    """批次库存列表：status=available/near_expiry/expired/out"""
    today = date.today()
    q = db.query(models.DrugBatch)
    if drug_id:
        q = q.filter(models.DrugBatch.drug_id == drug_id)
    rows = q.order_by(
        models.DrugBatch.drug_id.asc(),
        models.DrugBatch.expiry_date.asc(),
    ).all()
    out = [inv.batch_to_dict(b, today) for b in rows]
    if status_filter:
        out = [x for x in out if x["status"] == status_filter]
    if not include_empty:
        out = [x for x in out if x["qty_ok"] > 0 or x["qty_quarantine"] > 0]
    return out


@app.get("/api/inventory/batches/{batch_id}")
def get_batch(batch_id: int, db: Session = Depends(get_db)):
    b = db.get(models.DrugBatch, batch_id)
    if not b:
        raise HTTPException(404, "未找到该批次")
    return inv.batch_to_dict(b, date.today())


@app.get("/api/inventory/available")
def list_available(drug_id: int, on_date: Optional[date] = None,
                   db: Session = Depends(get_db)):
    """某药品当前可发批次（FEFO 排序），供领药/用药前端选择"""
    on_date = on_date or date.today()
    rows = inv.available_batches(db, drug_id, on_date)
    return [inv.batch_to_dict(b, on_date) for b in rows]


@app.get("/api/inventory/suggest")
def suggest_allocation(drug_id: int, qty: float = Query(..., gt=0),
                       on_date: Optional[date] = None, db: Session = Depends(get_db)):
    """FEFO 自动凑量建议（支持跨批次）"""
    on_date = on_date or date.today()
    if not db.get(models.DrugCatalog, drug_id):
        raise HTTPException(404, "未找到该药品")
    return inv.suggest_issue(db, drug_id, qty, on_date)


@app.post("/api/inventory/receipts", status_code=201)
def create_receipt(payload: schemas.ReceiptCreate, db: Session = Depends(get_db)):
    v = inv.create_receipt(
        db, drug_id=payload.drug_id, batch_no=payload.batch_no.strip(),
        expiry_date=payload.expiry_date, qty=payload.qty,
        voucher_date=payload.voucher_date or date.today(),
        supplier=payload.supplier, operator=payload.operator, note=payload.note,
    )
    db.commit()
    db.refresh(v)
    return inv.voucher_to_dict(v, db)


@app.post("/api/inventory/issues", status_code=201)
def create_issue(payload: schemas.IssueCreate, db: Session = Depends(get_db)):
    """领用出库（可多批次凑齐；过期/不足整单拒发）"""
    v = inv.create_issue(
        db, drug_id=payload.drug_id,
        lines=[ln.model_dump() for ln in payload.lines],
        voucher_date=payload.voucher_date or date.today(),
        cow_id=payload.cow_id, purpose=payload.purpose,
        operator=payload.operator, note=payload.note,
    )
    db.commit()
    db.refresh(v)
    return inv.voucher_to_dict(v, db)


@app.post("/api/inventory/returns", status_code=201)
def create_return(payload: schemas.ReturnCreate, db: Session = Depends(get_db)):
    v = inv.create_return(
        db, payload.issue_id,
        [it.model_dump() for it in payload.items],
        voucher_date=payload.voucher_date or date.today(),
        operator=payload.operator, note=payload.note,
    )
    db.commit()
    db.refresh(v)
    return inv.voucher_to_dict(v, db)


@app.post("/api/inventory/writeoffs", status_code=201)
def create_writeoff(payload: schemas.WriteoffCreate, db: Session = Depends(get_db)):
    v = inv.create_writeoff(
        db, [it.model_dump() for it in payload.items],
        voucher_date=payload.voucher_date or date.today(),
        reason=payload.reason, operator=payload.operator, note=payload.note,
    )
    db.commit()
    db.refresh(v)
    return inv.voucher_to_dict(v, db)


@app.post("/api/inventory/stocktakes", status_code=201)
def create_stocktake(payload: schemas.StocktakeCreate, db: Session = Depends(get_db)):
    v = inv.create_stocktake(
        db, [it.model_dump() for it in payload.items],
        voucher_date=payload.voucher_date or date.today(),
        operator=payload.operator, note=payload.note,
    )
    db.commit()
    db.refresh(v)
    return inv.voucher_to_dict(v, db)


@app.post("/api/inventory/issues/{issue_id}/revoke", status_code=201)
def revoke_issue(issue_id: int, payload: Optional[dict] = Body(default=None),
                 db: Session = Depends(get_db)):
    """整单撤销领用：数量原路退回原批次；关联用药记录同步作废。
    与退回已开封药（入待毁隔离）去向不同。"""
    operator = (payload or {}).get("operator")
    revoke, _med = inv.reverse_issue(db, issue_id, operator=operator)
    db.commit()
    db.refresh(revoke)
    return inv.voucher_to_dict(revoke, db)


@app.get("/api/inventory/vouchers")
def list_vouchers(
    voucher_type: Optional[str] = None,
    drug_id: Optional[int] = None,
    cow_id: Optional[int] = None,
    limit: int = Query(300, le=1000),
    db: Session = Depends(get_db),
):
    q = db.query(models.StockVoucher)
    if voucher_type:
        q = q.filter(models.StockVoucher.voucher_type == voucher_type)
    if drug_id:
        q = q.filter(models.StockVoucher.drug_id == drug_id)
    if cow_id:
        q = q.filter(models.StockVoucher.cow_id == cow_id)
    rows = q.order_by(
        models.StockVoucher.voucher_date.desc(),
        models.StockVoucher.id.desc(),
    ).limit(limit).all()
    return [inv.voucher_to_dict(v, db) for v in rows]


@app.get("/api/inventory/vouchers/{voucher_id}")
def get_voucher(voucher_id: int, db: Session = Depends(get_db)):
    v = db.get(models.StockVoucher, voucher_id)
    if not v:
        raise HTTPException(404, "未找到该单据")
    return inv.voucher_to_dict(v, db)


@app.get("/api/inventory/issues/{issue_id}/returnable")
def issue_returnable(issue_id: int, db: Session = Depends(get_db)):
    """某领用单可退回数量（区分未开封/已开封去向）"""
    v = db.get(models.StockVoucher, issue_id)
    if not v or v.voucher_type != "issue":
        raise HTTPException(404, "未找到该领用单")
    return {
        "voucher_id": v.id, "voucher_no": v.voucher_no, "status": v.status,
        "drug_name": v.drug.name if v.drug else None,
        "unit": v.drug.unit if v.drug else None,
        "cow_tag": v.cow.ear_tag if v.cow else None,
        "lines": [{
            "line_id": ln.id, "batch_id": ln.batch_id,
            "batch_no": ln.batch.batch_no,
            "expiry_date": str(ln.batch.expiry_date),
            "qty": ln.qty,
            "qty_returned_ok": ln.qty_returned_ok,
            "qty_returned_quar": ln.qty_returned_quar,
            "returnable": round(ln.qty - ln.qty_returned_ok - ln.qty_returned_quar, 3),
        } for ln in v.lines],
    }


@app.get("/api/inventory/ledger")
def list_ledger(
    drug_id: Optional[int] = None,
    batch_id: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    limit: int = Query(500, le=2000),
    db: Session = Depends(get_db),
):
    """库存流水（只追加）：可按药品/批次/日期核对"""
    q = db.query(models.StockLedger)
    if drug_id:
        q = q.filter(models.StockLedger.drug_id == drug_id)
    if batch_id:
        q = q.filter(models.StockLedger.batch_id == batch_id)
    if date_from:
        q = q.filter(models.StockLedger.voucher_date >= date_from)
    if date_to:
        q = q.filter(models.StockLedger.voucher_date <= date_to)
    rows = q.order_by(models.StockLedger.id.desc()).limit(limit).all()
    return [inv.ledger_to_dict(r, db) for r in rows]


@app.get("/api/inventory/reconcile")
def inventory_reconcile(db: Session = Depends(get_db)):
    """流水 ↔ 批次余额 ↔ 用药消耗对账"""
    return inv.reconcile(db)


@app.get("/api/inventory/summary")
def inventory_summary(db: Session = Depends(get_db)):
    return inv.stock_summary(db)


# ---------------- 用药记录 ----------------
@app.get("/api/medications")
def list_medications(cow_id: Optional[int] = None, db: Session = Depends(get_db)):
    query = db.query(models.Medication)
    if cow_id:
        query = query.filter_by(cow_id=cow_id)
    rows = query.order_by(models.Medication.date.desc()).limit(300).all()
    return [med_to_dict(m, date.today(), db) for m in rows]


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

    # 实际用药消耗必须关联真实领用：从指定批次（FEFO 或手动多批次凑齐）出库，
    # 过期/不足整单拒绝；与用药记录同一事务提交，要么都成功要么都不发生。
    voucher = None
    if payload.issue_lines:
        if not drug:
            raise StockError("按批次出库需选择药品目录中的药品，不能用自定义药名")
        voucher = inv.create_issue(
            db, drug_id=drug.id,
            lines=[ln.model_dump() for ln in payload.issue_lines],
            voucher_date=payload.date, cow_id=payload.cow_id,
            purpose=payload.reason, operator=payload.operator,
            note=f"登记用药 #{cow.ear_tag}",
        )

    m = models.Medication(
        cow_id=payload.cow_id, drug_id=drug.id if drug else None,
        drug_name=drug_name, date=payload.date, dose=payload.dose,
        route=payload.route, reason=payload.reason, withdrawal_days=wd,
        withdrawal_end=payload.date + timedelta(days=wd),
        next_dose_date=payload.next_dose_date,
        treated=payload.next_dose_date is None,
        operator=payload.operator, note=payload.note,
        voucher_id=voucher.id if voucher else None,
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return med_to_dict(m, date.today(), db)


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
    return med_to_dict(m, date.today(), db)


@app.delete("/api/medications/{med_id}", status_code=204)
def delete_medication(med_id: int, db: Session = Depends(get_db)):
    m = db.get(models.Medication, med_id)
    if not m:
        raise HTTPException(404, "未找到该记录")
    if m.voucher_id:
        v = db.get(models.StockVoucher, m.voucher_id)
        raise HTTPException(
            409,
            f"该用药已关联领用单 {v.voucher_no if v else ''}，删除会导致库存流水对不上账。"
            "请使用“撤销领用”——库存将原路退回并同步作废该用药记录。",
        )
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
            models.Medication.date <= today,
            models.Medication.withdrawal_end >= today,
        )
        .distinct().count()
    )
    stock = inv.stock_summary(db, today)

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
        "stock": stock,
    }


# ---------------- 静态前端 ----------------
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
