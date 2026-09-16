"""离线逻辑测试：用轻量桩模拟 SQLAlchemy 声明式模型，验证调查闭环对账规则"""
import sys
import types
import importlib.util
import json
import os
from datetime import date, timedelta

# ---------- 构造 sqlalchemy 桩 ----------
sa = types.ModuleType("sqlalchemy")
orm = types.ModuleType("sqlalchemy.orm")

_COLUMNS = {}      # 模型类 -> {列名: _Column}
_CURRENT_CLS = []  # 正在定义的类


class _Column:
    def __init__(self, name=None):
        self.name = name
    def desc(self): return _Order(self, True)
    def asc(self): return _Order(self, False)
    def __eq__(self, o): return _Expr(self, "eq", o)
    def __ne__(self, o): return _Expr(self, "ne", o)
    def __ge__(self, o): return _Expr(self, "ge", o)
    def __le__(self, o): return _Expr(self, "le", o)
    def __gt__(self, o): return _Expr(self, "gt", o)
    def __lt__(self, o): return _Expr(self, "lt", o)
    def in_(self, o): return _Expr(self, "in", o)
    def isnot(self, o): return _Expr(self, "isnot", o)
    def is_(self, o): return _Expr(self, "is", o)
    def like(self, o): return _Expr(self, "like", o)


class _Expr:
    def __init__(self, col, op, other):
        self.col, self.op, self.other = col, op, other


class _Order:
    def __init__(self, col, desc):
        self.col, self.desc = col, desc


def _make_col(*a, **kw):
    return _Column()


class _Exists:
    def where(self, *a): return self
def exists(): return _Exists()


sa.Column = _make_col
for n in ("Integer", "String", "Float", "Boolean", "Date", "DateTime", "Text"):
    setattr(sa, n, lambda *a, **k: None)
sa.ForeignKey = lambda *a, **kw: None
class _Or(list):
    """or_(...) 产生的条件组：行满足任一条件即可"""


sa.and_ = lambda *a: list(a)
sa.or_ = lambda *a: _Or(a)
sa.exists = exists


class _Rel:
    def __getitem__(self, i): raise IndexError
    def __iter__(self): return iter(())
    def __len__(self): return 0
orm.relationship = lambda *a, **kw: _Rel()
orm.Session = object
sys.modules["sqlalchemy"] = sa
sys.modules["sqlalchemy.orm"] = orm

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
pkg = types.ModuleType("backend")
pkg.__path__ = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend")]
sys.modules["backend"] = pkg
db_stub = types.ModuleType("backend.database")


class _Base:
    id = None

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        cols = dict(_COLUMNS.get(cls.__mro__[1], {})) if len(cls.__mro__) > 1 else {}
        for k, v in list(vars(cls).items()):
            if isinstance(v, _Column):
                v.name = k
                cols[k] = v
        _COLUMNS[cls] = cols

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    def __getattribute__(self, name):
        # 实例属性优先于同名 Column 类属性
        value = object.__getattribute__(self, name)
        if isinstance(value, _Column):
            return None
        return value


db_stub.Base = _Base
sys.modules["backend.database"] = db_stub


