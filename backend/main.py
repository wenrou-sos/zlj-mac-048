"""FastAPI 入口：牧场管理系统 API（登录鉴权 + 岗位 + 牛舍范围控制）"""
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from . import auth, migrate, models, schemas, services
from .auth import (
    COOKIE_NAME,
    Principal,
    create_session,
    get_principal,
    require_perm,
    revoke_sessions,
)
from .database import Base, engine, get_db
from .seed import init_db

Base.metadata.create_all(bind=engine)
migrate.run_migrations()  # 存量库幂等升级：补列、归并牛舍、确保初始化管理员
init_db()                 # 全新库写样例（含演示账号）；已有数据的库不会重灌

app = FastAPI(title="牧场管理系统 API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
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
        "note": r.note, "created_by": r.created_by,
        "created_at": str(r.created_at) if r.created_at else None,
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
        "created_by": m.created_by,
        "active_withdrawal": m.date <= today <= m.withdrawal_end,
    }


def scope_cow_ids(p: Principal, db: Session):
    """当前用户可访问的牛只 id 列表；None=全场。空范围返回 []（绝不允许漏过滤）"""
    sub = p.cow_ids_query()
    if sub is None:
        return None
    return [row[0] for row in sub.with_entities(models.Cow.id).all()]


# ================= 认证 =================
@app.post("/api/auth/login")
def login(payload: schemas.LoginIn, response: Response, db: Session = Depends(get_db)):
    user = db.query(models.User).filter_by(username=payload.username.strip()).first()
    # 统一错误文案，避免枚举用户名
    if not user or not auth.verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "用户名或密码错误")
    if not user.active:
        raise HTTPException(403, "账号已停用，请联系管理员")
    sess = create_session(db, user)
    response.set_cookie(
        COOKIE_NAME, sess.token,
        max_age=int(auth.SESSION_TTL.total_seconds()),
        httponly=True, samesite="lax", path="/",
    )
    return _me_payload(user, db, sess)


