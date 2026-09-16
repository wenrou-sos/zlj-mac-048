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
    courses = relationship("TreatmentCourse", back_populates="cow", cascade="all, delete-orphan")


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
    courses = relationship("TreatmentCourse", back_populates="health_record")


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


# ---------- 用药疗程 ----------
# 剂量状态：只有 administered（已实际给药）才进入休药期计算
DOSE_PLANNED = "planned"        # 待给药（计划中）
DOSE_ADMINISTERED = "administered"  # 已给药（实际时间/剂量可能偏离计划）
DOSE_MISSED = "missed"          # 漏用（当天未给，已注明原因，不补）
DOSE_DELAYED = "delayed"        # 延期（已改期，等待在新时间执行）
DOSE_CANCELLED = "cancelled"    # 取消（停药/换药/结束疗程时剩余计划取消）

COURSE_ACTIVE = "active"
COURSE_ENDED = "ended"


class TreatmentCourse(Base):
    """用药疗程：围绕一份病历安排多药、多次、多班给药"""

    __tablename__ = "treatment_courses"

    id = Column(Integer, primary_key=True, index=True)
    cow_id = Column(Integer, ForeignKey("cows.id", ondelete="CASCADE"), nullable=False, index=True)
    health_record_id = Column(
        Integer, ForeignKey("health_records.id", ondelete="SET NULL"),
        nullable=True, index=True, comment="所属病历（诊断）")
    title = Column(String(128), nullable=False, comment="疗程名称/诊断摘要")
    start_date = Column(Date, nullable=False, index=True, comment="疗程开始日期")
    planned_end_date = Column(Date, nullable=True, comment="计划结束日期")
    end_date = Column(Date, nullable=True, comment="实际结束日期（未结束为 NULL）")
    status = Column(String(16), nullable=False, default=COURSE_ACTIVE,
                    comment="active 进行中 / ended 已结束")
    end_reason = Column(String(128), nullable=True, comment="结束疗程原因（康复/无效转院/淘汰等）")
    veterinarian = Column(String(32), nullable=True, comment="主治兽医")
    note = Column(Text, nullable=True, comment="交班备注：剩余安排、注意事项")
    created_at = Column(DateTime, default=datetime.utcnow)

    cow = relationship("Cow", back_populates="courses")
    health_record = relationship("HealthRecord", back_populates="courses")
    drug_lines = relationship(
        "CourseDrug", back_populates="course", cascade="all, delete-orphan",
        order_by="CourseDrug.id")
    doses = relationship(
        "CourseDose", back_populates="course", cascade="all, delete-orphan")
    events = relationship(
        "CourseEvent", back_populates="course", cascade="all, delete-orphan",
        order_by="CourseEvent.id.desc()")


class CourseDrug(Base):
    """疗程中的一条用药行（一种药的整段安排；换药后原行停药、另起新行）"""

    __tablename__ = "course_drugs"

    id = Column(Integer, primary_key=True, index=True)
    course_id = Column(Integer, ForeignKey("treatment_courses.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    drug_id = Column(Integer, ForeignKey("drug_catalog.id"), nullable=True)
    drug_name = Column(String(64), nullable=False, comment="药品名称快照")
    planned_dose = Column(String(64), nullable=True, comment="计划剂量，如 1g/支")
    route = Column(String(32), nullable=True, comment="给药途径")
    times_per_day = Column(Integer, nullable=False, default=1, comment="每日次数（0=隔日）")
    interval_days = Column(Integer, nullable=False, default=1, comment="给药间隔天数，1=每日")
    withdrawal_days = Column(Integer, nullable=False, default=0, comment="本药牛奶休药期(天)快照")
    seq = Column(Integer, nullable=False, default=1, comment="代次：换药递增")
    status = Column(String(16), nullable=False, default="active",
                    comment="active 进行中 / switched 已换药停用 / stopped 已停药")
    change_reason = Column(String(255), nullable=True, comment="换药/停药原因")
    changed_at = Column(DateTime, nullable=True)

    course = relationship("TreatmentCourse", back_populates="drug_lines")
    drug = relationship("DrugCatalog")
    doses = relationship(
        "CourseDose", back_populates="drug_line", cascade="all, delete-orphan")


class CourseDose(Base):
    """疗程内逐次给药：计划时间与实际执行分开记录"""

    __tablename__ = "course_doses"

    id = Column(Integer, primary_key=True, index=True)
    course_id = Column(Integer, ForeignKey("treatment_courses.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    course_drug_id = Column(Integer, ForeignKey("course_drugs.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    cow_id = Column(Integer, ForeignKey("cows.id", ondelete="CASCADE"), nullable=False, index=True)
    dose_no = Column(Integer, nullable=False, comment="第几次（本用药行内序号）")
    planned_date = Column(Date, nullable=False, index=True, comment="计划给药日期")
    planned_time = Column(String(8), nullable=True, comment="计划班次 morning/noon/evening")
    planned_dose = Column(String(64), nullable=True, comment="计划剂量快照")
    status = Column(String(16), nullable=False, default=DOSE_PLANNED, index=True,
                    comment="planned/administered/missed/delayed/cancelled")
    # —— 实际执行信息（只有 administered 才有值）——
    administered_date = Column(Date, nullable=True, index=True, comment="实际给药日期")
    administered_time = Column(String(8), nullable=True, comment="实际班次")
    administered_dose = Column(String(64), nullable=True, comment="实际剂量（可不同于计划）")
    withdrawal_days = Column(Integer, nullable=True, comment="实际给药时采用的休药期(天)")
    withdrawal_end = Column(Date, nullable=True, index=True, comment="本次给药休药期截止日(含当天)")
    operator = Column(String(32), nullable=True, comment="执行人")
    # —— 异常/变更说明（漏用、延期、取消、换药）——
    reason = Column(String(255), nullable=True, comment="漏用/延期/取消原因")
    delayed_to = Column(Date, nullable=True, comment="延期后的新计划日期")
    cancel_scope = Column(
        String(16), nullable=True,
        comment="取消来源：manual 单次取消 / line_stop 停药 / switch 换药 / course_end 结束疗程；"
                "非 manual 的批量取消只能随用药行/疗程整批恢复")
    recorded_by = Column(String(32), nullable=True)
    recorded_at = Column(DateTime, nullable=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    course = relationship("TreatmentCourse", back_populates="doses")
    drug_line = relationship("CourseDrug", back_populates="doses")


class CourseEvent(Base):
    """疗程事件流：每次执行/变更自动留痕，供交班查阅"""

    __tablename__ = "course_events"

    id = Column(Integer, primary_key=True, index=True)
    course_id = Column(Integer, ForeignKey("treatment_courses.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    occurred_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    event_type = Column(String(24), nullable=False,
                        comment="created/administered/missed/delayed/cancelled/"
                                "switched/stopped/added_drug/ended/note")
    summary = Column(String(255), nullable=False, comment="事件摘要")
    detail = Column(Text, nullable=True)
    operator = Column(String(32), nullable=True)

    course = relationship("TreatmentCourse", back_populates="events")
