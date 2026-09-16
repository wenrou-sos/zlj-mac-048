"""奶量 CSV 导入：解析、预览校验、事务化入账

设计要点：
- file_hash（内容 SHA-256）作为幂等键：同一文件重复上传只会有一个批次；
- 批次留存原始 CSV 文本，commit 时用同一解析器重放并以最新档案/用药数据
  重新校验，保证预览与入账口径一致；
- 入账为单事务，任何异常整体回滚，不留半批数据；
- commit 可安全重试（断线重传）：已入账批次直接返回原结果；
- 休药期判定复用 services.check_withdrawal，与手工录入同一套规则。
"""
import csv
import hashlib
import io
import re
from datetime import date, datetime
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from . import models, services

MAX_ROWS = 10000  # 单次导入数据行上限，防止误传超大文件

SESSION_ALIASES = {
    "早": "morning", "早班": "morning", "上午": "morning", "morning": "morning",
    "m": "morning", "1": "morning",
    "午": "noon", "午班": "noon", "中午": "noon", "noon": "noon",
    "n": "noon", "2": "noon",
    "晚": "evening", "晚班": "evening", "晚上": "evening", "evening": "evening",
    "e": "evening", "3": "evening",
}

# 表头别名 -> 内部字段名（规范化后匹配）
HEADER_ALIASES = {
    "ear_tag": {"耳标", "耳标号", "耳号", "牛号", "牛只编号", "编号",
                "ear_tag", "eartag", "ear", "tag", "cow", "cow_id", "cowno"},
    "date": {"日期", "挤奶日期", "记录日期", "date", "milk_date", "milkingdate"},
    "session": {"班次", "班次名称", "挤奶班次", "session", "shift"},
    "yield_kg": {"产量", "产奶量", "奶量", "产量kg", "产奶量kg", "奶量kg",
                 "yield", "yield_kg", "milk", "milk_yield", "kg"},
    "scc": {"体细胞", "体细胞数", "体细胞数cells/ml", "scc"},
    "note": {"备注", "说明", "note", "remark", "comment"},
}

SESSION_LABEL = services.SESSION_LABEL
ROW_STATUS_LABEL = {
    "ok": "可入账",
    "unknown_tag": "未知耳标",
    "duplicate": "文件内重复",
    "conflict": "与已有记录冲突",
    "invalid": "格式错误",
}


class CsvFormatError(ValueError):
    """CSV 整体无法解析（编码/结构问题）"""


