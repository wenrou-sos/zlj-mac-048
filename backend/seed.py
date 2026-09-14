"""初始化并写入样例牛群数据（所有日期相对今天生成，保证提醒场景可直接演示）"""
import random
from datetime import date, timedelta

from sqlalchemy.orm import Session

from . import models
from .database import Base, SessionLocal, engine


def _d(offset: int) -> date:
    return date.today() + timedelta(days=offset)


DRUGS = [
    # 名称, 类别, 默认牛奶休药期(天), 备注
    ("青霉素G钾", "抗生素", 4, "用于呼吸道、全身细菌感染"),
    ("注射用头孢噻呋钠", "抗生素", 3, "三代头孢，产后保健/细菌感染"),
    ("盐酸林可霉素", "抗生素", 5, "厌氧菌及支原体感染"),
    ("土霉素注射液", "抗生素", 7, "广谱抗菌"),
    ("恩诺沙星注射液", "氟喹诺酮抗菌药", 5, "消化道/呼吸道感染，孕畜慎用"),
    ("复方阿莫西林乳房灌注剂", "乳腺局部用药", 4, "临床型乳房炎，乳头灌注"),
    ("氟尼辛葡甲胺", "非甾体消炎镇痛药", 2, "发热、炎症辅助治疗"),
    ("缩宫素注射液", "生殖激素", 2, "产后子宫复旧、排脓"),
    ("氯前列醇钠", "前列腺素类", 0, "同期发情/诱导分娩"),
    ("伊维菌素注射液", "驱虫药", 14, "体内外寄生虫，休药期长"),
    ("葡萄糖酸钙注射液", "营养补液", 0, "产后瘫痪/低血钙"),
    ("维生素ADE注射液", "营养补充", 0, "产后保健"),
]

# ear_tag, 名字, 品种, 胎次, 状态, 群组, 产犊偏移, 预产偏移, 标定日产
COWS = [
    ("1601", "花花", "荷斯坦牛", 3, "lactating", "A栋1栏", -155, None, 34.0),
    ("1602", "黑妞", "荷斯坦牛", 2, "lactating", "A栋2栏", -200, None, 30.0),
    ("1603", "金花", "娟姗牛", 2, "lactating", "B栋1栏", -72, None, 22.0),
    ("1604", "大兰", "荷斯坦牛", 1, "lactating", "B栋2栏", -150, None, 26.0),
    ("1605", "青青", "荷斯坦牛", 4, "lactating", "A栋3栏", -260, None, 28.0),
    ("1606", "小满", "荷斯坦牛", 2, "pregnant", "C栋待产栏", -270, 6, 24.0),
    ("1607", "阿菊", "娟姗牛", 1, "lactating", "C栋产后栏", -5, None, 18.0),
    ("1608", "百合", "荷斯坦牛", 3, "dry", "D栋干奶栏", -250, 25, 0.0),
    ("1609", "甜豆", "荷斯坦牛", 2, "lactating", "A栋4栏", -90, None, 29.0),
    ("1610", "玉珠", "荷斯坦牛", 3, "lactating", "B栋3栏", -110, None, 31.0),
    ("1611", "奶咖", "瑞士褐牛", 1, "lactating", "C栋1栏", -180, None, 25.0),
    ("1612", "老将", "荷斯坦牛", 6, "sold", "已离场", -900, None, 0.0),
]

SESSION_WEIGHT = {"morning": 0.38, "noon": 0.28, "evening": 0.34}
SESSIONS = ["morning", "noon", "evening"]

# (牛耳标, 用药日偏移, 休药天数) —— 与下方用药记录保持一致，用于挤奶记录自动标记废弃
WITHDRAWAL_WINDOWS = [
    ("1609", -2, 3),   # 头孢噻呋钠
    ("1610", -12, 4),  # 复方阿莫西林乳房灌注
    ("1610", -12, 2),  # 氟尼辛葡甲胺（被上一条窗口覆盖）
    ("1607", -4, 2),   # 缩宫素
]


def in_withdrawal_window(tag: str, day_offset: int) -> bool:
    return any(t == tag and d <= day_offset <= d + wd for t, d, wd in WITHDRAWAL_WINDOWS)


def _gen_yield(rng: random.Random, base: float, dim: int, day_offset: int,
               session: str, tag: str) -> float:
    """根据胎次泌乳曲线生成模拟班次产量"""
    # 产后40天爬坡，200天后缓慢下降
    factor = 0.72 + 0.28 * min(1.0, dim / 40.0)
    if dim > 200:
        factor *= 1 - (dim - 200) * 0.0025
    noise = rng.uniform(-0.06, 0.06)
    kg = base * factor * SESSION_WEIGHT[session] * (1 + noise)
    # 1610 玉珠：近3天乳房炎导致骤降
    if tag == "1610" and day_offset >= -3:
        kg *= rng.uniform(0.58, 0.70)
    # 1611 奶咖：近5天原因不明的持续下滑
    if tag == "1611" and day_offset >= -5:
        kg *= {0: 0.62, -1: 0.66, -2: 0.70, -3: 0.78, -4: 0.85, -5: 0.92}[day_offset]
    return round(max(0.5, kg), 1)


