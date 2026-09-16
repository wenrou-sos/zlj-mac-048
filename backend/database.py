"""数据库引擎与会话管理"""
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "dairy.db"

SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def run_migrations():
    """轻量迁移：为已存在的旧库补充导入功能所需的列与索引（create_all 只管新表）"""
    with engine.begin() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(milking_records)"))}
        if cols and "import_batch_id" not in cols:
            conn.execute(text(
                "ALTER TABLE milking_records ADD COLUMN import_batch_id INTEGER "
                "REFERENCES import_batches(id) ON DELETE SET NULL"
            ))
        # 唯一约束兜底：同一头牛同一天同一班次只允许一条记录，从数据库层杜绝重复记奶。
        # 若历史数据已存在重复则跳过建索引（由应用层继续拦截），避免启动失败。
        dup = conn.execute(text(
            "SELECT COUNT(*) FROM (SELECT cow_id, date, session FROM milking_records "
            "GROUP BY cow_id, date, session HAVING COUNT(*) > 1)"
        )).scalar()
        if not dup:
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_milking_cow_date_session "
                "ON milking_records(cow_id, date, session)"
            ))