def file_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def decode(content: bytes) -> str:
    for enc in ("utf-8-sig", "gbk"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    raise CsvFormatError("无法识别文件编码（仅支持 UTF-8 / GBK）")


def _norm_header(cell: str) -> str:
    return re.sub(r"[\s_\-（）()【】\[\]:：/]+", "", (cell or "")).lower()


def _header_map(header: List[str]) -> Optional[dict]:
    """把表头行映射为 {字段: 列下标}；缺少必需列时返回 None"""
    alias_lookup = {}
    for field, aliases in HEADER_ALIASES.items():
        for a in aliases:
            alias_lookup[_norm_header(a)] = field
    mapping = {}
    for idx, cell in enumerate(header):
        field = alias_lookup.get(_norm_header(cell))
        if field and field not in mapping:
            mapping[field] = idx
    required = {"ear_tag", "date", "session", "yield_kg"}
    if required <= set(mapping):
        return mapping
    return None


def _parse_date(raw: str) -> Optional[date]:
    s = (raw or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?$", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    elif re.match(r"^\d{8}$", s):
        y, mo, d = int(s[:4]), int(s[4:6]), int(s[6:8])
    else:
        return None
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _parse_session(raw: str) -> Optional[str]:
    return SESSION_ALIASES.get((raw or "").strip().lower())


def _parse_yield(raw: str) -> Optional[float]:
    s = (raw or "").strip()
    s = re.sub(r"(?i)kg|公斤", "", s).strip()
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if 0 <= v <= 200 else None


def _parse_scc(raw: str) -> Tuple[Optional[int], bool]:
    """返回 (值, 是否有效)。空为 (None, True)——SCC 选填"""
    s = (raw or "").strip()
    if not s:
        return None, True
    try:
        v = float(s)
    except ValueError:
        return None, False
    if v < 0 or v > 100_000_000:
        return None, False
    return int(v), True


def parse_text(text: str) -> List[dict]:
    """把 CSV 文本解析为 [{row_no, raw, ear_tag, date, session, yield_kg, scc, note, error}]

    支持：逗号/分号/制表符分隔；有无表头（无表头按
    耳标,日期,班次,产量,体细胞[,备注] 固定列序）。单行解析失败不抛错，
    在行内以 error 标记，交给预览展示。
    """
    if not text.strip():
        raise CsvFormatError("文件为空")
    try:
        dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    raw_rows = [row for row in reader if any((c or "").strip() for c in row)]
    if not raw_rows:
        raise CsvFormatError("文件中没有数据行")

    mapping = _header_map(raw_rows[0])
    if mapping is not None:
        data_rows, start_no = raw_rows[1:], 2
    else:
        # 无表头：按固定列序
        mapping = {"ear_tag": 0, "date": 1, "session": 2, "yield_kg": 3,
                   "scc": 4, "note": 5}
        data_rows, start_no = raw_rows, 1
    if len(data_rows) > MAX_ROWS:
        raise CsvFormatError(f"数据行超过上限 {MAX_ROWS} 行，请拆分文件")

    out = []
    for i, row in enumerate(data_rows):
        rec = {
            "row_no": start_no + i, "raw": ",".join(row),
            "ear_tag": None, "date": None, "session": None,
            "yield_kg": None, "scc": None, "note": None, "error": None,
        }

        def cell(field):
            idx = mapping.get(field)
            return (row[idx] or "").strip() if idx is not None and idx < len(row) else ""

        rec["ear_tag"] = cell("ear_tag") or None
        rec["note"] = cell("note") or None
        if not rec["ear_tag"]:
            rec["error"] = "缺少耳标号"
        else:
            d = _parse_date(cell("date"))
            sess = _parse_session(cell("session"))
            y = _parse_yield(cell("yield_kg"))
            scc, scc_ok = _parse_scc(cell("scc"))
            if d is None:
                rec["error"] = f"日期无法识别：{cell('date')!r}（支持 2026-09-16 / 2026/9/16 / 20260916）"
            elif sess is None:
                rec["error"] = f"班次无法识别：{cell('session')!r}（支持 早/午/晚 或 morning/noon/evening）"
            elif y is None:
                rec["error"] = f"产量无效：{cell('yield_kg')!r}（需为 0~200 的数字）"
            elif not scc_ok:
                rec["error"] = f"体细胞数无效：{cell('scc')!r}"
            else:
                rec.update(date=d, session=sess, yield_kg=y, scc=scc)
        out.append(rec)
    return out


def parse_csv(content: bytes) -> List[dict]:
    return parse_text(decode(content))


def validate_rows(db: Session, parsed: List[dict]) -> List[dict]:
    """对照档案与已有挤奶记录校验每一行，附加 status/message/休药期标记"""
    tags = {p["ear_tag"] for p in parsed if p.get("ear_tag")}
    cows = {c.ear_tag: c for c in
            db.query(models.Cow).filter(models.Cow.ear_tag.in_(tags)).all()} if tags else {}

    # 已存在的 (cow_id, date, session) 组合，用于冲突检测
    keys = {(cows[p["ear_tag"]].id, p["date"], p["session"]) for p in parsed
            if p.get("date") and p.get("session") and p.get("ear_tag") in cows}
    existing = set()
    if keys:
        q = (db.query(models.MilkingRecord.cow_id, models.MilkingRecord.date,
                      models.MilkingRecord.session)
             .filter(models.MilkingRecord.cow_id.in_({k[0] for k in keys}),
                     models.MilkingRecord.date.in_({k[1] for k in keys})))
        existing = {(cid, d, s) for cid, d, s in q.all()}

    seen_in_file = set()
    for p in parsed:
        p.pop("cow_id", None)
        p.pop("in_withdrawal", None)
        if p.get("error"):
            p["status"] = "invalid"
            p["message"] = p["error"]
            continue
        cow = cows.get(p["ear_tag"])
        if not cow:
            p["status"] = "unknown_tag"
            p["message"] = f"耳标 {p['ear_tag']} 不在牛群档案中，该行将跳过"
            continue
        p["cow_id"] = cow.id
        p["cow_name"] = cow.name
        if cow.status != "lactating":
            p["status"] = "invalid"
            p["message"] = (f"该牛当前状态为「{services.STATUS_LABEL.get(cow.status, cow.status)}」，"
                            f"不能登记挤奶，该行将跳过")
            continue
        key = (cow.id, p["date"], p["session"])
        if key in seen_in_file:
            p["status"] = "duplicate"
            p["message"] = "与本文件前面某行重复（同牛同日同班次），该行将跳过"
            continue
        seen_in_file.add(key)
        if key in existing:
            p["status"] = "conflict"
            p["message"] = "该牛当日该班次已有挤奶记录，该行将跳过（不覆盖已有数据）"
            continue
        chk = services.check_withdrawal(db, cow.id, p["date"])
        if chk["in_withdrawal"]:
            p["in_withdrawal"] = True
            p["withdrawal_end"] = str(chk["withdrawal_end"])
            p["drug_name"] = chk["drug_name"]
        p["status"] = "ok"
        p["message"] = (
            f"休药期内（{chk['drug_name']}，至 {chk['withdrawal_end']}），"
            f"入账时将自动标记为废弃奶"
        ) if chk["in_withdrawal"] else None
    return parsed


def summarize(parsed: List[dict]) -> dict:
    s = {"total": len(parsed), "ok": 0, "unknown_tag": 0, "duplicate": 0,
         "conflict": 0, "invalid": 0, "auto_discard": 0}
    for p in parsed:
        s[p["status"]] = s.get(p["status"], 0) + 1
        if p["status"] == "ok" and p.get("in_withdrawal"):
            s["auto_discard"] += 1
    return s


def rebuild_preview(db: Session, batch: models.ImportBatch, text: str):
    """（重新）解析并校验文件内容，刷新批次行与统计（preview 阶段可反复执行）"""
    parsed = validate_rows(db, parse_text(text))
    batch.rows.clear()
    db.flush()
    for p in parsed:
        batch.rows.append(models.ImportRow(
            row_no=p["row_no"], raw=p["raw"], ear_tag=p.get("ear_tag"),
            date=p.get("date"), session=p.get("session"),
            yield_kg=p.get("yield_kg"), scc=p.get("scc"),
            status=p["status"], message=p.get("message"),
        ))
    s = summarize(parsed)
    batch.total_rows = s["total"]
    batch.ok_rows = s["ok"]
    batch.unknown_tag_rows = s["unknown_tag"]
    batch.duplicate_rows = s["duplicate"]
    batch.conflict_rows = s["conflict"]
    batch.invalid_rows = s["invalid"]
    batch.auto_discard_rows = s["auto_discard"]
    batch.error = None
    db.flush()


def commit_batch(db: Session, batch: models.ImportBatch) -> dict:
    """事务化入账。可安全重试：已入账批次直接返回原结果。

    以最新数据重新校验每一行（preview 之后可能有人手工录入或登记用药），
    有效行全部插入后一次 commit；任何异常整体回滚，不留半批数据。
    """
    if batch.status == "committed":
        return commit_result(batch, reused=True)

    # 同一文件已有其他批次入账 -> 本批次不再重复记奶
    other = (db.query(models.ImportBatch)
             .filter(models.ImportBatch.file_hash == batch.file_hash,
                     models.ImportBatch.status == "committed",
                     models.ImportBatch.id != batch.id)
             .first())
    if other:
        batch.error = f"同一文件已由批次 #{other.id} 入账，本批次不再重复执行"
        db.commit()
        return commit_result(other, reused=True, superseded_by=other.id)

    try:
        # 用最新档案/记录/用药数据重放行校验（原始文本留存在批次上）
        parsed = validate_rows(db, parse_text(batch.content))
        by_row_no = {}
        inserted = 0
        for p in parsed:
            by_row_no[p["row_no"]] = p
            if p["status"] != "ok":
                continue
            discarded = bool(p.get("in_withdrawal"))
            note = p.get("note")
            if discarded:
                note = (note + "；" if note else "") + \
                    f"休药期自动废弃（{p['drug_name']}，至 {p['withdrawal_end']}）"
            rec = models.MilkingRecord(
                cow_id=p["cow_id"], date=p["date"], session=p["session"],
                yield_kg=p["yield_kg"], scc=p.get("scc"),
                discarded=discarded, note=note, import_batch_id=batch.id,
            )
            db.add(rec)
            db.flush()  # 触发唯一索引等约束检查；失败即抛错回滚
            p["record_id"] = rec.id
            inserted += 1

        # 回写每一行的最终状态与入账记录关联（事后可追溯）
        for row in batch.rows:
            p = by_row_no.get(row.row_no)
            if not p:
                continue
            row.status = p["status"]
            row.message = p.get("message")
            row.milking_record_id = p.get("record_id")

        s = summarize(parsed)
        batch.total_rows = s["total"]
        batch.ok_rows = s["ok"]
        batch.unknown_tag_rows = s["unknown_tag"]
        batch.duplicate_rows = s["duplicate"]
        batch.conflict_rows = s["conflict"]
        batch.invalid_rows = s["invalid"]
        batch.auto_discard_rows = s["auto_discard"]
        batch.inserted_rows = inserted
        batch.status = "committed"
        batch.committed_at = datetime.utcnow()
        batch.error = None
        db.commit()
    except Exception as exc:
        db.rollback()  # 不留半批数据
        batch.error = f"入账失败，已整体回滚：{exc}"
        db.commit()
        raise
    return commit_result(batch, reused=False)


def commit_result(batch: models.ImportBatch, reused: bool,
                  superseded_by: Optional[int] = None) -> dict:
    skipped = (batch.total_rows - batch.inserted_rows) if batch.status == "committed" \
        else (batch.total_rows - batch.ok_rows)
    return {
        "batch_id": batch.id,
        "status": batch.status,
        "reused": reused,
        "superseded_by": superseded_by,
        "inserted": batch.inserted_rows,
        "skipped": skipped,
        "auto_discarded": batch.auto_discard_rows,
        "committed_at": str(batch.committed_at) if batch.committed_at else None,
        "message": batch.error,
    }
