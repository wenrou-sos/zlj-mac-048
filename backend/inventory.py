"""药品库存领域服务：批次效期、入库/领用/退回/报损/盘点、库存流水与对账。

并发安全：
- 引擎层所有事务以 BEGIN IMMEDIATE 开始（见 database.py），写事务串行化，
  “查余额 → 判足 → 扣减”整体处于临界区；
- 扣减/回增一律走带余额条件的 UPDATE（WHERE qty_ok >= :need），
  即使绕过服务层也不可能把库存写成负数，批次表/流水表另有 CHECK 约束兜底。

流水只追加（StockLedger），任何红冲都另开反向单据（撤销领用），不留无痕修改。
"""
from datetime import date
from typing import Dict, List, Optional

from sqlalchemy import update
from sqlalchemy.orm import Session

from . import models

VOUCHER_PREFIX = {
    "receipt": "RK",    # 入库
    "issue": "LY",      # 领用
    "return": "TH",     # 退回
    "writeoff": "BS",   # 报损
    "check": "PD",      # 盘点
    "revoke": "CX",     # 撤销领用
}
VOUCHER_LABEL = {
    "receipt": "入库", "issue": "领用", "return": "退回",
    "writeoff": "报损", "check": "盘点", "revoke": "撤销领用",
}


class StockError(Exception):
    """库存业务规则错误（映射为 HTTP 400/409）"""


# ---------------- 单号 ----------------
def new_voucher_no(db: Session, vtype: str, on_date: date) -> str:
    prefix = VOUCHER_PREFIX[vtype]
    day = on_date.strftime("%Y%m%d")
    like = f"{prefix}-{day}-%"
    count = db.query(models.StockVoucher).filter(
        models.StockVoucher.voucher_no.like(like)
    ).count()
    return f"{prefix}-{day}-{count + 1:03d}"


# ---------------- 批次查询 / FEFO ----------------
def available_batches(db: Session, drug_id: int, on_date: date) -> List[models.DrugBatch]:
    """可发批次：未过期（效期当日仍可发）、合格库存 > 0，按 FEFO 近效期先出排序"""
    return (
        db.query(models.DrugBatch)
        .filter(
            models.DrugBatch.drug_id == drug_id,
            models.DrugBatch.active.is_(True),
            models.DrugBatch.expiry_date >= on_date,
            models.DrugBatch.qty_ok > 0,
        )
        .order_by(
            models.DrugBatch.expiry_date.asc(),
            models.DrugBatch.inbound_date.asc(),
            models.DrugBatch.id.asc(),
        )
        .all()
    )


def suggest_issue(db: Session, drug_id: int, qty: float, on_date: date) -> dict:
    """FEFO 自动凑量：从近效期批次开始分配，支持跨批次凑齐"""
    if qty <= 0:
        raise StockError("领用数量必须大于 0")
    allocations, remain = [], qty
    for b in available_batches(db, drug_id, on_date):
        if remain <= 0:
            break
        take = round(min(b.qty_ok, remain), 3)
        if take > 0:
            allocations.append({
                "batch_id": b.id, "batch_no": b.batch_no,
                "expiry_date": str(b.expiry_date),
                "available": b.qty_ok, "qty": take,
            })
            remain = round(remain - take, 3)
    return {
        "allocations": allocations,
        "shortage": max(0.0, remain),
        "fulfilled": remain <= 0,
    }


# ---------------- 底层记账 ----------------
def _adjust_batch(
    db: Session, batch_id: int, delta_ok: float = 0.0, delta_quar: float = 0.0
) -> models.DrugBatch:
    """
    条件 UPDATE 调整批次库存。WHERE 带余额下界，余额不足时 rowcount=0，
    调用方据此整单回滚——这是“库存不能扣成负数”的硬保证。
    """
    conds = [models.DrugBatch.id == batch_id]
    values = {}
    if delta_ok:
        if delta_ok < 0:
            conds.append(models.DrugBatch.qty_ok >= -delta_ok)
        values["qty_ok"] = models.DrugBatch.qty_ok + delta_ok
    if delta_quar:
        if delta_quar < 0:
            conds.append(models.DrugBatch.qty_quarantine >= -delta_quar)
        values["qty_quarantine"] = models.DrugBatch.qty_quarantine + delta_quar
    if not values:
        raise StockError("变动数量为 0")
    result = db.execute(update(models.DrugBatch).where(*conds).values(**values))
    if result.rowcount != 1:
        raise StockError("库存不足或批次不存在，操作被拒绝")
    db.expire_all()
    return db.get(models.DrugBatch, batch_id)


