"""ORM 数据模型"""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from .database import Base

# 受版本化审计管理的实体类型
AUDIT_ENTITIES = ("milking", "health", "medication")


class Cow(Base):
    """奶牛档案"""

    __tablename__ = "cows"

    id = Column(Integer, primary_key=True, index=True)
    ear_tag = Column(String(16), unique=True, nullable=False, index=True, comment="耳标号")
    name = Column(String(32), nullable=True, comment="牛名/昵称")
    breed = Column(String(32), nullable=False, default="荷斯坦牛", comment="品种")
    birth_date = Column(Date, nullable=False, comment="出生日期")
    parity = Column(Integer, nullable=False, default=1, comment="胎次")
    status = Column(
        String(16),
        nullable=False,
        default="lactating",
        comment="lactating 泌乳中 / dry 干奶 / pregnant 待产 / sold 已离场",
    )
    group = Column(String(32), nullable=True, comment="牛舍/群组")
    calving_date = Column(Date, nullable=True, comment="最近一次产犊日期")
    expected_calving_date = Column(Date, nullable=True, comment="预产期")
    avg_yield_kg = Column(Float, nullable=True, comment="标定日产奶量(kg)，用于异常参考")
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    milkings = relationship("MilkingRecord", back_populates="cow", cascade="all, delete-orphan")
    health_records = relationship("HealthRecord", back_populates="cow", cascade="all, delete-orphan")
    medications = relationship("Medication", back_populates="cow", cascade="all, delete-orphan")
    estruses = relationship("EstrusRecord", back_populates="cow", cascade="all, delete-orphan")


class MilkingRecord(Base):
    """挤奶记录"""

    __tablename__ = "milking_records"

    id = Column(Integer, primary_key=True, index=True)
    cow_id = Column(Integer, ForeignKey("cows.id", ondelete="CASCADE"), nullable=False, index=True)
    date = Column(Date, nullable=False, index=True)
    session = Column(String(8), nullable=False, comment="morning 早班 / noon 午班 / evening 晚班")
    yield_kg = Column(Float, nullable=False, comment="产奶量(kg)")
    scc = Column(Integer, nullable=True, comment="体细胞数 cells/mL")
    discarded = Column(Boolean, default=False, comment="是否因休药期废弃")
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    # ---- 版本化审计字段 ----
    version = Column(Integer, nullable=False, default=1, comment="当前版本号，每次更正+1")
    is_void = Column(Boolean, nullable=False, default=False, index=True, comment="是否已作废（软删除，可恢复）")
    voided_at = Column(DateTime, nullable=True)

    cow = relationship("Cow", back_populates="milkings")


class HealthRecord(Base):
    """健康检查/疾病记录"""

    __tablename__ = "health_records"

    id = Column(Integer, primary_key=True, index=True)
    cow_id = Column(Integer, ForeignKey("cows.id", ondelete="CASCADE"), nullable=False, index=True)
    date = Column(Date, nullable=False, index=True)
    record_type = Column(String(16), nullable=False, default="checkup",
                         comment="checkup 常规体检 / diagnosis 疾病诊断 / vaccination 免疫")
    diagnosis = Column(String(64), nullable=True, comment="诊断或检查项目，如 乳房炎")
    temperature = Column(Float, nullable=True, comment="体温 ℃")
    severity = Column(String(8), nullable=True, comment="mild/moderate/severe")
    follow_up_date = Column(Date, nullable=True, comment="复查日期")
    result = Column(String(16), nullable=True, comment="recovered 已康复 / ongoing 治疗中 / observed 观察中")
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    # ---- 版本化审计字段 ----
    version = Column(Integer, nullable=False, default=1)
    is_void = Column(Boolean, nullable=False, default=False, index=True)
    voided_at = Column(DateTime, nullable=True)

    cow = relationship("Cow", back_populates="health_records")