def seed_database(db: Session) -> None:
    # ---------- 药品目录 ----------
    drug_map = {}
    for name, usage, wd, note in DRUGS:
        d = models.DrugCatalog(name=name, usage=usage, default_withdrawal_days=wd, note=note)
        db.add(d)
        drug_map[name] = d
    db.flush()

    # ---------- 奶牛档案 ----------
    cows = {}
    for tag, name, breed, parity, status, group, calv_off, exp_off, avg in COWS:
        cow = models.Cow(
            ear_tag=tag,
            name=name,
            breed=breed,
            birth_date=_d(-(parity * 365 + 800 + int(tag[-2:]))),
            parity=parity,
            status=status,
            group=group,
            calving_date=_d(calv_off) if calv_off is not None else None,
            expected_calving_date=_d(exp_off) if exp_off is not None else None,
            avg_yield_kg=avg if avg > 0 else None,
            note="样例数据" if status != "sold" else "已于上月转育肥场",
        )
        db.add(cow)
        db.flush()
        cows[tag] = cow

    # ---------- 挤奶记录：近20个完整日 + 今日早班 ----------
    for tag, cow in cows.items():
        if cow.status not in ("lactating",):
            continue
        base = cow.avg_yield_kg or 20
        for off in range(-20, 1):  # -20 .. 0
            sessions_today = SESSIONS if off < 0 else ["morning"]
            dim = (cow.calving_date and (_d(off) - cow.calving_date).days) or 100
            for sess in sessions_today:
                rng = random.Random(int(tag) * 100 + (-off) * 7 + SESSIONS.index(sess))
                scc = None
                if tag == "1610" and off >= -3:
                    scc = rng.randint(580, 920) * 1000  # 体细胞骤升
                elif rng.random() < 0.15:
                    scc = rng.randint(80, 260) * 1000
                # 休药期牛奶一律废弃，唯独保留 1609 昨天早班一条“违规混装”演示样例
                discarded = False
                note = None
                in_window = in_withdrawal_window(tag, off)
                if in_window:
                    discarded = not (tag == "1609" and off == -1 and sess == "morning")
                    note = "休药期废弃" if discarded else "休药期内未标注废弃（违规样例）"
                # 1607 新产牛前3天初乳不上市
                if tag == "1607" and dim <= 2:
                    discarded = True
                    note = "初乳期废弃（犊牛饲喂）"
                db.add(models.MilkingRecord(
                    cow_id=cow.id, date=_d(off), session=sess,
                    yield_kg=_gen_yield(rng, base, max(dim, 1), off, sess, tag),
                    scc=scc, discarded=discarded, note=note,
                ))

    # ---------- 健康记录 ----------
    db.add_all([
        models.HealthRecord(cow_id=cows["1610"].id, date=_d(-12), record_type="diagnosis",
                            diagnosis="临床型乳房炎（左后乳区）", temperature=40.3,
                            severity="severe", follow_up_date=_d(2), result="ongoing",
                            note="乳区红肿、乳汁絮片，已隔离单独挤奶"),
        models.HealthRecord(cow_id=cows["1607"].id, date=_d(-4), record_type="diagnosis",
                            diagnosis="产后子宫复旧不良", temperature=39.2,
                            severity="mild", follow_up_date=_d(3), result="observed",
                            note="恶露不尽，持续观察体温与采食"),
        models.HealthRecord(cow_id=cows["1601"].id, date=_d(-40), record_type="vaccination",
                            diagnosis="口蹄疫O型灭活疫苗", result="recovered"),
        models.HealthRecord(cow_id=cows["1609"].id, date=_d(-2), record_type="diagnosis",
                            diagnosis="发热待查（上呼吸道感染）", temperature=40.6,
                            severity="moderate", follow_up_date=_d(1), result="ongoing"),
        models.HealthRecord(cow_id=cows["1604"].id, date=_d(-60), record_type="checkup",
                            diagnosis="常规体检", temperature=38.7, result="recovered"),
        models.HealthRecord(cow_id=cows["1611"].id, date=_d(-8), record_type="checkup",
                            diagnosis="常规体检", temperature=38.9, result="recovered",
                            note="体况评分3.0，建议关注采食量"),
    ])

    # ---------- 用药记录 ----------
    db.add_all([
        models.Medication(
            cow_id=cows["1609"].id, drug_id=drug_map["注射用头孢噻呋钠"].id,
            drug_name="注射用头孢噻呋钠", date=_d(-2), dose="1g/次，每日1次，连用3日",
            route="颈部肌注", reason="发热呼吸道感染", withdrawal_days=3,
            withdrawal_end=_d(-2 + 3), next_dose_date=_d(1), treated=False,
            operator="李兽医"),
        models.Medication(
            cow_id=cows["1610"].id, drug_id=drug_map["复方阿莫西林乳房灌注剂"].id,
            drug_name="复方阿莫西林乳房灌注剂", date=_d(-12), dose="每乳区1支，每日2次，连用3日",
            route="乳头灌注", reason="临床型乳房炎", withdrawal_days=4,
            withdrawal_end=_d(-12 + 4), next_dose_date=None, treated=True,
            operator="王兽医"),
        models.Medication(
            cow_id=cows["1610"].id, drug_id=drug_map["氟尼辛葡甲胺"].id,
            drug_name="氟尼辛葡甲胺", date=_d(-12), dose="15mL/次", route="静注",
            reason="乳房炎消炎退热", withdrawal_days=2, withdrawal_end=_d(-12 + 2),
            treated=True, operator="王兽医"),
        models.Medication(
            cow_id=cows["1607"].id, drug_id=drug_map["缩宫素注射液"].id,
            drug_name="缩宫素注射液", date=_d(-4), dose="50IU", route="肌注",
            reason="促进产后子宫复旧", withdrawal_days=2, withdrawal_end=_d(-4 + 2),
            treated=True, operator="李兽医"),
        models.Medication(
            cow_id=cows["1607"].id, drug_id=drug_map["葡萄糖酸钙注射液"].id,
            drug_name="葡萄糖酸钙注射液", date=_d(-5), dose="500mL 缓慢静滴", route="静注",
            reason="预防产后低血钙", withdrawal_days=0, withdrawal_end=_d(-5),
            treated=True, operator="李兽医"),
        models.Medication(
            cow_id=cows["1601"].id, drug_id=drug_map["伊维菌素注射液"].id,
            drug_name="伊维菌素注射液", date=_d(-60), dose="20mL", route="皮下注射",
            reason="季度驱虫", withdrawal_days=14, withdrawal_end=_d(-60 + 14),
            treated=True, operator="王兽医"),
    ])

    # ---------- 发情/配种记录 ----------
    db.add_all([
        # 今日发情、尚未配种（1603 金花）
        models.EstrusRecord(cow_id=cows["1603"].id, date=_d(0), detection="activity",
                            score=5, inseminated=False,
                            note="计步器活动量较基线上升210%，站立发情"),
        # 1604 配后21天，返情复配观察窗口
        models.EstrusRecord(cow_id=cows["1604"].id, date=_d(-21), detection="detector",
                            score=4, inseminated=True, insemination_date=_d(-21),
                            semen="HO-2025-0219", technician="张配种员", result="pending"),
        # 1605 配后35天，孕检窗口
        models.EstrusRecord(cow_id=cows["1605"].id, date=_d(-37), detection="observed",
                            score=3, inseminated=True, insemination_date=_d(-35),
                            semen="HO-2025-0188", technician="张配种员", result="pending"),
        # 1606 已孕，待产
        models.EstrusRecord(cow_id=cows["1606"].id, date=_d(-210), detection="observed",
                            score=4, inseminated=True, insemination_date=_d(-205),
                            semen="JE-2024-0077", technician="张配种员",
                            result="pregnant", result_date=_d(-170)),
        # 1608 干奶牛，已孕
        models.EstrusRecord(cow_id=cows["1608"].id, date=_d(-195), detection="observed",
                            score=3, inseminated=True, insemination_date=_d(-190),
                            semen="HO-2024-0612", technician="张配种员",
                            result="pregnant", result_date=_d(-155)),
        # 1601 已孕在挤（孕中期）
        models.EstrusRecord(cow_id=cows["1601"].id, date=_d(-110), detection="activity",
                            score=4, inseminated=True, insemination_date=_d(-108),
                            semen="HO-2025-0301", technician="李配种员",
                            result="pregnant", result_date=_d(-75)),
        # 1602 上次配种未孕，长期空怀
        models.EstrusRecord(cow_id=cows["1602"].id, date=_d(-140), detection="observed",
                            score=2, inseminated=True, insemination_date=_d(-138),
                            semen="HO-2024-0555", technician="张配种员",
                            result="negative", result_date=_d(-105),
                            note="复检未孕，之后未见明显发情"),
    ])

    db.commit()


def init_db(force: bool = False) -> None:
    import os
    from .database import DB_PATH

    if force and DB_PATH.exists():
        os.remove(DB_PATH)
    Base.metadata.create_all(bind=engine)
    if force or db_is_empty():
        db = SessionLocal()
        try:
            seed_database(db)
        finally:
            db.close()


def db_is_empty() -> bool:
    db = SessionLocal()
    try:
        return db.query(models.Cow).count() == 0
    finally:
        db.close()


if __name__ == "__main__":
    init_db(force=True)
    print("样例数据库已生成")
