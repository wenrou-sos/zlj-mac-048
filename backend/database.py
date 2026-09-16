"""数据库引擎与会话管理"""
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "dairy.db"

SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 15},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=True, bind=engine)
Base = declarative_base()


@event.listens_for(engine, "connect")
def _sqlite_connect(dbapi_connection, connection_record):
    # 锁等待而不是立刻抛 SQLITE_BUSY
    dbapi_connection.execute("PRAGMA busy_timeout=15000")


# 每个事务以 BEGIN IMMEDIATE 开启：写事务跨线程/跨进程串行化，
# 保证"检查-修改"类关键操作（如防止最后一个管理员被并发停用）的原子性
@event.listens_for(engine, "begin")
def _begin_immediate(conn):
    conn.exec_driver_sql("BEGIN IMMEDIATE")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
