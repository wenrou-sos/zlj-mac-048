"""Pydantic 请求/响应模型"""
from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel, Field


# ---------- 奶牛 ----------
class CowBase(BaseModel):
    ear_tag: str = Field(..., max_length=16, description="耳标号")
    name: Optional[str] = Field(None, max_length=32)
    breed: str = "荷斯坦牛"
    birth_date: date
    parity: int = Field(1, ge=0, le=20)
    status: str = "lactating"
    group: Optional[str] = None
    calving_date: Optional[date] = None
    expected_calving_date: Optional[date] = None
    avg_yield_kg: Optional[float] = Field(None, ge=0)
    note: Optional[str] = None


class CowCreate(CowBase):
    pass


class CowUpdate(BaseModel):
    name: Optional[str] = None
    breed: Optional[str] = None
    birth_date: Optional[date] = None
    parity: Optional[int] = Field(None, ge=0, le=20)
    status: Optional[str] = None
    group: Optional[str] = None
    calving_date: Optional[date] = None
    expected_calving_date: Optional[date] = None
    avg_yield_kg: Optional[float] = Field(None, ge=0)
    note: Optional[str] = None


class CowOut(CowBase):
    id: int
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ---------- 挤奶记录 ----------
class MilkingCreate(BaseModel):
    cow_id: int
    date: date
    session: str = Field(..., pattern="^(morning|noon|evening)$")
    yield_kg: float = Field(..., ge=0)
    scc: Optional[int] = Field(None, ge=0)
    discarded: bool = False
    note: Optional[str] = None


class MilkingUpdate(BaseModel):
    session: Optional[str] = Field(None, pattern="^(morning|noon|evening)$")
    yield_kg: Optional[float] = Field(None, ge=0)
    scc: Optional[int] = Field(None, ge=0)
    discarded: Optional[bool] = None
    note: Optional[str] = None


class MilkingOut(BaseModel):
    id: int
    cow_id: int
    date: date
    session: str
    yield_kg: float
    scc: Optional[int] = None
    discarded: bool
    note: Optional[str] = None
    created_at: Optional[datetime] = None
    cow_ear_tag: Optional[str] = None
    cow_name: Optional[str] = None
    in_withdrawal: Optional[bool] = False
    withdrawal_until: Optional[date] = None

    class Config:
        from_attributes = True


# ---------- 健康 ----------
class HealthCreate(BaseModel):
    cow_id: int
    date: date
    record_type: str = Field("checkup", pattern="^(checkup|diagnosis|vaccination)$")
    diagnosis: Optional[str] = None
    temperature: Optional[float] = Field(None, ge=30, le=45)
    severity: Optional[str] = Field(None, pattern="^(mild|moderate|severe)$")
    follow_up_date: Optional[date] = None
    result: Optional[str] = Field(None, pattern="^(recovered|ongoing|observed)$")
    note: Optional[str] = None


class HealthUpdate(BaseModel):
    record_type: Optional[str] = None
    diagnosis: Optional[str] = None
    temperature: Optional[float] = None
    severity: Optional[str] = None
    follow_up_date: Optional[date] = None
    result: Optional[str] = None
    note: Optional[str] = None


class HealthOut(HealthCreate):
    id: int
    created_at: Optional[datetime] = None
    cow_ear_tag: Optional[str] = None

    class Config:
        from_attributes = True


# ---------- 药品 ----------
class DrugCreate(BaseModel):
    name: str = Field(..., max_length=64)
    usage: Optional[str] = None
    default_withdrawal_days: int = Field(0, ge=0, le=365)
    unit: str = Field("支", max_length=16)
    note: Optional[str] = None


class DrugOut(DrugCreate):
    id: int
    active: bool = True

    class Config:
        from_attributes = True


# ---------- 库存：入库 / 领用 / 退回 / 报损 / 盘点 ----------
class ReceiptCreate(BaseModel):
    drug_id: int
    batch_no: str = Field(..., min_length=1, max_length=64)
    expiry_date: date
    qty: float = Field(..., gt=0)
    voucher_date: Optional[date] = None
    supplier: Optional[str] = None
    operator: Optional[str] = None
    note: Optional[str] = None


class IssueLine(BaseModel):
    batch_id: int
    qty: float = Field(..., gt=0)