def _append_ledger(
    db: Session, *, batch: models.DrugBatch, voucher: models.StockVoucher,
    delta_ok: float = 0.0, delta_quar: float = 0.0, cow_id: Optional[int] = None,
    note: Optional[str] = None,
) -> None:
    db.add(models.StockLedger(
        batch_id=batch.id, drug_id=batch.drug_id,
        voucher_id=voucher.id, voucher_no=voucher.voucher_no,
        voucher_type=voucher.voucher_type, voucher_date=voucher.voucher_date,
        qty_change_ok=round(delta_ok, 3), qty_change_quarantine=round(delta_quar, 3),
        balance_ok=batch.qty_ok, balance_quarantine=batch.qty_quarantine,
        cow_id=cow_id, note=note,
    ))


def _create_voucher(db: Session, *, vtype: str, on_date: date,
                    drug_id: Optional[int], cow_id: Optional[int] = None,
                    disposition: Optional[str] = None,
                    related_voucher_id: Optional[int] = None,
                    purpose: Optional[str] = None,
                    operator: Optional[str] = None,
                    note: Optional[str] = None) -> models.StockVoucher:
    v = models.StockVoucher(
        voucher_no=new_voucher_no(db, vtype, on_date), voucher_type=vtype,
        voucher_date=on_date, drug_id=drug_id, cow_id=cow_id, status="posted",
        disposition=disposition, related_voucher_id=related_voucher_id,
        purpose=purpose, operator=operator, note=note,
    )
    db.add(v)
    db.flush()
    return v


# ---------------- 入库 ----------------
def create_receipt(db: Session, *, drug_id: int, batch_no: str,
                   expiry_date: date, qty: float, voucher_date: date,
                   supplier: Optional[str] = None, operator: Optional[str] = None,
                   note: Optional[str] = None) -> models.StockVoucher:
    if qty <= 0:
        raise StockError("入库数量必须大于 0")
    if not batch_no or not batch_no.strip():
        raise StockError("批号不能为空")
    drug = db.get(models.DrugCatalog, drug_id)
    if not drug or not drug.active:
        raise StockError("药品不存在或已停用")
    if expiry_date < voucher_date:
        raise StockError(f"该批号效期 {expiry_date} 已过期，不能入库（请走报损/拒收流程）")

    batch = db.query(models.DrugBatch).filter_by(drug_id=drug_id, batch_no=batch_no).first()
    if batch and batch.expiry_date != expiry_date:
        raise StockError(
            f"批号 {batch_no} 已存在但效期为 {batch.expiry_date}，"
            "同一批号效期不一致，请核实后改用新批号"
        )
    if not batch:
        batch = models.DrugBatch(
            drug_id=drug_id, batch_no=batch_no, expiry_date=expiry_date,
            qty_ok=0, qty_quarantine=0, supplier=supplier,
            inbound_date=voucher_date, active=True,
        )
        db.add(batch)
        db.flush()

    voucher = _create_voucher(
        db, vtype="receipt", on_date=voucher_date, drug_id=drug_id,
        operator=operator, note=note,
    )
    db.add(models.StockVoucherLine(voucher_id=voucher.id, batch_id=batch.id, qty=qty,
                                   note=supplier))
    batch = _adjust_batch(db, batch.id, delta_ok=qty)
    batch.active = True
    if supplier and not batch.supplier:
        batch.supplier = supplier
    _append_ledger(db, batch=batch, voucher=voucher, delta_ok=qty, note=f"入库 {batch_no}")
    return voucher


