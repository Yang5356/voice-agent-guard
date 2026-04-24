from datetime import datetime
import json
import os
import re
import uuid
import urllib.request
import urllib.error


EXTRACTION_PROMPT = """
你是一个访客登记信息抽取器。

你的任务是：从用户输入中提取门卫登记所需信息，并返回严格 JSON。

需要提取的字段：
- plate_number：车牌号
- target：来找谁
- reason：来做什么
- phone：手机号

规则：
1. 只返回 JSON，不要输出任何解释、说明或额外文字。
2. 如果某字段无法确定，填 null。
3. missing_fields 是所有缺失字段名组成的数组，可选字段名只能是：
   ["plate_number", "target", "reason", "phone"]
4. 如果 missing_fields 为空，则 status 必须为 "completed"。
5. 如果 missing_fields 非空，则 status 必须为 "incomplete"。
6. 不要臆造信息，不确定就填 null。
7. 手机号只保留 11 位手机号本身，不要混入车牌或其他数字。
8. target 可以是个人，也可以是公司或部门名称。
9. reason 使用简洁短语，例如：送货、面试、送材料、拿快递、送午饭、拜访。
10. 如果文本中同一字段前后出现多个版本，应优先采用“最后一次明确、更正后、确认后”的值。
11. 如果文本中前面是模糊表达，后面又补充了更清楚的车牌号、手机号、来找谁或来做什么，应采用后面补充后的版本。
12. 对于车牌号，优先保留完整格式，包括省份简称、字母和数字；不要随意丢掉开头的省份简称。
13. 对于手机号，如果是中文口语数字（如“幺五七六...”），也应理解为对应的阿拉伯数字手机号。
14. 如果用户最后只是简单确认，如“对”“对的”“嗯”，不要把它当成新的字段值。
15. 输出中的 plate_number、target、reason、phone 应尽量是最终确认后的结果，而不是中间过程值。

输出格式必须严格如下：
{
  "plate_number": "...",
  "target": "...",
  "reason": "...",
  "phone": "...",
  "missing_fields": [],
  "status": "completed"
}
""".strip()


ALL_PROVINCE_PREFIXES = [
    "京", "津", "沪", "渝",
    "冀", "豫", "云", "辽", "黑", "湘",
    "皖", "鲁", "新", "苏", "浙", "赣",
    "鄂", "桂", "甘", "晋", "蒙", "陕",
    "吉", "闽", "贵", "粤", "青", "藏",
    "川", "宁", "琼",
    "港", "澳",
]

PROVINCE_CLASS = "".join(ALL_PROVINCE_PREFIXES)


def _clean_nullable_text(value):
    if value is None:
        return None

    text = str(value).strip()

    if text == "":
        return None

    lowered = text.lower()
    if lowered in {"null", "none", "unknown", "n/a"}:
        return None

    if text in {"未知", "不清楚", "不确定", "无", "没有"}:
        return None

    return text


def _normalize_plate(plate):
    if not plate:
        return None

    plate = str(plate).strip()
    plate = plate.replace(" ", "")
    plate = plate.replace("　", "")
    plate = plate.upper()
    plate = plate.strip("，。,.；;：:“”\"'()（）[]【】")

    zh_digit_map = {
        "零": "0",
        "〇": "0",
        "一": "1",
        "二": "2",
        "三": "3",
        "四": "4",
        "五": "5",
        "六": "6",
        "七": "7",
        "八": "8",
        "九": "9",
        "幺": "1",
    }

    plate = "".join(zh_digit_map.get(ch, ch) for ch in plate)

    return plate or None


def _normalize_target(target):
    if not target:
        return None

    target = str(target).strip()
    target = target.replace("这个", "")
    target = target.replace("那个", "")
    target = target.replace("呃", "")
    target = target.replace("啊", "")
    target = target.strip("，。,.；;：: ")

    return target or None


def _normalize_reason(reason):
    if not reason:
        return None

    reason = str(reason).strip()
    reason = reason.replace("呃，", "")
    reason = reason.replace("呃", "")
    reason = reason.replace("啊，", "")
    reason = reason.replace("啊", "")

    reason = reason.replace("拿一下我的午饭", "拿午饭")
    reason = reason.replace("拿一下我的东西", "拿东西")
    reason = reason.replace("拿一下快递", "拿快递")
    reason = reason.replace("送一下材料", "送材料")
    reason = reason.replace("送一下货", "送货")
    reason = reason.replace("谈一下合同的事情", "谈合同")
    reason = reason.replace("谈合同的事情", "谈合同")

    reason = reason.strip("，。,.；;：: ")

    return reason or None


