from pathlib import Path
from typing import Dict
from datetime import datetime, timezone
import json
import threading

from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .db import Base, engine, get_db, SessionLocal
from .schemas import TextInputRequest
from .extraction_service import llm_extract
from .storage_service import (
    create_record,
    get_latest_record,
    get_recent_records,
    answer_guard_query,
    update_record,
)
from .notification_service import send_guard_notification


# =========================
# 基础初始化
# =========================
Path("data").mkdir(exist_ok=True)
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Voice Agent Guard Dashboard")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

CALLBACK_TOKEN = "guard-demo-token"
CALL_TIMEOUT_SECONDS = 60


# =========================
# 回调聚合缓存（版本2 + 60s兜底）
# =========================
CALL_CACHE: Dict[str, Dict] = {}
CALL_CACHE_LOCK = threading.Lock()

SHORT_ACKS = {
    "对",
    "对。",
    "对的",
    "对的。",
    "嗯",
    "嗯。",
    "好的",
    "好的。",
    "好",
    "好。",
    "是的",
    "是的。",
    "没错",
    "没错。",
}

ENDING_PHRASES = [
    "已经帮您登记好了",
    "请稍等门卫确认放行",
    "先通知门卫协助处理",
    "您稍等一下",
]


# =========================
# 工具函数
# =========================
def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_iso_ts(ts: str) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts).timestamp()
    except Exception:
        return None


def _utc_now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _is_short_ack(text: str) -> bool:
    cleaned = (text or "").strip()
    return cleaned in SHORT_ACKS


def _is_agent_ending(text: str) -> bool:
    content = (text or "").strip()
    return any(phrase in content for phrase in ENDING_PHRASES)


def _get_or_create_cache(instance_id: str, callback_ts: str | None = None) -> Dict:
    with CALL_CACHE_LOCK:
        if instance_id not in CALL_CACHE:
            started_at_ts = _parse_iso_ts(callback_ts) or _utc_now_ts()
            CALL_CACHE[instance_id] = {
                "instance_id": instance_id,
                "user_texts": [],
                "created_at": _now_str(),
                "started_at_ts": started_at_ts,
                "last_callback_ts": started_at_ts,
                "finalized": False,
                "timeout_triggered": False,
            }
        else:
            parsed_ts = _parse_iso_ts(callback_ts)
            if parsed_ts:
                CALL_CACHE[instance_id]["last_callback_ts"] = parsed_ts

        return CALL_CACHE[instance_id]


def _append_user_text(instance_id: str, text: str) -> None:
    with CALL_CACHE_LOCK:
        cache = CALL_CACHE.setdefault(
            instance_id,
            {
                "instance_id": instance_id,
                "user_texts": [],
                "created_at": _now_str(),
                "started_at_ts": _utc_now_ts(),
                "last_callback_ts": _utc_now_ts(),
                "finalized": False,
                "timeout_triggered": False,
            },
        )
        cache["user_texts"].append(text)


def _get_full_user_text(instance_id: str) -> str:
    with CALL_CACHE_LOCK:
        cache = CALL_CACHE.get(instance_id)
        if not cache:
            return ""
        return "\n".join(cache.get("user_texts", []))


def _mark_finalized(instance_id: str) -> bool:
    with CALL_CACHE_LOCK:
        cache = CALL_CACHE.get(instance_id)
        if not cache:
            return False
        if cache.get("finalized"):
            return False
        cache["finalized"] = True
        return True


def _cleanup_cache(instance_id: str) -> None:
    with CALL_CACHE_LOCK:
        CALL_CACHE.pop(instance_id, None)


def _get_elapsed_seconds(instance_id: str, current_callback_ts: str | None = None) -> float:
    with CALL_CACHE_LOCK:
        cache = CALL_CACHE.get(instance_id)
        if not cache:
            return 0.0

        started_at_ts = cache.get("started_at_ts") or _utc_now_ts()
        current_ts = _parse_iso_ts(current_callback_ts)

        if current_ts is None:
            current_ts = cache.get("last_callback_ts") or _utc_now_ts()

        return max(0.0, current_ts - started_at_ts)


def _mark_timeout_triggered(instance_id: str) -> None:
    with CALL_CACHE_LOCK:
        cache = CALL_CACHE.get(instance_id)
        if cache:
            cache["timeout_triggered"] = True


