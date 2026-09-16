"""端到端冒烟：牛舍容量 / 批量转群 / 争用 / 隔离 / 延期 / 取消 / 补录重叠"""
from datetime import date, timedelta

from fastapi.testclient import TestClient

from backend.main import app

client = TestClient(app)
T = {}
failures = []


def check(name, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {extra if cond else ''}")
    if not cond:
        failures.append(name)


def expect_conflict(r):
    try:
        detail = r.json().get("detail", "")
    except Exception:
        detail = r.text
    return r.status_code == 409 and ("重叠" in detail or "冲突" in detail or "容量" in detail), detail


# ---------- 栏位 ----------
pens = {p["name"]: p for p in client.get("/api/pens").json()}
check("A栋2栏容量2、当前在栏1(1602)、剩1",
      pens["A栋2栏"]["capacity"] == 2 and pens["A栋2栏"]["occupied"] == 1
      and pens["A栋2栏"]["free"] == 1, str(pens["A栋2栏"]))
check("E栋隔离舍当前在栏1(1610)", pens["E栋隔离舍"]["occupied"] == 1)

cows = {c["ear_tag"]: c for c in client.get("/api/cows").json()}
T["1603"], T["1604"], T["1609"], T["1610"] = (
    cows["1603"]["id"], cows["1604"]["id"], cows["1609"]["id"], cows["1610"]["id"])

tomorrow = (date.today() + timedelta(days=1)).isoformat()
day_after = (date.today() + timedelta(days=2)).isoformat()
a2 = pens["A栋2栏"]["id"]
b1 = pens["B栋1栏"]["id"]
iso = pens["E栋隔离舍"]["id"]
a4 = pens["A栋4栏"]["id"]

plans0 = {p["title"]: p for p in client.get("/api/transfers").json()}
pa = plans0["金花转入挤奶动线前排"]
pb = plans0["大兰调整至 A栋2栏"]
pc = plans0["甜豆呼吸道感染隔离"]
check("种子：甲/乙为待确认明天生效", pa["status"] == "draft" and pb["status"] == "draft")

# ---------- 预检：乙与甲争用，应报冲突（含其他草稿推演） ----------
prev = client.post("/api/transfers/preview", json={
    "title": "再次安排大兰", "effective_date": tomorrow, "kind": "group",
    "items": [{"cow_id": T["1604"], "to_pen_id": a2}],
}).json()
check("预检能提前看到 A栋2栏 容量冲突",
      not prev["ok"] and any(c["name"] == "A栋2栏" for c in prev["pen_conflicts"]),
      str(prev["pen_conflicts"]))

# ---------- 先确认甲成功 ----------
r = client.post(f"/api/transfers/{pa['id']}/confirm")
check("先确认者甲成功(200)", r.status_code == 200, r.text[:200])

# ---------- 再确认乙失败：争用最后栏位 ----------
r = client.post(f"/api/transfers/{pb['id']}/confirm")
ok, detail = expect_conflict(r)
check("后确认者乙因容量不足失败(409)", ok, detail[:200])

pens2 = {p["name"]: p for p in client.get("/api/pens", params={"on_date": tomorrow}).json()}
check("明天 A栋2栏 在栏2满栏（1602+1603，1604未进）",
      pens2["A栋2栏"]["occupied"] == 2 and pens2["A栋2栏"]["free"] == 0, str(pens2["A栋2栏"]))

# ---------- 草稿安排可自由延期 ----------
r = client.post(f"/api/transfers/{pb['id']}/postpone",
                json={"effective_date": day_after})
check("草稿安排可自由延期", r.status_code == 200, r.text[:200])
client.post(f"/api/transfers/{pb['id']}/postpone", json={"effective_date": tomorrow})

# ---------- 取消甲（未来生效），stays 回收；再确认乙成功 ----------
r = client.post(f"/api/transfers/{pa['id']}/cancel", json={"reason": "测试取消"})
check("取消未生效的甲成功", r.status_code == 200, r.text[:150])
pens3 = {p["name"]: p for p in client.get("/api/pens", params={"on_date": tomorrow}).json()}
check("取消后明天 A栋2栏 恢复剩1", pens3["A栋2栏"]["free"] == 1, str(pens3["A栋2栏"]))
r = client.post(f"/api/transfers/{pb['id']}/confirm")
check("甲取消后乙确认成功", r.status_code == 200, r.text[:200])

# ---------- 隔离：确认丙（明天隔离，5天后返回） ----------
r = client.post(f"/api/transfers/{pc['id']}/confirm")
check("确认隔离安排丙成功", r.status_code == 200, r.text[:200])
pens_iso_tom = {p["name"]: p for p in client.get("/api/pens", params={"on_date": tomorrow}).json()}
check("明天隔离舍在栏2（1610+1609）", pens_iso_tom["E栋隔离舍"]["occupied"] == 2,
      str(pens_iso_tom["E栋隔离舍"]))
# 隔离舍只有2位：再安排一头牛明天进隔离应失败
r = client.post("/api/transfers", json={
    "title": "第三头牛隔离", "effective_date": tomorrow, "kind": "isolation",
    "items": [{"cow_id": T["1603"], "to_pen_id": iso, "from_pen_id": b1,
               "return_date": (date.today() + timedelta(days=8)).isoformat()}],
})
ok, detail = expect_conflict(r)
check("隔离舍满员时第三头无法创建(409)", ok, detail[:200])

# 丙延期到后天（隔离段整体后移，返回日不变），明天隔离舍只剩1610
r = client.post(f"/api/transfers/{pc['id']}/postpone",
                json={"effective_date": day_after})
check("已确认隔离安排可延期", r.status_code == 200, r.text[:200])
pens_iso_tom2 = {p["name"]: p for p in client.get("/api/pens", params={"on_date": tomorrow}).json()}
check("延期后明天隔离舍在栏1", pens_iso_tom2["E栋隔离舍"]["occupied"] == 1,
      str(pens_iso_tom2["E栋隔离舍"]))
pens_iso_d2 = {p["name"]: p for p in client.get("/api/pens", params={"on_date": day_after}).json()}
check("后天隔离舍在栏2", pens_iso_d2["E栋隔离舍"]["occupied"] == 2)

# 丙取消（仍未生效）：stays 回收，1609 一直住 A栋4栏
r = client.post(f"/api/transfers/{pc['id']}/cancel", json={"reason": "康复不隔离"})
check("未生效隔离可取消", r.status_code == 200)
stays_1609 = client.get("/api/stays", params={"cow_id": T["1609"]}).json()
check("取消后1609仅一条开放区间在A栋4栏",
      len(stays_1609) == 1 and stays_1609[0]["pen_name"] == "A栋4栏"
      and stays_1609[0]["end_date"] is None, str(stays_1609))

# ---------- 已生效隔离（1610）提前回迁 ----------
iso_plan = next(p for p in client.get("/api/transfers").json()
                if p["title"] == "玉珠乳房炎隔离治疗")
ret_date = tomorrow
r = client.post(f"/api/transfers/{iso_plan['id']}/release",
                json={"return_date": ret_date})
check("1610 办理提前回迁成功", r.status_code == 200, r.text[:200])
stays_1610 = client.get("/api/stays", params={"cow_id": T["1610"]}).json()
names = [(s["pen_name"], s["start_date"], s["end_date"]) for s in stays_1610]
check("1610 居住链：B3 -> 隔离 -> B3(明天返回)",
      any(s["pen_name"] == "E栋隔离舍" and s["end_date"] == ret_date for s in stays_1610)
      and any(s["pen_name"] == "B栋3栏" and s["start_date"] == ret_date for s in stays_1610),
      str(names))

# ---------- 补录历史 ----------
r = client.post("/api/stays/backfill", json={
    "cow_id": T["1603"], "pen_id": b1,
    "start_date": (date.today() - timedelta(days=10)).isoformat(),
    "end_date": (date.today() - timedelta(days=5)).isoformat(),
})
ok, detail = expect_conflict(r)
check("与现有开放区间重叠的封闭补录被拒绝(409)", ok, detail[:200])

r = client.post("/api/stays/backfill", json={
    "cow_id": T["1603"], "pen_id": a4,
    "start_date": (date.today() - timedelta(days=10)).isoformat(),
})
ok, detail = expect_conflict(r)
check("更晚迁入日的开放补录被拒绝(409)", ok, detail[:200])

r = client.post("/api/stays/backfill", json={
    "cow_id": T["1603"], "pen_id": pens["A栋3栏"]["id"],
    "start_date": (date.today() - timedelta(days=120)).isoformat(),
    "end_date": (date.today() - timedelta(days=100)).isoformat(),
    "note": "历史寄养",
})
check("不重叠封闭补录成功", r.status_code == 201, r.text[:200])

r = client.post("/api/stays/backfill", json={
    "cow_id": T["1603"], "pen_id": b1,
    "start_date": (date.today() - timedelta(days=90)).isoformat(),
})
check("开放区间补录（更早迁入点）成功并自动截断旧区间", r.status_code == 201, r.text[:200])
st = sorted(client.get("/api/stays", params={"cow_id": T["1603"]}).json(),
            key=lambda s: s["start_date"])
check("1603 时间链无重叠",
      all(st[i]["end_date"] is None or st[i]["end_date"] <= st[i + 1]["start_date"]
          for i in range(len(st) - 1)), str([(s["pen_name"], s["start_date"], s["end_date"]) for s in st]))
check("当前开放区间起点为补录迁入日",
      st[-1]["start_date"] == (date.today() - timedelta(days=90)).isoformat()
      and st[-1]["end_date"] is None, str(st[-1]))

# 补录未来日期必须拒绝
cow_1602b = next(c["id"] for c in client.get("/api/cows").json() if c["ear_tag"] == "1602")
r = client.post("/api/stays/backfill", json={
    "cow_id": cow_1602b, "pen_id": a4,
    "start_date": tomorrow,
})
check("补录未来日期被拒绝", r.status_code == 409, r.text[:150])

# ---------- 牛只详情 ----------
detail = client.get(f"/api/cows/{T['1610']}").json()
check("详情含 current_pen=隔离舍 与 stays",
      detail["current_pen"] and detail["current_pen"]["name"] == "E栋隔离舍"
      and len(detail["stays"]) >= 2, str(detail.get("current_pen")))

# ---------- 容量下调不允许低于在栏数 ----------
r = client.patch(f"/api/pens/{a2}", json={"capacity": 0})
check("容量不能低于当前在栏数", r.status_code == 400)

# ---------- 停用栏位不能迁入 ----------
cow_1602 = next(c["id"] for c in client.get("/api/cows").json() if c["ear_tag"] == "1602")
r = client.post("/api/transfers", json={
    "title": "迁入停用栏", "effective_date": tomorrow, "kind": "group",
    "items": [{"cow_id": cow_1602, "to_pen_id": pens["已离场"]["id"]}],
})
check("迁入停用栏位被拒绝", r.status_code == 409 and "停用" in r.json()["detail"], r.text[:200])

print()
print("FAILURES:", failures if failures else "none")
