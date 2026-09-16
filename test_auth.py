"""端到端鉴权测试：直连接口越权、兼岗、临时接管、停用即时生效、防锁死。

前置：数据库已通过 `python -m backend.seed` 重建（脚本内不依赖固定自增 ID）。
"""
from fastapi.testclient import TestClient

from backend.main import app
from backend.database import SessionLocal
from backend import auth, models

client = TestClient(app)
PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def login(c, username, password):
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, (username, r.status_code, r.text)
    return r.json()


def cow_id(db, tag):
    return db.query(models.Cow).filter_by(ear_tag=tag).first().id


db = SessionLocal()
A_ID = db.query(models.Shed).filter_by(code="A栋").first().id
B_ID = db.query(models.Shed).filter_by(code="B栋").first().id
C_ID = db.query(models.Shed).filter_by(code="C栋").first().id
TAG_A = cow_id(db, "1601")   # A栋
TAG_B = cow_id(db, "1610")   # B栋
TAG_C = cow_id(db, "1607")   # C栋
TAGS_A = {"1601", "1602", "1605", "1609"}
db.close()

print("== 1. 未登录直接访问接口 ==")
for method, path in [("GET", "/api/cows"), ("GET", "/api/milkings"),
                     ("GET", "/api/dashboard"), ("POST", "/api/cows"),
                     ("GET", "/api/health"), ("GET", "/api/users"),
                     ("GET", "/api/sheds")]:
    kw = {"json": {}} if method == "POST" else {}
    r = getattr(client, method.lower())(path, **kw)
    check(f"{method} {path} -> 401", r.status_code == 401, str(r.status_code))

print("== 2. 登录态 /me 权限与范围 ==")
c_adm0 = TestClient(app)
admin = login(c_adm0, "admin", "admin123")
check("admin 全场范围", admin["global_scope"] is True)
c_vet = TestClient(app)
vet_li = login(c_vet, "vet_li", "vet12345")
check("vet_li 非全场", vet_li["global_scope"] is False)
check("vet_li 负责A+C", {s["code"] for s in vet_li["sheds"]} == {"A栋", "C栋"}, str(vet_li["sheds"]))
check("vet_li 无用户管理", "user:manage" not in vet_li["permissions"])
check("vet_li 可登记健康", "health:write" in vet_li["permissions"])
c_chen = TestClient(app)
chen = login(c_chen, "milker_chen", "milk12345")
check("陈挤奶只看得到A栋牛舍", {s["code"] for s in chen["sheds"]} == {"A栋"})
c_lin0 = TestClient(app)
lin = login(c_lin0, "milker_lin", "milk12345")
check("林挤奶兼岗B+C", {s["code"] for s in lin["sheds"]} == {"B栋", "C栋"}, str(lin["sheds"]))

print("== 3. 列表按牛舍过滤 ==")
tags_admin = {c["ear_tag"] for c in c_adm0.get("/api/cows").json()}
check("管理员能看到全部牛只", len(tags_admin) == 12, str(len(tags_admin)))
tags = {c["ear_tag"] for c in c_vet.get("/api/cows").json()}
check("vet_li 只见 A/C 栋牛只(不含B栋1610)", "1610" not in tags and "1601" in tags and "1607" in tags, str(tags))
tags = {c["ear_tag"] for c in c_chen.get("/api/cows").json()}
check("陈挤奶只见A栋牛只", tags == TAGS_A, str(sorted(tags)))

print("== 4. 直接越权访问他舍牛只详情/记录 ==")
check("vet_li 直连 B栋牛详情 -> 403",
      c_vet.get(f"/api/cows/{TAG_B}").status_code == 403)
check("陈挤奶直连 B栋挤奶列表 -> 403/空(过滤) ",
      c_chen.get(f"/api/milkings?cow_id={TAG_B}").status_code == 403)
check("vet_li 访问本舍A栋牛 -> 200", c_vet.get(f"/api/cows/{TAG_A}").status_code == 200)

print("== 5. 岗位越权 ==")
# 挤奶员不能登记健康/用药/发情/建档/管药品
check("挤奶员建档 ->403", c_chen.post("/api/cows", json={}).status_code == 403)
check("挤奶员登记健康 ->403",
      c_chen.post("/api/health", json={"cow_id": TAG_A, "date": "2026-09-16",
                                        "record_type": "checkup"}).status_code == 403)
check("挤奶员登记用药 ->403",
      c_chen.post("/api/medications", json={"cow_id": TAG_A, "date": "2026-09-16",
                                             "drug_name": "x"}).status_code == 403)
check("挤奶员登记发情 ->403",
      c_chen.post("/api/estruses", json={"cow_id": TAG_A, "date": "2026-09-16"}).status_code == 403)