class IssueCreate(BaseModel):
    drug_id: int
    lines: List[IssueLine] = Field(..., min_length=1)
    voucher_date: Optional[date] = None
    cow_id: Optional[int] = None
    purpose: Optional[str] = None
    operator: Optional[str] = None
    note: Optional[str] = None


class ReturnItem(BaseModel):
    line_id: int
    qty_unopened: float = Field(0, ge=0)
    qty_opened: float = Field(0, ge=0)


class ReturnCreate(BaseModel):
    issue_id: int
    items: List[ReturnItem] = Field(..., min_length=1)
    voucher_date: Optional[date] = None
    operator: Optional[str] = None
    note: Optional[str] = None


class WriteoffItem(BaseModel):
    batch_id: int
    qty: float = Field(..., gt=0)
    location: str = Field("ok", pattern="^(ok|quarantine)$")


class WriteoffCreate(BaseModel):
    items: List[WriteoffItem] = Field(..., min_length=1)
    reason: str = Field(..., min_length=1, max_length=128)
    voucher_date: Optional[date] = None
    operator: Optional[str] = None
    note: Optional[str] = None


class StocktakeItem(BaseModel):
    batch_id: int
    actual_ok: Optional[float] = Field(None, ge=0)
    actual_quarantine: Optional[float] = Field(None, ge=0)


class StocktakeCreate(BaseModel):
    items: List[StocktakeItem] = Field(..., min_length=1)
    voucher_date: Optional[date] = None
    operator: Optional[str] = None
    note: Optional[str] = None


# ---------- 用药 ----------
class MedicationCreate(BaseModel):
    cow_id: int
    drug_id: Optional[int] = None
    drug_name: Optional[str] = Field(None, max_length=64)
    date: date
    dose: Optional[str] = None
    route: Optional[str] = None
    reason: Optional[str] = None
    withdrawal_days: Optional[int] = Field(None, ge=0, le=365)
    next_dose_date: Optional[date] = None
    operator: Optional[str] = None
    note: Optional[str] = None
    # 实际用药消耗：指定领用批次（多批次凑齐）；不传则只登记用药不扣库存（历史补录）
    issue_lines: Optional[List[IssueLine]] = None


class MedicationUpdate(BaseModel):
    treated: Optional[bool] = None
    dose: Optional[str] = None
    route: Optional[str] = None
    reason: Optional[str] = None
    withdrawal_days: Optional[int] = Field(None, ge=0, le=365)
    next_dose_date: Optional[date] = None
    note: Optional[str] = None


class MedicationOut(BaseModel):
    id: int
    cow_id: int
    drug_id: Optional[int] = None
    drug_name: str
    date: date
    dose: Optional[str] = None
    route: Optional[str] = None
    reason: Optional[str] = None
    withdrawal_days: int
    withdrawal_end: date
    next_dose_date: Optional[date] = None
    treated: bool
    operator: Optional[str] = None
    note: Optional[str] = None
    created_at: Optional[datetime] = None
    cow_ear_tag: Optional[str] = None
    active_withdrawal: Optional[bool] = None

    class Config:
        from_attributes = True


# ---------- 发情/配种 ----------
class EstrusCreate(BaseModel):
    cow_id: int
    date: date
    detection: str = Field("observed", pattern="^(observed|activity|detector)$")
    score: Optional[int] = Field(None, ge=1, le=5)
    inseminated: bool = False
    insemination_date: Optional[date] = None
    semen: Optional[str] = None
    technician: Optional[str] = None
    result: Optional[str] = Field(None, pattern="^(pending|pregnant|negative|unknown)$")
    result_date: Optional[date] = None
    note: Optional[str] = None


class EstrusUpdate(BaseModel):
    score: Optional[int] = Field(None, ge=1, le=5)
    inseminated: Optional[bool] = None
    insemination_date: Optional[date] = None
    semen: Optional[str] = None
    technician: Optional[str] = None
    result: Optional[str] = Field(None, pattern="^(pending|pregnant|negative|unknown)$")
    result_date: Optional[date] = None
    note: Optional[str] = None


class EstrusOut(EstrusCreate):
    id: int
    created_at: Optional[datetime] = None
    cow_ear_tag: Optional[str] = None

    class Config:
        from_attributes = True
