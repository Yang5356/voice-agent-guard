from datetime import datetime, timedelta
from collections import Counter
import re

from sqlalchemy.orm import Session
from .models import VisitorRecord


def create_record(db: Session, data: dict) -> VisitorRecord:
    if "missing_fields" in data and isinstance(data["missing_fields"], list):
        data["missing_fields"] = ", ".join(data["missing_fields"])

    record = VisitorRecord(**data)
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def get_latest_record(db: Session):
    return db.query(VisitorRecord).order_by(VisitorRecord.id.desc()).first()


def get_recent_records(db: Session, limit: int = 20):
    return db.query(VisitorRecord).order_by(VisitorRecord.id.desc()).limit(limit).all()


def get_record_by_id(db: Session, record_id: int):
    return db.query(VisitorRecord).filter(VisitorRecord.id == record_id).first()


def update_record(db: Session, record_id: int, data: dict) -> VisitorRecord | None:
    record = get_record_by_id(db, record_id)
    if not record:
        return None

    allowed_fields = {
        "plate_number",
        "target",
        "reason",
        "phone",
        "status",
        "missing_fields",
    }

    for key, value in data.items():
        if key not in allowed_fields:
            continue

        if isinstance(value, str):
            value = value.strip()

        setattr(record, key, value)

    # 自动整理 missing_fields 和 status
    missing = []
    if not record.plate_number:
        missing.append("plate_number")
    if not record.target:
        missing.append("target")
    if not record.reason:
        missing.append("reason")
    if not record.phone:
        missing.append("phone")

    record.missing_fields = ", ".join(missing)

    # 允许保安手动设置状态；但如果字段仍然缺失，则强制回退 incomplete
    manual_status = (record.status or "").strip()
    if manual_status not in {"completed", "incomplete"}:
        manual_status = "incomplete"

    if manual_status == "completed" and missing:
        record.status = "incomplete"
    else:
        record.status = manual_status

    db.commit()
    db.refresh(record)
    return record


def _safe_parse_entry_time(value):
    if not value:
        return None

    if isinstance(value, datetime):
        return value

    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def answer_guard_query(db: Session, query: str) -> str:
    """
    最小可行版门卫查询 Agent
    当前支持：
    1. 今天来了多少车 / 本周来了多少车 / 本周多少访问车辆
    2. 某个被访对象这个月被来访了几次 / 今天被找了几次
    3. 什么时间段访问最多
    """
    q = (query or "").strip()
    if not q:
        return "请输入查询内容。"

    records = db.query(VisitorRecord).all()
    parsed_records = []

    for r in records:
        dt = _safe_parse_entry_time(r.entry_time)
        parsed_records.append(
            {
                "id": r.id,
                "entry_time": dt,
                "target": (r.target or "").strip(),
                "plate_number": (r.plate_number or "").strip(),
                "status": (r.status or "").strip(),
            }
        )

    now = datetime.now()

    # 1) 今天来了多少车
    if (
        "今天" in q
        and (
            "多少车" in q
            or "多少辆车" in q
            or "来了多少" in q
            or "多少访问车辆" in q
            or "多少访客车辆" in q
            or "多少来访车辆" in q
        )
    ):
        count = sum(
            1 for r in parsed_records
            if r["entry_time"] and r["entry_time"].date() == now.date()
        )
        return f"今天一共来了 {count} 辆车。"

    # 2) 本周来了多少车
    if (
        "本周" in q
        and (
            "多少车" in q
            or "多少辆车" in q
            or "来了多少" in q
            or "多少访问车辆" in q
            or "多少访客车辆" in q
            or "多少来访车辆" in q
        )
    ):
        week_start = now.date() - timedelta(days=now.weekday())
        count = sum(
            1 for r in parsed_records
            if r["entry_time"] and r["entry_time"].date() >= week_start
        )
        return f"本周一共来了 {count} 辆车。"

    # 3) 某个被访对象这个月被来访了几次
    m_month = re.search(r"(.+?)这个月被来访了几次", q)
    if not m_month:
        m_month = re.search(r"这个月有多少人来找(.+)", q)

    if m_month:
        name = m_month.group(1).strip()
        month_count = 0
        for r in parsed_records:
            dt = r["entry_time"]
            if not dt:
                continue
            if dt.year == now.year and dt.month == now.month and name in r["target"]:
                month_count += 1
        return f"{name} 这个月一共被来访了 {month_count} 次。"

    # 4) 某个被访对象今天被找了几次
    m_today = re.search(r"(.+?)今天被找了几次", q)
    if not m_today:
        m_today = re.search(r"今天有多少人来找(.+)", q)

    if m_today:
        name = m_today.group(1).strip()
        today_count = 0
        for r in parsed_records:
            dt = r["entry_time"]
            if not dt:
                continue
            if dt.date() == now.date() and name in r["target"]:
                today_count += 1
        return f"{name} 今天一共被找了 {today_count} 次。"

    # 5) 什么时间段访问最多
    if "什么时间段访问最多" in q or "哪个时间段访问最多" in q or "高峰时段" in q:
        hour_counter = Counter()
        for r in parsed_records:
            dt = r["entry_time"]
            if not dt:
                continue
            hour_counter[dt.hour] += 1

        if not hour_counter:
            return "目前还没有足够的来访记录，无法判断高峰时段。"

        top_hour, top_count = hour_counter.most_common(1)[0]
        return f"目前访问最多的时间段大约是 {top_hour}:00 - {top_hour}:59，共有 {top_count} 条记录。"

    return (
        "当前支持的问题包括：今天来了多少车、本周一共多少访问车辆、"
        "张云霄这个月被来访了几次、王老师今天被找了几次、什么时间段访问最多。"
    )