@app.post("/api/auth/logout")
def logout(
    response: Response,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    revoke_sessions(db, p.user.id)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def me(p: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return _me_payload(p.user, db)


def _me_payload(user: models.User, db: Session, sess: Optional[models.AuthSession] = None) -> dict:
    principal = Principal(user=user, db=db)
    roles = principal.roles
    shed_ids = principal.shed_ids()
    sheds = []
    if shed_ids is not None and shed_ids:
        sheds = [
            {"id": s.id, "code": s.code, "name": s.name}
            for s in db.query(models.Shed).filter(models.Shed.id.in_(shed_ids)).all()
        ]
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "roles": roles,
        "role_labels": [auth.ROLE_LABELS.get(r, r) for r in roles],
        "permissions": sorted(p for p in principal.permissions() if p != "*"),
        "is_admin": "admin" in roles,
        "global_scope": principal.is_global,
        "sheds": sheds,
        "note": user.note,
    }


@app.post("/api/auth/change-password")
def change_password(
    payload: schemas.ChangePasswordIn,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    if not auth.verify_password(payload.old_password, p.user.password_hash):
        raise HTTPException(400, "原密码不正确")
    p.user.password_hash = auth.hash_password(payload.new_password)
    db.commit()
    revoke_sessions(db, p.user.id)  # 改密后其它设备立即下线，需重新登录
    return {"ok": True}


# ================= 牛舍 =================
def shed_to_dict(s: models.Shed, cow_count: Optional[int] = None) -> dict:
    return {"id": s.id, "code": s.code, "name": s.name, "active": s.active,
            "cow_count": cow_count}


@app.get("/api/sheds")
def list_sheds(
    include_inactive: bool = False,
    p: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    q = db.query(models.Shed)
    if not include_inactive:
        q = q.filter_by(active=True)
    sheds = q.order_by(models.Shed.code.asc()).all()
    # 在场牛只数（按牛舍汇总）
    cnt_rows = (
        db.query(models.Cow.shed_id)
        .filter(models.Cow.status != "sold")
        .all()
    )
    cnt: dict = defaultdict(int)
    for sid, in cnt_rows:
        if sid is not None:
            cnt[sid] += 1
    ids = p.shed_ids()
    return [
        shed_to_dict(s, cnt.get(s.id, 0))
        for s in sheds
        if ids is None or s.id in ids
    ]


@app.post("/api/sheds", response_model=schemas.ShedOut, status_code=201)
def create_shed(
    payload: schemas.ShedIn,
    p: Principal = Depends(require_perm("shed:manage")),
    db: Session = Depends(get_db),
):
    if db.query(models.Shed).filter_by(code=payload.code).first():
        raise HTTPException(409, f"牛舍编号 {payload.code} 已存在")
    s = models.Shed(**payload.model_dump())
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


@app.patch("/api/sheds/{shed_id}", response_model=schemas.ShedOut)
def update_shed(
    shed_id: int,
    payload: schemas.ShedIn,
    p: Principal = Depends(require_perm("shed:manage")),
    db: Session = Depends(get_db),
):
    s = db.get(models.Shed, shed_id)
    if not s:
        raise HTTPException(404, "未找到该牛舍")
    clash = db.query(models.Shed).filter(
        models.Shed.code == payload.code, models.Shed.id != shed_id
    ).first()
    if clash:
        raise HTTPException(409, "牛舍编号已被占用")
    for k, v in payload.model_dump().items():
        setattr(s, k, v)
    db.commit()
    db.refresh(s)
    return s


# ================= 奶牛档案 =================
@app.get("/api/cows", response_model=List[schemas.CowOut])
def list_cows(
    q: Optional[str] = None,
    status: Optional[str] = None,
    p: Principal = Depends(require_perm("cow:read")),
    db: Session = Depends(get_db),
):
    query = db.query(models.Cow)
    ids = scope_cow_ids(p, db)
    if ids is not None:
        query = query.filter(models.Cow.id.in_(ids or [-1]))
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
def create_cow(
    payload: schemas.CowCreate,
    p: Principal = Depends(require_perm("cow:write")),
    db: Session = Depends(get_db),
):
    if payload.status not in VALID_STATUS:
        raise HTTPException(400, "非法状态")
    if db.query(models.Cow).filter_by(ear_tag=payload.ear_tag).first():
        raise HTTPException(409, f"耳标号 {payload.ear_tag} 已存在")
    if payload.expected_calving_date and payload.calving_date and \
            payload.expected_calving_date <= payload.calving_date:
        raise HTTPException(400, "预产期应晚于产犊日期")
    if payload.shed_id is not None:
        shed = db.get(models.Shed, payload.shed_id)
        if not shed:
            raise HTTPException(400, "所选牛舍不存在")
        # 场长/管理员全场；其它岗位不应有 cow:write，这里再兜底一次牛舍范围
        if p.shed_ids() is not None and payload.shed_id not in p.shed_ids():
            raise HTTPException(403, "不能在您负责范围外的牛舍建档")
    cow = models.Cow(**payload.model_dump())
    db.add(cow)
    db.commit()
    db.refresh(cow)
    return cow


@app.get("/api/cows/{cow_id}")
def get_cow(
    cow_id: int,
    p: Principal = Depends(require_perm("cow:read")),
    db: Session = Depends(get_db),
):
    cow = db.get(models.Cow, cow_id)
    p.require_cow(cow)
    return cow_detail(cow_id, db)


@app.patch("/api/cows/{cow_id}", response_model=schemas.CowOut)
def update_cow(
    cow_id: int,
    payload: schemas.CowUpdate,
    p: Principal = Depends(require_perm("cow:write")),
    db: Session = Depends(get_db),
):
    cow = db.get(models.Cow, cow_id)
    p.require_cow(cow)
    data = payload.model_dump(exclude_unset=True)
    if "status" in data and data["status"] not in VALID_STATUS:
        raise HTTPException(400, "非法状态")
    if data.get("shed_id") is not None:
        new_shed = db.get(models.Shed, data["shed_id"])
        if not new_shed:
            raise HTTPException(400, "目标牛舍不存在")
        # 把牛转入牛舍即把数据移交到新范围：仅全场角色（管理员/场长）可跨舍调牛
        if p.shed_ids() is not None and data["shed_id"] not in p.shed_ids():
            raise HTTPException(403, "不能把牛只调出您负责的牛舍范围")
    for k, v in data.items():
        setattr(cow, k, v)
    db.commit()
    db.refresh(cow)
    return cow


@app.delete("/api/cows/{cow_id}", status_code=204)
def delete_cow(
    cow_id: int,
    p: Principal = Depends(require_perm("cow:write")),
    db: Session = Depends(get_db),
):
    cow = db.get(models.Cow, cow_id)
    p.require_cow(cow)
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
        "shed_id": cow.shed_id,
        "shed_code": cow.shed.code if cow.shed else None,
        "shed_name": cow.shed.name if cow.shed else None,
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
            "result": h.result, "note": h.note, "created_by": h.created_by,
        } for h in health],
        "medications": [med_to_dict(m, today) for m in meds],
        "estruses": [{
            "id": e.id, "date": str(e.date), "detection": e.detection, "score": e.score,
            "inseminated": e.inseminated,
            "insemination_date": str(e.insemination_date) if e.insemination_date else None,
            "semen": e.semen, "technician": e.technician, "result": e.result,
            "result_date": str(e.result_date) if e.result_date else None, "note": e.note,
            "created_by": e.created_by,
        } for e in estruses],
        "yield_trend": trend,
    }