# ---------------- 领用（可跨批次凑齐） ----------------
def _normalize_lines(lines: List[dict]) -> Dict[int, float]:
    agg: Dict[int, float] = {}
    for ln in lines or []:
        bid = ln.get("batch_id")
        qty = round(float(ln.get("qty") or 0), 3)
        if bid is None or qty <= 0:
            continue
        agg[bid] = round(agg.get(bid, 0) + qty, 3)
    if not agg:
        raise StockError("请至少指定一个领用批次和数量")
    return agg


def create_issue(db: Session, *, drug_id: int, lines: List[dict], voucher_date: date,
                 cow_id: Optional[int] = None, purpose: Optional[str] = None,
                 operator: Optional[str] = None, note: Optional[str] = None,
                 ) -> models.StockVoucher:
    """领用出库。任一批次过期/不足 → 抛 StockError，整单不出库。"""
    need = _normalize_lines(lines)
    drug = db.get(models.DrugCatalog, drug_id)
    if not drug or not drug.active:
        raise StockError("药品不存在或已停用")
    if cow_id is not None and not db.get(models.Cow, cow_id):
        raise StockError("领用牛只不存在")

    # 先做全部预检：批次归属、效期、余额，任何一项不过都不写库
    batches = {}
    for bid, qty in need.items():
        b = db.get(models.DrugBatch, bid)
        if not b or b.drug_id != drug_id:
            raise StockError("领用批次不属于所选药品")
        if b.expiry_date < voucher_date:
            raise StockError(
                f"批号 {b.batch_no} 已于 {b.expiry_date} 过期，禁止发出"
            )
        if b.qty_ok < qty:
            raise StockError(
                f"批号 {b.batch_no} 可发库存仅 {b.qty_ok}{drug.unit}，"
                f"不足 {qty}{drug.unit}，整单未出库"
            )
        batches[bid] = (b, qty)

    voucher = _create_voucher(
        db, vtype="issue", on_date=voucher_date, drug_id=drug_id, cow_id=cow_id,
        purpose=purpose, operator=operator, note=note,
    )
    for bid, (b, qty) in batches.items():
        db.add(models.StockVoucherLine(voucher_id=voucher.id, batch_id=bid, qty=qty))
    db.flush()
    for bid, (b, qty) in batches.items():
        fresh = _adjust_batch(db, bid, delta_ok=-qty)
        _append_ledger(db, batch=fresh, voucher=voucher, delta_ok=-qty,
                       cow_id=cow_id, note=f"领用出库 {b.batch_no}")
    return voucher


