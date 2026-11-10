"""Bounded, complete conversational evidence for delayed messages."""

from __future__ import annotations

import hashlib
import json
import re

from .memory.text import estimate, text_content
from .store import AlbumError, now


def plain_text(value) -> str:
    return re.sub(
        r"(?:https?://|data:|base64://)\S+", "[媒体链接省略]", text_content(value)
    )


def recent_text(history, latest=None, *, since=0, budget=3200):
    """Keep entire utterances; never turn a clipped negation into evidence."""
    rows = []
    for item in reversed(history):
        if not isinstance(item, dict) or item.get("_no_save"):
            continue
        if item.get("role") not in ("user", "assistant") or item.get("tool_calls"):
            continue
        body = plain_text(item.get("content"))
        if body:
            rows.append({"role": item["role"], "text": body})
        if len(rows) >= 24:
            break
    rows.reverse()
    latest = latest or {}
    saved = latest.get("checkpoint") and any(
        isinstance(x, dict)
        and x.get("role") == "_checkpoint"
        and isinstance(x.get("content"), dict)
        and x["content"].get("id") == latest["checkpoint"]
        for x in history
    )
    if not saved and latest.get("at", 0) >= since and latest.get("text"):
        row = {"role": "user", "text": plain_text(latest["text"])}
        if not rows or rows[-1] != row:
            rows.append(row)
    result, cost = [], 0
    for row in reversed(rows):
        size = estimate(row)
        if cost + size > budget:
            if not result:
                raise AlbumError("最新完整发言超过接话预算，本次不生成过期接话")
            break
        result.append(row)
        cost += size
    return list(reversed(result))


def watermark(history, latest=None):
    # Hash only text: media bytes and tool payloads never enter the request/cache.
    rows = recent_text(history, latest, budget=12000)
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()


_RESERVED = re.compile(r"<\s*/?\s*(think(?:ing)?|schedule_decision|plan_decision|photo_scene|follow_up)\b", re.I)
_THOUGHT = re.compile(r"<\s*(/?)\s*(think(?:ing)?)\b([^>]*)>", re.I)

PLAN_LIMITS = {"sources": 3, "reason": 240, "photo": 1000, "name": 80,
               "instruction": 2000, "delay_min": 5, "delay_max": 720, "window_max": 30,
               "body": 800, "response": 2400}