# ================= 挤奶记录 =================
@app.get("/api/milkings")
def list_milkings(
    cow_id: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    only_violations: bool = False,
    limit: int = Query(200, le=1000),
    p: Principal = Depends(require_perm("milking:read")),
    db: Session = Depends(get_db),
):
    query = db.query(models.MilkingRecord)
    ids = scope_cow_ids(p, db)
    if ids is not None:
        query = query.filter(models.MilkingRecord.cow_id.in_(ids or [-1]))
    if cow_id:
        cow = db.get(models.Cow, cow_id)
        p.require_cow(cow)
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
            "note": r.note, "created_by": r.created_by,
            "created_at": str(r.created_at) if r.created_at else None,
            "cow_ear_tag": cow.ear_tag if cow else None,
            "cow_name": cow.name if cow else None,
            "shed_id": cow.shed_id if cow else None,
            "in_withdrawal": in_w,
            "withdrawal_until": str(med.withdrawal_end) if med else None,
            "violation": bool(in_w and not r.discarded),
        })
    return out


@app.post("/api/milkings/check-withdrawal")
def milking_check(
    payload: dict,
    p: Principal = Depends(require_perm("milking:read")),
    db: Session = Depends(get_db),
):
    """录入前休药期预检"""
    try:
        cid, on = int(payload["cow_id"]), date.fromisoformat(payload["date"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "需要 cow_id 与 date(YYYY-MM-DD)")
    cow = db.get(models.Cow, cid)
    p.require_cow(cow)
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


def _can_modify_milking(p: Principal, rec: models.MilkingRecord) -> None:
    """挤奶员仅可改/删本人录入的记录；管理员/场长不限"""
    if p.is_global:
        return
    if "milking:write" in p.permissions() and rec.created_by == p.user.id:
        return
    raise HTTPException(403, "只能修改本人登记的挤奶记录")


@app.post("/api/milkings", status_code=201)
def create_milking(
    payload: schemas.MilkingCreate,
    p: Principal = Depends(require_perm("milking:write")),
    db: Session = Depends(get_db),
):
    cow = db.get(models.Cow, payload.cow_id)
    p.require_cow(cow)
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
        **payload.model_dump(exclude={"discarded"}), discarded=discarded,
        created_by=p.user.id,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    result = milking_to_dict(rec, db)
    result["warnings"] = warnings
    return result


@app.patch("/api/milkings/{rec_id}")
def update_milking(
    rec_id: int,
    payload: schemas.MilkingUpdate,
    p: Principal = Depends(require_perm("milking:write")),
    db: Session = Depends(get_db),
):
    rec = db.get(models.MilkingRecord, rec_id)
    if not rec:
        raise HTTPException(404, "未找到该记录")
    p.require_cow(db.get(models.Cow, rec.cow_id))
    _can_modify_milking(p, rec)
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(rec, k, v)
    db.commit()
    db.refresh(rec)
    return milking_to_dict(rec, db)


@app.delete("/api/milkings/{rec_id}", status_code=204)
def delete_milking(
    rec_id: int,
    p: Principal = Depends(require_perm("milking:write")),
    db: Session = Depends(get_db),
):
    rec = db.get(models.MilkingRecord, rec_id)
    if not rec:
        raise HTTPException(404, "未找到该记录")
    p.require_cow(db.get(models.Cow, rec.cow_id))
    _can_modify_milking(p, rec)
    db.delete(rec)
    db.commit()


@app.post("/api/milkings/{rec_id}/discard", status_code=200)
def mark_milking_discarded(
    rec_id: int,
    p: Principal = Depends(require_perm("milking:discard")),
    db: Session = Depends(get_db),
):
    """违规混装一键补标废弃（兽医/挤奶员在本舍内也可执行）"""
    rec = db.get(models.MilkingRecord, rec_id)
    if not rec:
        raise HTTPException(404, "未找到该记录")
    p.require_cow(db.get(models.Cow, rec.cow_id))
    rec.discarded = True
    db.commit()
    db.refresh(rec)
    return milking_to_dict(rec, db)


# ================= 健康记录 =================
@app.get("/api/health")
def list_health(
    cow_id: Optional[int] = None,
    p: Principal = Depends(require_perm("health:read")),
    db: Session = Depends(get_db),
):
    query = db.query(models.HealthRecord)
    ids = scope_cow_ids(p, db)
    if ids is not None:
        query = query.filter(models.HealthRecord.cow_id.in_(ids or [-1]))
    if cow_id:
        cow = db.get(models.Cow, cow_id)
        p.require_cow(cow)
        query = query.filter_by(cow_id=cow_id)
    rows = query.order_by(models.HealthRecord.date.desc()).limit(300).all()
    cows = {c.id: c for c in db.query(models.Cow).all()}
    return [{
        "id": h.id, "cow_id": h.cow_id, "date": str(h.date),
        "record_type": h.record_type, "diagnosis": h.diagnosis,
        "temperature": h.temperature, "severity": h.severity,
        "follow_up_date": str(h.follow_up_date) if h.follow_up_date else None,
        "result": h.result, "note": h.note, "created_by": h.created_by,
        "cow_ear_tag": cows[h.cow_id].ear_tag if h.cow_id in cows else None,
        "cow_name": cows[h.cow_id].name if h.cow_id in cows else None,
    } for h in rows]


@app.post("/api/health", response_model=schemas.HealthOut, status_code=201)
def create_health(
    payload: schemas.HealthCreate,
    p: Principal = Depends(require_perm("health:write")),
    db: Session = Depends(get_db),
):
    cow = db.get(models.Cow, payload.cow_id)
    p.require_cow(cow)
    h = models.HealthRecord(**payload.model_dump(), created_by=p.user.id)
    db.add(h)
    db.commit()
    db.refresh(h)
    return h


@app.patch("/api/health/{rec_id}", response_model=schemas.HealthOut)
def update_health(
    rec_id: int,
    payload: schemas.HealthUpdate,
    p: Principal = Depends(require_perm("health:write")),
    db: Session = Depends(get_db),
):
    h = db.get(models.HealthRecord, rec_id)
    if not h:
        raise HTTPException(404, "未找到该记录")
    p.require_cow(db.get(models.Cow, h.cow_id))
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(h, k, v)
    db.commit()
    db.refresh(h)
    return h


@app.delete("/api/health/{rec_id}", status_code=204)
def delete_health(
    rec_id: int,
    p: Principal = Depends(require_perm("health:write")),
    db: Session = Depends(get_db),
):
    h = db.get(models.HealthRecord, rec_id)
    if not h:
        raise HTTPException(404, "未找到该记录")
    p.require_cow(db.get(models.Cow, h.cow_id))
    db.delete(h)
    db.commit()


# ================= 药品目录 =================
@app.get("/api/drugs", response_model=List[schemas.DrugOut])
def list_drugs(
    p: Principal = Depends(require_perm("drug:read")),
    db: Session = Depends(get_db),
):
    return db.query(models.DrugCatalog).filter_by(active=True).order_by(
        models.DrugCatalog.name.asc()
    ).all()


@app.post("/api/drugs", response_model=schemas.DrugOut, status_code=201)
def create_drug(
    payload: schemas.DrugCreate,
    p: Principal = Depends(require_perm("drug:write")),
    db: Session = Depends(get_db),
):
    if db.query(models.DrugCatalog).filter_by(name=payload.name).first():
        raise HTTPException(409, "药品已存在")
    d = models.DrugCatalog(**payload.model_dump())
    db.add(d)
    db.commit()
    db.refresh(d)
    return d


# ================= 用药记录 =================
@app.get("/api/medications")
def list_medications(
    cow_id: Optional[int] = None,
    p: Principal = Depends(require_perm("medication:read")),
    db: Session = Depends(get_db),
):
    query = db.query(models.Medication)
    ids = scope_cow_ids(p, db)
    if ids is not None:
        query = query.filter(models.Medication.cow_id.in_(ids or [-1]))
    if cow_id:
        cow = db.get(models.Cow, cow_id)
        p.require_cow(cow)
        query = query.filter_by(cow_id=cow_id)
    rows = query.order_by(models.Medication.date.desc()).limit(300).all()
    return [med_to_dict(m, date.today()) for m in rows]


@app.post("/api/medications", status_code=201)
def create_medication(
    payload: schemas.MedicationCreate,
    p: Principal = Depends(require_perm("medication:write")),
    db: Session = Depends(get_db),
):
    cow = db.get(models.Cow, payload.cow_id)
    p.require_cow(cow)

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
        operator=payload.operator or p.user.display_name,
        note=payload.note, created_by=p.user.id,
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return med_to_dict(m, date.today())


@app.patch("/api/medications/{med_id}")
def update_medication(
    med_id: int,
    payload: schemas.MedicationUpdate,
    p: Principal = Depends(require_perm("medication:write")),
    db: Session = Depends(get_db),
):
    m = db.get(models.Medication, med_id)
    if not m:
        raise HTTPException(404, "未找到该记录")
    p.require_cow(db.get(models.Cow, m.cow_id))
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
def delete_medication(
    med_id: int,
    p: Principal = Depends(require_perm("medication:write")),
    db: Session = Depends(get_db),
):
    m = db.get(models.Medication, med_id)
    if not m:
        raise HTTPException(404, "未找到该记录")
    p.require_cow(db.get(models.Cow, m.cow_id))
    db.delete(m)
    db.commit()


# ================= 发情/配种 =================
@app.get("/api/estruses")
def list_estruses(
    cow_id: Optional[int] = None,
    p: Principal = Depends(require_perm("estrus:read")),
    db: Session = Depends(get_db),
):
    query = db.query(models.EstrusRecord)
    ids = scope_cow_ids(p, db)
    if ids is not None:
        query = query.filter(models.EstrusRecord.cow_id.in_(ids or [-1]))
    if cow_id:
        cow = db.get(models.Cow, cow_id)
        p.require_cow(cow)
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
        "created_by": e.created_by,
        "cow_ear_tag": cows[e.cow_id].ear_tag if e.cow_id in cows else None,
        "cow_name": cows[e.cow_id].name if e.cow_id in cows else None,
    } for e in rows]


