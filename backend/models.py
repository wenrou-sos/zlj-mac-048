"""ORM 数据模型"""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from .database import Base


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
    anomaly_cases = relationship("AnomalyCase", back_populates="cow", cascade="all, delete-orphan")


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


class AnomalyCase(Base):
    """奶量异常调查单：同一头牛一次连续异常归为一单，恢复后再次异常另开新单并指向上一单"""

    __tablename__ = "anomaly_cases"

    id = Column(Integer, primary_key=True, index=True)
    cow_id = Column(Integer, ForeignKey("cows.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(
        String(16), nullable=False, default="open", index=True,
        comment="open 调查中 / resolved 已恢复关闭 / false_positive 误报关闭 / reopened 重开续跟",
    )
    detected_on = Column(Date, nullable=False, index=True, comment="首次发现日期（取首个命中日）")
    first_tags = Column(String(128), nullable=True, comment="发现时异常类型快照，逗号分隔")
    first_level = Column(String(8), nullable=False, default="info", comment="发现时风险等级")
    latest_level = Column(String(8), nullable=False, default="info", comment="最近一次评估风险等级")
    latest_tags = Column(String(128), nullable=True, comment="最近一次评估异常类型，逗号分隔")
    latest_evidence_date = Column(Date, nullable=True, index=True, comment="最近一次命中/评估日期")
    first_snapshot = Column(Text, nullable=True, comment="发现时证据快照（JSON 冻结，不随后续补改变化）")
    finding = Column(Text, nullable=True, comment="排查结论/调查说明")
    false_reason = Column(Text, nullable=True, comment="误报原因（status=false_positive 时必填）")
    follow_up_date = Column(Date, nullable=True, index=True, comment="计划复查日")
    closed_at = Column(Date, nullable=True)
    close_note = Column(Text, nullable=True, comment="关闭时备注")
    parent_case_id = Column(
        Integer, ForeignKey("anomaly_cases.id", ondelete="SET NULL"),
        nullable=True, index=True, comment="恢复后再次异常时，上一单ID",
    )
    created_at = Column(DateTime, default=datetime.utcnow)

    cow = relationship("Cow", back_populates="anomaly_cases")
    evidence = relationship(
        "AnomalyEvidence", back_populates="case",
        cascade="all, delete-orphan", order_by="AnomalyEvidence.id.desc()",
    )
    health_links = relationship(
        "CaseHealthLink", back_populates="case", cascade="all, delete-orphan",
    )


class AnomalyEvidence(Base):
    """调查证据：发现 / 跟踪 / 复查 / 关闭节点的奶量与体细胞证据快照（只追加，不修改）"""

    __tablename__ = "anomaly_evidence"

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("anomaly_cases.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = Column(
        String(16), nullable=False, default="detect",
        comment="detect 发现 / followup 系统跟踪 / recheck 人工复查 / close 关闭 / reopen 重开",
    )
    eval_date = Column(Date, nullable=False, index=True, comment="评估日期")
    level = Column(String(8), nullable=True, comment="danger/warning/info/ok（ok=复查正常）")
    tags = Column(String(128), nullable=True)
    signature = Column(String(64), nullable=True, comment="信号指纹，相同判断不重复追加")
    snapshot = Column(Text, nullable=True, comment="当时奶量/SCC/基线/命中记录的 JSON 快照")
    note = Column(Text, nullable=True, comment="复查意见/说明")
    operator = Column(String(32), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    case = relationship("AnomalyCase", back_populates="evidence")


class CaseHealthLink(Base):
    """调查单与已有健康记录的关联（多对多，手动关联；删健康记录时关联自动解除）"""

    __tablename__ = "case_health_links"

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("anomaly_cases.id", ondelete="CASCADE"), nullable=False, index=True)
    health_id = Column(Integer, ForeignKey("health_records.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    case = relationship("AnomalyCase", back_populates="health_links")
    health = relationship("HealthRecord")
