"""存量库幂等迁移：旧版本数据库无牛舍/用户/会话表与列，启动时自动升级，不丢数据。

- 新表由 Base.metadata.create_all 创建；
- 旧表新增列用 ALTER TABLE ADD COLUMN 补齐（SQLite 支持，不重写数据）；
- 依据 cows.group（如 A栋1栏、C栋待产栏）归并出牛舍（按"X栋"前缀），回填 cows.shed_id；
- users 表为空时自动创建初始化管理员 admin，避免系统锁死；
  初始口令取环境变量 DAIRY_ADMIN_PASSWORD，未设置时用 admin123（首登后应修改）。
"""
import os
import re
from typing import Set

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from . import auth, models
from .database import engine

SHED_PREFIX = re.compile(r"^\s*([0-9A-Za-z]*\d*栋)")
# 不含"X栋"前缀时，整组作为牛舍编号的最低要求（避免把"已离场"这类状态词建成牛舍）
SHED_LIKE = re.compile(r"(栋|舍|栏|棚)")

# 旧表 -> 新增列 (列名, 列定义)
ADDED_COLUMNS = {
    "cows": [("shed_id", "INTEGER")],
    "milking_records": [("created_by", "INTEGER")],
    "health_records": [("created_by", "INTEGER")],
    "medications": [("created_by", "INTEGER")],
    "estrus_records": [("created_by", "INTEGER")],
}

DEFAULT_ADMIN_USERNAME = os.environ.get("DAIRY_ADMIN_USERNAME", "admin")
DEFAULT_ADMIN_PASSWORD = os.environ.get("DAIRY_ADMIN_PASSWORD", "admin123")


def _existing_columns(conn, table: str) -> Set[str]:
    return {row[1] for row in conn.exec_driver_sql(f'PRAGMA table_info("{table}")')}


def _add_missing_columns(conn) -> list:
    added = []
    for table, columns in ADDED_COLUMNS.items():
        try:
            present = _existing_columns(conn, table)
        except Exception:
            continue  # 表尚不存在（全新库由 create_all 建），跳过
        if not present:
            continue
        for name, ddl in columns:
            if name not in present:
                conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN {name} {ddl}'))
                added.append(f"{table}.{name}")
    return added


def _backfill_sheds(db: Session) -> int:
    """从现有 cows.group 归并牛舍并回填 shed_id，返回新建牛舍数。"""
    created = 0
    groups = [g for (g,) in db.query(models.Cow.group).distinct().all() if g]
    for group in groups:
        m = SHED_PREFIX.match(group)
        if m:
            code = m.group(1)
        elif SHED_LIKE.search(group):
            code = group.strip()
        else:
            # 如"已离场"等状态性分组：不建牛舍、不回填
            continue
        shed = db.query(models.Shed).filter_by(code=code).first()
        if not shed:
            shed = models.Shed(code=code, name=f"{code}牛舍", active=True)
            db.add(shed)
            db.flush()
            created += 1
        db.query(models.Cow).filter(models.Cow.group == group).update(
            {models.Cow.shed_id: shed.id}, synchronize_session=False
        )
    db.commit()
    return created


def ensure_default_admin(db: Session, password: str = None) -> models.User:
    """保证至少存在一个启用中的管理员（防锁死的最后保障）。"""
    admin = (
        db.query(models.User)
        .filter(models.User.roles.like("%admin%"), models.User.active == 1)
        .first()
    )
    if admin:
        return admin
    username = DEFAULT_ADMIN_USERNAME
    if db.query(models.User).filter_by(username=username).first():
        # admin 用户名被占用且没有启用管理员：换一个保底用户名
        username = "sysadmin"
        i = 1
        while db.query(models.User).filter_by(username=username).first():
            i += 1
            username = f"sysadmin{i}"
    admin = models.User(
        username=username,
        display_name="系统管理员(初始化)",
        password_hash=auth.hash_password(password or DEFAULT_ADMIN_PASSWORD),
        roles="admin",
        active=True,
        note="系统自动创建的初始化管理员，请及时修改口令",
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin


def run_migrations() -> dict:
    """幂等执行，返回简单执行报告。"""
    report = {"columns_added": [], "sheds_created": 0, "admin_ensured": False}
    with engine.begin() as conn:
        report["columns_added"] = _add_missing_columns(conn)

    db = Session(bind=engine)
    try:
        # 仅当系统里已有牛只时才需要从 group 回填牛舍
        if db.query(models.Cow).count() > 0 and db.query(models.Shed).count() == 0:
            report["sheds_created"] = _backfill_sheds(db)
        if db.query(models.User).count() == 0:
            ensure_default_admin(db)
            report["admin_ensured"] = True
    finally:
        db.close()
    return report