@app.post("/api/estruses", response_model=schemas.EstrusOut, status_code=201)
def create_estrus(
    payload: schemas.EstrusCreate,
    p: Principal = Depends(require_perm("estrus:write")),
    db: Session = Depends(get_db),
):
    cow = db.get(models.Cow, payload.cow_id)
    p.require_cow(cow)
    if payload.insemination_date and payload.insemination_date < payload.date:
        raise HTTPException(400, "配种日期不能早于发情日期")
    e = models.EstrusRecord(**payload.model_dump(), created_by=p.user.id)
    if e.inseminated and not e.result:
        e.result = "pending"
    db.add(e)
    db.commit()
    db.refresh(e)
    return e


@app.patch("/api/estruses/{rec_id}", response_model=schemas.EstrusOut)
def update_estrus(
    rec_id: int,
    payload: schemas.EstrusUpdate,
    p: Principal = Depends(require_perm("estrus:write")),
    db: Session = Depends(get_db),
):
    e = db.get(models.EstrusRecord, rec_id)
    if not e:
        raise HTTPException(404, "未找到该记录")
    p.require_cow(db.get(models.Cow, e.cow_id))
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(e, k, v)
    db.commit()
    db.refresh(e)
    return e


@app.delete("/api/estruses/{rec_id}", status_code=204)
def delete_estrus(
    rec_id: int,
    p: Principal = Depends(require_perm("estrus:write")),
    db: Session = Depends(get_db),
):
    e = db.get(models.EstrusRecord, rec_id)
    if not e:
        raise HTTPException(404, "未找到该记录")
    p.require_cow(db.get(models.Cow, e.cow_id))
    db.delete(e)
    db.commit()