class DrugCatalog(Base):
    """药品目录（含默认休药期）"""

    __tablename__ = "drug_catalog"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(64), unique=True, nullable=False, comment="药品名称")
    usage = Column(String(128), nullable=True, comment="用途/类别，如 抗生素/激素/消炎")
    default_withdrawal_days = Column(Integer, nullable=False, default=0, comment="默认牛奶休药期(天)")
    note = Column(Text, nullable=True)
    active = Column(Boolean, default=True)


class Medication(Base):
    """用药记录（关联药品休药期）"""

    __tablename__ = "medications"

    id = Column(Integer, primary_key=True, index=True)
    cow_id = Column(Integer, ForeignKey("cows.id", ondelete="CASCADE"), nullable=False, index=True)
    drug_id = Column(Integer, ForeignKey("drug_catalog.id"), nullable=True)
    drug_name = Column(String(64), nullable=False, comment="药品名称快照")
    date = Column(Date, nullable=False, index=True, comment="用药日期")
    dose = Column(String(64), nullable=True, comment="剂量")
    route = Column(String(32), nullable=True, comment="给药途径：肌注/静注/乳头灌注/口服")
    reason = Column(String(128), nullable=True, comment="用药原因")
    withdrawal_days = Column(Integer, nullable=False, default=0, comment="牛奶休药期(天)")
    withdrawal_end = Column(Date, nullable=False, index=True, comment="休药期截止日(当天仍休药)")
    next_dose_date = Column(Date, nullable=True, comment="下次用药日期")
    treated = Column(Boolean, default=False, comment="下次用药是否已处理")
    operator = Column(String(32), nullable=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    # ---- 版本化审计字段 ----
    version = Column(Integer, nullable=False, default=1)
    is_void = Column(Boolean, nullable=False, default=False, index=True)
    voided_at = Column(DateTime, nullable=True)

    cow = relationship("Cow", back_populates="medications")
    drug = relationship("DrugCatalog")


class EstrusRecord(Base):
    """发情/配种记录"""

    __tablename__ = "estrus_records"

    id = Column(Integer, primary_key=True, index=True)
    cow_id = Column(Integer, ForeignKey("cows.id", ondelete="CASCADE"), nullable=False, index=True)
    date = Column(Date, nullable=False, index=True, comment="发情日期")
    detection = Column(String(16), nullable=False, default="observed",
                       comment="observed 人工观察 / activity 计步器 / detector 蜡笔检测")
    score = Column(Integer, nullable=True, comment="发情强度评分1-5")
    inseminated = Column(Boolean, default=False, comment="是否已配种")
    insemination_date = Column(Date, nullable=True, comment="配种日期")
    semen = Column(String(64), nullable=True, comment="冻精编号/公牛号")
    technician = Column(String(32), nullable=True, comment="配种员")
    result = Column(String(16), nullable=True,
                    comment="pending 待检 / pregnant 已孕确认 / negative 未孕 / unknown")
    result_date = Column(Date, nullable=True, comment="孕检日期")
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    cow = relationship("Cow", back_populates="estruses")


class AuditLog(Base):
    """记录更正/作废/恢复/撤销的审计流水（只追加，不修改不删除）"""

    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    entity_type = Column(String(16), nullable=False, index=True,
                         comment="milking / health / medication")
    entity_id = Column(Integer, nullable=False, index=True)
    cow_id = Column(Integer, nullable=False, index=True)
    seq = Column(Integer, nullable=False, comment="该实体上的操作序号，从1开始")
    action = Column(String(16), nullable=False,
                    comment="create 录入 / correct 更正 / void 作废 / restore 恢复 / undo 撤销更正 / baseline 历史起点")
    version_before = Column(Integer, nullable=True, comment="操作前版本号")
    version_after = Column(Integer, nullable=True, comment="操作后版本号")
    operator = Column(String(32), nullable=True, comment="操作人（手填，不接入账号体系）")
    reason = Column(Text, nullable=True, comment="操作原因")
    before_data = Column(JSON, nullable=True, comment="操作前业务字段快照")
    after_data = Column(JSON, nullable=True, comment="操作后业务字段快照")
    operated_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        Index("ix_audit_entity", "entity_type", "entity_id", "seq"),
    )