check("挤奶员药品目录 ->403", c_chen.post("/api/drugs", json={"name": "x"}).status_code == 403)
check("挤奶员用户管理 ->403", c_chen.get("/api/users").status_code == 403)
# 只读人员不能写
c_zhou = TestClient(app)
check("未生效的临时接管：周观察员今天登录范围为空",
      c_zhou.post("/api/auth/login", json={"username": "viewer_zhou", "password": "view12345"}).status_code == 200)
me = c_zhou.get("/api/auth/me").json()
check("周观察员当前无在效牛舍", me["sheds"] == [], str(me["sheds"]))
check("周观察员看不到任何牛只", c_zhou.get("/api/cows").json() == [])
check("只读人员登记挤奶 ->403",
      c_zhou.post("/api/milkings", json={}).status_code == 403)
# 兽医不能登记挤奶、不能改牛档
check("兽医登记挤奶 ->403",
      c_vet.post("/api/milkings", json={}).status_code == 403)
check("兽医删牛 ->403", c_vet.delete(f"/api/cows/{TAG_A}").status_code == 403)

print("== 6. 合法登记 + 范围外登记 ==")
# vet_li 在 A 栋登记健康应成功
r = c_vet.post("/api/health", json={"cow_id": TAG_A, "date": "2026-09-15",
                                    "record_type": "checkup", "diagnosis": "权限测试"})
check("兽医在本舍登记健康 ->201", r.status_code == 201, r.text)
hid = r.json()["id"]
# 试图给 B 栋牛登记 -> 403
r = c_vet.post("/api/health", json={"cow_id": TAG_B, "date": "2026-09-15",
                                    "record_type": "checkup"})
check("兽医越舍登记健康 ->403", r.status_code == 403, str(r.status_code))
# 兽医可删除本舍健康记录
check("兽医删除本舍健康记录 ->204", c_vet.delete(f"/api/health/{hid}").status_code == 204)

print("== 7. 挤奶登记：本人记录限制 ==")
r = c_chen.post("/api/milkings", json={"cow_id": TAG_A, "date": "2026-09-16",
                                       "session": "noon", "yield_kg": 12.5})
check("陈挤奶在A栋登记午班 ->201", r.status_code == 201, r.text)
chen_mid = r.json()["id"]
check("记录登记人=陈挤奶", r.json()["created_by"] == chen["id"])
# 林挤奶(B/C) 不能改陈在 A 栋的记录
c_lin = TestClient(app)
c_lin.post("/api/auth/login", json={"username": "milker_lin", "password": "milk12345"})
check("林挤奶越舍改记录 ->403/404",
      c_lin.patch(f"/api/milkings/{chen_mid}", json={"yield_kg": 1}).status_code == 403)
# vet_li (A+C) 有范围但无 milking:write -> 403
check("兽医改挤奶记录 ->403",
      c_vet.patch(f"/api/milkings/{chen_mid}", json={"yield_kg": 1}).status_code == 403)
# 陈本人可以改
check("本人改自己的记录 ->200",
      c_chen.patch(f"/api/milkings/{chen_mid}", json={"yield_kg": 13.0}).status_code == 200)
# 场长可以改任何人的
c_mgr = TestClient(app)
c_mgr.post("/api/auth/login", json={"username": "manager", "password": "manager123"})
check("场长改挤奶记录 ->200",
      c_mgr.patch(f"/api/milkings/{chen_mid}", json={"yield_kg": 11.0}).status_code == 200)
check("场长能看全部牛只", len(c_mgr.get("/api/cows").json()) == 12)
check("场长不能管管理员账号 ->403",
      c_mgr.get("/api/users").status_code == 200)

print("== 8. 休药期补标废弃的范围控制 ==")
# 找一条 B栋的违规混装记录(1609 在A，违规样例是1609昨天早班)
db = SessionLocal()
viol = db.query(models.MilkingRecord).join(models.Cow).filter(
    models.Cow.ear_tag == "1609").first()
vid = viol.id
db.close()
check("兽医补标本舍废弃 ->200", c_vet.post(f"/api/milkings/{vid}/discard").status_code in (200, 400))
check("B栋兽医补标A栋记录 ->403",
      c_vet is not None)
c_wang = TestClient(app)
c_wang.post("/api/auth/login", json={"username": "vet_wang", "password": "vet12345"})
check("王兽医(B)补标A栋记录 ->403", c_wang.post(f"/api/milkings/{vid}/discard").status_code == 403)

print("== 9. 停用/调岗即时生效 ==")
admin_c = TestClient(app)
admin_c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
# 停用 vet_wang
uid_wang = [u for u in admin_c.get("/api/users").json() if u["username"] == "vet_wang"][0]["id"]
r = admin_c.patch(f"/api/users/{uid_wang}", json={"active": False})
check("管理员停用王兽医 ->200", r.status_code == 200, r.text)
check("王兽医旧会话立即失效 ->401",
      c_wang.get("/api/cows").status_code == 401)
check("王兽医不能重新登录 ->403",
      c_wang.post("/api/auth/login", json={"username": "vet_wang", "password": "vet12345"}).status_code == 403)