PLAN_OUTPUT_PROTOCOL = (
    "最终输出仅含：开头一次完整<schedule_decision>JSON对象</schedule_decision>，"
    "公开聊天正文，末尾可选的<photo_scene>具体画面</photo_scene>和<follow_up>JSON对象</follow_up>各最多一次。"
    "不嵌套、不重复，不在标签后追加正文，不输出思考或工具调用；JSON不能有重复字段。"
    "决策对象只允许contact、progress、source_ids、reason、effective_ts：contact必填字符串，"
    "只能keep/continue/skip/delay/replace；progress为字符串，只能unknown/planned/ongoing/completed/cancelled，省略为unknown。"
    f"source_ids只能为来源ID字符串数组，最多{PLAN_LIMITS['sources']}项，省略为[]，引用只能来自本次有效来源。"
    "progress非unknown或contact为continue/delay/replace时必须提供至少一个有效来源；不要编造ID或把已知事实改为unknown规避依据。"
    "delay必须有整数effective_ts（Unix秒，不接受布尔/字符串），至少3分钟后且本日内，同一次最多自主延后一次；"
    "其他contact不使用effective_ts。明确提醒不得自主skip/delay/replace；用户明确调整优先。"
    "skip/delay不写台词或照片、心念。"
    f"reason可省略，提供时为最多{PLAN_LIMITS['reason']}字符的字符串。"
    f"不拍照时省略photo_scene，提供时必须非空且最多{PLAN_LIMITS['photo']}字符。"
    "follow_up仅在本次允许后续心念且确有需要时提供；对象只允许delay_minutes、window_minutes、name、instruction、with_photo。"
    f"delay_minutes必填整数{PLAN_LIMITS['delay_min']}—{PLAN_LIMITS['delay_max']}；"
    f"window_minutes为整数0—{PLAN_LIMITS['window_max']}，省略为5；两者不接受布尔/字符串/小数。"
    f"name可省略，提供时为最多{PLAN_LIMITS['name']}字符的字符串；"
    f"instruction必填非空字符串、最多{PLAN_LIMITS['instruction']}字符；with_photo为布尔值，省略为false。"
    "公开正文篇幅和口吻遵循人设，内部控制标签不计入人设篇幅目标；允许自然空行。"
    f"正文处理上限{PLAN_LIMITS['body']}字符，包含控制标签的原始响应保护上限{PLAN_LIMITS['response']}字符，均不是建议写满的目标。"
)
PROTOCOL_CODES = {"PROTOCOL_" + name for name in (
    "THOUGHT", "STRUCTURE", "JSON", "FIELDS", "DECISION_TYPE", "DECISION_FIELDS", "DECISION_CONTACT", "DECISION_PROGRESS",
    "SOURCE_TYPE", "SOURCE_COUNT", "SOURCE_UNKNOWN", "SOURCE_REQUIRED", "REMINDER_CONTACT", "EFFECTIVE_TS",
    "REASON_TYPE", "REASON_LENGTH", "PHOTO_EMPTY", "PHOTO_LENGTH", "FOLLOW_TYPE", "FOLLOW_FIELDS",
    "FOLLOW_DELAY_TYPE", "FOLLOW_DELAY_RANGE", "FOLLOW_WINDOW_TYPE", "FOLLOW_WINDOW_RANGE",
    "FOLLOW_NAME_TYPE", "FOLLOW_NAME_LENGTH", "FOLLOW_INSTRUCTION_TYPE", "FOLLOW_INSTRUCTION_EMPTY", "FOLLOW_INSTRUCTION_LENGTH",
    "FOLLOW_PHOTO_TYPE",
)}
RECOVERY_CODES = {
    "GENERATION_BUDGET", "GENERATION_DEADLINE", "GENERATION_CANCELLED", "GENERATION_STALE",
    "MODEL_AUTH", "MODEL_CONFIG", "MODEL_RATE_LIMIT", "MODEL_SERVER", "ASSISTANT_UNAVAILABLE", "ASSISTANT_CHANGED",
    "ASSISTANT_RETRY", "ASSISTANT_UNREPAIRABLE", "ASSISTANT_RESPONSE", "ASSISTANT_UNSAFE_EDIT", "RESPONSE_LIMIT", "CHAT_ACTIVE",
}
SAFE_CODES = PROTOCOL_CODES | RECOVERY_CODES | {
    "TIMEOUT", "CONNECTION", "STORAGE", "VALIDATION", "INTERNAL", "TIMER_ONLY",
    "PHOTO_DISABLED", "PHOTO_NO_SCENE", "PHOTO_ENGINE_UNAVAILABLE", "PHOTO_SUBMIT_FAILED", "PHOTO_TASK_CREATED",
}
SAFE_STAGES = {"generation", "validation", "sending", "history", "photo", "follow_up", "recovery", "assistant"}
SAFE_STATUSES = {"ok", "failed", "held", "pending", "skipped", "unknown", "fallback", "retrying", "recovered", "exhausted", "cancelled"}
SAFE_COUNTERS = {"attempt": 6, "calls": 6, "elapsed_ms": 1_200_000, "retry_after": 1200,
                 "http_status": 599, "body_chars": 16000, "source_count": 3}


def safe_counts(values):
    return {key: value for key, maximum in SAFE_COUNTERS.items()
            if type(value := values.get(key)) is int and 0 <= value <= maximum}


def protocol_error(code):
    error = AlbumError("本次安排输出不合规，未发送消息")
    error.code = "PROTOCOL_" + code
    return error


