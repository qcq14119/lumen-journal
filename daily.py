"""Bounded same-chat evidence and adjustments to one scheduled occurrence."""

from __future__ import annotations

from .restore_guard import managed

import hashlib
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .store import AlbumError, now

DECISIONS = ("keep", "continue", "skip", "delay", "replace", "restore")
PROGRESS = ("unknown", "planned", "ongoing", "completed", "cancelled")
TERMINAL = ("sent", "unknown", "failed", "held", "skipped", "expired")


def plain_text(value) -> str:
    """Retain complete ordinary text, excluding media, tools and secret URLs."""
    if isinstance(value, list):
        value = "\n".join(
            p.get("text", "")
            for p in value
            if isinstance(p, dict)
            and p.get("type") == "text"
            and isinstance(p.get("text"), str)
            and not p.get("_no_save")
        )
    if not isinstance(value, str):
        return ""
    value = re.sub(r"(?is)<think(?:ing)?>.*?</think(?:ing)?>", "", value)
    value = re.sub(r"data:[^\s,]+;base64,[A-Za-z0-9+/=]+", "[媒体已省略]", value)
    value = re.sub(r"base64://[A-Za-z0-9+/=]+", "[媒体已省略]", value)
    value = re.sub(r"https?://\S+\?\S+", "[带参数链接已省略]", value)
    return value.strip()


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def _terms(value: str) -> set[str]:
    values = set(re.findall(r"[a-z0-9]{2,}", value.lower()))
    for run in re.findall(r"[\u3400-\u9fff]+", value):
        values.update(run[i : i + 2] for i in range(len(run) - 1))
    return values