# ---------------- 退回（未开封回库 / 已开封入待毁） ----------------
def create_return(db: Session, issue_id: int, items: List[dict], *,
                  voucher_date: date, operator: Optional[str] = None,
                  note: Optional[str] = None) -> models.StockVoucher:
    """
    退回领用单：
    - qty_unopened 未开封 → 回原批次合格库存（可继续发出；若已过期则虽回库但发不出去）
    - qty_opened   已开封 → 入该批次“待毁隔离”库存，永远不再进入可发库存，只能报损
    """
    issue = db.get(models.StockVoucher, issue_id)
    if not issue or issue.voucher_type != "issue":
        raise StockError("原领用单不存在")
    if issue.status != "posted":
        raise StockError("原领用单已撤销，不能再退回")

    by_line = {}
    for it in items or []:
        unopened = round(float(it.get("qty_unopened") or 0), 3)
        opened = round(float(it.get("qty_opened") or 0), 3)
        line = db.get(models.StockVoucherLine, it.get("line_id"))
        if not line or line.voucher_id != issue.id:
            raise StockError("退回明细不属于该领用单")
        if unopened < 0 or opened < 0 or (unopened + opened) <= 0:
            raise StockError("退回数量不合法")
        prev = by_line.get(line.id, (0.0, 0.0))
        by_line[line.id] = (round(prev[0] + unopened, 3), round(prev[1] + opened, 3))

    if not by_line:
        raise StockError("请填写退回数量")

    # 累计退回不得超过原领量
    for line in issue.lines:
        u, o = by_line.get(line.id, (0.0, 0))
        total_returned = line.qty_returned_ok + line.qty_returned_quar + u + o
        if round(total_returned, 3) > round(line.qty, 3) + 1e-9:
            b = db.get(models.DrugBatch, line.batch_id)
            raise StockError(
                f"批号 {b.batch_no} 累计退回 {total_returned:g} 超过原领用 {line.qty:g}"
            )

    voucher = _create_voucher(
        db, vtype="return", on_date=voucher_date, drug_id=issue.drug_id,
        cow_id=issue.cow_id, related_voucher_id=issue.id,
        operator=operator, note=note,
    )
    for line in issue.lines:
        u, o = by_line.get(line.id, (0.0, 0))
        if u + o <= 0:
            continue
        db.add(models.StockVoucherLine(
            voucher_id=voucher.id, batch_id=line.batch_id, qty=u + o, qty_opened=o,
            note=f"未开封 {u:g} 回库；已开封 {o:g} 入待毁",
        ))
        fresh = _adjust_batch(db, line.batch_id, delta_ok=u, delta_quar=o)
        line.qty_returned_ok = round(line.qty_returned_ok + u, 3)
        line.qty_returned_quar = round(line.qty_returned_quar + o, 3)
        _append_ledger(
            db, batch=fresh, voucher=voucher, delta_ok=u, delta_quar=o,
            cow_id=issue.cow_id,
            note=f"退回批号 {fresh.batch_no}：未开封回库 {u:g}，已开封待毁 {o:g}",
        )
    return voucher


# ---------------- 报损 ----------------
def create_writeoff(db: Session, items: List[dict], *, voucher_date: date,
                    reason: str, operator: Optional[str] = None,
                    note: Optional[str] = None) -> models.StockVoucher:
    """
    报损出库：location=ok 核销合格库存（破损/过期），
    location=quarantine 核销待毁隔离库存（已开封退回后的销毁）。
    """
    if not reason or not reason.strip():
        raise StockError("报损必须填写原因")
    agg: Dict[int, Dict[str, float]] = {}
    drug_id = None
    for it in items or []:
        b = db.get(models.DrugBatch, it.get("batch_id"))
        if not b:
            raise StockError("报损批次不存在")
        drug_id = drug_id or b.drug_id
        if drug_id != b.drug_id:
            raise StockError("一张报损单只能处理同一种药品")
        qty = round(float(it.get("qty") or 0), 3)
        loc = it.get("location", "ok")
        if loc not in ("ok", "quarantine"):
            raise StockError("报损来源只能是 ok/quarantine")
        if qty <= 0:
            raise StockError("报损数量必须大于 0")
        slot = agg.setdefault(b.id, {"ok": 0.0, "quarantine": 0.0})
        slot[loc] = round(slot[loc] + qty, 3)
    if not agg:
        raise StockError("请填写报损明细")

    voucher = _create_voucher(
        db, vtype="writeoff", on_date=voucher_date, drug_id=drug_id,
        disposition="mixed", purpose=reason, operator=operator, note=note,
    )
    for bid, slot in agg.items():
        b = db.get(models.DrugBatch, bid)
        db.add(models.StockVoucherLine(
            voucher_id=voucher.id, batch_id=bid,
            qty=slot["ok"] + slot["quarantine"], qty_opened=slot["quarantine"],
            note=reason,
        ))
        fresh = _adjust_batch(
            db, bid,
            delta_ok=-slot["ok"] if slot["ok"] else 0.0,
            delta_quar=-slot["quarantine"] if slot["quarantine"] else 0.0,
        )
        _append_ledger(
            db, batch=fresh, voucher=voucher,
            delta_ok=-slot["ok"] if slot["ok"] else 0.0,
            delta_quar=-slot["quarantine"] if slot["quarantine"] else 0.0,
            note=f"报损（{reason}）批号 {b.batch_no}",
        )
    return voucher


