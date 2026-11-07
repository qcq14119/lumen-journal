"""Pure helpers: host history grouping, turn time, token estimates. No IO."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from ..proactive import parse_marker

TZ = ZoneInfo("Asia/Shanghai")
MEDIA_TYPES = ("image_url", "audio_url", "input_audio")
REMINDER = re.compile(
    r"<system_reminder>.*?Current datetime:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})"
    r"(?:\s*\(([^)]*)\))?.*?</system_reminder>",
    re.S,
)
# Earlier plugin versions wrote a host-style summary block into chat history.
LEGACY_SUMMARY_PREFIX = "Our previous history conversation summary: "
LEGACY_MARKER = "lumen-memory-summary:"
WEEKDAYS = "一二三四五六日"
IMAGE_UNKNOWN = "[曾分享图片，内容未记录]"
IMAGE_START = re.compile(
    r"\[图片#[0-9a-f]{12,32}(?=[：:\]])|\[图片(?:描述)?[：:]"
    r"|\[曾分享图片，内容未记录\]|\[图片，内容未识别\]",
    re.I,
)
IMAGE_REF_MARKER = re.compile(r"\[图片#([0-9a-f]{12,32})(?:[：:][^\]]*)?\]", re.I)
IMAGE_PRIVATE = re.compile(
    r"(?:https?|ftp|file)://|//\S|www\.|data\s*[:：]|base64\s*[:：,]"
    r"|(?<![A-Za-z0-9_-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"(?:[A-Za-z]{2,63}|xn--[A-Za-z0-9-]{2,59})(?![A-Za-z0-9_-])"
    r"|(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?=[:/])"
    r"|(?:sk-|Bearer\s)[A-Za-z0-9_-]{20,}"
    r"|(?:api[_ -]?key|authorization|password|cookie)\s*[:=]"
    r"|(?<![A-Za-z0-9])[A-Za-z0-9+/]{80,}={0,2}(?![A-Za-z0-9])",
    re.I,
)


def image_url(part: dict) -> str:
    value = part.get("image_url") if isinstance(part, dict) else None
    value = value.get("url") if isinstance(value, dict) else value
    return value if isinstance(value, str) else ""


def image_ref(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:32] if isinstance(url, str) and url else ""


def clean_image_description(value) -> str:
    """Safe image-only projection; inspect the full result before shortening it."""
    if not isinstance(value, str) or IMAGE_PRIVATE.search(value):
        return ""
    value = re.sub(r"(?is)<think(?:ing)?>.*?</think(?:ing)?>", "", value)
    if re.search(r"(?i)</?think", value):
        return ""
    value = " ".join(value.split()).strip("\"'“”‘’` ")
    # Keep one unambiguous bracketed image token even when the model uses brackets.
    return value.replace("[", "（").replace("]", "）")[:120].strip()


def image_parts(value):
    """Image parts only; never copy the potentially large URL strings."""
    if isinstance(value, list):
        for part in value:
            if isinstance(part, dict) and part.get("type") == "image_url" and not part.get("_no_save"):
                yield part


def iter_image_markers(text: str):
    """Yield (start, end, text), consuming nested brackets in old alt values."""
    offset = 0
    while match := IMAGE_START.search(text, offset):
        depth, end = 1, match.start() + 1
        while end < len(text) and depth:
            depth += (text[end] == "[") - (text[end] == "]")
            end += 1
        # An unfinished image description is not safe to split into ordinary text.
        yield match.start(), end, text[match.start():end]
        offset = end


def replace_image_markers(text: str, replacement) -> str:
    pieces, offset = [], 0
    for start, end, marker in iter_image_markers(text):
        pieces.extend((text[offset:start], replacement(marker)))
        offset = end
    pieces.append(text[offset:])
    return "".join(pieces)


def image_identity(text: str) -> str:
    return replace_image_markers(text, lambda _: IMAGE_UNKNOWN)


def content_image_refs(value) -> list[str]:
    """Ordered short identities, also retained after images become text parts."""
    if isinstance(value, str):
        return [m[1][:12].lower() for m in IMAGE_REF_MARKER.finditer(value)]
    refs = []
    if isinstance(value, list):
        for part in value:
            if not isinstance(part, dict) or part.get("_no_save"):
                continue
            if part.get("type") == "image_url":
                ref = image_ref(image_url(part))
                if ref:
                    refs.append(ref[:12])
            elif part.get("type") == "text":
                refs.extend(content_image_refs(part.get("text", "")))
    return refs


def image_content(value, *, legacy=False, markers=None, token="", side="", positions=None) -> str:
    """Render real image parts, optionally carrying temporary exact boundaries."""
    if not isinstance(value, list):
        if positions is not None:
            positions[side] += sum(1 for _ in iter_image_markers(text_content(value)))
        return text_content(value)
    parts = []
    for part in value:
        if not isinstance(part, dict) or part.get("_no_save"):
            continue
        if part.get("type") == "image_url":
            if legacy:
                description = part.get("description") or part.get("alt")
                rendered = f"[图片描述：{description.strip()}]" if isinstance(description, str) and description.strip() else IMAGE_UNKNOWN
            else:
                rendered = media_text(part)
            if markers is not None:
                index = len(markers)
                markers.append((side, positions[side], part))
                positions[side] += 1
                rendered = f"{token}S{index}Z" + rendered + f"{token}E{index}Z"
            parts.append({"type": "text", "text": rendered})
        else:
            parts.append(part)
            if positions is not None and part.get("type") == "text":
                positions[side] += sum(1 for _ in iter_image_markers(text_content([part])))
    return text_content(parts)


def image_turn_evidence(turn, *, legacy=False):
    """Exact final bodies and image spans, derived only from structured parts."""
    bodies, tagged = {"user": [], "assistant": []}, {"user": [], "assistant": []}
    positions = {"user": 0, "assistant": 0}
    markers, token = [], "LUMEN" + uuid.uuid4().hex
    for message in turn["messages"]:
        side = message.get("role")
        if side not in bodies or message.get("tool_calls") or message.get("_no_save"):
            continue
        value = image_content(message.get("content"), legacy=legacy)
        tagged_value = image_content(message.get("content"), legacy=legacy, markers=markers, token=token, side=side, positions=positions)
        if value:
            bodies[side].append(value)
            tagged[side].append(tagged_value)
    bodies = {side: "\n".join(parts) for side, parts in bodies.items()}
    spans = {"user": {}, "assistant": {}}
    retained = []
    boundary = re.compile(re.escape(token) + r"([SE])(\d+)Z")
    for side, parts in tagged.items():
        value = "\n".join(parts)
        output, starts, pairs, offset, size = [], {}, [], 0, 0
        for match in boundary.finditer(value):
            chunk = value[offset:match.start()]
            output.append(chunk)
            size += len(chunk)
            index = int(match[2])
            if match[1] == "S":
                starts[index] = size
            elif index in starts:
                pairs.append((starts[index], size, index))
            offset = match.end()
        output.append(value[offset:])
        if "".join(output) != bodies[side]:
            continue
        for start, end, index in sorted(pairs):
            original_side, position, part = markers[index]
            if original_side != side or not 0 <= start < end <= len(bodies[side]):
                continue
            spans[side][position] = (start, end)
            retained.append((side, position, part))
    return {"bodies": bodies, "spans": spans, "parts": tuple(retained)}


def persisted_turn_images(turn):
    """The exact original parts whose tagged ranges survive the text renderer."""
    current = turn.get("image_evidence", {}).get("current_verified")
    if current is None:
        current = image_turn_evidence(turn)
    return current["parts"]


def persisted_image_refs(turn):
    """Ordered full refs for real images and short refs quoted in saved text."""
    current = turn.get("image_evidence", {}).get("current_verified")
    if current is None:
        current = image_turn_evidence(turn)
    refs = []
    parts = {(side, position): part for side, position, part in current["parts"]}
    for side in ("user", "assistant"):
        value, offset = current["bodies"][side], 0
        for position, (start, end) in sorted(current["spans"][side].items(), key=lambda item: item[1]):
            refs.extend(content_image_refs(value[offset:start]))
            ref = image_ref(image_url(parts[(side, position)]))
            if ref:
                refs.append(ref)
            offset = end
        refs.extend(content_image_refs(value[offset:]))
    return tuple(refs)


def legacy_turn_metadata(turn):
    has_images = any(
        any(image_parts(message.get("content"))) for message in turn["messages"]
        if message.get("role") in ("user", "assistant")
        and not message.get("tool_calls") and not message.get("_no_save")
    )
    evidence = {}
    if has_images:
        legacy = image_turn_evidence(turn, legacy=True)
        bodies = legacy["bodies"]
        evidence = {"current_verified": image_turn_evidence(turn), "legacy_verified": legacy}
    else:
        # Ordinary and already projected text has no structural image proof to
        # construct. It still participates in the exact legacy identity hash.
        bodies = {side: turn[side] for side in ("user", "assistant")}
    body = hashlib.sha256(json.dumps(
        [without_reminder(bodies["user"]), bodies["assistant"].strip(), turn["kind"]], ensure_ascii=False,
    ).encode()).hexdigest()
    return {
        "legacy_key": turn.get("checkpoint") or f"d:{body}:{reminder_time(bodies['user'])}",
        "legacy_body_digest": body,
        "image_evidence": evidence,
    }


def image_body_hash(value):
    """Version of the exact saved side, including its time reminder."""
    return hashlib.sha256(value.encode()).hexdigest()


def verified_image_spans(value, records):
    """Read-only proof check shared by projection and identity comparison.

    An old offset without a source is never proof. Overlapping records invalidate
    both claims; no query ordering may arbitrarily choose which one to trust.
    """
    expected = image_body_hash(value)
    spans = []
    for record in records:
        record = dict(record)
        start, end = record.get("start_pos"), record.get("end_pos")
        if (
            record.get("source") in ("current_verified", "legacy_verified")
            and record.get("body_hash") == expected
            and type(start) is int and type(end) is int and 0 <= start < end <= len(value)
        ):
            spans.append(record)
    spans.sort(key=lambda record: (record["start_pos"], record["end_pos"]))
    result, group, boundary = [], [], -1
    for record in spans:
        if record["start_pos"] >= boundary:
            if len(group) == 1:
                result.extend(group)
            group = []
        group.append(record)
        boundary = max(boundary, record["end_pos"])
    if len(group) == 1:
        result.extend(group)
    return result


def media_text(part: dict) -> str:
    label = "图片" if part.get("type") == "image_url" else "音频"
    description = part.get("description") or part.get("alt")
    if label == "图片":
        description = clean_image_description(description)
        if description:
            return f"[图片描述：{description}]"
        ref = image_ref(image_url(part))
        return f"[图片#{ref[:12]}]" if ref else IMAGE_UNKNOWN
    if isinstance(description, str) and description.strip():
        return f"[{label}描述：{description.strip()}]"
    return f"[曾分享{label}，内容未记录]"


def text_content(value) -> str:
    """Ordinary text of one message; media becomes a short label, never Base64."""
    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        parts = []
        for part in value:
            if not isinstance(part, dict) or part.get("_no_save"):
                continue
            if part.get("type") == "text":
                parts.append(str(part.get("text", "")))
            elif part.get("type") in MEDIA_TYPES:
                parts.append(media_text(part))
        text = "\n".join(parts)
    else:
        return ""
    text = re.sub(r"(?is)<think(?:ing)?>.*?</think(?:ing)?>", "", text)
    text = re.sub(r"data:[^\s,]+;base64,[A-Za-z0-9+/=]+", "[媒体数据已省略]", text)
    text = re.sub(r"base64://[A-Za-z0-9+/=]+", "[媒体数据已省略]", text)
    return text.strip()


def estimate(value) -> int:
    """One conservative estimate used everywhere: CJK 1 token/char, else ~3 chars."""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, default=str)
    wide = len(re.findall(r"[^\x00-\x7f]", value))
    return max(1, wide + (len(value) - wide + 2) // 3)


def reminder_time(text: str) -> int:
    """Unix time from AstrBot's saved time-awareness reminder, else 0."""
    match = REMINDER.search(text or "")
    if not match:
        return 0
    try:
        return int(
            datetime.strptime(match[1], "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp()
        )
    except ValueError:
        return 0


def time_label(at: int) -> str:
    if not at:
        return "时间未知"
    moment = datetime.fromtimestamp(at, TZ)
    return moment.strftime("%Y-%m-%d ") + "周" + WEEKDAYS[moment.weekday()] + moment.strftime(" %H:%M")


def without_reminder(text: str) -> str:
    """Display/prompt text only; backups keep the original content untouched."""
    return REMINDER.sub("", text or "").strip()


def digest(user: str, assistant: str, kind: str) -> str:
    """Identity of a round's words; the volatile time reminder is not part of it."""
    return hashlib.sha256(
        json.dumps(
            [image_identity(without_reminder(user)), image_identity(assistant.strip()), kind], ensure_ascii=False
        ).encode()
    ).hexdigest()


def legacy_block(history: list) -> int:
    """Length of an old plugin-written summary block at the start, else 0."""
    if (
        len(history) >= 3
        and isinstance(history[0], dict)
        and history[0].get("role") == "user"
        and isinstance(history[0].get("content"), str)
        and history[0]["content"].startswith(LEGACY_SUMMARY_PREFIX)
        and isinstance(history[2], dict)
        and history[2].get("role") == "_checkpoint"
        and str((history[2].get("content") or {}).get("id", "")).startswith(LEGACY_MARKER)
    ):
        return 3
    return 0


def history_turns(history: list) -> list[dict]:
    """Group host messages into complete rounds; each keeps its raw messages.

    A round starts at a user message (or a proactive assistant message) and owns
    every following assistant/tool/checkpoint message until the next round.
    """
    if not isinstance(history, list):
        return []
    turns, current = [], None
    for index, item in enumerate(history):
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role == "_checkpoint":
            if current is not None:
                content = item.get("content")
                if isinstance(content, dict) and not current["checkpoint"]:
                    current["checkpoint"] = str(content.get("id", ""))
                current["messages"].append(item)
            continue
        if role == "system" or item.get("_no_save"):
            continue
        following = history[index + 1] if index + 1 < len(history) else None
        proactive = parse_marker(following) if following else None
        if role == "assistant" and not item.get("tool_calls") and proactive:
            if current is not None:
                turns.append(current)
            current = {
                "user": "",
                "assistant": text_content(item.get("content")),
                "checkpoint": proactive["checkpoint"],
                "kind": "proactive",
                "at": proactive["at"],
                "messages": [item],
            }
            continue
        if role == "user":
            if current is not None:
                turns.append(current)
            raw = text_content(item.get("content"))
            stamp = item.get("timestamp")
            current = {
                "user": raw,
                "assistant": "",
                "checkpoint": "",
                "kind": "chat",
                "at": reminder_time(raw)
                or (stamp if type(stamp) is int and 0 < stamp < 32503680000 else 0),
                "messages": [item],
            }
        elif current is not None:
            current["messages"].append(item)
            if role == "assistant" and not item.get("tool_calls"):
                body = text_content(item.get("content"))
                if body:
                    current["assistant"] += ("\n" if current["assistant"] else "") + body
    if current is not None:
        turns.append(current)
    for turn in turns:
        if turn["kind"] == "chat" and turn["user"].startswith(LEGACY_SUMMARY_PREFIX):
            turn["kind"] = "host_summary"
        turn["digest"] = digest(turn["user"], turn["assistant"], turn["kind"])
        turn["image_refs"] = [
            ref for message in turn["messages"]
            if message.get("role") in ("user", "assistant") and not message.get("tool_calls")
            for ref in content_image_refs(message.get("content"))
        ]
        turn["complete"] = bool(turn["assistant"]) or turn["kind"] == "proactive"
        turn.update(legacy_turn_metadata(turn))
    return turns


def turn_key(turn: dict) -> str:
    """Stable identity: AstrBot checkpoint, else words plus the saved send time."""
    if turn.get("checkpoint"):
        return turn["checkpoint"]
    key = f"d:{turn['digest']}:{reminder_time(turn.get('user', ''))}"
    refs = turn.get("image_refs") or content_image_refs(turn.get("user", "")) + content_image_refs(turn.get("assistant", ""))
    if refs:
        key += ":i:" + hashlib.sha256(json.dumps(refs).encode()).hexdigest()[:16]
    return key