class DailyEvidence:
    """Rebuildable local evidence; first observation establishes a baseline only."""

    def __init__(self, store):
        self.store = store
        with store.lock, store.db:
            store.db.execute(
                "CREATE INDEX IF NOT EXISTS records_daily_scope ON records"
                "(kind,json_extract(data,'$.scope'),json_extract(data,'$.cid'),"
                "json_extract(data,'$.at'))"
            )

    def _state(self, scope, cid):
        try:
            return self.store.get("daily_cursor", _hash([scope, cid]))
        except AlbumError:
            return {}

    def revision(self, scope: str, cid: str) -> int:
        return self._state(scope, cid).get("revision", 0)

    def _rows(self, scope, cid):
        rows = self.store.db.execute(
            "SELECT id,data,revision FROM records WHERE kind='daily_source' "
            "AND json_extract(data,'$.scope')=? AND json_extract(data,'$.cid')=? "
            "ORDER BY json_extract(data,'$.at') DESC,rowid DESC LIMIT 512",
            (scope, cid),
        ).fetchall()
        return [
            {**json.loads(r["data"]), "id": r["id"], "revision": r["revision"]}
            for r in rows
        ]

    def _observed_message(self, scope, cid, history, index, at, generation):
        """Use the same source proof for new evidence and cached-source invalidation."""
        from .proactive import body_matches, marker as expected_marker, parse_marker

        item = history[index] if 0 <= index < len(history) else {}
        if not isinstance(item, dict):
            return "", "", at, "observed_saved"
        role = item.get("role")
        text = plain_text(item.get("content")) if role in ("user", "assistant") and not item.get("tool_calls") else ""
        if len(text) > 6000:
            text = ""
        source_at, time_kind = at, "observed_saved"
        marker = parse_marker(history[index + 1]) if index + 1 < len(history) else None
        if text and marker:
            try:
                sent = self.store.get("proactive_message", marker["key"])
            except AlbumError:
                sent = {}
            if (
                role != "assistant" or sent.get("scope") != scope or sent.get("cid") != cid
                or sent.get("state") != "sent" or sent.get("kind") == "photo"
                or not body_matches(sent, text) or marker["checkpoint"] != expected_marker(sent)
                or generation is not None and sent.get("generation", -1) not in (-1, generation)
            ):
                text = ""
            else:
                source_at, time_kind = int(sent["sent_at"]), "sent"
        return role, text, source_at, time_kind

    @managed
    def observe(
        self,
        scope: str,
        cid: str,
        history: list,
        *,
        at: int | None = None,
        generation=None,
        capture: bool = True,
    ):
        """Observe final saved history, never a provider draft or imported old text.

        Only an appended suffix with a known predecessor receives an observation
        time. Reset/replacement establishes a fresh baseline; edits invalidate
        evidence. No historical timestamps are fabricated at initial enablement.
        """
        if not scope or not cid or not isinstance(history, list):
            return
        at = now() if at is None else int(at)
        with self.store.lock, self.store.db:
            old = self._state(scope, cid)
            start = max(0, len(history) - 256)
            tail = []
            texts = {}
            for index in range(start, len(history)):
                item = history[index]
                if not isinstance(item, dict):
                    continue
                role, text, source_at, time_kind = self._observed_message(scope, cid, history, index, at, generation)
                fp = _hash([role, text])
                tail.append([index, fp])
                if text:
                    checkpoint = history[index + 1]["content"]["id"] if time_kind == "sent" else ""
                    texts[index] = (role, text, fp, source_at, time_kind, checkpoint)
            generation_changed = (
                generation is not None
                and old.get("generation") is not None
                and generation != old.get("generation")
            )
            existing = self._rows(scope, cid)
            invalid = set()
            for source in existing:
                index = source.get("index", -1)
                role, text, source_at, time_kind = self._observed_message(scope, cid, history, index, at, generation)
                fingerprint = _hash([role, text]) if text else ""
                checkpoint = history[index + 1]["content"]["id"] if time_kind == "sent" else ""
                if (
                    source.get("fingerprint") != fingerprint
                    or source.get("time_kind") == "sent" and (
                        time_kind != "sent" or source.get("at") != source_at
                        or source.get("checkpoint", checkpoint) != checkpoint
                    )
                    or source.get("at", 0) < at - 3 * 86400
                ):
                    invalid.add(source["id"])
            if (
                old.get("tail") == tail
                and old.get("count") == len(history)
                and not generation_changed
                and not invalid
            ):
                return
            previous = dict(old.get("tail", []))
            current = dict(tail)
            old_count = old.get("count", 0)
            appended = bool(
                old
                and not generation_changed
                and old_count >= 0
                and old_count <= len(history)
                and (
                    old_count == 0
                    or previous.get(old_count - 1) == current.get(old_count - 1)
                )
            )
            for source in existing:
                if not appended or source["id"] in invalid:
                    self.store.db.execute(
                        "DELETE FROM records WHERE kind='daily_source' AND id=?",
                        (source["id"],),
                    )
            if appended and capture:
                for index, (role, text, fp, source_at, time_kind, checkpoint) in texts.items():
                    if index < old_count:
                        continue
                    key = _hash([scope, cid, index, fp, old.get("epoch", 0)])
                    self.store.save(
                        "daily_source",
                        key,
                        {
                            "scope": scope,
                            "cid": cid,
                            "index": index,
                            "fingerprint": fp,
                            "role": role,
                            "text": text,
                            "at": source_at,
                            "time_kind": time_kind,
                            "checkpoint": checkpoint,
                            "terms": sorted(_terms(text)),
                        },
                    )
            self.store.save(
                "daily_cursor",
                _hash([scope, cid]),
                {
                    "scope": scope,
                    "cid": cid,
                    "count": len(history),
                    "tail": tail,
                    "at": at,
                    "epoch": old.get("epoch", 0) + (0 if appended else 1),
                    "generation": generation
                    if generation is not None
                    else old.get("generation"),
                },
                old.get("revision"),
            )
            self.store.db.execute(
                "DELETE FROM records WHERE kind='daily_source' AND id IN "
                "(SELECT id FROM records WHERE kind='daily_source' AND "
                "json_extract(data,'$.scope')=? AND json_extract(data,'$.cid')=? "
                "ORDER BY json_extract(data,'$.at') DESC,rowid DESC LIMIT -1 OFFSET 256)",
                (scope, cid),
            )

    def query(
        self,
        scope: str,
        cid: str,
        query: str,
        *,
        day_start: int,
        day_end: int,
        limit: int = 3,
        budget: int = 1800,
        exclude_ids=(),
    ) -> list[dict]:
        """Rank bounded same-day complete statements; keywords never decide facts."""
        terms = _terms(query)
        if not terms:
            return []
        with self.store.lock:
            candidates = [
                r
                for r in self._rows(scope, cid)
                if day_start <= r["at"] < day_end and r["id"] not in exclude_ids
            ]
        ranked = sorted(
            candidates,
            key=lambda r: (len(terms & set(r["terms"])), r["at"]),
            reverse=True,
        )
        result, used = [], 0
        for row in ranked:
            if not terms & set(row["terms"]):
                continue
            cost = len(row["text"])
            if used + cost > budget:
                continue
            result.append(
                {
                    k: row[k]
                    for k in ("id", "revision", "role", "text", "at", "time_kind")
                }
            )
            used += cost
            if len(result) >= min(3, max(1, limit)):
                break
        return result

    @managed
    def capture_final(
        self,
        scope: str,
        cid: str,
        history: list,
        checkpoint: str,
        *,
        final_text: str = "",
        at: int | None = None,
        generation=None,
    ) -> bool:
        """Capture a known saved final response independently of cursor ordering.

        The caller supplies the live event checkpoint, not one discovered during
        generic history reading. This tolerates a newer request establishing its
        baseline before the completed event's queued capture acquires the lock.
        """
        from .proactive import body_matches, marker as expected_marker, parse_marker

        if (
            not scope
            or not cid
            or not checkpoint
            or not final_text
            or not isinstance(history, list)
        ):
            return False
        positions = [
            i
            for i, item in enumerate(history)
            if isinstance(item, dict)
            and item.get("role") == "_checkpoint"
            and isinstance(item.get("content"), dict)
            and item["content"].get("id") == checkpoint
        ]
        if len(positions) != 1 or positions[0] == 0:
            return False
        index = positions[0] - 1
        assistant = history[index]
        if (
            not isinstance(assistant, dict)
            or assistant.get("role") != "assistant"
            or assistant.get("tool_calls")
        ):
            return False
        body = plain_text(assistant.get("content"))
        if body != plain_text(final_text) or not body or len(body) > 6000:
            return False
        at = now() if at is None else int(at)
        time_kind = "observed_saved"
        marker = parse_marker(history[positions[0]])
        selected = [(index, "assistant", body)]
        with self.store.lock, self.store.db:
            cursor = self._state(scope, cid)
            if not cursor:
                self.observe(
                    scope, cid, history, at=at, generation=generation, capture=False
                )
                cursor = self._state(scope, cid)
            if generation is not None and cursor.get("generation") not in (
                None,
                generation,
            ):
                return False
            if marker:
                try:
                    record = self.store.get("proactive_message", marker["key"])
                except AlbumError:
                    return False
                if (
                    record.get("scope") != scope
                    or record.get("cid") != cid
                    or record.get("state") != "sent"
                    or record.get("kind") == "photo"
                    or not body_matches(record, body)
                    or marker["checkpoint"] != expected_marker(record)
                    or generation is not None
                    and record.get("generation", -1) not in (-1, generation)
                ):
                    return False
                at, time_kind = int(record["sent_at"]), "sent"
            else:
                for preceding in range(index - 1, max(-1, index - 65), -1):
                    item = history[preceding]
                    if not isinstance(item, dict):
                        continue
                    if item.get("role") == "_checkpoint":
                        break
                    if item.get("role") == "user":
                        text = plain_text(item.get("content"))
                        if text and len(text) <= 6000:
                            selected.insert(0, (preceding, "user", text))
                        break
            changed = False
            for position, role, text in selected:
                key = _hash([scope, cid, checkpoint, role])
                fingerprint = _hash([role, text])
                try:
                    old = self.store.get("daily_source", key)
                except AlbumError:
                    old = {}
                if (
                    old.get("fingerprint") == fingerprint
                    and old.get("index") == position
                ):
                    continue
                self.store.save(
                    "daily_source",
                    key,
                    {
                        "scope": scope,
                        "cid": cid,
                        "index": position,
                        "checkpoint": checkpoint,
                        "fingerprint": fingerprint,
                        "role": role,
                        "text": text,
                        "at": old.get("at", at)
                        if old.get("fingerprint") == fingerprint
                        else at,
                        "time_kind": time_kind,
                        "terms": sorted(_terms(text)),
                    },
                    old.get("revision"),
                )
                changed = True
            if changed:
                self.store.save(
                    "daily_cursor",
                    cursor["id"],
                    {**cursor, "at": max(cursor.get("at", 0), at)},
                    cursor["revision"],
                )
                self.store.db.execute(
                    "DELETE FROM records WHERE kind='daily_source' AND id IN (SELECT id FROM records WHERE kind='daily_source' AND json_extract(data,'$.scope')=? AND json_extract(data,'$.cid')=? ORDER BY json_extract(data,'$.at') DESC,rowid DESC LIMIT -1 OFFSET 256)",
                    (scope, cid),
                )
        return True

    def valid_sources(self, scope, cid, source_ids) -> bool:
        if not isinstance(source_ids, list) or len(source_ids) > 3:
            return False
        with self.store.lock:
            available = {r["id"] for r in self._rows(scope, cid)}
        return all(isinstance(key, str) and key in available for key in source_ids)

    def get_sources(self, scope: str, cid: str, source_ids: list) -> list[dict]:
        if not isinstance(source_ids, list) or len(source_ids) > 3:
            raise AlbumError("来源引用格式不正确")
        with self.store.lock:
            available = {r["id"]: r for r in self._rows(scope, cid)}
        return [
            {
                k: available[key][k]
                for k in ("id", "revision", "role", "text", "at", "time_kind")
            }
            for key in source_ids
            if isinstance(key, str) and key in available
        ]

    def recent_sources(
        self,
        scope: str,
        cid: str,
        *,
        day_start: int,
        day_end: int,
        limit: int = 3,
        budget: int = 1800,
    ) -> list[dict]:
        with self.store.lock:
            rows = self._rows(scope, cid)
        result, used = [], 0
        for row in rows:
            if not day_start <= row["at"] < day_end or used + len(row["text"]) > budget:
                continue
            result.append(
                {
                    k: row[k]
                    for k in ("id", "revision", "role", "text", "at", "time_kind")
                }
            )
            used += len(row["text"])
            if len(result) >= min(3, max(1, limit)):
                break
        return result

    @managed
    def invalidate(self, scope: str, cid: str):
        """Drop rebuildable evidence only; explicit occurrence agreements survive."""
        with self.store.lock, self.store.db:
            self.store.db.execute(
                "DELETE FROM records WHERE kind='daily_source' AND json_extract(data,'$.scope')=? AND json_extract(data,'$.cid')=?",
                (scope, cid),
            )
            old = self._state(scope, cid)
            if old:
                self.store.save(
                    "daily_cursor",
                    old["id"],
                    {**old, "tail": [], "count": -1, "epoch": old.get("epoch", 0) + 1},
                    old["revision"],
                )


