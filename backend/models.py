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
    import_batch_id = Column(
        Integer, ForeignKey("import_batches.id", ondelete="SET NULL"),
        nullable=True, index=True, comment="来源导入批次（手工录入为空）",
    )
    created_at = Column(DateTime, default=datetime.utcnow)

    cow = relationship("Cow", back_populates="milkings")
    import_batch = relationship("ImportBatch", back_populates="milkings")


class ImportBatch(Base):
    """奶量 CSV 导入批次（file_hash 为幂等键，防止同一文件重复入账）"""

    __tablename__ = "import_batches"

    id = Column(Integer, primary_key=True, index=True)
    file_hash = Column(String(64), nullable=False, index=True, comment="文件内容 SHA-256")
    filename = Column(String(128), nullable=False, comment="原始文件名")
    content = Column(Text, nullable=False, comment="原始 CSV 文本（解码后），供入账时重放解析")
    status = Column(String(16), nullable=False, default="preview",
                    comment="preview 待确认 / committed 已入账")
    total_rows = Column(Integer, nullable=False, default=0, comment="数据行总数")
    ok_rows = Column(Integer, nullable=False, default=0, comment="可入账行数")
    unknown_tag_rows = Column(Integer, nullable=False, default=0, comment="未知耳标行数")
    duplicate_rows = Column(Integer, nullable=False, default=0, comment="文件内重复行数")
    conflict_rows = Column(Integer, nullable=False, default=0, comment="与已有记录冲突行数")
    invalid_rows = Column(Integer, nullable=False, default=0, comment="格式错误行数")
    auto_discard_rows = Column(Integer, nullable=False, default=0, comment="休药期自动废弃行数")
    inserted_rows = Column(Integer, nullable=False, default=0, comment="实际入账记录数")
    error = Column(Text, nullable=True, comment="最近一次入账失败原因（未留下半批数据）")
    created_at = Column(DateTime, default=datetime.utcnow)
    committed_at = Column(DateTime, nullable=True)

    rows = relationship("ImportRow", back_populates="batch",
                        cascade="all, delete-orphan", order_by="ImportRow.row_no")
    milkings = relationship("MilkingRecord", back_populates="import_batch")


class ImportRow(Base):
    """导入批次的单行：保留原始文本与解析/校验结果，便于事后追溯"""

    __tablename__ = "import_rows"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("import_batches.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    row_no = Column(Integer, nullable=False, comment="CSV 中的行号（含表头从1计）")
    raw = Column(Text, nullable=False, comment="原始行文本")
    ear_tag = Column(String(16), nullable=True)
    date = Column(Date, nullable=True)
    session = Column(String(8), nullable=True)
    yield_kg = Column(Float, nullable=True)
    scc = Column(Integer, nullable=True)
    status = Column(String(16), nullable=False,
                    comment="ok 可入账 / unknown_tag 未知耳标 / duplicate 文件内重复 / "
                            "conflict 与已有记录冲突 / invalid 格式错误")
    message = Column(Text, nullable=True, comment="状态说明")
    milking_record_id = Column(Integer, ForeignKey("milking_records.id", ondelete="SET NULL"),
                               nullable=True, comment="入账后关联的挤奶记录")

    batch = relationship("ImportBatch", back_populates="rows")
    milking_record = relationship("MilkingRecord")


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
