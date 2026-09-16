"""业务规则：发情/配种/用药/健康提醒、休药期校验、奶量异常发现"""
from collections import defaultdict
from datetime import date, timedelta
from typing import Dict, List, Optional

from sqlalchemy import and_, exists, or_
from sqlalchemy.orm import Session

from . import models

SESSION_LABEL = {"morning": "早班", "noon": "午班", "evening": "晚班"}
STATUS_LABEL = {
    "lactating": "泌乳中", "dry": "干奶", "pregnant": "待产", "sold": "已离场",
}


def withdrawal_violation_clause(on_column=None, cow_column=None):
    """
    生成“该挤奶记录当日处于休药期内且未标记废弃”的 SQL EXISTS 条件，
    可直接用于 WHERE 过滤，避免先 limit 截断再在内存里漏判。
    作废的挤奶记录与作废的用药均不参与判定。
    """
    on_column = on_column or models.MilkingRecord.date
    cow_column = cow_column or models.MilkingRecord.cow_id
    med = models.Medication
    return and_(
        models.MilkingRecord.discarded.is_(False),
        models.MilkingRecord.is_void.is_(False),
        exists().where(
            med.cow_id == cow_column,
            med.withdrawal_days > 0,
            med.is_void.is_(False),
            med.date <= on_column,
            med.withdrawal_end >= on_column,
        ),
    )


def cow_label(cow: models.Cow) -> str:
    n = f"（{cow.name}）" if cow.name else ""
    return f"{cow.ear_tag}{n}"


def latest_medication_window(
    db: Session, cow_id: int, on_date: date
) -> Optional[models.Medication]:
    """返回指定日期仍处于休药期内的用药记录（取截止日最晚的一条）"""
    med = (
        db.query(models.Medication)
        .filter(
            models.Medication.cow_id == cow_id,
            models.Medication.withdrawal_days > 0,
            models.Medication.is_void.is_(False),
            models.Medication.date <= on_date,
            models.Medication.withdrawal_end >= on_date,
        )
        .order_by(models.Medication.withdrawal_end.desc())
        .first()
    )
    return med


def check_withdrawal(db: Session, cow_id: int, on_date: date) -> dict:
    """休药期校验：返回某牛某日是否处于休药期及明细"""
    med = latest_medication_window(db, cow_id, on_date)
    if not med:
        return {"in_withdrawal": False, "medication": None}
    return {
        "in_withdrawal": True,
        "medication": med,
        "withdrawal_end": med.withdrawal_end,
        "drug_name": med.drug_name,
        "message": f"该牛自 {med.date} 使用 {med.drug_name}，"
                   f"牛奶休药期至 {med.withdrawal_end}（含当天），当日鲜奶应废弃",
    }