# ================= 提醒 / 异常 / 仪表盘 =================
@app.get("/api/reminders")
def get_reminders(
    p: Principal = Depends(require_perm("report:read")),
    db: Session = Depends(get_db),
):
    ids = scope_cow_ids(p, db)
    return services.build_reminders(db, cow_filter=ids)


@app.get("/api/anomalies")
def get_anomalies(
    days: int = Query(7, ge=1, le=30),
    p: Principal = Depends(require_perm("report:read")),
    db: Session = Depends(get_db),
):
    ids = scope_cow_ids(p, db)
    return services.detect_yield_anomalies(db, days=days, cow_filter=ids)


@app.get("/api/dashboard")
def dashboard(
    p: Principal = Depends(require_perm("report:read")),
    db: Session = Depends(get_db),
):
    today = date.today()
    yesterday = today - timedelta(days=1)
    ids = scope_cow_ids(p, db)
    cow_q = db.query(models.Cow)
    if ids is not None:
        cow_q = cow_q.filter(models.Cow.id.in_(ids or [-1]))
    cows = cow_q.all()
    active = [c for c in cows if c.status != "sold"]
    by_status = defaultdict(int)
    for c in cows:
        by_status[c.status] += 1

    def day_milk(d: date):
        q = db.query(models.MilkingRecord).filter_by(date=d)
        if ids is not None:
            q = q.filter(models.MilkingRecord.cow_id.in_(ids or [-1]))
        rows = q.all()
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

    reminders = services.build_reminders(db, today, cow_filter=ids)
    anomalies = services.detect_yield_anomalies(db, days=7, today=today, cow_filter=ids)
    viol_q = db.query(models.MilkingRecord).filter(services.withdrawal_violation_clause())
    wd_q = (
        db.query(models.Medication.cow_id)
        .filter(
            models.Medication.withdrawal_days > 0,
            models.Medication.date <= today,
            models.Medication.withdrawal_end >= today,
        )
    )
    if ids is not None:
        viol_q = viol_q.filter(models.MilkingRecord.cow_id.in_(ids or [-1]))
        wd_q = wd_q.filter(models.Medication.cow_id.in_(ids or [-1]))
    violation_count = viol_q.count()
    cows_in_withdrawal = wd_q.distinct().count()

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
        "scoped": ids is not None,
    }


