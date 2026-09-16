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
    note: Optional[str] = None


class DrugOut(DrugCreate):
    id: int
    active: bool = True

    class Config:
        from_attributes = True


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


# ---------- 用药疗程 ----------
class CourseDrugItem(BaseModel):
    """创建疗程时的一条用药安排"""
    drug_id: Optional[int] = None
    drug_name: Optional[str] = Field(None, max_length=64)
    planned_dose: Optional[str] = None
    route: Optional[str] = None
    times_per_day: int = Field(1, ge=1, le=3)
    interval_days: int = Field(1, ge=1, le=30)
    planned_times: Optional[List[str]] = Field(
        None, description="每日班次，如 ['morning','evening']，长度需与 times_per_day 一致")
    withdrawal_days: Optional[int] = Field(None, ge=0, le=365)
    total_doses: Optional[int] = Field(None, ge=1, le=60,
                                       description="总次数；不填则按 days 推算")
    days: Optional[int] = Field(None, ge=1, le=60, description="连用天数")


class CourseCreate(BaseModel):
    cow_id: int
    health_record_id: Optional[int] = None
    title: str = Field(..., max_length=128)
    start_date: date
    planned_end_date: Optional[date] = None
    veterinarian: Optional[str] = None
    note: Optional[str] = None
    drugs: List[CourseDrugItem]


class CourseNote(BaseModel):
    note: str = Field(..., max_length=2000)


class CourseEnd(BaseModel):
    end_date: Optional[date] = None
    end_reason: str = Field(..., min_length=1, max_length=128)


class AddDrugPayload(CourseDrugItem):
    """疗程中途加药/换药（新用药行）"""
    start_date: Optional[date] = None
    reason: Optional[str] = Field(None, max_length=255, description="换药/加药原因；换药时必填")
    replace_line_id: Optional[int] = Field(None, description="被替换（换药停用）的用药行 id")


class AdministerPayload(BaseModel):
    """逐次给药：记录实际时间与剂量；休药期随实际给药日更新"""
    administered_date: Optional[date] = None
    administered_time: Optional[str] = Field(None, pattern="^(morning|noon|evening)$")
    administered_dose: Optional[str] = None
    withdrawal_days: Optional[int] = Field(None, ge=0, le=365)
    operator: Optional[str] = None
    note: Optional[str] = None


class DoseSkipPayload(BaseModel):
    """漏用 / 取消：必须注明原因"""
    reason: str = Field(..., min_length=1, max_length=255)
    note: Optional[str] = None


class DoseDelayPayload(BaseModel):
    """延期：保留原计划，记录新日期与原因"""
    delayed_to: date
    reason: str = Field(..., min_length=1, max_length=255)
    note: Optional[str] = None


class LineChangePayload(BaseModel):
    """停药：必须注明原因"""
    reason: str = Field(..., min_length=1, max_length=255)


class LineResumePayload(BaseModel):
    """恢复用药（撤销停药）：备注可选"""
    reason: Optional[str] = Field(None, max_length=255)


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