def build_reminders(db: Session, today: Optional[date] = None) -> List[dict]:
    """汇总全部提醒（发情、配种孕检、用药、健康复查、待产、长期空怀）"""
    today = today or date.today()
    out: List[dict] = []

    # 1) 发情未配种：发情后 24~48 小时为最佳配种窗口，提醒保留 2 天
    for e in (
        db.query(models.EstrusRecord)
        .filter(~models.EstrusRecord.inseminated)
        .order_by(models.EstrusRecord.date.desc())
        .all()
    ):
        cow = db.get(models.Cow, e.cow_id)
        if not cow or cow.status == "sold":
            continue
        if 0 <= (today - e.date).days <= 2:
            urgent = (today - e.date).days == 0
            out.append({
                "type": "estrus",
                "level": "danger" if urgent else "warning",
                "cow_id": cow.id,
                "cow_tag": cow.ear_tag,
                "title": f"{cow_label(cow)} 发情待配种",
                "detail": f"{e.date} 通过{_det_label(e.detection)}发现发情"
                          f"{f'，强度{e.score}级' if e.score else ''}，"
                          f"请于发情后 12 小时内适时输精",
                "due_date": str(e.date + timedelta(days=1)),
                "days_overdue": max(0, (today - e.date - timedelta(days=1)).days),
            })

    # 2) 配种后孕检 / 返情
    seen_cows: set = set()
    for e in (
        db.query(models.EstrusRecord)
        .filter(models.EstrusRecord.inseminated)
        .order_by(models.EstrusRecord.insemination_date.desc())
        .all()
    ):
        if e.cow_id in seen_cows:
            continue
        seen_cows.add(e.cow_id)
        cow = db.get(models.Cow, e.cow_id)
        if not cow or cow.status == "sold":
            continue
        if e.result == "pregnant":
            continue
        insem = e.insemination_date or e.date
        days = (today - insem).days
        if days < 18:
            continue
        # 18~24天：返情观察；35~45天：孕检；超过60天无结果：长期未确认
        if 18 <= days <= 24 and e.result in ("pending", None):
            out.append({
                "type": "return_estrus",
                "level": "warning",
                "cow_id": cow.id,
                "cow_tag": cow.ear_tag,
                "title": f"{cow_label(cow)} 进入返情观察期",
                "detail": f"配种已 {days} 天（冻精 {e.semen or '-'}），"
                          f"注意观察是否返情，必要时复配",
                "due_date": str(insem + timedelta(days=24)),
                "days_overdue": 0,
            })
        elif 25 <= days <= 60 and e.result in ("pending", None):
            overdue = max(0, days - 42)
            out.append({
                "type": "preg_check",
                "level": "danger" if overdue else "warning",
                "cow_id": cow.id,
                "cow_tag": cow.ear_tag,
                "title": f"{cow_label(cow)} 待妊娠检查",
                "detail": f"配种已 {days} 天，建议尽快做直肠/B超孕检并回填结果",
                "due_date": str(insem + timedelta(days=42)),
                "days_overdue": overdue,
            })
        elif days > 60 and e.result in ("pending", None, "negative"):
            out.append({
                "type": "open_cow",
                "level": "danger",
                "cow_id": cow.id,
                "cow_tag": cow.ear_tag,
                "title": f"{cow_label(cow)} 长期空怀需处理",
                "detail": f"上次配种/未孕距今 {days} 天，请兽医评估卵巢与子宫状态，"
                          f"安排同期发情或淘汰评估",
                "due_date": str(insem + timedelta(days=60)),
                "days_overdue": days - 60,
            })

    # 3) 用药提醒：下次用药 / 休药期进行中
    for m in db.query(models.Medication).filter(models.Medication.is_void.is_(False)).all():
        cow = db.get(models.Cow, m.cow_id)
        if not cow or cow.status == "sold":
            continue
        if m.next_dose_date and not m.treated:
            delta = (m.next_dose_date - today).days
            if -3 <= delta <= 3:
                out.append({
                    "type": "medication_dose",
                    "level": "danger" if delta <= 0 else "warning",
                    "cow_id": cow.id,
                    "cow_tag": cow.ear_tag,
                    "title": f"{cow_label(cow)} 待续用药",
                    "detail": f"{m.drug_name}（{m.dose or ''}）下次用药日 "
                              f"{m.next_dose_date}，{m.reason or ''}",
                    "due_date": str(m.next_dose_date),
                    "days_overdue": max(0, -delta),
                })
        if m.withdrawal_days > 0 and m.date <= today <= m.withdrawal_end:
            left = (m.withdrawal_end - today).days
            out.append({
                "type": "withdrawal",
                "level": "danger" if left == 0 else "warning",
                "cow_id": cow.id,
                "cow_tag": cow.ear_tag,
                "title": f"{cow_label(cow)} 休药期内·鲜奶须废弃",
                "detail": f"{m.drug_name} 休药期至 {m.withdrawal_end}"
                          f"（剩余 {left + 1} 个挤奶日），该牛牛奶不可混入大罐",
                "due_date": str(m.withdrawal_end + timedelta(days=1)),
                "days_overdue": 0,
            })

    # 4) 健康复查（result 为 NULL 的“未结案”记录也必须纳入，
    #    注意 SQL 三值逻辑：NULL != 'recovered' 结果为 NULL 会被 WHERE 过滤掉）
    for h in (
        db.query(models.HealthRecord)
        .filter(models.HealthRecord.is_void.is_(False))
        .filter(models.HealthRecord.follow_up_date.isnot(None))
        .filter(or_(
            models.HealthRecord.result.is_(None),
            models.HealthRecord.result != "recovered",
        ))
        .all()
    ):
        cow = db.get(models.Cow, h.cow_id)
        if not cow or cow.status == "sold":
            continue
        delta = (h.follow_up_date - today).days
        if -7 <= delta <= 3:
            out.append({
                "type": "health_followup",
                "level": "danger" if delta < 0 else "warning",
                "cow_id": cow.id,
                "cow_tag": cow.ear_tag,
                "title": f"{cow_label(cow)} 健康复查提醒",
                "detail": f"{h.diagnosis or h.record_type}：计划复查日 "
                          f"{h.follow_up_date}，当前状态 {_result_label(h.result)}",
                "due_date": str(h.follow_up_date),
                "days_overdue": max(0, -delta),
            })

    # 5) 待产 / 预产临近（7 天内）
    for cow in db.query(models.Cow).filter(models.Cow.expected_calving_date.isnot(None)).all():
        if cow.status == "sold":
            continue
        delta = (cow.expected_calving_date - today).days
        if -3 <= delta <= 10:
            out.append({
                "type": "calving",
                "level": "danger" if delta <= 2 else "info",
                "cow_id": cow.id,
                "cow_tag": cow.ear_tag,
                "title": f"{cow_label(cow)} 临近预产期",
                "detail": f"预产期 {cow.expected_calving_date}（{'剩' if delta >= 0 else '超'}"
                          f" {abs(delta)} 天），做好产房与接产准备",
                "due_date": str(cow.expected_calving_date),
                "days_overdue": max(0, -delta),
            })

    # 6) 产后首配窗口（产后 45~70 天未配种）
    for cow in db.query(models.Cow).filter(models.Cow.calving_date.isnot(None)).all():
        if cow.status in ("sold", "dry"):
            continue
        dim = (today - cow.calving_date).days
        if not (40 <= dim <= 80):
            continue
        insem = (
            db.query(models.EstrusRecord)
            .filter(models.EstrusRecord.cow_id == cow.id)
            .filter(models.EstrusRecord.inseminated)
            .order_by(models.EstrusRecord.insemination_date.desc())
            .first()
        )
        if insem and (insem.result == "pregnant"):
            continue
        if insem and insem.insemination_date and insem.insemination_date >= cow.calving_date:
            continue
        out.append({
            "type": "first_insemination",
            "level": "info" if dim < 55 else "warning",
            "cow_id": cow.id,
            "cow_tag": cow.ear_tag,
            "title": f"{cow_label(cow)} 进入产后首配窗口",
            "detail": f"产后已 {dim} 天，建议加强发情监测并安排首次配种",
            "due_date": str(cow.calving_date + timedelta(days=60)),
            "days_overdue": max(0, dim - 60),
        })

    level_rank = {"danger": 0, "warning": 1, "info": 2}
    out.sort(key=lambda r: (level_rank.get(r["level"], 9), r.get("days_overdue", 0) * -1))
    return out