# ================= 用户与授权管理 =================
def _active_admin_count(db: Session, *, exclude_user_id: Optional[int] = None) -> int:
    q = db.query(models.User).filter(
        models.User.roles.like("%admin%"), models.User.active.is_(True)
    )
    if exclude_user_id is not None:
        q = q.filter(models.User.id != exclude_user_id)
    return q.count()


def assignment_to_dict(a: models.UserShedAssignment) -> dict:
    return {
        "id": a.id, "shed_id": a.shed_id,
        "valid_from": str(a.valid_from) if a.valid_from else None,
        "valid_to": str(a.valid_to) if a.valid_to else None,
        "note": a.note,
        "shed_code": a.shed.code if a.shed else None,
        "shed_name": a.shed.name if a.shed else None,
    }


def user_to_dict(u: models.User) -> dict:
    roles = [r for r in (u.roles or "").split(",") if r]
    return {
        "id": u.id, "username": u.username, "display_name": u.display_name,
        "roles": roles, "active": u.active, "note": u.note,
        "global_scope": bool(auth.GLOBAL_ROLES.intersection(roles)),
        "assignments": [assignment_to_dict(a) for a in u.assignments],
        "created_at": str(u.created_at) if u.created_at else None,
    }


def _validate_roles(roles: list) -> list:
    roles = [r for r in dict.fromkeys(roles or [])]
    bad = [r for r in roles if r not in auth.ROLES]
    if bad:
        raise HTTPException(400, f"非法岗位：{','.join(bad)}")
    if not roles:
        raise HTTPException(400, "至少需要选择一个岗位")
    return roles


def _is_admin_user(u: models.User) -> bool:
    return "admin" in [r for r in (u.roles or "").split(",") if r]


def _can_manage_actor(p: Principal, target: Optional[models.User], new_roles: Optional[list]) -> None:
    """场长可管非管理员；管理员相关的一切（含把他人升为管理员）只有管理员能做。"""
    if "admin" in p.roles:
        return
    if target is not None and _is_admin_user(target):
        raise HTTPException(403, "只有管理员可以管理管理员账号")
    if new_roles is not None and "admin" in new_roles:
        raise HTTPException(403, "只有管理员可以授予管理员岗位")