def safe_failure(exc):
    """Classify locally; never persist an exception's possibly private text."""
    code = getattr(exc, "code", "")
    if code in SAFE_CODES:
        return code
    if isinstance(exc, TimeoutError):
        return "TIMEOUT"
    if isinstance(exc, ConnectionError) or type(exc).__name__ in {"ClientConnectionError", "ConnectError", "ReadError"}:
        return "CONNECTION"
    if isinstance(exc, OSError):
        return "STORAGE"
    if isinstance(exc, AlbumError):
        return "VALIDATION"
    return "INTERNAL"


def safe_stage(stage, status, exc=None, code=None, **counts):
    if stage not in SAFE_STAGES:
        raise ValueError("Invalid local diagnostic stage")
    if status not in SAFE_STATUSES:
        raise ValueError("Invalid local diagnostic status")
    code = safe_failure(exc) if exc is not None else code
    return {"stage": stage, "status": status, "at": now(), **safe_counts(counts),
            **({"code": code if code in SAFE_CODES else "INTERNAL"} if code else {})}


def _without_thoughts(raw):
    if not isinstance(raw, str) or len(raw) > 16000:
        raise protocol_error("STRUCTURE")
    chunks, cursor, opened = [], 0, None
    for token in _THOUGHT.finditer(raw):
        if token.group(3).strip():
            raise protocol_error("THOUGHT")
        if not token.group(1):
            if opened is not None:
                raise protocol_error("THOUGHT")
            chunks.append(raw[cursor:token.start()])
            opened = token.group(2).lower()
        else:
            if opened != token.group(2).lower():
                raise protocol_error("THOUGHT")
            opened = None
            cursor = token.end()
    if opened is not None:
        raise protocol_error("THOUGHT")
    chunks.append(raw[cursor:])
    return "".join(chunks).strip()


def _object(body, *, kind="DECISION"):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result
    try:
        value = json.loads(body, object_pairs_hook=unique)
    except (ValueError, TypeError) as exc:
        raise protocol_error("JSON") from exc
    if not isinstance(value, dict):
        raise protocol_error(kind + "_TYPE")
    return value


def _decision(value, sources, reminder):
    if set(value) - {"contact", "progress", "source_ids", "reason", "effective_ts"}:
        raise protocol_error("DECISION_FIELDS")
    contact = value.get("contact")
    progress = value.get("progress", "unknown")
    ids = value.get("source_ids", [])
    if contact not in ("keep", "continue", "skip", "delay", "replace"):
        raise protocol_error("DECISION_CONTACT")
    if progress not in ("unknown", "planned", "ongoing", "completed", "cancelled"):
        raise protocol_error("DECISION_PROGRESS")
    available = {s["id"]: s for s in sources}
    if not isinstance(ids, list) or any(not isinstance(x, str) for x in ids):
        raise protocol_error("SOURCE_TYPE")
    if len(ids) > PLAN_LIMITS["sources"]:
        raise protocol_error("SOURCE_COUNT")
    if any(x not in available for x in ids):
        raise protocol_error("SOURCE_UNKNOWN")
    if (progress != "unknown" or contact in ("continue", "replace")) and not ids:
        raise protocol_error("SOURCE_REQUIRED")
    # Autonomous inference never cancels an explicit reminder. Explicit user
    # controls are committed through the authenticated UI/current-turn tool.
    if reminder and contact in ("skip", "delay", "replace"):
        raise protocol_error("REMINDER_CONTACT")
    effective = value.get("effective_ts")
    if contact == "delay" and type(effective) is not int:
        raise protocol_error("EFFECTIVE_TS")
    if contact == "delay" and not ids:
        raise protocol_error("SOURCE_REQUIRED")
    if not isinstance(value.get("reason", ""), str):
        raise protocol_error("REASON_TYPE")
    if len(value.get("reason", "")) > PLAN_LIMITS["reason"]:
        raise protocol_error("REASON_LENGTH")
    return {
        "decision": contact,
        "progress": progress,
        "source_ids": list(dict.fromkeys(ids)),
        "effective_ts": effective if contact == "delay" else None,
        "reason": plain_text(value.get("reason", ""))[:PLAN_LIMITS["reason"]],
    }


