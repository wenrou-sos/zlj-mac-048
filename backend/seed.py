"""初始化并写入样例牛群数据（所有日期相对今天生成，保证提醒场景可直接演示）"""
import datetime
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
                # 休药期废弃在用药疗程落库后按实际给药窗口统一回算（见 _mark_withdrawal_milk）
                discarded = False
                note = None
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
    db.flush()

    # ---------- 用药记录（旧零散记录：继续保留、继续参与休药校验） ----------
    db.add_all([
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

    # ---------- 用药疗程（围绕病历，多药/多次/多班，逐次记录实际给药） ----------
    hr_1609 = next(h for h in db.query(models.HealthRecord).filter_by(cow_id=cows["1609"].id))
    hr_1610 = next(h for h in db.query(models.HealthRecord).filter_by(cow_id=cows["1610"].id))
    hr_1611 = models.HealthRecord(
        cow_id=cows["1611"].id, date=_d(-2), record_type="diagnosis",
        diagnosis="前胃弛缓/食欲下降（治疗观察）", temperature=39.1,
        severity="mild", follow_up_date=_d(2), result="ongoing",
        note="近5天产奶量持续下滑，采食减少，瘤胃蠕动弱，建立疗程并交班观察")
    db.add(hr_1611)
    db.flush()

    def line(course, drug_name, planned_dose, route, tpd, wd, times,
             status="active", seq=1, change_reason=None):
        ln = models.CourseDrug(
            course_id=course.id, drug_id=drug_map[drug_name].id, drug_name=drug_name,
            planned_dose=planned_dose, route=route, times_per_day=tpd,
            interval_days=1, withdrawal_days=wd, seq=seq, status=status,
            change_reason=change_reason)
        db.add(ln)
        db.flush()
        return ln

    def planned_dose(course_obj, ln, no, day_off, t, dose_text=None):
        obj = models.CourseDose(
            course_id=course_obj.id, course_drug_id=ln.id, cow_id=course_obj.cow_id,
            dose_no=no, planned_date=_d(day_off), planned_time=t,
            planned_dose=dose_text or ln.planned_dose, status="planned")
        db.add(obj)
        return obj

    def give(obj, day_off, t=None, dose_text=None, wd=None, operator="李兽医", note=None):
        line_id = obj.course_drug_id
        d = _d(day_off)
        obj.status = "administered"
        obj.administered_date = d
        obj.administered_time = t or obj.planned_time
        obj.administered_dose = dose_text or obj.planned_dose
        obj.withdrawal_days = ln_wd[line_id] if wd is None else wd
        obj.withdrawal_end = d + timedelta(days=obj.withdrawal_days)
        obj.operator = operator
        obj.recorded_by = operator
        obj.recorded_at = datetime.datetime.utcnow()
        obj.note = note
        return obj

    def miss(obj, reason):
        obj.status = "missed"
        obj.reason = reason
        obj.recorded_at = datetime.datetime.utcnow()
        return obj

    # 1609 甜豆：发热呼吸道感染，头孢每日1次×3。首针已打→第2针漏用（交班可见）
    # → 今天第3针待执行（休药期因此只来自首针，演示“未执行计划不算已用药”）
    c1609 = models.TreatmentCourse(
        cow_id=cows["1609"].id, health_record_id=hr_1609.id,
        title="发热（上呼吸道感染）抗菌疗程", start_date=_d(-2),
        planned_end_date=_d(0), veterinarian="李兽医",
        note="交班：今天必须完成第3针并复查体温；若仍发热请王兽医会诊。")
    db.add(c1609)
    db.flush()
    ln1609 = line(c1609, "注射用头孢噻呋钠", "1g/次", "颈部肌注", 1, 3, ["morning"])
    ln_wd = {ln1609.id: 3}
    d1 = planned_dose(c1609, ln1609, 1, -2, "morning")
    d2 = planned_dose(c1609, ln1609, 2, -1, "morning")
    d3 = planned_dose(c1609, ln1609, 3, 0, "morning")
    give(d1, -2, operator="李兽医")
    miss(d2, "清晨牛舍周转遗漏，夜班发现时已错过，交班评估是否补做")
    db.add_all([
        models.CourseEvent(course_id=c1609.id, event_type="created",
                           summary="建立疗程「发热（上呼吸道感染）抗菌疗程」，安排 1 种药",
                           operator="李兽医"),
        models.CourseEvent(course_id=c1609.id, event_type="administered",
                           summary="第 1 次 注射用头孢噻呋钠 已给药：" + str(_d(-2)) + " 早班",
                           detail="剂量 1g/次；休药 3 天，至 " + str(_d(-2 + 3)),
                           operator="李兽医"),
        models.CourseEvent(course_id=c1609.id, event_type="missed",
                           summary="第 2 次 注射用头孢噻呋钠（" + str(_d(-1))
                                   + " 早班）漏用：清晨牛舍周转遗漏，夜班发现时已错过，交班评估是否补做"),
        models.CourseEvent(course_id=c1609.id, event_type="note",
                           summary="更新交班备注", detail=c1609.note),
    ])

    # 1610 玉珠：临床型乳房炎，两种药并行——阿莫西林每日2班×3日（6次，已完成），
    # 氟尼辛当日单次辅助消炎（已完成）；疗程已结束，但休药限制仍有效
    c1610 = models.TreatmentCourse(
        cow_id=cows["1610"].id, health_record_id=hr_1610.id,
        title="临床型乳房炎（左后乳区）联合治疗", start_date=_d(-12),
        planned_end_date=_d(-10), end_date=_d(-10), status="ended",
        end_reason="用药3日后体温正常、乳区红肿消退，医嘱疗程结束进入休药观察",
        veterinarian="王兽医",
        note="休药至" + str(_d(-12 + 4)) + "，次日鲜奶方可上市；继续监测SCC。")
    db.add(c1610)
    db.flush()
    ln_amx = line(c1610, "复方阿莫西林乳房灌注剂", "每乳区1支", "乳头灌注", 2, 4,
                  ["morning", "evening"])
    ln_fln = line(c1610, "氟尼辛葡甲胺", "15mL/次", "静注", 1, 2, ["noon"])
    ln_wd.update({ln_amx.id: 4, ln_fln.id: 2})
    amx_days = [-12, -11, -10]
    no = 1
    for off in amx_days:
        for t in ("morning", "evening"):
            give(planned_dose(c1610, ln_amx, no, off, t), off, t, operator="王兽医")
            no += 1
    give(planned_dose(c1610, ln_fln, 1, -12, "noon"), -12, "noon", operator="王兽医")
    db.add(models.CourseEvent(
        course_id=c1610.id, event_type="ended",
        summary="结束疗程：" + c1610.end_reason,
        detail="既有休药限制继续有效"))

    # 1611 奶咖：食欲下降，维ADE原计划2日→第2日换药为葡萄糖酸钙（注明原因）；
    # 钙静滴每日1次×3：首针已打、今天待执行、明天待执行（交班剩余安排）
    c1611 = models.TreatmentCourse(
        cow_id=cows["1611"].id, health_record_id=hr_1611.id,
        title="前胃弛缓/食欲下降支持治疗", start_date=_d(-2),
        planned_end_date=_d(1), veterinarian="李兽医",
        note="交班：今明两天各1次葡萄糖酸钙静滴；观察采食与产奶量回升情况。")
    db.add(c1611)
    db.flush()
    ln_vit = line(c1611, "维生素ADE注射液", "10mL/次", "颈部肌注", 1, 0, ["morning"],
                  status="switched", seq=1,
                  change_reason="换为 葡萄糖酸钙注射液：肌注后精神改善不明显，改静注补钙促采食")
    ln_ca = line(c1611, "葡萄糖酸钙注射液", "500mL 缓慢静滴", "静注", 1, 0, ["morning"],
                 seq=2)
    ln_wd.update({ln_vit.id: 0, ln_ca.id: 0})
    v1 = planned_dose(c1611, ln_vit, 1, -2, "morning")
    v2 = planned_dose(c1611, ln_vit, 2, -1, "morning")
    give(v1, -2, operator="李兽医", note="首次肌注")
    v2.status = "cancelled"
    v2.cancel_scope = "switch"
    v2.cancel_reason = "换药为 葡萄糖酸钙注射液：肌注后精神改善不明显，改静注补钙促采食"
    v2.recorded_at = datetime.datetime.utcnow()
    ca1 = planned_dose(c1611, ln_ca, 1, -1, "morning")
    ca2 = planned_dose(c1611, ln_ca, 2, 0, "morning")
    ca3 = planned_dose(c1611, ln_ca, 3, 1, "morning")
    give(ca1, -1, operator="夜班赵师傅")
    db.add_all([
        models.CourseEvent(course_id=c1611.id, event_type="created",
                           summary="建立疗程「前胃弛缓/食欲下降支持治疗」，安排 1 种药",
                           operator="李兽医"),
        models.CourseEvent(course_id=c1611.id, event_type="administered",
                           summary="第 1 次 维生素ADE注射液 已给药：" + str(_d(-2)) + " 早班",
                           operator="李兽医"),
        models.CourseEvent(course_id=c1611.id, event_type="switched",
                           summary="维生素ADE注射液 换药为 葡萄糖酸钙注射液",
                           detail="肌注后精神改善不明显，改静注补钙促采食"),
        models.CourseEvent(course_id=c1611.id, event_type="cancelled",
                           summary="第 2 次 维生素ADE注射液（" + str(_d(-1))
                                   + " 早班）取消：换药为 葡萄糖酸钙注射液"),
        models.CourseEvent(course_id=c1611.id, event_type="administered",
                           summary="第 1 次 葡萄糖酸钙注射液 已给药：" + str(_d(-1)) + " 早班",
                           detail="剂量 500mL 缓慢静滴；休药 0 天", operator="夜班赵师傅"),
    ])

    db.flush()

    # ---------- 按“已实际给药”的休药窗口回算挤奶废弃标记 ----------
    # 唯一例外：1609 昨天早班故意保留一条休药期内未废弃记录，用于演示违规混装拦截
    from .course_services import all_withdrawal_windows
    all_cows = list(cows.values())
    cow_ids = [c.id for c in all_cows]
    cows_by_id = {c.id: c for c in all_cows}
    min_d, max_d = _d(-20), _d(0)
    windows = all_withdrawal_windows(db, cow_ids, min_d, max_d)
    milk_rows = db.query(models.MilkingRecord).filter(
        models.MilkingRecord.date >= min_d).all()
    for r in milk_rows:
        if r.discarded:
            continue  # 初乳期废弃等既定标记不动
        wins = windows.get(r.cow_id, [])
        in_w = any(w["start"] <= r.date <= w["end"] for w in wins)
        if not in_w:
            continue
        tag = cows_by_id[r.cow_id].ear_tag
        deliberate = tag == "1609" and r.date == _d(-1) and r.session == "morning"
        r.discarded = not deliberate
        r.note = "休药期内未标注废弃（违规样例）" if deliberate else "休药期废弃"

    db.commit()

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


def _infer_cancel_scope(d: models.CourseDose, line: models.CourseDrug) -> str:
    """为历史已取消剂量推断来源。

    优先看剂量自身原因前缀（最准确），再回退到用药行状态：
    - “结束疗程：…” 优先于行状态（结束疗程时各行也会被置为 stopped）
    - “换药为 …” / switched 行 -> switch
    - “停药：…” / stopped 行 -> line_stop
    - 其余（含 active 行上的单次取消）-> manual
    """
    reason = d.reason or ""
    change_reason = (line.change_reason if line is not None else "") or ""
    if reason.startswith("结束疗程：") or change_reason.startswith("结束疗程："):
        return "course_end"
    if reason.startswith("换药为") or (line is not None and line.status == "switched"):
        return "switch"
    if reason.startswith("停药：") or (line is not None and line.status == "stopped"):
        return "line_stop"
    return "manual"


def backfill_cancel_scope(db: Session) -> None:
    """老数据迁移：补齐取消来源与批量取消原因。

    - 上一版已批量取消但只写了 reason：推断 scope，并把批量原因挪到 cancel_reason，
      保留延期日期（老版数据延期信息若已丢失则无法找回）。
    - 单次手动取消保持 reason 不变。
    """
    from .migrate import table_exists
    if not table_exists(engine, "treatment_courses"):
        return
    line_ids = [row[0] for row in db.query(models.CourseDrug.id).all()]
    lines = {
        ln.id: ln for ln in
        db.query(models.CourseDrug).filter(models.CourseDrug.id.in_(line_ids)).all()
    } if line_ids else {}
    changed = False
    q = db.query(models.CourseDose).filter(
        models.CourseDose.status == "cancelled",
        models.CourseDose.cancel_scope.is_(None),
    )
    for d in q.all():
        ln = lines.get(d.course_drug_id)
        scope = _infer_cancel_scope(d, ln)
        d.cancel_scope = scope
        if scope in ("line_stop", "switch", "course_end") and d.reason:
            # 老版把批量取消原因写在了 dose.reason：迁移到专门字段，
            # 若无延期信息（delayed_to 为空），清空 reason 以免与延期原因混淆
            d.cancel_reason = d.reason
            if d.delayed_to is None:
                d.reason = None
        changed = True
    if changed:
        db.commit()


def init_db(force: bool = False) -> None:
    import os
    from .database import DB_PATH
    from .migrate import run_lightweight_migrations

    if force and DB_PATH.exists():
        os.remove(DB_PATH)
    Base.metadata.create_all(bind=engine)
    # 老库升级：补齐新增列（create_all 不会改已存在的表）
    run_lightweight_migrations(engine)
    if force or db_is_empty():
        db = SessionLocal()
        try:
            seed_database(db)
        finally:
            db.close()
    else:
        db = SessionLocal()
        try:
            backfill_cancel_scope(db)
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
