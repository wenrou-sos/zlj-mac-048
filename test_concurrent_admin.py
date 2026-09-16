"""并发防锁死测试：两个启用管理员同时各自停用自己，至少一个必须被拒绝。

运行前：python -m backend.seed（库内需有 admin/manager；manager 非 admin 角色，
因此先创建第二个 admin）。
"""
import threading
import time

from fastapi.testclient import TestClient

from backend.main import app
from backend.database import SessionLocal
from backend import models


def login(username, password):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, (username, r.text)
    return c


def active_admin_ids(db):
    return {u.id for u in db.query(models.User).filter(
        models.User.roles.like("%admin%"), models.User.active.is_(True)).all()}


db = SessionLocal()
# 确保存在两个启用管理员：admin 与把 vet_wang 临时升为管理员
u_wang = db.query(models.User).filter_by(username="vet_wang").first()
u_wang.roles = "admin,vet"
u_wang.active = True
admin_id = db.query(models.User).filter_by(username="admin").first().id
wang_id = u_wang.id
db.commit()
db.close()

c1 = login("admin", "admin123")
c2 = login("vet_wang", "vet12345")

results = {}
barrier = threading.Barrier(2)


def deactivate(client, uid, tag):
    barrier.wait()          # 尽量同时发出
    r = client.patch(f"/api/users/{uid}", json={"active": False})
    results[tag] = r.status_code


t1 = threading.Thread(target=deactivate, args=(c1, admin_id, "admin"))
t2 = threading.Thread(target=deactivate, args=(c2, wang_id, "wang"))
t1.start(); t2.start(); t1.join(); t2.join()

print("并发停用结果：", results)
db = SessionLocal()
remaining = active_admin_ids(db)
print("剩余启用管理员：", remaining)

ok = True
if len(remaining) < 1:
    print("✗ 致命：两个管理员都被停用，系统已锁死！")
    ok = False
else:
    print(f"✓ 至少保留 1 个启用管理员（{len(remaining)} 个）")
codes = list(results.values())
if 409 in codes:
    print("✓ 其中一个并发请求被 409 拒绝")
else:
    print("✗ 两个请求都成功但仍有管理员（可能其中一方改了别人？），检查:", results)
    # 该情形仅当存在第三个管理员时可接受；本测试只有两个
    ok = False
db.close()

# 串行基线：只有一个管理员时停用自己仍应 409
c = login("vet_wang", "vet12345") if False else None
# 重新把 admin 置回，再单独测最后管理员
db = SessionLocal()
db.query(models.User).filter_by(id=admin_id).update({"active": True, "roles": "admin"})
db.commit()
# 把 wang 降回非管理员，形成唯一管理员
db.query(models.User).filter_by(id=wang_id).update({"active": False, "roles": "vet"})
db.commit()
db.close()
c1 = login("admin", "admin123")
r = c1.patch(f"/api/users/{admin_id}", json={"active": False})
print("唯一管理员停用自己 ->", r.status_code)
if r.status_code != 409:
    print("✗ 串行防锁死失效")
    ok = False
else:
    print("✓ 唯一管理员无法停用自己")

print("\n结果：", "通过" if ok else "失败")
raise SystemExit(0 if ok else 1)
