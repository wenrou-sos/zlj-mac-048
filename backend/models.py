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

# 待办任务生命周期状态
TASK_STATUS_OPEN = "open"          # 待处理（未认领）
TASK_STATUS_CLAIMED = "claimed"    # 已认领/指派，处理中
TASK_STATUS_POSTPONED = "postponed"  # 已延期，等待改期后的日期
TASK_STATUS_DONE = "done"          # 已处理完成
TASK_STATUS_INVALID = "invalid"    # 源记录已失效/删除（留痕，不再提醒）
TASK_STATUS_CHANGED = "changed"    # 源记录发生变更，内容已更新待确认
# 待办相对源记录的状态（对账时计算）
SOURCE_STATE_ACTIVE = "active"
SOURCE_STATE_UPDATED = "updated"
SOURCE_STATE_INVALID = "invalid"
SOURCE_STATE_REGISTERED = "registered"  # 已在业务模块完成真实登记


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


class Person(Base):
    """值班人员名单（无登录，直接维护姓名即可）"""

    __tablename__ = "persons"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(32), unique=True, nullable=False)
    role = Column(String(32), nullable=True, comment="岗位，如 兽医/配种员/挤奶工/班长")
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ReminderTask(Base):
    """
    提醒待办（持久化派工单）。
    dedup_key 为提醒引擎对同一业务事项算出的稳定指纹：
    刷新/重启只做“对账”，同指纹绝不重复建单，负责人、延期与处理记录随之保留。
    """

    __tablename__ = "reminder_tasks"

    id = Column(Integer, primary_key=True, index=True)
    dedup_key = Column(String(96), unique=True, nullable=False, index=True)
    type = Column(String(32), nullable=False, comment="提醒类型，同 services 的 reminder type")
    cow_id = Column(Integer, ForeignKey("cows.id", ondelete="SET NULL"), nullable=True, index=True)
    ref_type = Column(String(24), nullable=True, comment="estrus/medication/health/cow")
    ref_id = Column(Integer, nullable=True, comment="源记录主键")
    title = Column(String(160), nullable=False)
    detail = Column(String(512), nullable=True)
    level = Column(String(8), nullable=False, default="warning")
    due_date = Column(Date, nullable=True, index=True)
    source_hash = Column(String(64), nullable=True, comment="源内容指纹，变化则标记已更新")

    status = Column(String(16), nullable=False, default=TASK_STATUS_OPEN, index=True)
    source_state = Column(String(16), nullable=False, default=SOURCE_STATE_ACTIVE,
                          comment="active/updated/invalid/registered")
    owner_id = Column(Integer, ForeignKey("persons.id"), nullable=True, index=True)
    owner_name = Column(String(32), nullable=True, comment="负责人姓名快照，人员改名也不丢历史")
    postpone_reason = Column(String(256), nullable=True)
    result_note = Column(String(512), nullable=True, comment="处理结果")
    registered = Column(Boolean, default=False,
                        comment="真实业务登记是否已完成（用药执行/孕检结果回填等）")

    shift_code = Column(String(16), nullable=False, index=True, comment="首次派单班次 YYYY-MM-DD#序号")
    shift_label = Column(String(16), nullable=False)
    carry_count = Column(Integer, default=0, comment="跨班续传次数")
    version = Column(Integer, nullable=False, default=1, comment="乐观锁版本，防并发认领覆盖")

    created_at = Column(DateTime, default=datetime.utcnow)
    claimed_at = Column(DateTime, nullable=True)
    postponed_at = Column(DateTime, nullable=True)
    done_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True, comment="失效/结案时间")
    last_seen_at = Column(DateTime, default=datetime.utcnow, comment="对账时最近一次仍在提醒中")

    owner = relationship("Person")
    events = relationship("TaskEvent", back_populates="task",
                          cascade="all, delete-orphan", order_by="TaskEvent.id")


class TaskEvent(Base):
    """待办操作流水：认领/指派/延期/完成/失效全部留痕，供交班与追溯"""

    __tablename__ = "task_events"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("reminder_tasks.id", ondelete="CASCADE"), index=True)
    event = Column(String(24), nullable=False,
                   comment="created/claimed/assigned/postponed/done/acknowledged/"
                           "updated/invalid/reopened/carried")
    actor = Column(String(32), nullable=True, comment="操作人姓名")
    detail = Column(String(512), nullable=True)
    shift_code = Column(String(16), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    task = relationship("ReminderTask", back_populates="events")


class Handover(Base):
    """交班单：一个班次一次，记录交给谁及当时未完成事项快照"""

    __tablename__ = "handovers"

    id = Column(Integer, primary_key=True, index=True)
    shift_code = Column(String(16), nullable=False, index=True, comment="交出班次")
    shift_label = Column(String(16), nullable=False)
    from_person = Column(String(32), nullable=True, comment="交班人")
    to_person = Column(String(32), nullable=True, comment="接班人")
    note = Column(String(512), nullable=True, comment="交班备注")
    open_count = Column(Integer, default=0, comment="未完成事项数（带到下一班）")
    items_json = Column(Text, nullable=True, comment="未完成事项快照 JSON")
    created_at = Column(DateTime, default=datetime.utcnow)
