"""ORM 数据模型"""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
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
    unit = Column(String(16), nullable=False, default="支", comment="库存计量单位：支/瓶/盒/mL")
    note = Column(Text, nullable=True)
    active = Column(Boolean, default=True)

    batches = relationship(
        "DrugBatch", back_populates="drug", cascade="all, delete-orphan"
    )


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
    voucher_id = Column(
        Integer,
        ForeignKey("stock_vouchers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="实际用药消耗关联的领用单（库存来源）",
    )
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    cow = relationship("Cow", back_populates="medications")
    drug = relationship("DrugCatalog")
    voucher = relationship("StockVoucher", foreign_keys=[voucher_id])


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


# ---------------- 药品库存：批次 / 单据 / 流水 ----------------
class DrugBatch(Base):
    """药品批次库存：同一药品按批号+效期分别建账"""

    __tablename__ = "drug_batches"
    __table_args__ = (
        UniqueConstraint("drug_id", "batch_no", name="uq_drug_batch"),
        # 数据库层兜底：任何路径都不允许把库存写成负数
        CheckConstraint("qty_ok >= 0", name="ck_batch_qty_ok_nonneg"),
        CheckConstraint("qty_quarantine >= 0", name="ck_batch_qty_quar_nonneg"),
    )

    id = Column(Integer, primary_key=True, index=True)
    drug_id = Column(Integer, ForeignKey("drug_catalog.id", ondelete="RESTRICT"),
                     nullable=False, index=True)
    batch_no = Column(String(64), nullable=False, comment="生产批号")
    expiry_date = Column(Date, nullable=False, index=True, comment="有效期至（该日及之前可发）")
    qty_ok = Column(Float, nullable=False, default=0, comment="合格可发库存")
    qty_quarantine = Column(
        Float, nullable=False, default=0,
        comment="待毁隔离库存：已开封退回/不能再发的数量，仅可报损",
    )
    supplier = Column(String(64), nullable=True, comment="供应商")
    inbound_date = Column(Date, nullable=False, comment="首次入库日期")
    active = Column(Boolean, default=True, comment="无库存且无流水后置为停用")
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    drug = relationship("DrugCatalog", back_populates="batches")
    lines = relationship("StockVoucherLine", back_populates="batch")


class StockVoucher(Base):
    """库存单据：入库 / 领用 / 退回 / 报损 / 盘点 / 撤销领用"""

    __tablename__ = "stock_vouchers"
    __table_args__ = (
        Index("ix_voucher_drug_date", "drug_id", "voucher_date"),
    )

    id = Column(Integer, primary_key=True, index=True)
    voucher_no = Column(String(24), unique=True, nullable=False, index=True, comment="单据编号")
    voucher_type = Column(
        String(12), nullable=False,
        comment="receipt 入库 / issue 领用 / return 退回 / writeoff 报损 / "
                "check 盘点 / revoke 撤销领用",
    )
    voucher_date = Column(Date, nullable=False, index=True)
    drug_id = Column(Integer, ForeignKey("drug_catalog.id"), nullable=True, index=True)
    cow_id = Column(Integer, ForeignKey("cows.id", ondelete="SET NULL"),
                    nullable=True, index=True, comment="领用/用药对象")
    status = Column(
        String(12), nullable=False, default="posted",
        comment="posted 已生效 / reversed 已撤销（仅领用单会进入此状态）",
    )
    operator = Column(String(32), nullable=True)
    purpose = Column(String(128), nullable=True, comment="领用用途/用药原因")
    # 退回：unopened 未开封回库 / opened 已开封入待毁；报损：ok 合格库存 / quarantine 待毁库存
    disposition = Column(String(12), nullable=True)
    related_voucher_id = Column(
        Integer, ForeignKey("stock_vouchers.id"), nullable=True,
        comment="退回/撤销指向的原领用单",
    )
    med_id = Column(Integer, ForeignKey("medications.id", ondelete="SET NULL"),
                    nullable=True, comment="撤销领用同步作废的用药记录")
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    lines = relationship(
        "StockVoucherLine", back_populates="voucher",
        cascade="all, delete-orphan",
    )
    drug = relationship("DrugCatalog")
    cow = relationship("Cow")


class StockVoucherLine(Base):
    """单据明细：批次 + 数量（盘点单的 qty 为账面、qty_actual 为实盘）"""

    __tablename__ = "stock_voucher_lines"

    id = Column(Integer, primary_key=True, index=True)
    voucher_id = Column(Integer, ForeignKey("stock_vouchers.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    batch_id = Column(Integer, ForeignKey("drug_batches.id", ondelete="RESTRICT"),
                      nullable=False, index=True)
    qty = Column(Float, nullable=False, comment="本单数量（盘点=账面数量）")
    qty_actual = Column(Float, nullable=True, comment="盘点实盘数量")
    qty_opened = Column(Float, nullable=False, default=0, comment="退回中已开封数量")
    qty_returned_ok = Column(Float, nullable=False, default=0,
                             comment="该行累计未开封退回数量")
    qty_returned_quar = Column(Float, nullable=False, default=0,
                               comment="该行累计已开封退回（待毁）数量")
    note = Column(Text, nullable=True)

    voucher = relationship("StockVoucher", back_populates="lines")
    batch = relationship("DrugBatch", back_populates="lines")


class StockLedger(Base):
    """库存流水（只追加，不允许修改/删除）：批次每笔变动与变动后余额"""

    __tablename__ = "stock_ledger"
    __table_args__ = (
        Index("ix_ledger_batch", "batch_id", "id"),
        CheckConstraint("balance_ok >= 0", name="ck_ledger_balance_ok_nonneg"),
        CheckConstraint("balance_quarantine >= 0", name="ck_ledger_balance_quar_nonneg"),
    )

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("drug_batches.id", ondelete="RESTRICT"),
                      nullable=False, index=True)
    drug_id = Column(Integer, ForeignKey("drug_catalog.id"), nullable=False, index=True)
    voucher_id = Column(Integer, ForeignKey("stock_vouchers.id"), nullable=False, index=True)
    voucher_no = Column(String(24), nullable=False)
    voucher_type = Column(String(12), nullable=False)
    voucher_date = Column(Date, nullable=False, index=True)
    qty_change_ok = Column(Float, nullable=False, default=0, comment="合格库存变动（正负）")
    qty_change_quarantine = Column(Float, nullable=False, default=0, comment="待毁库存变动")
    balance_ok = Column(Float, nullable=False, comment="变动后合格库存")
    balance_quarantine = Column(Float, nullable=False, comment="变动后待毁库存")
    cow_id = Column(Integer, nullable=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