def save_text_to_pipeline(user_text: str, force_incomplete: bool = False) -> Dict:
    db = SessionLocal()
    try:
        extracted = llm_extract(user_text)
        if not isinstance(extracted, dict):
            raise ValueError("llm_extract must return dict")

        extracted.setdefault("transcript", user_text)

        if force_incomplete:
            existing_missing = extracted.get("missing_fields", [])
            if not isinstance(existing_missing, list):
                existing_missing = []

            for field in ["plate_number", "target", "reason", "phone"]:
                value = extracted.get(field)
                if not value and field not in existing_missing:
                    existing_missing.append(field)

            extracted["missing_fields"] = existing_missing
            extracted["status"] = "incomplete"

        record = create_record(db, extracted)
        send_guard_notification(record)

        return {
            "record_id": record.id,
            "status": record.status,
        }
    finally:
        db.close()


def _finalize_call(
    instance_id: str,
    finalize_reason: str,
    force_incomplete: bool = False,
) -> JSONResponse:
    if not _mark_finalized(instance_id):
        print(f"[CALLBACK] instance already finalized: {instance_id}")
        return JSONResponse(
            {
                "ok": True,
                "message": "instance already finalized",
                "instance_id": instance_id,
            }
        )

    full_user_text = _get_full_user_text(instance_id).strip()
    print(f"[CALLBACK] finalize_reason={finalize_reason}, instance={instance_id}")
    print(f"[CALLBACK] merged user text:\n{full_user_text}")

    if not full_user_text:
        print("[CALLBACK] no user text found, skip pipeline")
        _cleanup_cache(instance_id)
        return JSONResponse(
            {
                "ok": True,
                "message": "no user text to process",
                "instance_id": instance_id,
            }
        )

    try:
        result = save_text_to_pipeline(
            full_user_text,
            force_incomplete=force_incomplete,
        )
    except Exception as e:
        print(f"[PIPELINE ERROR] {e}")
        raise HTTPException(status_code=500, detail=f"Pipeline failed: {e}")

    _cleanup_cache(instance_id)

    return JSONResponse(
        {
            "ok": True,
            "message": "call finalized and processed",
            "instance_id": instance_id,
            "record_id": result["record_id"],
            "status": result["status"],
            "user_text": full_user_text,
            "finalize_reason": finalize_reason,
            "force_incomplete": force_incomplete,
        }
    )


# =========================
# 页面路由
# =========================
@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    latest = get_latest_record(db)
    records = get_recent_records(db, limit=50)
    stats = {
        "today_total": len(records),
        "completed_count": len([r for r in records if r.status == "completed"]),
        "incomplete_count": len([r for r in records if r.status == "incomplete"]),
    }
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "latest": latest,
            "records": records,
            "stats": stats,
        },
    )


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    latest = get_latest_record(db)
    records = get_recent_records(db, limit=50)
    stats = {
        "today_total": len(records),
        "completed_count": len([r for r in records if r.status == "completed"]),
        "incomplete_count": len([r for r in records if r.status == "incomplete"]),
    }
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "latest": latest,
            "records": records,
            "stats": stats,
        },
    )


@app.get("/records")
def records(db: Session = Depends(get_db)):
    result = get_recent_records(db, limit=50)
    return result


@app.post("/submit_text")
def submit_text(payload: TextInputRequest, db: Session = Depends(get_db)):
    extracted = llm_extract(payload.text)
    record = create_record(db, extracted)
    send_guard_notification(record)
    return {
        "message": "record created",
        "record_id": record.id,
        "status": record.status,
    }


@app.post("/guard_query")
def guard_query(payload: TextInputRequest, db: Session = Depends(get_db)):
    answer = answer_guard_query(db, payload.text)
    return {
        "message": "ok",
        "answer": answer,
    }


@app.post("/records/{record_id}/update")
def update_record_api(record_id: int, payload: Request, db: Session = Depends(get_db)):
    raise HTTPException(status_code=500, detail="Please use /records/update_json")


@app.post("/records/update_json")
async def update_record_json(request: Request, db: Session = Depends(get_db)):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    record_id = payload.get("id")
    if not record_id:
        raise HTTPException(status_code=400, detail="Missing record id")

    update_data = {
        "plate_number": payload.get("plate_number"),
        "target": payload.get("target"),
        "reason": payload.get("reason"),
        "phone": payload.get("phone"),
        "status": payload.get("status"),
    }

    record = update_record(db, int(record_id), update_data)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")

    return {
        "message": "updated",
        "record_id": record.id,
        "status": record.status,
    }


