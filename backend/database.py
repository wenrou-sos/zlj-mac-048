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
    connect_args={"check_same_thread": False},
)


# SQLite 并发配置：
# 1) WAL 模式：读写不互相阻塞，仅“写-写”串行
# 2) busy_timeout：两个写事务冲突时等待而非立刻 SQLITE_BUSY
# 3) 全部事务以 BEGIN IMMEDIATE 开始：领药扣库存这类写操作一进事务即拿 RESERVED 锁，
#    把“读余额→判断足够→扣减”变成临界区；再配合条件 UPDATE 兜底，余额不可能扣成负数
@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=8000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
    # 完全关闭 pysqlite 自己发出的 BEGIN，交给下面的 begin 事件
    dbapi_connection.isolation_level = None


@event.listens_for(engine, "begin")
def _begin_immediate(conn):
    conn.exec_driver_sql("BEGIN IMMEDIATE")


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