# ---------------- 盘点 ----------------
def create_stocktake(db: Session, items: List[dict], *, voucher_date: date,
                     operator: Optional[str] = None,
                     note: Optional[str] = None) -> models.StockVoucher:
    """按实盘数量调整批次库存，盘盈盘亏都留流水；一张盘点单可跨药品。"""
    rows = []
    for it in items or []:
        b = db.get(models.DrugBatch, it.get("batch_id"))
        if not b:
            raise StockError("盘点批次不存在")
        actual_ok = it.get("actual_ok")
        actual_quar = it.get("actual_quarantine")
        if actual_ok is None and actual_quar is None:
            continue
        actual_ok = round(float(b.qty_ok if actual_ok is None else actual_ok), 3)
        actual_quar = round(float(b.qty_quarantine if actual_quar is None else actual_quar), 3)
        if actual_ok < 0 or actual_quar < 0:
            raise StockError("实盘数量不能为负")
        rows.append((b, actual_ok, actual_quar))
    if not rows:
        raise StockError("请至少盘点一个批次")

    voucher = _create_voucher(
        db, vtype="check", on_date=voucher_date, drug_id=None,
        operator=operator, note=note,
    )
    for b, actual_ok, actual_quar in rows:
        delta_ok = round(actual_ok - b.qty_ok, 3)
        delta_quar = round(actual_quar - b.qty_quarantine, 3)
        db.add(models.StockVoucherLine(
            voucher_id=voucher.id, batch_id=b.id, qty=b.qty_ok,
            qty_actual=actual_ok, qty_opened=actual_quar,
            note=f"待毁账面 {b.qty_quarantine:g} 实盘 {actual_quar:g}",
        ))
        if delta_ok == 0 and delta_quar == 0:
            db.add(models.StockLedger(
                batch_id=b.id, drug_id=b.drug_id, voucher_id=voucher.id,
                voucher_no=voucher.voucher_no, voucher_type="check",
                voucher_date=voucher.voucher_date,
                qty_change_ok=0, qty_change_quarantine=0,
                balance_ok=b.qty_ok, balance_quarantine=b.qty_quarantine,
                note=f"账实相符 批号 {b.batch_no}",
            ))
            continue
        fresh = _adjust_batch(db, b.id, delta_ok=delta_ok, delta_quar=delta_quar)
        parts = []
        if delta_ok:
            parts.append(f"合格库存{'盘盈' if delta_ok > 0 else '盘亏'} {abs(delta_ok):g}")
        if delta_quar:
            parts.append(f"待毁库存{'盘盈' if delta_quar > 0 else '盘亏'} {abs(delta_quar):g}")
        _append_ledger(db, batch=fresh, voucher=voucher,
                       delta_ok=delta_ok, delta_quar=delta_quar,
                       note=f"盘点批号 {b.batch_no}：{'，'.join(parts)}")
    return voucher


