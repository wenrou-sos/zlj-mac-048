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
    UniqueConstraint,
    event,
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
    stays = relationship("Stay", back_populates="cow", cascade="all, delete-orphan")
    transfer_items = relationship("TransferItem", back_populates="cow")


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


# ---------------- 牛舍与转群 ----------------
class Pen(Base):
    """牛舍/栏位：容量与用途"""

    __tablename__ = "pens"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(64), unique=True, nullable=False, index=True, comment="栏位名称")
    purpose = Column(
        String(16), nullable=False, default="lactating",
        comment="lactating 泌乳 / dry 干奶 / maternity 产房待产 / isolation 隔离 / other 其他",
    )
    capacity = Column(Integer, nullable=False, default=0, comment="栏位容量(头)，0 表示停用")
    active = Column(Boolean, default=True, comment="是否启用")
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Stay(Base):
    """
    居住历史（半开区间 [start_date, end_date)，end_date 为空表示至今）。
    同一头牛的区间不允许重叠——应用层校验之外，再用 SQLite 触发器兜底，
    任何并发/漏判导致的“同一头牛同时住两栏”都会在数据库层被拒绝。
    """

    __tablename__ = "stays"

    id = Column(Integer, primary_key=True, index=True)
    cow_id = Column(Integer, ForeignKey("cows.id", ondelete="CASCADE"), nullable=False, index=True)
    pen_id = Column(Integer, ForeignKey("pens.id"), nullable=False, index=True)
    start_date = Column(Date, nullable=False, index=True)
    end_date = Column(Date, nullable=True, index=True, comment="迁出日（迁出当日已不在本栏）")
    source = Column(
        String(16), nullable=False, default="seed",
        comment="seed 建账/迁移 / transfer 转群计划 / history 补录 / manual 手工调整",
    )
    transfer_item_id = Column(Integer, ForeignKey("transfer_items.id", ondelete="SET NULL"),
                              nullable=True,
                              comment="由哪条转群明细产生，取消计划时据此回收")
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    cow = relationship("Cow", back_populates="stays")
    pen = relationship("Pen")
    transfer_item = relationship("TransferItem", back_populates="stays")


class TransferPlan(Base):
    """批量转群安排：按生效时间组织一批牛，可预检冲突、延期、取消"""

    __tablename__ = "transfer_plans"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(128), nullable=False, comment="安排名称")
    effective_date = Column(Date, nullable=False, index=True, comment="计划生效日期")
    kind = Column(
        String(16), nullable=False, default="group",
        comment="group 普通转群 / isolation 临时隔离",
    )
    status = Column(
        String(16), nullable=False, default="draft", index=True,
        comment="draft 待确认 / confirmed 已确认 / cancelled 已取消",
    )
    operator = Column(String(32), nullable=True)
    note = Column(Text, nullable=True)
    confirmed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    items = relationship("TransferItem", back_populates="plan",
                         cascade="all, delete-orphan")
    events = relationship("TransferEvent", back_populates="plan",
                          cascade="all, delete-orphan", order_by="TransferEvent.id")


