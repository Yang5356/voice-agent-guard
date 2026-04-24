import os
import json
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen, Request


def build_guard_message(record: Any) -> str:
    """
    把访客记录格式化成通知文本。
    给 Dashboard / 控制台 / Server酱共用。
    """
    plate_number = getattr(record, "plate_number", None) or "未知"
    target = getattr(record, "target", None) or "未知"
    reason = getattr(record, "reason", None) or "未知"
    phone = getattr(record, "phone", None) or "未知"
    entry_time = getattr(record, "entry_time", None) or "未知"
    status = getattr(record, "status", None) or "unknown"
    missing_fields = getattr(record, "missing_fields", None) or ""

    status_text = "已完成" if status == "completed" else "待补充"

    lines = [
        "【访客登记提醒】",
        f"车牌号：{plate_number}",
        f"来找谁：{target}",
        f"来做什么：{reason}",
        f"手机号：{phone}",
        f"入场时间：{entry_time}",
        f"状态：{status_text}",
    ]

    if missing_fields:
        lines.append(f"缺失字段：{missing_fields}")

    return "\n".join(lines)


def send_guard_notification(record: Any) -> None:
    """
    当前支持两种模式：
    1. mock 模式：默认，只打印通知，不真实发送
    2. serverchan 模式：配置好 SENDKEY 后真实推送到 Server酱 Turbo

    环境变量：
    - NOTIFY_MODE=mock 或 serverchan
    - SERVERCHAN_SENDKEY=你的 SendKey
    """
    message = build_guard_message(record)
    notify_mode = os.getenv("NOTIFY_MODE", "mock").strip().lower()

    if notify_mode == "serverchan":
        sendkey = os.getenv("SERVERCHAN_SENDKEY", "").strip()
        if not sendkey:
            print("[notification] SERVERCHAN_SENDKEY 未配置，自动回退到 mock 模式。")
            _print_mock(message)
            return

        title = "访客登记提醒"
        desp = message

        try:
            result = _send_via_serverchan(sendkey=sendkey, title=title, desp=desp)
            print("[notification] Server酱推送成功。")
            print(json.dumps(result, ensure_ascii=False))
        except Exception as e:
            print(f"[notification] Server酱推送失败：{e}")
            print("[notification] 自动回退到 mock 模式。")
            _print_mock(message)
        return

    _print_mock(message)


def _send_via_serverchan(sendkey: str, title: str, desp: str) -> dict:
    """
    调用 Server酱 Turbo 接口发送消息。
    """
    query = urlencode({"title": title, "desp": desp})
    url = f"https://sctapi.ftqq.com/{sendkey}.send?{query}"

    req = Request(
        url,
        method="GET",
        headers={
            "User-Agent": "voice-agent-guard/1.0",
        },
    )

    with urlopen(req, timeout=10) as resp:
        body = resp.read().decode("utf-8", errors="ignore")

    try:
        data = json.loads(body)
    except Exception:
        raise RuntimeError(f"Server酱返回非 JSON 内容: {body}")

    code = data.get("code")
    if code != 0:
        raise RuntimeError(f"Server酱返回失败: {data}")

    return data


def _print_mock(message: str) -> None:
    print("\n========== Guard Notification ==========")
    print(message)
    print("========================================\n")