# ---------------- 撤销领用（整单红冲） ----------------
def reverse_issue(db: Session, issue_id: int, *, operator: Optional[str] = None
                  ) -> tuple[models.StockVoucher, Optional[models.Medication]]:
    """
    整单撤销领用：未发生过退回的已生效领用单可撤销，数量原路退回各批次合格库存；
    与“退回已开封药（入待毁）”去向严格区分。
    若该领用单关联了用药记录，用药记录同步删除（撤销即从未出库/用药）。
    """
    issue = db.get(models.StockVoucher, issue_id)
    if not issue or issue.voucher_type != "issue":
        raise StockError("领用单不存在")
    if issue.status == "reversed":
        raise StockError("该领用单已撤销，不能重复撤销")
    returned = [ln for ln in issue.lines
                if ln.qty_returned_ok > 0 or ln.qty_returned_quar > 0]
    if returned:
        raise StockError("该领用单已有部分退回，不能整单撤销；剩余部分请走退回流程")

    med = (
        db.query(models.Medication).filter_by(voucher_id=issue.id).first()
    )
    med_snapshot = ""
    if med:
        cow = db.get(models.Cow, med.cow_id)
        med_snapshot = f"同步作废用药记录 #{med.id}（{cow.ear_tag if cow else ''} {med.drug_name} {med.date}）"

    revoke = _create_voucher(
        db, vtype="revoke", on_date=date.today(), drug_id=issue.drug_id,
        cow_id=issue.cow_id, related_voucher_id=issue.id,
        operator=operator,
        note=f"撤销领用单 {issue.voucher_no}。{med_snapshot}{('；' + issue.note) if issue.note else ''}",
    )
    for ln in issue.lines:
        db.add(models.StockVoucherLine(
            voucher_id=revoke.id, batch_id=ln.batch_id, qty=ln.qty,
            note=f"红冲领用单 {issue.voucher_no}",
        ))
        fresh = _adjust_batch(db, ln.batch_id, delta_ok=ln.qty)
        _append_ledger(db, batch=fresh, voucher=revoke, delta_ok=ln.qty,
                       cow_id=issue.cow_id,
                       note=f"撤销领用，退回批号 {fresh.batch_no} {ln.qty:g}")

    issue.status = "reversed"
    if med:
        db.delete(med)
    db.flush()
    return revoke, med


# ---------------- 序列化 ----------------
def batch_to_dict(b: models.DrugBatch, today: Optional[date] = None,
                  drug: Optional[models.DrugCatalog] = None) -> dict:
    today = today or date.today()
    drug = drug or b.drug
    days = (b.expiry_date - today).days
    if days < 0:
        status = "expired"
    elif days <= 30:
        status = "near_expiry"
    elif b.qty_ok <= 0:
        status = "out"
    else:
        status = "available"
    return {
        "id": b.id, "drug_id": b.drug_id,
        "drug_name": drug.name if drug else None,
        "unit": drug.unit if drug else None,
        "batch_no": b.batch_no, "expiry_date": str(b.expiry_date),
        "inbound_date": str(b.inbound_date),
        "qty_ok": b.qty_ok, "qty_quarantine": b.qty_quarantine,
        "supplier": b.supplier, "active": b.active, "note": b.note,
        "days_to_expiry": days, "status": status,
    }


def voucher_to_dict(v: models.DrugBatch, db: Session) -> dict:
    drug = v.drug
    cow = v.cow
    return {
        "id": v.id, "voucher_no": v.voucher_no, "voucher_type": v.voucher_type,
        "type_label": VOUCHER_LABEL.get(v.voucher_type, v.voucher_type),
        "voucher_date": str(v.voucher_date),
        "drug_id": v.drug_id,
        "drug_name": drug.name if drug else None,
        "unit": drug.unit if drug else None,
        "cow_id": v.cow_id,
        "cow_tag": cow.ear_tag if cow else None,
        "cow_name": cow.name if cow else None,
        "status": v.status,
        "disposition": v.disposition, "purpose": v.purpose,
        "related_voucher_id": v.related_voucher_id,
        "operator": v.operator, "note": v.note,
        "created_at": str(v.created_at) if v.created_at else None,
        "lines": [{
            "line_id": ln.id, "batch_id": ln.batch_id,
            "batch_no": ln.batch.batch_no if ln.batch else None,
            "expiry_date": str(ln.batch.expiry_date) if ln.batch else None,
            "qty": ln.qty, "qty_actual": ln.qty_actual,
            "qty_opened": ln.qty_opened,
            "qty_returned_ok": ln.qty_returned_ok,
            "qty_returned_quar": ln.qty_returned_quar,
            "note": ln.note,
        } for ln in v.lines],
    }


