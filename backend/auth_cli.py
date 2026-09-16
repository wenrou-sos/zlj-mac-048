"""管理员账号命令行工具（初始化入口 / 忘记密码或误操作锁死后的恢复手段）

用法：
  python -m backend.auth_cli ensure-admin [用户名] [密码]   # 确保存在一个启用管理员
  python -m backend.auth_cli reset-password <用户名> <密码> # 重置任意用户密码并启用
  python -m backend.auth_cli list                          # 列出全部账号
不设密码时随机生成并打印；用户名默认 admin。
"""
import secrets
import sys

from . import auth
from .database import Base, SessionLocal, engine
from .migrate import run_migrations


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    Base.metadata.create_all(bind=engine)
    run_migrations()
    cmd = argv[0] if argv else "ensure-admin"
    db = SessionLocal()
    try:
        if cmd == "list":
            for u in db.query(auth.models.User).order_by(auth.models.User.id).all():
                print(f"{u.id}\t{u.username}\t{u.display_name}\t"
                      f"[{u.roles}]\tactive={u.active}")
            return 0

        if cmd == "ensure-admin":
            username = argv[1] if len(argv) > 1 else "admin"
            password = argv[2] if len(argv) > 2 else secrets.token_urlsafe(10)
            from .migrate import ensure_default_admin
            u = db.query(auth.models.User).filter_by(username=username).first()
            if u:
                if "admin" not in u.roles.split(","):
                    u.roles = ("admin," + u.roles).strip(",")
                u.active = True
                u.password_hash = auth.hash_password(password)
                db.commit()
                created = False
            else:
                u = auth.models.User(
                    username=username, display_name="管理员",
                    password_hash=auth.hash_password(password),
                    roles="admin", active=True, note="CLI 创建",
                )
                db.add(u)
                db.commit()
                created = True
            auth.revoke_sessions(db, u.id)
            print(f"{'已创建' if created else '已更新'}管理员：{username} / {password}")
            return 0

        if cmd == "reset-password":
            if len(argv) < 2:
                print("用法：reset-password <用户名> [密码]")
                return 2
            username = argv[1]
            password = argv[2] if len(argv) > 2 else secrets.token_urlsafe(10)
            u = db.query(auth.models.User).filter_by(username=username).first()
            if not u:
                print(f"用户不存在：{username}")
                return 1
            u.password_hash = auth.hash_password(password)
            u.active = True
            db.commit()
            auth.revoke_sessions(db, u.id)
            print(f"已重置 {username} 的密码并启用账号：{password}")
            return 0

        print(f"未知命令：{cmd}")
        return 2
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
