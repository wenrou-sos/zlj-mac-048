"""数据库引擎与会话管理"""
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "dairy.db"

SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
)


# SQLite 并发策略（WAL + 混合事务）：
# - 只读请求：默认延迟事务（BEGIN 后首句是 SELECT，不取写锁），
#   读不阻塞写、写不阻塞读，也不会两个读请求互相 database is locked。
# - 写请求：事务内第一条 INSERT/UPDATE/DELETE 发出前，把延迟事务升级为
#   BEGIN IMMEDIATE，立刻拿 RESERVED 写锁。于是“读余额→判足→条件扣减”
#   整个临界区在同一把写锁内串行；配合条件 UPDATE，库存不可能扣成负数。
# - busy_timeout 让并发写者等待而非立刻 SQLITE_BUSY。
@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record):
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=8000")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()
    # 关闭 pysqlite 自动 BEGIN，事务边界完全交给 SQLAlchemy
    dbapi_connection.isolation_level = None


_WRITE_PREFIXES = ("INSERT", "UPDATE", "DELETE", "REPLACE")


@event.listens_for(engine, "begin")
def _begin_deferred(conn):
    # 普通延迟事务：首个语句决定锁类型
    conn.info["immediate"] = False
    conn.exec_driver_sql("BEGIN")


@event.listens_for(engine, "before_cursor_execute")
def _upgrade_to_immediate(conn, cursor, statement, parameters, context, executemany):
    if conn.info.get("immediate"):
        return
    if statement.lstrip().upper().startswith(_WRITE_PREFIXES):
        # 提交此前的空延迟事务，立即以 IMMEDIATE 重开；
        # 之后的写语句及其后的“判断+扣减”都在 RESERVED 锁保护下。
        cursor.execute("COMMIT")
        cursor.execute("BEGIN IMMEDIATE")
        conn.info["immediate"] = True


@event.listens_for(engine, "rollback")
@event.listens_for(engine, "commit")
def _clear_immediate(conn):
    conn.info["immediate"] = False


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
        # 结束只读延迟事务、释放可能的 SHARED 锁；写接口已自行 commit 时为空操作
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