class DailySchedule:
    """Persist only the delta; the recurring plan remains the default authority."""

    def __init__(self, plans):
        self.plans, self.store = plans, plans.store
        self._pruned_day = None

    @managed
    def prune(self, at: int):
        if self._pruned_day == at // 86400:
            return
        with self.store.lock, self.store.db:
            self.store.db.execute(
                "DELETE FROM records AS old WHERE kind='life_adjustment' AND json_extract(data,'$.anchor_ts')<? AND json_extract(data,'$.effective_ts')<? "
                "AND NOT EXISTS (SELECT 1 FROM records p WHERE p.kind='restore_pending' "
                "AND json_extract(p.data,'$.kind')='life_plan' AND json_extract(p.data,'$.target_id')=json_extract(old.data,'$.plan_id'))",
                (at - 4 * 86400, at - 3 * 86400),
            )
        self._pruned_day = at // 86400

    def _adjustments(self, plan_id="", scope="", cid=""):
        sql = "SELECT id,data,revision FROM records WHERE kind='life_adjustment'"
        args = []
        for field, value in (("plan_id", plan_id), ("scope", scope), ("cid", cid)):
            if value:
                sql += f" AND json_extract(data,'$.{field}')=?"
                args.append(value)
        with self.store.lock:
            rows = self.store.db.execute(sql, args).fetchall()
        return [
            {
                **json.loads(r["data"]),
                "adjustment_id": r["id"],
                "revision": r["revision"],
            }
            for r in rows
        ]

    def _base(self, plan, anchor, cid, calendar=None):
        from .plans import _daily_anchors_for_plan, day_profile, jitter_offset

        zone = ZoneInfo(plan["timezone"])
        date = datetime.fromtimestamp(anchor, zone).date()
        calendar = calendar or self.plans.get_calendar()
        if plan.get("run_once"):
            if anchor != plan["trigger_at"]:
                raise AlbumError("本次安排不存在")
            item = {
                "anchor_ts": anchor,
                "before": plan["window_before"],
                "after": plan["window_after"],
                "profile": day_profile(date, calendar, plan["scope"]),
            }
        else:
            items = _daily_anchors_for_plan(
                plan,
                date,
                zone,
                calendar,
                self.plans.override_wake_dates_for(plan["scope"], cid),
            )
            item = next((i for i in items if i["anchor_ts"] == anchor), None)
            if item is None:
                raise AlbumError("本次安排已因时令或行程改变，请刷新")
        actual = anchor + jitter_offset(
            plan["id"], anchor, item["before"], item["after"]
        )
        return {**item, "default_ts": actual}

    def _project(self, plan, base, cid, at, adjustments):
        anchor = base["anchor_ts"]
        key = f"{plan['id']}:{anchor}"
        stored = next(
            (a for a in adjustments if a["anchor_ts"] == anchor),
            {},
        )
        adjustment = (
            stored
            if stored.get("plan_revision") == plan["revision"]
            and stored.get("character_id") == plan["character_id"]
            else {}
        )
        if adjustment and adjustment["cid"] != cid:
            return None
        try:
            run = self.store.get("life_run", key)
        except AlbumError:
            run = {}
        if run and run.get("conversation_id") != cid:
            return None
        needs_review = False
        if adjustment and not adjustment.get("explicit"):
            try:
                cursor = self.store.get("daily_cursor", _hash([plan["scope"], cid]))
            except AlbumError:
                cursor = {}
            needs_review = adjustment.get(
                "evidence_revision"
            ) is not None and adjustment["evidence_revision"] != cursor.get(
                "revision", 0
            )
            for source_id in adjustment.get("source_ids", []):
                try:
                    source = self.store.get("daily_source", source_id)
                except AlbumError:
                    source = {}
                if source.get("scope") != plan["scope"] or source.get("cid") != cid:
                    needs_review = True
        effective = adjustment.get("effective_ts", base["default_ts"])
        zone = ZoneInfo(plan["timezone"])
        state = run.get("state") or ("pending" if effective >= at else "expired")
        return {
            "id": key,
            "plan_id": plan["id"],
            "plan_revision": plan["revision"],
            "scope": plan["scope"],
            "cid": cid,
            "character_id": plan["character_id"],
            "anchor_ts": anchor,
            "default_ts": base["default_ts"],
            "effective_ts": effective,
            "anchor_local": datetime.fromtimestamp(anchor, zone).isoformat(),
            "default_local": datetime.fromtimestamp(
                base["default_ts"], zone
            ).isoformat(),
            "effective_local": datetime.fromtimestamp(effective, zone).isoformat(),
            "timezone": plan["timezone"],
            "name": plan["name"],
            "instruction": plan["instruction"],
            "mode": plan.get("contact_mode", "adaptive"),
            "state": state,
            "progress": "unknown"
            if needs_review
            else adjustment.get("progress", "unknown"),
            "decision": adjustment.get("decision", "keep"),
            "reason": adjustment.get("reason", ""),
            "source_ids": adjustment.get("source_ids", []),
            "revision": stored.get("revision", 0) if stored.get("cid") == cid else 0,
            "actor": adjustment.get("actor", ""),
            "explicit": adjustment.get("explicit", False),
            "auto_delays": stored.get("auto_delays", 0)
            if stored.get("cid") == cid
            else 0,
            "needs_review": needs_review,
            "replacement": adjustment.get("replacement", ""),
            "evidence_revision": adjustment.get("evidence_revision"),
            "editable": state == "deferred" or not run and effective >= at,
        }

    def today(self, scope: str, cid: str, *, at: int | None = None, limit: int = 60):
        from .plans import _daily_anchors_for_plan, jitter_offset

        if not scope or not cid:
            return []
        try:
            character_id = self.store.character_for(scope)["id"]
        except AlbumError:
            return []
        at = now() if at is None else int(at)
        calendar = self.plans.get_calendar()
        result = []
        for plan in self.store.all("life_plan"):
            if (
                plan.get("deleted")
                or not plan.get("enabled")
                or plan["scope"] != scope
                or not self.plans._belongs(plan, cid)
                or plan["character_id"] != character_id
            ):
                continue
            zone = ZoneInfo(plan["timezone"])
            day = datetime.fromtimestamp(at, zone).date()
            adjustments = self._adjustments(plan["id"])
            anchors = {
                a["anchor_ts"]
                for a in adjustments
                if a["cid"] == cid
                and datetime.fromtimestamp(a["effective_ts"], zone).date() == day
            }
            bases = {}
            if plan.get("run_once"):
                if datetime.fromtimestamp(plan["trigger_at"], zone).date() == day:
                    anchors.add(plan["trigger_at"])
            else:
                for info in _daily_anchors_for_plan(
                    plan,
                    day,
                    zone,
                    calendar,
                    self.plans.override_wake_dates_for(scope, cid),
                ):
                    anchor = info["anchor_ts"]
                    anchors.add(anchor)
                    bases[anchor] = {
                        **info,
                        "default_ts": anchor
                        + jitter_offset(
                            plan["id"], anchor, info["before"], info["after"]
                        ),
                    }
            for anchor in sorted(anchors)[:200]:
                try:
                    base = bases.get(anchor) or self._base(plan, anchor, cid, calendar)
                except AlbumError:
                    continue
                item = self._project(plan, base, cid, at, adjustments)
                if item:
                    result.append(item)
        return sorted(result, key=lambda r: (r["effective_ts"], r["id"]))[
            : max(1, min(200, int(limit)))
        ]

    def occurrence(self, scope, cid, occurrence_id, *, at=None):
        try:
            plan_id, raw = occurrence_id.rsplit(":", 1)
            anchor = int(raw)
        except (ValueError, AttributeError):
            raise AlbumError("本次安排编号无效") from None
        plan = self.store.get("life_plan", plan_id)
        if (
            not cid
            or plan["scope"] != scope
            or not self.plans._belongs(plan, cid)
            or plan.get("deleted")
            or not plan.get("enabled")
        ):
            raise AlbumError("只能操作当前聊天有效的本次安排")
        if self.store.character_for(scope)["id"] != plan["character_id"]:
            raise AlbumError("计划绑定角色已变化")
        base = self._base(plan, anchor, cid)
        result = self._project(
            plan, base, cid, now() if at is None else at, self._adjustments(plan_id)
        )
        if result is None:
            raise AlbumError("本次安排已绑定另一聊天")
        return result

    def _conflict(self, scope, cid, occurrence_id, effective):
        from .plans import _daily_anchors_for_plan, jitter_offset

        calendar = self.plans.get_calendar()
        for plan in self.store.all("life_plan"):
            if (
                plan["scope"] != scope
                or not plan.get("enabled")
                or plan.get("deleted")
                or not self.plans._belongs(plan, cid)
            ):
                continue
            zone = ZoneInfo(plan["timezone"])
            day = datetime.fromtimestamp(effective, zone).date()
            candidates = {}
            if plan.get("run_once"):
                info = self._base(plan, plan["trigger_at"], cid, calendar)
                candidates[info["anchor_ts"]] = info["default_ts"]
            else:
                for offset in (-1, 0, 1):
                    for info in _daily_anchors_for_plan(
                        plan,
                        day + timedelta(days=offset),
                        zone,
                        calendar,
                        self.plans.override_wake_dates_for(scope, cid),
                    ):
                        anchor = info["anchor_ts"]
                        candidates[anchor] = anchor + jitter_offset(
                            plan["id"], anchor, info["before"], info["after"]
                        )
            for adjustment in self._adjustments(plan["id"]):
                if adjustment["plan_revision"] != plan["revision"]:
                    continue
                candidates.pop(adjustment["anchor_ts"], None)
                if adjustment["cid"] == cid and adjustment["decision"] != "skip":
                    candidates[adjustment["anchor_ts"]] = adjustment["effective_ts"]
            for anchor, actual in candidates.items():
                key = f"{plan['id']}:{anchor}"
                if key == occurrence_id or abs(actual - effective) >= 1200:
                    continue
                try:
                    run = self.store.get("life_run", key)
                except AlbumError:
                    run = {}
                if run.get("state") not in TERMINAL:
                    return plan["name"], actual
        return None

    @managed
    def adjust(
        self,
        scope: str,
        cid: str,
        occurrence_id: str,
        decision: str,
        *,
        expected_revision: int = 0,
        effective_ts: int | None = None,
        reason: str = "",
        source_ids=None,
        progress: str = "unknown",
        explicit: bool = False,
        actor: str = "automatic",
        at: int | None = None,
        allow_cross_day: bool = False,
        allow_running: bool = False,
        replacement: str = "",
        evidence_revision: int | None = None,
    ):
        at = now() if at is None else int(at)
        if (
            decision not in DECISIONS
            or progress not in PROGRESS
            or actor not in ("chat", "user", "automatic")
        ):
            raise AlbumError("本次调整格式不正确")
        source_ids = source_ids or []
        if (
            not isinstance(source_ids, list)
            or len(source_ids) > 3
            or any(not isinstance(i, str) or len(i) > 200 for i in source_ids)
        ):
            raise AlbumError("来源引用格式不正确")
        if (
            actor != "user"
            and not explicit
            and (
                progress not in ("unknown", "planned")
                or decision in ("continue", "replace")
            )
            and not source_ids
        ):
            raise AlbumError("进展判断需要当前聊天的可靠来源")
        with self.store.lock:
            item = self.occurrence(scope, cid, occurrence_id, at=at)
            if (
                type(expected_revision) is not int
                or expected_revision != item["revision"]
            ):
                raise AlbumError("本次安排已改变，请刷新后再决定")
            if (
                item["state"] in TERMINAL
                or item["state"] == "running"
                and not allow_running
            ):
                raise AlbumError("本次已执行或正在发送，不能恢复重放")
            if item["explicit"] and not explicit and actor != "user":
                raise AlbumError("本次已有明确约定，自主判断不能覆盖")
            if (
                item["mode"] == "reminder"
                and decision not in ("keep", "continue")
                and not (explicit or actor == "user")
            ):
                raise AlbumError("按约提醒只接受用户明确修改或取消")
            if abs(item["anchor_ts"] - at) > 2 * 86400:
                raise AlbumError("仅可调整今天或明确约定的相邻日这一次")
            effective = item["effective_ts"]
            auto_delays = item["auto_delays"]
            if decision == "delay":
                if (
                    type(effective_ts) is not int
                    or effective_ts < at + 180
                    or effective_ts > at + 86400
                ):
                    raise AlbumError("新时间须在 3 分钟后、24 小时内")
                effective = effective_ts // 60 * 60
                zone = ZoneInfo(item["timezone"])
                cross_day = (
                    datetime.fromtimestamp(effective, zone).date()
                    != datetime.fromtimestamp(item["anchor_ts"], zone).date()
                )
                if cross_day and not (
                    allow_cross_day and (explicit or actor == "user")
                ):
                    raise AlbumError("跨日改点需要用户明确约定")
                if not explicit and actor != "user":
                    if auto_delays >= 1:
                        raise AlbumError("同一次安排最多自主改点一次")
                    auto_delays += 1
                conflict = self._conflict(scope, cid, item["id"], effective)
                if conflict:
                    alternative = datetime.fromtimestamp(
                        conflict[1] + 1200, zone
                    ).isoformat()
                    raise AlbumError(
                        f"与「{conflict[0]}」相距不足 20 分钟，可改到 {alternative} 后再检查"
                    )
            elif decision == "restore":
                if item["default_ts"] < at:
                    raise AlbumError("原时间已过，恢复不会补发；请另选未来时间")
                effective, progress, replacement = item["default_ts"], "unknown", ""
            elif effective < at and item["state"] not in ("running", "deferred"):
                raise AlbumError("本次时间已过，不能补发或恢复")
            if (
                not isinstance(reason, str)
                or not isinstance(replacement, str)
                or len(reason) > 500
                or len(replacement) > 2000
            ):
                raise AlbumError("本次调整内容过长")
            key = _hash([scope, cid, occurrence_id])
            saved = self.store.save(
                "life_adjustment",
                key,
                {
                    "scope": scope,
                    "cid": cid,
                    "plan_id": item["plan_id"],
                    "plan_revision": item["plan_revision"],
                    "character_id": item["character_id"],
                    "anchor_ts": item["anchor_ts"],
                    "effective_ts": effective,
                    "decision": "keep" if decision == "restore" else decision,
                    "progress": progress,
                    "reason": plain_text(reason),
                    "source_ids": source_ids,
                    "actor": actor,
                    "explicit": bool(explicit or actor == "user"),
                    "auto_delays": auto_delays,
                    "at": at,
                    "replacement": plain_text(replacement),
                    "evidence_revision": evidence_revision,
                },
                item["revision"] or None,
            )
            return self.occurrence(scope, cid, occurrence_id, at=at) | {
                "revision": saved["revision"]
            }

    def match(self, plan, timestamp, cid, calendar, base):
        """Choose effective time without minting a second occurrence identity."""
        adjustments = [
            a
            for a in self._adjustments(plan["id"])
            if a["plan_revision"] == plan["revision"]
            and a["character_id"] == plan["character_id"]
        ]
        if base:
            adjustment = next(
                (a for a in adjustments if a["anchor_ts"] == base["anchor_ts"]), None
            )
            if adjustment and adjustment["effective_ts"] != timestamp:
                base = None
            elif adjustment:
                base = {**base, "adjustment": adjustment}
        for adjustment in adjustments:
            if adjustment["effective_ts"] != timestamp:
                continue
            try:
                info = self._base(
                    plan, adjustment["anchor_ts"], adjustment["cid"], calendar
                )
            except AlbumError:
                continue
            return {**info, "adjustment": adjustment}
        if base is None:
            with self.store.lock:
                row = self.store.db.execute(
                    "SELECT data FROM records WHERE kind='life_run' AND json_extract(data,'$.plan_id')=? "
                    "AND json_extract(data,'$.state')='deferred' AND json_extract(data,'$.capacity_wait')=1 "
                    "AND json_extract(data,'$.defer_until')<=? ORDER BY json_extract(data,'$.anchor_at') LIMIT 1",
                    (plan["id"], timestamp),
                ).fetchone()
            if row:
                run = json.loads(row["data"])
                try:
                    info = self._base(
                        plan, run["anchor_at"], run["conversation_id"], calendar
                    )
                except AlbumError:
                    return None
                adjustment = next(
                    (a for a in adjustments if a["anchor_ts"] == run["anchor_at"]), {}
                )
                # Capacity waiting never overrides the same occurrence's latest
                # effective time. Keep its original run/anchor identity.
                if adjustment.get("effective_ts", info["default_ts"]) > timestamp:
                    return None
                return {
                    **info,
                    "capacity_cid": run["conversation_id"],
                    "adjustment": adjustment,
                }
        return base