def load_mod(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


models = load_mod("backend.models", "backend/models.py")
services = load_mod("backend.services", "backend/services.py")

T = date.today()


class DB:
    def __init__(self):
        self.tables = {}
        self.next_id = {}

    def add(self, obj):
        t = type(obj).__name__
        self.tables.setdefault(t, []).append(obj)
        if getattr(obj, "id", None) is None:
            self.next_id[t] = self.next_id.get(t, 0) + 1
            obj.id = self.next_id[t]

    def add_all(self, objs):
        for o in objs:
            self.add(o)

    def flush(self): pass
    def commit(self): pass
    def refresh(self, o): pass
    def get(self, model, id_):
        return next((o for o in self.tables.get(model.__name__, []) if o.id == id_), None)
    def query(self, model):
        return Q(self, list(self.tables.get(model.__name__, [])), model)


def col_of(model, col):
    return col.name if isinstance(col, _Column) else None


class Q:
    def __init__(self, db, rows, model):
        self.db, self.rows, self.model = db, list(rows), model

    def filter(self, *exprs, **kw):
        rows = self.rows
        for k, v in kw.items():
            rows = [r for r in rows if getattr(r, k, None) == v]

        def flatten(x):
            out = []
            for i in x:
                if isinstance(i, list):
                    out.extend(flatten(i))
                elif isinstance(i, _Expr):
                    out.append(i)
            return out

        for expr in exprs:
            if isinstance(expr, _Exists):
                continue
            if isinstance(expr, _Or):
                rows = [r for r in rows if any(
                    self._op(getattr(r, col_of(self.model, e.col)), e) for e in flatten(list(expr))
                )]
                continue
            for e in flatten([expr]):
                f = col_of(self.model, e.col)
                rows = [r for r in rows if self._op(getattr(r, f, None), e)]
        return Q(self.db, rows, self.model)

    @staticmethod
    def _op(val, e):
        o = e.other
        if e.op == "eq": return val == o
        if e.op == "ne": return val != o
        if e.op == "in": return val in o
        if e.op == "ge": return val is not None and val >= o
        if e.op == "le": return val is not None and val <= o
        if e.op == "gt": return val is not None and val > o
        if e.op == "lt": return val is not None and val < o
        if e.op == "isnot": return val is not o
        if e.op == "is": return val is o
        if e.op == "like":
            seg = o.strip("%")
            return bool(val) and seg in val
        return True

    def filter_by(self, **kw): return self.filter(**kw)
    def order_by(self, *orders):
        rows = self.rows
        for o in orders:
            if isinstance(o, _Order):
                f = o.col.name
                rows = sorted(
                    rows,
                    key=lambda r, f=f: (getattr(r, f) is None, getattr(r, f) or ""),
                    reverse=o.desc,
                )
        return Q(self.db, rows, self.model)
    def limit(self, n): return Q(self.db, self.rows[:n], self.model)
    def join(self, *a, **kw): return self
    def all(self): return list(self.rows)
    def first(self): return self.rows[0] if self.rows else None
    def count(self): return len(self.rows)


def make_cow(db, tag="1610", name="玉珠"):
    c = models.Cow(id=1, ear_tag=tag, name=name, status="lactating")
    db.tables["Cow"] = [c]
    return c


def snap_danger(t_hit):
    return {
        "cow_id": 1, "level": "danger", "tags": ["high_scc", "yield_drop"],
        "reasons": ["SCC 90万；早班骤降35%"],
        "drop_hits": [{"date": str(t_hit), "session": "morning",
                       "milking_id": 1, "yield_kg": 7.1, "baseline_kg": 11.2, "drop_pct": 36.6}],
        "scc_hits": [{"date": str(t_hit), "session": "morning", "milking_id": 1, "scc": 900000}],
        "trend_hit": None, "latest_date": str(t_hit),
    }


# 场景1：首次异常 -> 自动开单 + detect 证据 + 发现日取最早命中日
db = DB()
make_cow(db)
snap = snap_danger(T - timedelta(days=2))
case = services._create_case(db, 1, snap, T)
assert case.status == "open"
assert case.detected_on == T - timedelta(days=2)
assert case.first_level == "danger" and case.first_snapshot
ev = db.query(models.AnomalyEvidence).filter_by(case_id=case.id).all()
assert len(ev) == 1 and ev[0].kind == "detect"
print("场景1 通过：自动开单，发现日 =", case.detected_on)

# 场景2：变严重 -> 追加 followup；发现时快照冻结
snap2 = json.loads(json.dumps(snap))
snap2["drop_hits"][0].update(yield_kg=5.0, drop_pct=55.4)
snap2["latest_date"] = str(T - timedelta(days=1))
services._update_open_case(db, case, snap2, T, "scheduled")
ev = db.query(models.AnomalyEvidence).filter_by(case_id=case.id).all()
assert len(ev) == 2 and case.latest_level == "danger"
detect_ev = next(e for e in ev if e.kind == "detect")
assert json.loads(detect_ev.snapshot)["drop_hits"][0]["yield_kg"] == 7.1
print("场景2 通过：变严重追加跟踪证据，发现时快照未被改写")

# 场景3：同判断再对账 -> 幂等
services._update_open_case(db, case, snap2, T, "scheduled")
assert db.query(models.AnomalyEvidence).filter_by(case_id=case.id).count() == 2
print("场景3 通过：判断不变不重复追加证据")

# 场景4：补改历史奶量导致结论变化 -> 新证据注明“补改”，旧证据不动
snap2b = json.loads(json.dumps(snap2))
snap2b["level"] = "warning"
snap2b["tags"] = ["yield_drop"]
services._update_open_case(db, case, snap2b, T, "milk_change")
ev = db.query(models.AnomalyEvidence).filter_by(case_id=case.id).order_by(
    models.AnomalyEvidence.id.desc()).all()
assert len(ev) == 3 and "补改后重新判断" in ev[0].note
assert json.loads(detect_ev.snapshot)["drop_hits"][0]["yield_kg"] == 7.1
print("场景4 通过：补改历史奶量产生新证据且注明来源，当时处置依据保留")

# 场景5：信号消退 -> ok 证据、不自动关闭；重复消退幂等
services._clear_open_case(db, case, T)
services._clear_open_case(db, case, T)
assert case.latest_level == "ok"
assert db.query(models.AnomalyEvidence).filter_by(case_id=case.id).count() == 4
print("场景5 通过：消退只记录待复查，不自动结案")

# 场景6：恢复关闭后再次异常 -> 另开新单、parent 指向旧单、状态 reopened
case.status, case.closed_at = "resolved", T
snap3 = snap_danger(T + timedelta(days=5))
new_case = services._open_or_reopen(db, 1, snap3, T + timedelta(days=5))
assert new_case and new_case.id != case.id and new_case.parent_case_id == case.id
assert new_case.status == "open"  # 复发另开新单：新单本身是“调查中”，复发关系由 parent 表达
print("场景6 通过：恢复后再次异常另开新单 #%d，关联上一单 #%d" % (new_case.id, case.id))

# 场景7：窗口内残留的纯旧命中（全部早于关闭日）-> 不重开也不另开；误报原因保留
db2 = DB()
make_cow(db2)
old_snap = snap_danger(T - timedelta(days=25))
old = services._create_case(db2, 1, old_snap, T - timedelta(days=25))
old.status, old.closed_at, old.false_reason = "false_positive", T - timedelta(days=20), "计量设备故障"
assert services._open_or_reopen(db2, 1, old_snap, T) is None
print("场景7 通过：误报关闭单+旧数据残留不重开/另开，误报原因保留")

# 场景8：完整 reconcile —— detector 全部消退时进行中单转 ok（追加1条证据）
db3 = DB()
make_cow(db3)
open_case = services._create_case(db3, 1, snap_danger(T - timedelta(days=3)), T - timedelta(days=3))
orig = services.detect_yield_anomalies
services.detect_yield_anomalies = lambda d, days=7, today=None: []
touched = services.reconcile_anomaly_cases(db3)
services.detect_yield_anomalies = orig
assert open_case.latest_level == "ok" and any(c.id == open_case.id for c in touched)
assert db3.query(models.AnomalyEvidence).filter_by(case_id=open_case.id).count() == 2
print("场景8 通过：例行对账把信号消退的进行中单标记待复查")

# 场景9：自动关联同牛30天内未结案的健康记录
db4 = DB()
make_cow(db4)
h = models.HealthRecord(id=1, cow_id=1, date=T - timedelta(days=5), record_type="diagnosis",
                        diagnosis="临床型乳房炎", result="ongoing")
db4.tables["HealthRecord"] = [h]
c9 = services._create_case(db4, 1, snap_danger(T - timedelta(days=3)), T)
links = db4.query(models.CaseHealthLink).filter_by(case_id=c9.id).all()
assert len(links) == 1 and links[0].health_id == 1
# 已康复的旧病历不自动关联
h2 = models.HealthRecord(id=2, cow_id=1, date=T - timedelta(days=40), record_type="checkup",
                         diagnosis="旧伤", result="recovered")
db4.tables["HealthRecord"].append(h2)
services._auto_link_health(db4, c9, T - timedelta(days=3))
assert len(db4.query(models.CaseHealthLink).filter_by(case_id=c9.id).all()) == 1
print("场景9 通过：开单自动关联近30天未结案健康记录，已康复/过旧记录不关联")

print("\n全部场景通过 ✅")