def _normalize_phone(phone):
    if not phone:
        return None

    phone = str(phone)
    digits = "".join(ch for ch in phone if ch.isdigit())

    if len(digits) == 11:
        return digits

    return None


def normalize_result(result: dict, raw_text: str) -> dict:
    allowed_missing = {"plate_number", "target", "reason", "phone"}

    plate_number = _clean_nullable_text(result.get("plate_number"))
    target = _clean_nullable_text(result.get("target"))
    reason = _clean_nullable_text(result.get("reason"))
    phone = _clean_nullable_text(result.get("phone"))

    normalized = {
        "session_id": str(uuid.uuid4()),
        "plate_number": _normalize_plate(plate_number),
        "target": _normalize_target(target),
        "reason": _normalize_reason(reason),
        "phone": _normalize_phone(phone),
        "entry_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "transcript": raw_text,
        "missing_fields": [],
        "status": "incomplete",
    }

    missing_fields = result.get("missing_fields", [])
    if not isinstance(missing_fields, list):
        missing_fields = []

    normalized["missing_fields"] = [
        field for field in missing_fields if field in allowed_missing
    ]

    if not normalized["missing_fields"]:
        recalculated = []
        for field in ["plate_number", "target", "reason", "phone"]:
            if not normalized[field]:
                recalculated.append(field)
        normalized["missing_fields"] = recalculated

    if normalized["phone"] is None and "phone" not in normalized["missing_fields"]:
        normalized["missing_fields"].append("phone")

    for field in ["plate_number", "target", "reason", "phone"]:
        if not normalized[field] and field not in normalized["missing_fields"]:
            normalized["missing_fields"].append(field)

    deduped_missing = []
    for field in normalized["missing_fields"]:
        if field not in deduped_missing:
            deduped_missing.append(field)
    normalized["missing_fields"] = deduped_missing

    normalized["status"] = (
        "completed" if len(normalized["missing_fields"]) == 0 else "incomplete"
    )

    return normalized


def extract_json_from_text(text: str) -> dict:
    text = text.strip()

    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()

    return json.loads(text)


def simple_extract(text: str) -> dict:
    result = {
        "session_id": str(uuid.uuid4()),
        "plate_number": None,
        "target": None,
        "reason": None,
        "phone": None,
        "entry_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "transcript": text,
    }

    if any(p in text for p in ALL_PROVINCE_PREFIXES):
        match = re.search(
            rf"[{PROVINCE_CLASS}][A-Za-z][A-Za-z0-9一二三四五六七八九零幺〇]{{3,6}}",
            text
        )
        if match:
            result["plate_number"] = match.group(0)

    if "找" in text:
        try:
            after = text.split("找", 1)[1]
            result["target"] = after.split("，")[0].split(",")[0].strip()
        except Exception:
            pass

    reason_map = [
        ("送货", "送货"),
        ("面试", "面试"),
        ("送材料", "送材料"),
        ("拿快递", "拿快递"),
        ("午饭", "送午饭"),
        ("录取通知书", "拿录取通知书"),
        ("合同", "谈合同"),
        ("拜访", "拜访"),
    ]
    for keyword, value in reason_map:
        if keyword in text:
            result["reason"] = value
            break

    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 11:
        result["phone"] = digits[:11]

    missing = []
    for field in ["plate_number", "target", "reason", "phone"]:
        if not result[field]:
            missing.append(field)

    result["missing_fields"] = missing
    result["status"] = "completed" if not missing else "incomplete"

    return normalize_result(result, text)


def call_llm_api(text: str) -> dict:
    api_url = os.getenv("LLM_API_URL")
    api_key = os.getenv("LLM_API_KEY")
    model = os.getenv("LLM_MODEL")

    if not api_url or not api_key or not model:
        raise RuntimeError("LLM env vars are missing")

    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": EXTRACTION_PROMPT},
            {"role": "user", "content": text},
        ],
    }

    req = urllib.request.Request(
        api_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
        data = json.loads(body)

    content = data["choices"][0]["message"]["content"]
    return extract_json_from_text(content)


def llm_extract(text: str) -> dict:
    """
    优先走真实模型。
    如果环境变量没配好，或者模型返回异常，则 fallback 到 simple_extract。
    """
    try:
        raw_result = call_llm_api(text)
        return normalize_result(raw_result, text)
    except Exception as e:
        print(f"[llm_extract fallback] {e}")
        return simple_extract(text)


if __name__ == "__main__":
    sample_text = "哦不对，不是张经理，是王经理，我来送货，车牌沪A12345，电话13800000000"
    result = llm_extract(sample_text)
    print(result)