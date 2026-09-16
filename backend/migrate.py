"""SQLite 轻量结构迁移（幂等）

create_all 只能建新表，不会给已存在的表补列。老库升级时新增字段必须用
ALTER TABLE ADD COLUMN，否则应用一查询新列即报 "no such column"。
"""
from sqlalchemy import text
from sqlalchemy.engine import Engine


def table_exists(engine: Engine, table: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:t"),
            {"t": table},
        ).first()
    return row is not None


def column_exists(engine: Engine, table: str, column: str) -> bool:
    with engine.connect() as conn:
        rows = conn.execute(text(f'PRAGMA table_info("{table}")')).fetchall()
    return any(r[1] == column for r in rows)


def add_column_if_missing(engine: Engine, table: str, column: str, ddl: str) -> bool:
    """缺列则 ALTER TABLE ADD COLUMN；返回是否执行了变更。"""
    if not table_exists(engine, table) or column_exists(engine, table, column):
        return False
    with engine.begin() as conn:
        conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN {column} {ddl}'))
    return True


# 新增列登记：表名 -> [(列名, 列DDL)]
_PENDING_COLUMNS = {
    "course_doses": [
        ("cancel_scope", "VARCHAR(16)"),
        ("cancel_reason", "VARCHAR(255)"),
    ],
}


def run_lightweight_migrations(engine: Engine) -> list:
    """执行全部待应用的列迁移，返回已添加的 '表.列' 列表。"""
    applied = []
    for table, cols in _PENDING_COLUMNS.items():
        if not table_exists(engine, table):
            continue  # 全新库：create_all 会按最新模型直接建表
        for column, ddl in cols:
            if add_column_if_missing(engine, table, column, ddl):
                applied.append(f"{table}.{column}")
    return applied