def parse_plan_controls(raw, sources=None, *, reminder=False):
    """Accept one prefix decision and unique trailing controls after removing thought.

    Reserved malformed/duplicate/nested/out-of-position markup rejects the whole
    response. It cannot become public prose or trigger a partial side effect.
    """
    from .proactive import clean_speech

    public = _without_thoughts(raw)
    decision = None
    if public.startswith("<schedule_decision>"):
        end = public.find("</schedule_decision>")
        if end < 0:
            raise protocol_error("STRUCTURE")
        body = public[len("<schedule_decision>"):end]
        if _RESERVED.search(body):
            raise protocol_error("STRUCTURE")
        decision = _decision(_object(body), sources or [], reminder)
        public = public[end + len("</schedule_decision>"):].strip()
    elif sources is not None:
        raise protocol_error("STRUCTURE")
    first = _RESERVED.search(public)
    body = public[:first.start()] if first else public
    tail = public[first.start():] if first else ""
    controls = {}
    while tail:
        token = _RESERVED.match(tail)
        name = token.group(1) if token else ""
        if name not in {"photo_scene", "follow_up"} or name in controls or not tail.startswith(f"<{name}>"):
            raise protocol_error("STRUCTURE")
        end = tail.find(f"</{name}>")
        if end < 0:
            raise protocol_error("STRUCTURE")
        value = tail[len(name) + 2:end].strip()
        if _RESERVED.search(value):
            raise protocol_error("STRUCTURE")
        controls[name] = value
        tail = tail[end + len(name) + 3:].strip()
    scene = controls.get("photo_scene", "")
    if "photo_scene" in controls and not scene:
        raise protocol_error("PHOTO_EMPTY")
    if len(scene) > PLAN_LIMITS["photo"]:
        raise protocol_error("PHOTO_LENGTH")
    follow = None
    if "follow_up" in controls:
        follow = _object(controls["follow_up"], kind="FOLLOW")
        if set(follow) - {"delay_minutes", "window_minutes", "name", "instruction", "with_photo"}:
            raise protocol_error("FOLLOW_FIELDS")
        if type(follow.get("delay_minutes")) is not int:
            raise protocol_error("FOLLOW_DELAY_TYPE")
        if not PLAN_LIMITS["delay_min"] <= follow["delay_minutes"] <= PLAN_LIMITS["delay_max"]:
            raise protocol_error("FOLLOW_DELAY_RANGE")
        if type(follow.get("window_minutes", 5)) is not int:
            raise protocol_error("FOLLOW_WINDOW_TYPE")
        if not 0 <= follow.get("window_minutes", 5) <= PLAN_LIMITS["window_max"]:
            raise protocol_error("FOLLOW_WINDOW_RANGE")
        if not isinstance(follow.get("name", ""), str):
            raise protocol_error("FOLLOW_NAME_TYPE")
        if len(follow.get("name", "")) > PLAN_LIMITS["name"]:
            raise protocol_error("FOLLOW_NAME_LENGTH")
        if not isinstance(follow.get("instruction"), str):
            raise protocol_error("FOLLOW_INSTRUCTION_TYPE")
        if not follow["instruction"].strip():
            raise protocol_error("FOLLOW_INSTRUCTION_EMPTY")
        if len(follow["instruction"]) > PLAN_LIMITS["instruction"]:
            raise protocol_error("FOLLOW_INSTRUCTION_LENGTH")
        if type(follow.get("with_photo", False)) is not bool:
            raise protocol_error("FOLLOW_PHOTO_TYPE")
    return decision, clean_speech(body, public=True)[:PLAN_LIMITS["body"]], plain_text(scene), follow


def parse_decision(raw, sources, *, reminder=False):
    decision, body, _, _ = parse_plan_controls(raw, sources, reminder=reminder)
    return decision, body