def _det_label(code: str) -> str:
    return {"observed": "人工观察", "activity": "计步器", "detector": "尾根蜡笔"}.get(code, code)


def _result_label(code: Optional[str]) -> str:
    return {"recovered": "已康复", "ongoing": "治疗中", "observed": "观察中"}.get(code, "未结案")


# ---------- 奶量异常发现 ----------
def detect_yield_anomalies(db: Session, days: int = 7, today: Optional[date] = None) -> List[dict]:
    """
    逐牛、逐班次比较：以异常日前 8~2 天的同班次均值为基线
    - 单班产量下降超过 25%：异常
    - 连续 >=3 天该牛同方向下降：趋势异常
    - SCC > 500,000 cells/mL：乳房炎风险
    """
    today = today or date.today()
    start = today - timedelta(days=days + 9)

    records = (
        db.query(models.MilkingRecord)
        .filter(
            models.MilkingRecord.date >= start,
            models.MilkingRecord.is_void.is_(False),
        )
        .order_by(models.MilkingRecord.date.asc(), models.MilkingRecord.id.asc())
        .all()
    )
    cows = {c.id: c for c in db.query(models.Cow).all()}
    # cow -> session -> [(date, rec)]（休药期废弃奶仍计入产奶量基线，避免误报产量下降）
    grouped: Dict[int, Dict[str, List]] = defaultdict(lambda: defaultdict(list))
    for r in records:
        grouped[r.cow_id][r.session].append((r.date, r))

    # cow_id -> 聚合信息
    agg: Dict[int, dict] = {}
    window_start = today - timedelta(days=days - 1)

    for cow_id, by_sess in grouped.items():
        cow = cows.get(cow_id)
        if not cow or cow.status == "sold":
            continue
        drop_hits: List[dict] = []
        scc_hits: List[dict] = []
        for sess, series in by_sess.items():
            for d, rec in series:
                if d < window_start or d > today:
                    continue
                base_vals = [
                    v.yield_kg for pd_, v in series
                    if (d - timedelta(days=8)) <= pd_ <= (d - timedelta(days=2))
                ]
                if len(base_vals) >= 3:
                    baseline = sum(base_vals) / len(base_vals)
                    if baseline >= 5 and rec.yield_kg < baseline * 0.75:
                        drop = (baseline - rec.yield_kg) / baseline * 100
                        drop_hits.append({
                            "date": str(d), "session_label": SESSION_LABEL[sess],
                            "yield_kg": rec.yield_kg, "baseline_kg": round(baseline, 1),
                            "drop_pct": round(drop, 1),
                        })
                if rec.scc and rec.scc >= 500_000:
                    scc_hits.append({
                        "date": str(d), "session_label": SESSION_LABEL[sess],
                        "scc": rec.scc,
                    })

        # 连续下降趋势：仅用完整班次的日期，避免今日早班误判
        counts = defaultdict(int)
        daily = defaultdict(float)
        for sess, series in by_sess.items():
            for d, rec in series:
                counts[d] += 1
                daily[d] += rec.yield_kg
        full_days = sorted(d for d in daily if counts[d] >= 3 and d < today)
        streak = 0
        for i in range(len(full_days) - 1, 0, -1):
            if daily[full_days[i]] < daily[full_days[i - 1]] - 0.5:
                streak += 1
            else:
                break
        trend_hit = None
        if streak >= 3:
            first, last = full_days[-streak - 1], full_days[-1]
            pct = (daily[first] - daily[last]) / daily[first] * 100 if daily[first] else 0
            if pct >= 15:
                trend_hit = {
                    "date": str(last),
                    "streak_days": streak + 1,
                    "yield_kg": round(daily[last], 1),
                    "baseline_kg": round(daily[first], 1),
                    "drop_pct": round(pct, 1),
                }

        if not (drop_hits or scc_hits or trend_hit):
            continue

        tags, reasons = [], []
        if scc_hits:
            tags.append("high_scc")
            worst = max(scc_hits, key=lambda h: h["scc"])
            reasons.append(f"体细胞数最高 {worst['scc'] / 10000:.1f} 万/mL，疑似乳房炎")
        if trend_hit:
            tags.append("downward_trend")
            reasons.append(
                f"日产奶量连续 {trend_hit['streak_days']} 天下滑："
                f"{trend_hit['baseline_kg']}kg → {trend_hit['yield_kg']}kg"
                f"（-{trend_hit['drop_pct']:.0f}%）")
        if drop_hits:
            tags.append("yield_drop")
            worst = max(drop_hits, key=lambda h: h["drop_pct"])
            reasons.append(
                f"最近 {worst['date']} {worst['session_label']}产奶量 "
                f"{worst['yield_kg']}kg，较同班次基线 {worst['baseline_kg']}kg "
                f"下降 {worst['drop_pct']:.0f}%（共 {len(drop_hits)} 个班次异常）")

        # 风险定级：高体细胞+骤降=高风险；趋势性下滑=中风险；单次波动=关注
        if "high_scc" in tags and "yield_drop" in tags:
            level = "danger"
        elif "downward_trend" in tags:
            level = "warning"
        else:
            level = "info"

        latest_date = max(
            [h["date"] for h in drop_hits + scc_hits] +
            ([trend_hit["date"]] if trend_hit else [])
        )
        agg[cow_id] = {
            "cow_id": cow_id,
            "cow_tag": cow.ear_tag,
            "cow_name": cow.name,
            "latest_date": latest_date,
            "level": level,
            "tags": tags,
            "tag_labels": [
                {"high_scc": "体细胞异常", "downward_trend": "持续下滑",
                 "yield_drop": "单班骤降"}[t] for t in tags
            ],
            "reasons": reasons,
            "message": f"{cow_label(cow)}：" + "；".join(reasons),
            "drop_hits": sorted(drop_hits, key=lambda h: h["date"], reverse=True)[:6],
            "scc_hits": sorted(scc_hits, key=lambda h: h["date"], reverse=True)[:6],
            "trend_hit": trend_hit,
        }

    anomalies = list(agg.values())
    level_rank = {"danger": 0, "warning": 1, "info": 2}
    anomalies.sort(key=lambda a: (level_rank[a["level"]], a["latest_date"]))
    return anomalies