def _sync_assignments(db: Session, user: models.User, items: list) -> None:
    shed_ids = []
    for it in items:
        shed = db.get(models.Shed, it.shed_id)
        if not shed:
            raise HTTPException(400, f"牛舍 id {it.shed_id} 不存在")
        if it.valid_from and it.valid_to and it.valid_to < it.valid_from:
            raise HTTPException(400, "接管截止日不能早于起始日")
        shed_ids.append(it.shed_id)
    # 全量覆盖：调岗/收回牛舍在这里生效
    db.query(models.UserShedAssignment).filter_by(user_id=user.id).delete()
    for it in items:
        db.add(models.UserShedAssignment(
            user_id=user.id, shed_id=it.shed_id,
            valid_from=it.valid_from, valid_to=it.valid_to, note=it.note,
        ))


@app.get("/api/users")
def list_users(
    p: Principal = Depends(require_perm("user:manage")),
    db: Session = Depends(get_db),
):
    users = db.query(models.User).order_by(models.User.id.asc()).all()
    return [user_to_dict(u) for u in users]


@app.post("/api/users", response_model=schemas.UserOut, status_code=201)
def create_user(
    payload: schemas.UserCreate,
    p: Principal = Depends(require_perm("user:manage")),
    db: Session = Depends(get_db),
):
    roles = _validate_roles(payload.roles)
    _can_manage_actor(p, None, roles)
    username = payload.username.strip()
    if db.query(models.User).filter_by(username=username).first():
        raise HTTPException(409, "登录名已存在")
    user = models.User(
        username=username, display_name=payload.display_name,
        password_hash=auth.hash_password(payload.password),
        roles=",".join(roles), active=payload.active, note=payload.note,
    )
    db.add(user)
    db.flush()
    _sync_assignments(db, user, payload.assignments)
    db.commit()
    db.refresh(user)
    return user_to_dict(user)


@app.patch("/api/users/{user_id}", response_model=schemas.UserOut)
def update_user(
    user_id: int,
    payload: schemas.UserUpdate,
    p: Principal = Depends(require_perm("user:manage")),
    db: Session = Depends(get_db),
):
    user = db.get(models.User, user_id)
    if not user:
        raise HTTPException(404, "未找到该用户")
    _can_manage_actor(p, user, payload.roles)

    new_roles = None
    if payload.roles is not None:
        new_roles = _validate_roles(payload.roles)

    will_be_active = payload.active if payload.active is not None else user.active
    will_be_admin = "admin" in (new_roles if new_roles is not None
                                else [r for r in (user.roles or "").split(",") if r])
    removing_last_admin = (
        _is_admin_user(user) and (not will_be_active or not will_be_admin)
    )
    # 快速预检（友好报错）；最终的并发安全校验在 flush 后、同一事务内再做一次
    if removing_last_admin and _active_admin_count(db, exclude_user_id=user_id) == 0:
        raise HTTPException(409, "系统必须至少保留一个启用中的管理员，已阻止该操作")

    if payload.display_name is not None:
        user.display_name = payload.display_name
    if payload.note is not None:
        user.note = payload.note
    if new_roles is not None:
        user.roles = ",".join(new_roles)
    if payload.active is not None and payload.active != user.active:
        user.active = payload.active
    if payload.password:
        user.password_hash = auth.hash_password(payload.password)
    if payload.assignments is not None:
        _sync_assignments(db, user, payload.assignments)

    # 权威终检：在写锁事务内（BEGIN IMMEDIATE 串行化）把待提交修改一起统计。
    # 两个管理员并发各自停用自己时，后拿到写锁的一方会在此看到 0 个管理员并回滚，
    # 杜绝预检通过但最终无人可用的竞态。
    if removing_last_admin:
        db.flush()
        if db.query(models.User).filter(
            models.User.roles.like("%admin%"), models.User.active.is_(True)
        ).count() == 0:
            db.rollback()
            raise HTTPException(409, "系统必须至少保留一个启用中的管理员，已阻止该操作")

    db.commit()
    # 调岗、停用、改密、收回牛舍后：所有现有会话立即失效，强制重新登录拿到新权限
    revoke_sessions(db, user_id)
    db.refresh(user)
    return user_to_dict(user)


# ---------------- 静态前端 ----------------
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