def ledger_to_dict(r: models.StockLedger, db: Session) -> dict:
    batch = db.get(models.DrugBatch, r.batch_id)
    cow = db.get(models.Cow, r.cow_id) if r.cow_id else None
    return {
        "id": r.id, "batch_id": r.batch_id,
        "batch_no": batch.batch_no if batch else f"#{r.batch_id}",
        "expiry_date": str(batch.expiry_date) if batch else None,
        "drug_id": r.drug_id,
        "drug_name": (db.get(models.DrugCatalog, r.drug_id).name
                      if db.get(models.DrugCatalog, r.drug_id) else None),
        "voucher_id": r.voucher_id, "voucher_no": r.voucher_no,
        "voucher_type": r.voucher_type,
        "type_label": VOUCHER_LABEL.get(r.voucher_type, r.voucher_type),
        "voucher_date": str(r.voucher_date),
        "qty_change_ok": r.qty_change_ok,
        "qty_change_quarantine": r.qty_change_quarantine,
        "balance_ok": r.balance_ok, "balance_quarantine": r.balance_quarantine,
        "cow_tag": cow.ear_tag if cow else None,
        "note": r.note, "created_at": str(r.created_at) if r.created_at else None,
    }


# ---------------- 对账：流水与库存/用药消耗 ----------------
def reconcile(db: Session, today: Optional[date] = None) -> dict:
    """
    三项核对：
    1) 每个批次：末条流水余额 == 批次表现存（流水账与实物账对得上）
    2) 每条已生效领用流水：数量与单据明细一致
    3) 用药消耗：关联了领用单的用药，其领用单真实存在且在生效状态（消耗对得上出库）
    """
    today = today or date.today()
    problems: List[dict] = []

    batches = db.query(models.DrugBatch).all()
    for b in batches:
        last = (
            db.query(models.StockLedger)
            .filter_by(batch_id=b.id)
            .order_by(models.StockLedger.id.desc()).first()
        )
        if last is None:
            if b.qty_ok or b.qty_quarantine:
                problems.append({
                    "type": "missing_ledger", "batch_id": b.id,
                    "batch_no": b.batch_no,
                    "message": f"批号 {b.batch_no} 有库存但无流水",
                })
            continue
        if abs(last.balance_ok - b.qty_ok) > 1e-6 or \
                abs(last.balance_quarantine - b.qty_quarantine) > 1e-6:
            problems.append({
                "type": "balance_mismatch", "batch_id": b.id,
                "batch_no": b.batch_no,
                "message": f"批号 {b.batch_no} 流水余额 "
                           f"({last.balance_ok:g}/{last.balance_quarantine:g}) "
                           f"与批次库存 ({b.qty_ok:g}/{b.qty_quarantine:g}) 不一致",
            })

    # 用药消耗 ↔ 领用流水
    meds = db.query(models.Medication).filter(models.Medication.voucher_id.isnot(None)).all()
    for m in meds:
        v = db.get(models.StockVoucher, m.voucher_id)
        if not v or v.voucher_type != "issue":
            problems.append({
                "type": "med_orphan_voucher", "medication_id": m.id,
                "message": f"用药记录 #{m.id} 关联的领用单不存在",
            })
        elif v.status != "posted":
            problems.append({
                "type": "med_reversed_voucher", "medication_id": m.id,
                "message": f"用药记录 #{m.id} 关联的领用单 {v.voucher_no} 已撤销",
            })

    return {
        "checked_at": str(today),
        "ok": not problems,
        "problems": problems,
        "batch_count": len(batches),
        "ledger_count": db.query(models.StockLedger).count(),
        "voucher_count": db.query(models.StockVoucher).count(),
    }


def stock_summary(db: Session, today: Optional[date] = None) -> dict:
    """首页统计：品种数、批次、可发/待毁、近效期与过期"""
    today = today or date.today()
    batches = db.query(models.DrugBatch).filter_by(active=True).all()
    near = [b for b in batches if 0 <= (b.expiry_date - today).days <= 30 and b.qty_ok > 0]
    expired = [b for b in batches if b.expiry_date < today and
               (b.qty_ok > 0 or b.qty_quarantine > 0)]
    return {
        "drug_kind_count": db.query(models.DrugCatalog).filter_by(active=True).count(),
        "batch_count": len(batches),
        "near_expiry_count": len(near),
        "expired_count": len(expired),
        "quarantine_batch_count": sum(1 for b in batches if b.qty_quarantine > 0),
    }
