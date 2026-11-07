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


def protocol_error(code):
    error = AlbumError("本次安排输出不合规，未发送消息")
    error.code = "PROTOCOL_" + code
    return error


def safe_failure(exc):
    """Classify locally; never persist an exception's possibly private text."""
    code = getattr(exc, "code", "")
    if code in {"PROTOCOL_THOUGHT", "PROTOCOL_STRUCTURE", "PROTOCOL_JSON", "PROTOCOL_FIELDS"}:
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


def safe_stage(stage, status, exc=None, code=None):
    if stage not in {"generation", "validation", "sending", "history", "photo", "follow_up"}:
        raise ValueError("Invalid local diagnostic stage")
    if status not in {"ok", "failed", "held", "pending", "skipped", "unknown", "fallback"}:
        raise ValueError("Invalid local diagnostic status")
    allowed = {"TIMEOUT", "CONNECTION", "STORAGE", "VALIDATION", "INTERNAL", "TIMER_ONLY", "PROTOCOL_THOUGHT", "PROTOCOL_STRUCTURE", "PROTOCOL_JSON", "PROTOCOL_FIELDS"}
    code = safe_failure(exc) if exc is not None else code
    return {"stage": stage, "status": status, "at": now(), **({"code": code if code in allowed else "INTERNAL"} if code else {})}


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


def _object(body):
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
        raise protocol_error("FIELDS")
    return value


def _decision(value, sources, reminder):
    if set(value) - {"contact", "progress", "source_ids", "reason", "effective_ts"}:
        raise protocol_error("FIELDS")
    contact = value.get("contact")
    progress = value.get("progress", "unknown")
    ids = value.get("source_ids", [])
    if contact not in ("keep", "continue", "skip", "delay", "replace"):
        raise protocol_error("FIELDS")
    if progress not in ("unknown", "planned", "ongoing", "completed", "cancelled"):
        raise protocol_error("FIELDS")
    available = {s["id"]: s for s in sources}
    if (
        not isinstance(ids, list)
        or len(ids) > 3
        or any(not isinstance(x, str) or x not in available for x in ids)
    ):
        raise protocol_error("FIELDS")
    if progress != "unknown" and not ids:
        raise protocol_error("FIELDS")
    # Autonomous inference never cancels an explicit reminder. Explicit user
    # controls are committed through the authenticated UI/current-turn tool.
    if reminder and contact in ("skip", "delay", "replace"):
        raise protocol_error("FIELDS")
    effective = value.get("effective_ts")
    if contact == "delay" and (type(effective) is not int or not ids):
        raise protocol_error("FIELDS")
    if not isinstance(value.get("reason", ""), str) or len(value.get("reason", "")) > 240:
        raise protocol_error("FIELDS")
    return {
        "decision": contact,
        "progress": progress,
        "source_ids": list(dict.fromkeys(ids)),
        "effective_ts": effective if contact == "delay" else None,
        "reason": plain_text(value.get("reason", ""))[:240],
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
    if "photo_scene" in controls and (not scene or len(scene) > 1000):
        raise protocol_error("FIELDS")
    follow = None
    if "follow_up" in controls:
        follow = _object(controls["follow_up"])
        if (
            set(follow) - {"delay_minutes", "window_minutes", "name", "instruction", "with_photo"}
            or type(follow.get("delay_minutes")) is not int
            or not 5 <= follow["delay_minutes"] <= 720
            or type(follow.get("window_minutes", 5)) is not int
            or not 0 <= follow.get("window_minutes", 5) <= 30
            or not isinstance(follow.get("name", ""), str)
            or len(follow.get("name", "")) > 80
            or not isinstance(follow.get("instruction"), str)
            or not follow["instruction"].strip()
            or len(follow["instruction"]) > 2000
            or type(follow.get("with_photo", False)) is not bool
        ):
            raise protocol_error("FIELDS")
    return decision, clean_speech(body, public=True)[:800], plain_text(scene), follow


def parse_decision(raw, sources, *, reminder=False):
    decision, body, _, _ = parse_plan_controls(raw, sources, reminder=reminder)
    return decision, body