class TransferItem(Base):
    """转群明细：一头牛从哪栏去哪栏（隔离时 from_pen 可能为空）"""

    __tablename__ = "transfer_items"
    __table_args__ = (
        UniqueConstraint("plan_id", "cow_id", name="uq_plan_cow"),
    )

    id = Column(Integer, primary_key=True, index=True)
    plan_id = Column(Integer, ForeignKey("transfer_plans.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    cow_id = Column(Integer, ForeignKey("cows.id", ondelete="CASCADE"), nullable=False, index=True)
    from_pen_id = Column(Integer, ForeignKey("pens.id"), nullable=True)
    to_pen_id = Column(Integer, ForeignKey("pens.id"), nullable=False)
    return_pen_id = Column(Integer, ForeignKey("pens.id"), nullable=True,
                           comment="隔离返回栏；为空表示返回原栏(from_pen)")
    return_date = Column(Date, nullable=True, comment="隔离计划返回日期")

    plan = relationship("TransferPlan", back_populates="items")
    cow = relationship("Cow", back_populates="transfer_items")
    from_pen = relationship("Pen", foreign_keys=[from_pen_id])
    to_pen = relationship("Pen", foreign_keys=[to_pen_id])
    return_pen = relationship("Pen", foreign_keys=[return_pen_id])
    stays = relationship("Stay", back_populates="transfer_item")


class TransferEvent(Base):
    """迁入迁出/计划生命周期流水"""

    __tablename__ = "transfer_events"

    id = Column(Integer, primary_key=True, index=True)
    plan_id = Column(Integer, ForeignKey("transfer_plans.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    cow_id = Column(Integer, ForeignKey("cows.id", ondelete="SET NULL"), nullable=True, index=True)
    at = Column(DateTime, default=datetime.utcnow, nullable=False)
    action = Column(
        String(16), nullable=False,
        comment="created/confirmed/postponed/cancelled/released/returned/backfilled",
    )
    detail = Column(String(255), nullable=True)

    plan = relationship("TransferPlan", back_populates="events")


# SQLite 触发器（仅 SQLite 生效；本系统单机 SQLite 部署）：
# 1) 同一头牛居住区间不得重叠（半开区间 [start, end)，end 为空视为无限远）
# 2) 任一日期栏位在栏数不得超过容量（容量 0 的栏位不可占用）
def _install_sqlite_triggers(target, connection, **_kw):
    if connection.dialect.name != "sqlite":
        return
    conn = connection.connection
    conn.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS trg_stay_overlap_insert
        AFTER INSERT ON stays
        WHEN EXISTS (
            SELECT 1 FROM stays s
            WHERE s.id <> NEW.id AND s.cow_id = NEW.cow_id
              AND s.start_date < COALESCE(NEW.end_date, '9999-12-31')
              AND COALESCE(s.end_date, '9999-12-31') > NEW.start_date
        )
        BEGIN
            SELECT RAISE(ABORT, '同一头牛存在时间重叠的居住记录');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_stay_overlap_update
        AFTER UPDATE OF cow_id, start_date, end_date ON stays
        WHEN EXISTS (
            SELECT 1 FROM stays s
            WHERE s.id <> NEW.id AND s.cow_id = NEW.cow_id
              AND s.start_date < COALESCE(NEW.end_date, '9999-12-31')
              AND COALESCE(s.end_date, '9999-12-31') > NEW.start_date
        )
        BEGIN
            SELECT RAISE(ABORT, '同一头牛存在时间重叠的居住记录');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_stay_date_order_insert
        AFTER INSERT ON stays
        WHEN NEW.end_date IS NOT NULL AND NEW.end_date <= NEW.start_date
        BEGIN
            SELECT RAISE(ABORT, '迁出日期必须晚于迁入日期');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_stay_date_order_update
        AFTER UPDATE OF start_date, end_date ON stays
        WHEN NEW.end_date IS NOT NULL AND NEW.end_date <= NEW.start_date
        BEGIN
            SELECT RAISE(ABORT, '迁出日期必须晚于迁入日期');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_pen_capacity_insert
        AFTER INSERT ON stays
        WHEN EXISTS (SELECT 1 FROM pens WHERE id = NEW.pen_id AND capacity > 0)
         AND (
            SELECT COUNT(*) FROM stays s
            WHERE s.pen_id = NEW.pen_id
              AND s.start_date <= NEW.start_date
              AND COALESCE(s.end_date, '9999-12-31') > NEW.start_date
        ) > (
            SELECT capacity FROM pens WHERE id = NEW.pen_id
        )
        BEGIN
            SELECT RAISE(ABORT, '栏位容量不足：迁入会造成超栏');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_pen_capacity_update
        AFTER UPDATE OF pen_id, start_date, end_date ON stays
        WHEN EXISTS (SELECT 1 FROM pens WHERE id = NEW.pen_id AND capacity > 0)
         AND (
            SELECT 1 + COUNT(*) FROM stays s
            WHERE s.id <> NEW.id AND s.pen_id = NEW.pen_id
              AND s.start_date <= NEW.start_date
              AND COALESCE(s.end_date, '9999-12-31') > NEW.start_date
        ) > (
            SELECT capacity FROM pens WHERE id = NEW.pen_id
        )
        BEGIN
            SELECT RAISE(ABORT, '栏位容量不足：调整会造成超栏');
        END;
        """
    )


event.listen(Base.metadata, "after_create", _install_sqlite_triggers)