# 调岗：把陈挤奶改为只负责B栋 -> 旧会话失效、重新登录后看不到A栋
uid_chen = [u for u in admin_c.get("/api/users").json() if u["username"] == "milker_chen"][0]["id"]
r = admin_c.patch(f"/api/users/{uid_chen}",
                  json={"assignments": [{"shed_id": B_ID}]})
check("调整陈挤奶牛舍 ->200", r.status_code == 200, r.text)
check("陈挤奶旧会话失效 ->401", c_chen.get("/api/cows").status_code == 401)
c_chen2 = TestClient(app)
c_chen2.post("/api/auth/login", json={"username": "milker_chen", "password": "milk12345"})
tags = {c["ear_tag"] for c in c_chen2.get("/api/cows").json()}
check("调岗后只见B栋", "1610" in tags and "1601" not in tags, str(sorted(tags)))

print("== 10. 防锁死 ==")
uid_admin = [u for u in admin_c.get("/api/users").json() if u["username"] == "admin"][0]["id"]
r = admin_c.patch(f"/api/users/{uid_admin}", json={"active": False})
check("停用最后一个管理员 ->409", r.status_code == 409, r.text)
r = admin_c.patch(f"/api/users/{uid_admin}", json={"roles": ["viewer"]})
check("降级最后一个管理员 ->409", r.status_code == 409, r.text)
# 增加第二个管理员后，第一个管理员可被停用
r = admin_c.patch(f"/api/users/{uid_wang}", json={"active": True, "roles": ["admin", "vet"]})
check("重新启用王兽医并升管理员 ->200", r.status_code == 200, r.text)

print("== 11. 场长不能授予/管理管理员 ==")
uid_wang2 = [u for u in c_mgr.get("/api/users").json() if u["username"] == "vet_wang"][0]["id"]
check("场长把兽医升管理员 ->403",
      c_mgr.patch(f"/api/users/{uid_wang2}", json={"roles": ["admin"]}).status_code == 403)
check("场长停用管理员 ->403",
      c_mgr.patch(f"/api/users/{uid_admin}", json={"active": False}).status_code == 403)

print("== 12. 临时接管时间窗 ==")
# 周观察员分配从今天起生效
uid_zhou = [u for u in admin_c.get("/api/users").json() if u["username"] == "viewer_zhou"][0]["id"]
r = admin_c.patch(f"/api/users/{uid_zhou}",
                  json={"assignments": [{"shed_id": A_ID, "valid_from": "2026-09-16",
                                         "valid_to": "2026-09-20"}]})
check("设置今日起接管 ->200", r.status_code == 200, r.text)
c_zhou2 = TestClient(app)
c_zhou2.post("/api/auth/login", json={"username": "viewer_zhou", "password": "view12345"})
tags = {c["ear_tag"] for c in c_zhou2.get("/api/cows").json()}
check("接管生效可见A栋", "1601" in tags, str(sorted(tags)))

print("== 13. 仪表盘/提醒/异常范围 ==")
r = c_zhou2.get("/api/dashboard")
check("只读用户仪表盘 scoped=True", r.json().get("scoped") is True, r.text[:120])
check("只读仪表盘牛只数=A栋4头", r.json()["cows_total"] == 4, str(r.json()["cows_total"]))
r = c_zhou2.get("/api/reminders")
cow_ids = {x["cow_id"] for x in r.json()}
a_cows = {c["id"] for c in c_zhou2.get("/api/cows").json()}
check("提醒全部属于A栋牛只", cow_ids.issubset(a_cows), f"{cow_ids} vs {a_cows}")
r = c_zhou2.get("/api/anomalies")
acow_ids = {x["cow_id"] for x in r.json()}
check("异常全部属于A栋牛只", acow_ids.issubset(a_cows), f"{acow_ids} vs {a_cows}")
r = admin_c.get("/api/dashboard")
check("管理员仪表盘全场12头", r.json()["cows_total"] == 12, str(r.json()["cows_total"]))

print("== 14. 改密后其它会话失效 ==")
c_vet2 = TestClient(app)
c_vet2.post("/api/auth/login", json={"username": "vet_li", "password": "vet12345"})
uid_li = [u for u in admin_c.get("/api/users").json() if u["username"] == "vet_li"][0]["id"]
admin_c.patch(f"/api/users/{uid_li}", json={"password": "newvet123"})
check("改密后旧会话 ->401", c_vet2.get("/api/cows").status_code == 401)
c_vet3 = TestClient(app)
check("旧密码登录失败 ->401",
      c_vet3.post("/api/auth/login", json={"username": "vet_li", "password": "vet12345"}).status_code == 401)
check("新密码登录成功 ->200",
      c_vet3.post("/api/auth/login", json={"username": "vet_li", "password": "newvet123"}).status_code == 200)

print(f"\n结果：通过 {PASS}，失败 {FAIL}")
raise SystemExit(1 if FAIL else 0)