# =========================
# 阿里云回调（版本2 + 60s兜底）
# =========================
@app.post("/aliyun/callback")
async def aliyun_callback(request: Request):
    auth = (request.headers.get("Authorization") or "").strip()

    accepted_tokens = {
        CALLBACK_TOKEN,
        f"Bearer {CALLBACK_TOKEN}",
    }

    if auth not in accepted_tokens:
        print(f"[AUTH ERROR] expected one of={accepted_tokens}, got={auth}")
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        payload = await request.json()
    except Exception as e:
        print(f"[JSON ERROR] {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    print("\n========== ALIYUN CALLBACK ==========")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print("=====================================\n")

    event = payload.get("event")
    instance_id = payload.get("instanceId", "")
    code = payload.get("code")
    message = payload.get("message")
    timestamp = payload.get("timestamp")
    data = payload.get("data", {}) or {}

    print(
        f"[CALLBACK] event={event}, instance_id={instance_id}, "
        f"code={code}, message={message}, timestamp={timestamp}"
    )

    if event != "chat_record":
        print("[CALLBACK] ignore non-chat_record event")
        return JSONResponse(
            {
                "ok": True,
                "message": "ignored non-chat_record",
                "event": event,
            }
        )

    print("\n---------- CHAT_RECORD DATA ----------")
    print(json.dumps(data, ensure_ascii=False, indent=2))
    print("--------------------------------------\n")

    role = (data.get("role") or "").strip()
    text = (data.get("text") or "").strip()
    sentence_id = data.get("sentence_id")
    interrupted = data.get("interrupted")

    if not instance_id:
        print("[CALLBACK] missing instanceId")
        return JSONResponse(
            {
                "ok": False,
                "message": "missing instanceId",
            },
            status_code=400,
        )

    _get_or_create_cache(instance_id, callback_ts=timestamp)

    elapsed_seconds = _get_elapsed_seconds(instance_id, current_callback_ts=timestamp)
    if elapsed_seconds >= CALL_TIMEOUT_SECONDS:
        print(
            f"[CALLBACK] timeout reached: instance={instance_id}, "
            f"elapsed_seconds={elapsed_seconds:.2f}"
        )
        _mark_timeout_triggered(instance_id)
        return _finalize_call(
            instance_id=instance_id,
            finalize_reason="timeout_60s",
            force_incomplete=True,
        )

    if role == "user":
        if not text:
            print("[CHAT_RECORD] empty user text, ignored")
            return JSONResponse(
                {
                    "ok": True,
                    "message": "empty user text ignored",
                    "instance_id": instance_id,
                }
            )

        if _is_short_ack(text):
            print(f"[CHAT_RECORD] short ack ignored: {text}")
            return JSONResponse(
                {
                    "ok": True,
                    "message": "short ack ignored",
                    "instance_id": instance_id,
                    "user_text": text,
                }
            )

        _append_user_text(instance_id, text)

        full_user_text = _get_full_user_text(instance_id)
        print(f"[CHAT_RECORD] cached user text: {text}")
        print(f"[CHAT_RECORD] current merged text:\n{full_user_text}")

        return JSONResponse(
            {
                "ok": True,
                "message": "user text cached",
                "instance_id": instance_id,
                "sentence_id": sentence_id,
                "user_text": text,
            }
        )

    if role == "agent":
        print(f"[CHAT_RECORD] agent text received: {text} | interrupted={interrupted}")

        if interrupted == 1:
            return JSONResponse(
                {
                    "ok": True,
                    "message": "interrupted agent text ignored",
                    "instance_id": instance_id,
                }
            )

        if not _is_agent_ending(text):
            return JSONResponse(
                {
                    "ok": True,
                    "message": "agent text ignored",
                    "instance_id": instance_id,
                }
            )

        return _finalize_call(
            instance_id=instance_id,
            finalize_reason="agent_ending_phrase",
            force_incomplete=False,
        )

    print(f"[CALLBACK] ignored role={role}")
    return JSONResponse(
        {
            "ok": True,
            "message": "ignored unknown role",
            "instance_id": instance_id,
            "role": role,
        }
    )