"""Durable outgoing speech and host-coordinated, idempotent history writes."""

from __future__ import annotations

from .restore_guard import managed

import asyncio
import hashlib
import json
import re
import time

from astrbot.core.utils.session_lock import session_lock_manager

from .store import AlbumError, now

KIND = "proactive_message"
PREFIX = "lumen-proactive:v1:"
DAY = 86400
RECOVERABLE = ("kind='proactive_message' AND (json_extract(data,'$.state')='sending' OR "
               "(json_extract(data,'$.state')='sent' AND json_extract(data,'$.history_state') IN ('pending','orphaned') "
               "AND COALESCE(json_extract(data,'$.chat_deleted_at'),0)=0))")
HAS_BODY = "kind='proactive_message' AND COALESCE(json_extract(data,'$.body_compacted'),0)!=1"
OWN_TOOLS = frozenset({
    "memory_search", "memory_read", "album_today", "album_plan_adjust",
    "album_search", "album_favorite", "album_save_scene", "album_generate",
    "album_send", "album_publish_qzone", "album_plan_schedule",
    "album_plan_cancel", "album_schedule_trip",
    "album_qq_profile", "album_qq_status", "album_qq_status_read", "album_qq_poke", "album_qq_avatar",
})
_ENVELOPE_OR_JSON = re.compile(
    r'\[Tool result for (?P<tool>[A-Za-z_][A-Za-z_0-9]*)\]\s*:\s*|[\[{"]'
)


def clean_tool_echo(text: str) -> str:
    """Remove complete owned envelopes, never JSON examples or other payloads."""
    decoder = json.JSONDecoder()
    cursor = kept_from = 0
    parts = []
    while match := _ENVELOPE_OR_JSON.search(text, cursor):
        tool = match.group("tool")
        try:
            _, end = decoder.raw_decode(text, match.end() if tool else match.start())
        except RecursionError:
            # A deeply nested value is not a verified envelope. Preserve its
            # tail without repeatedly decoding each nested opening bracket.
            break
        except ValueError:
            cursor = match.end()
            continue
        # Consume the complete JSON before looking for another envelope. A
        # quoted envelope inside normal JSON or another tool is only data.
        cursor = end
        if tool in OWN_TOOLS:
            parts.append(text[kept_from:match.start()])
            kept_from = end
            while kept_from < len(text) and text[kept_from] in "\r\n ":
                kept_from += 1
            cursor = kept_from
    return ("".join(parts) + text[kept_from:]).strip() if parts else text


def body_fingerprint(text: str) -> str:
    """Versioned identity of public speech; never a readable replacement body."""
    return "sha256:v1:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def body_matches(record: dict, text: str) -> bool:
    if not isinstance(text, str):
        return False
    saved = record.get("body_fingerprint")
    return saved == body_fingerprint(text) if saved else record.get("text") == text


def host_body(record: dict, history: list) -> str:
    """Only the exact sent checkpoint and adjacent original body are evidence."""
    expected = marker(record)
    positions = [i for i, item in enumerate(history) if isinstance(item, dict)
                 and item.get("role") == "_checkpoint"
                 and isinstance(item.get("content"), dict)
                 and item["content"].get("id") == expected]
    if len(positions) != 1 or positions[0] == 0:
        return ""
    previous = history[positions[0] - 1]
    if not isinstance(previous, dict) or previous.get("role") != "assistant" or previous.get("tool_calls"):
        return ""
    text = previous.get("content")
    if isinstance(text, list):
        if not all(isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
                   and not part.get("_no_save") for part in text):
            return ""
        text = "\n".join(part["text"] for part in text)
    return text if isinstance(text, str) and body_matches(record, text) else ""


def clean_speech(value: str, *, public=False) -> str:
    """Clean new public speech; keep legacy normalization at persistence boundaries.

    Only the new-output parser opts into public mode. Already determined bodies
    re-entering prepare/recovery must retain their original identity and wording.
    """
    if not isinstance(value, str) or len(value) > 16000:
        raise AlbumError("待发正文格式或长度不正确")
    if public:
        value = clean_tool_echo(value)
    value = re.sub(
        r"(?is)<(?:think(?:ing)?|photo_scene|follow_up|plan_decision|schedule_decision)\b[^>]*>.*?</(?:think(?:ing)?|photo_scene|follow_up|plan_decision|schedule_decision)>",
        "",
        value,
    )
    media = "" if public else "[媒体数据已省略]"
    link = "" if public else "[带参数链接已省略]"
    value = re.sub(r"data:[^\s,]+;base64,[A-Za-z0-9+/=]+", media, value)
    value = re.sub(r"base64://[A-Za-z0-9+/=]+", media, value)
    value = re.sub(r"https?://\S+\?\S+", link, value)
    return value.strip()


def marker(record: dict) -> str:
    return (
        f"{PREFIX}{record.get('generation', -1)}:"
        f"{record.get('sent_at', 0)}:{record['id']}"
    )


def tail_fingerprint(history, count):
    return hashlib.sha256(
        json.dumps(
            history[max(0, count - 2) : count], sort_keys=True, ensure_ascii=False
        ).encode()
    ).hexdigest()


def parse_marker(item: dict) -> dict | None:
    if not isinstance(item, dict) or item.get("role") != "_checkpoint":
        return None
    content = item.get("content")
    value = content.get("id", "") if isinstance(content, dict) else ""
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return None
    try:
        generation, at, key = value[len(PREFIX) :].split(":", 2)
        generation, at = int(generation), int(at)
        if not key or len(key) > 160 or not 0 < at < 32503680000:
            return None
    except (ValueError, TypeError):
        return None
    return {"key": key, "generation": generation, "at": at, "checkpoint": value}


class ProactiveHistory:
    """Serialize delivery with the host's final save, independently of memory.

    Model generation happens before this boundary. ``deliver`` is for background
    tasks only: a host tool already holding the session lock must use ``finish``
    and ``request_sync`` instead of recursively awaiting this lock.
    """

    def __init__(self, store, context, memory_runtime=None):
        self.store, self.context = store, context
        self.memory_runtime = memory_runtime
        self.tasks = {}
        self.inflight = set()
        with store.lock, store.db:
            store.db.execute("CREATE INDEX IF NOT EXISTS jobs_proactive_origin ON jobs(json_extract(data,'$.origin_proactive_id'))")
            store.db.execute("CREATE INDEX IF NOT EXISTS records_proactive_recover ON records(id) WHERE " + RECOVERABLE)
            store.db.execute("CREATE INDEX IF NOT EXISTS records_proactive_bodies ON records(id) WHERE " + HAS_BODY)
        if memory_runtime:
            memory_runtime.proactive = self

    def get(self, key):
        return self.store.get(KIND, key)

    def list(self, *, scope="", cid="", job_id="", plan_id="", run_id="", limit=100):
        clauses, args = ["kind=?"], [KIND]
        for field, value in (
            ("scope", scope),
            ("cid", cid),
            ("job_id", job_id),
            ("plan_id", plan_id),
            ("run_id", run_id),
        ):
            if value:
                clauses.append(f"json_extract(data,'$.{field}')=?")
                args.append(value)
        args.append(max(1, min(200, int(limit))))
        with self.store.lock:
            rows = self.store.db.execute(
                "SELECT id,data,revision FROM records WHERE "
                + " AND ".join(clauses)
                + " ORDER BY json_extract(data,'$.created') DESC,rowid DESC LIMIT ?",
                args,
            ).fetchall()
        return [
            {**json.loads(r["data"]), "id": r["id"], "revision": r["revision"]}
            for r in rows
        ]

    @managed
    def prepare(
        self,
        key,
        scope,
        cid,
        character_id,
        text,
        *,
        kind="text",
        asset_id="",
        generation=None,
        job_id="",
        plan_id="",
        run_id="",
    ):
        if not all(isinstance(x, str) and x for x in (key, scope, cid, character_id)):
            raise AlbumError("待发消息缺少固定聊天或角色归属")
        if len(key) > 160 or kind not in (
            "text",
            "caption",
            "photo",
            "notice",
            "started",
        ):
            raise AlbumError("待发消息标识或类型不正确")
        body = clean_speech(text)
        if not body:
            raise AlbumError("待发正文为空")
        payload = {
            "scope": scope,
            "cid": cid,
            "character_id": character_id,
            "text": body,
            "kind": kind,
            "asset_id": str(asset_id),
            "generation": generation if type(generation) is int else -1,
            "job_id": str(job_id),
            "plan_id": str(plan_id),
            "run_id": str(run_id),
        }
        with self.store.lock:
            try:
                old = self.get(key)
            except AlbumError:
                old = None
            if old:
                if not body_matches(old, body) or any(old.get(k) != v for k, v in payload.items() if k != "text"):
                    raise AlbumError("同一业务消息的正文或归属已变化，不能覆盖")
                return old
            return self.store.save(
                KIND,
                key,
                {
                    **payload,
                    "body_fingerprint": body_fingerprint(body),
                    "created": now(),
                    "state": "prepared",
                    "sent_at": 0,
                    "history_state": "waiting",
                    "history_error": "",
                    "receipt": {},
                },
            )

    async def snapshot(self, scope, cid):
        async with session_lock_manager.acquire_lock(scope):
            return await self._conversation(scope, cid)

    async def original_text(self, key):
        record = self.get(key)
        if record.get("state") != "sent":
            return ""
        if not record.get("body_compacted"):
            return record.get("text", "")
        try:
            conv = await self.snapshot(record["scope"], record["cid"])
            history = json.loads(conv.history or "[]")
            return host_body(record, history) if isinstance(history, list) else ""
        except Exception:
            return ""

    @managed
    def observe_saved(self, scope, cid, history):
        """A caller holding the host lock supplies freshly read persisted history.

        Before memory can remove a sent marker, durably acknowledge the exact
        host write. A failed acknowledgement aborts that trim; a mere backup or
        a missing marker never proves the host consumed a message.
        """
        keys = {m["key"] for item in history if (m := parse_marker(item))}
        for key in keys:
            try:
                record = self.get(key)
            except AlbumError:
                continue
            if record.get("scope") != scope or record.get("cid") != cid or record.get("state") != "sent":
                continue
            if not host_body(record, history):
                continue
            if record.get("history_state") != "saved":
                self._saved(record)

    def _saved(self, record):
        return self.store.save(KIND, record["id"], {
            **record, "history_state": "saved", "history_error": "",
            "history_saved_at": now(), "history_proof": {
                "scope": record["scope"], "cid": record["cid"],
                "checkpoint": marker(record),
                "body_fingerprint": record.get("body_fingerprint") or body_fingerprint(record["text"]),
            },
        }, record["revision"])

    def trim_confirmed(self, scope, cid, turn):
        parsed = parse_marker({"role": "_checkpoint", "content": {"id": turn.get("checkpoint", "")}})
        if not parsed:
            return True
        try:
            record = self.get(parsed["key"])
        except AlbumError:
            return True  # An imported unrelated backup keeps the existing memory policy.
        if record.get("scope") != scope or record.get("cid") != cid:
            return True
        return (record.get("state") == "sent" and record.get("history_state") == "saved"
                and parsed["checkpoint"] == marker(record) and body_matches(record, turn["assistant"]))

    async def _conversation(self, scope, cid):
        conv = await self.context.conversation_manager.get_conversation(scope, cid)
        if conv is None or conv.cid != cid or conv.user_id != scope:
            raise AlbumError("原聊天已删除或归属不符")
        return conv

    @managed
    def finish(self, key, state, receipt=None, *, confirmed_not_sent=False):
        """Persist only a transport result; this method never acquires host locks."""
        if state not in ("sent", "failed", "unknown", "held", "skipped"):
            raise AlbumError("发送状态不正确")
        old = self.get(key)
        if old["state"] == "sent":
            return old
        safe_receipt = {}
        if isinstance(receipt, dict) and receipt.get("message_id") is not None:
            safe_receipt["message_id"] = str(receipt["message_id"])[:160]
        return self.store.save(
            KIND,
            key,
            {
                **old,
                "state": state,
                "receipt": safe_receipt,
                "sent_at": now() if state == "sent" else 0,
                "sent_order": time.time_ns() if state == "sent" else 0,
                "history_state": "pending" if state == "sent" else "not_sent",
                "not_sent_confirmed": old.get("not_sent_confirmed", False) or (
                    old["state"] == "prepared" and state in ("failed", "held", "skipped")
                ) or (confirmed_not_sent is True and state in ("failed", "held", "skipped")),
            },
            old["revision"],
        )

    @managed
    async def deliver(self, key, sender, *, preflight=None):
        record = self.get(key)
        self.store.restore_guard.check_chat(record["scope"], record["cid"])
        # Maintenance must never classify an in-process send as a crashed send.
        async with session_lock_manager.acquire_lock("proactive-delivery:" + key):
            self.inflight.add(key)
            try:
                return await self._deliver(key, sender, preflight=preflight)
            finally:
                self.inflight.discard(key)

    async def _deliver(self, key, sender, *, preflight=None):
        """Run a final preflight/transport callback under the host's session lock."""
        record = self.get(key)
        async with session_lock_manager.acquire_lock(record["scope"]):
            record = self.get(key)
            if record["state"] != "prepared":
                if record["state"] == "sent":
                    await self._sync_locked(record)
                return record["state"], record.get("receipt", {})
            current = await self.context.conversation_manager.get_curr_conversation_id(
                record["scope"]
            )
            try:
                conv = await self._conversation(record["scope"], record["cid"])
                bound = self.store.character_for(record["scope"])
                if current != record["cid"] or bound["id"] != record["character_id"]:
                    raise AlbumError("等待发送期间会话或角色已变化")
                if preflight:
                    replacement = await preflight(record)
                    if replacement is not None:
                        body = clean_speech(replacement)
                        if not body:
                            raise AlbumError("待发正文为空")
                        if body != record["text"]:
                            record = self.store.save(KIND, key, {
                                **record, "text": body, "body_fingerprint": body_fingerprint(body),
                            }, record["revision"])
            except AlbumError:
                self.finish(key, "held")
                raise
            history = json.loads(conv.history or "[]")
            if not isinstance(history, list):
                raise AlbumError("聊天历史格式不正确，暂未发送")
            self.store.save(
                KIND,
                key,
                {
                    **record,
                    "state": "sending",
                    "history_anchor": {
                        "count": len(history),
                        "tail": tail_fingerprint(history, len(history)),
                    },
                },
                record["revision"],
            )
            try:
                state, receipt = await sender()
            except BaseException:
                self.finish(key, "unknown")
                raise
            # A returned failed result is the adapter's explicit no-send receipt;
            # exceptions and absent receipts take the unknown path above.
            record = self.finish(key, state, receipt, confirmed_not_sent=state == "failed")
            if state == "sent":
                await self._sync_locked(record)
            return state, record["receipt"]

    @managed
    async def _sync_locked(self, record):
        self.store.restore_guard.check_chat(record["scope"], record["cid"])
        if record["state"] != "sent" or record["history_state"] == "saved" or record.get("chat_deleted_at"):
            return record
        with self.store.lock:
            generation = None
            if self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name='mem_chats'").fetchone():
                row = self.store.db.execute("SELECT generation FROM mem_chats WHERE scope=? AND cid=?", (record["scope"], record["cid"])).fetchone()
                generation = row[0] if row else None
            mismatch = generation is not None and record.get("generation", -1) >= 1 and record["generation"] != generation
            if record.get("restore_obsolete") or mismatch:
                reason = "聊天已恢复，原投递历史待人工核对"
                if record.get("history_error") == reason:
                    return record
                return self.store.save(KIND, record["id"], {**record, "history_error": reason}, record["revision"])
        try:
            conv = await self.context.conversation_manager.get_conversation(record["scope"], record["cid"])
            if conv is None:
                return self.store.save(KIND, record["id"], {
                    **record, "history_state": "orphaned", "history_error": "原聊天已确认删除，停止补存",
                    "chat_deleted_at": record.get("chat_deleted_at") or now(),
                }, record["revision"])
            if conv.cid != record["cid"] or conv.user_id != record["scope"]:
                raise AlbumError("原聊天归属不符")
        except Exception:
            return self.store.save(
                KIND,
                record["id"],
                {
                    **record,
                    "history_state": "pending",
                    "history_error": "原聊天暂时无法读取，可重试保存历史",
                },
                record["revision"],
            )
        try:
            history = json.loads(conv.history or "[]")
            if not isinstance(history, list):
                raise ValueError("invalid history")
            expected = marker(record)
            matches = [
                i
                for i, item in enumerate(history)
                if isinstance(item, dict)
                and item.get("role") == "_checkpoint"
                and isinstance(item.get("content"), dict)
                and item["content"].get("id") == expected
            ]
            if matches:
                # A crash after the host commit must not append a duplicate.
                for index in matches:
                    if index == 0 or history[index - 1].get("role") != "assistant":
                        raise ValueError("invalid source marker")
                if not host_body(record, history):
                    raise ValueError("source body does not match")
            else:
                if record.get("body_compacted"):
                    return record  # A lightweight tombstone can never recreate speech.
                # Retry at the original send position when the retained prefix
                # still exists, keeping later replies after the outgoing speech.
                position = len(history)
                anchor = record.get("history_anchor", {})
                count = anchor.get("count")
                if (
                    type(count) is int
                    and 0 <= count <= len(history)
                    and tail_fingerprint(history, count) == anchor.get("tail")
                ):
                    position = count
                while position + 1 < len(history):
                    following = parse_marker(history[position + 1])
                    if not following or history[position].get("role") != "assistant":
                        break
                    try:
                        following_record = self.get(following["key"])
                    except AlbumError:
                        break
                    if following_record.get("sent_order", 0) > record.get(
                        "sent_order", 0
                    ):
                        break
                    position += 2
                history[position:position] = [
                    {"role": "assistant", "content": record["text"]},
                    {"role": "_checkpoint", "content": {"id": expected}},
                ]
                await self.context.conversation_manager.update_conversation(
                    record["scope"],
                    record["cid"],
                    history=history,
                )
            record = self._saved(record)
        except Exception:
            # Keep the confirmed transport state. Never re-enter the sender.
            fresh = self.get(record["id"])
            return self.store.save(
                KIND,
                record["id"],
                {
                    **fresh,
                    "history_state": "pending",
                    "history_error": "历史保存失败，可重试保存",
                },
                fresh["revision"],
            )
        runtime = self.memory_runtime
        if runtime:
            try:
                if runtime.store.state(record["scope"], record["cid"])["enabled"]:
                    runtime.capture(record["scope"], record["cid"], history)
            except Exception:
                # The next normal memory capture retries independently.
                pass
        return record

    @managed
    async def retry(self, key):
        record = self.get(key)
        async with session_lock_manager.acquire_lock(record["scope"]):
            return await self._sync_locked(self.get(key))

    def request_sync(self, key):
        """Schedule after a host-owned tool returns, without recursive locking."""
        if key not in self.tasks:
            task = asyncio.create_task(self.retry(key))
            self.tasks[key] = task

            def completed(finished):
                self.tasks.pop(key, None)
                if not finished.cancelled():
                    finished.exception()

            task.add_done_callback(completed)
        return self.tasks[key]

    @managed
    async def recover(self, limit=100):
        results = []
        for key in self._page("recover", limit):
            if key in self.inflight or key in self.tasks:
                continue
            try:
                record = self.get(key)
                if self.store.restore_guard.pending("memory", scope=record["scope"], cid=record["cid"]):
                    continue
                if record["state"] == "sending":
                    results.append(self.finish(record["id"], "unknown"))
                elif record["state"] == "sent" and record.get("history_state") in ("pending", "orphaned") and not record.get("chat_deleted_at"):
                    results.append(await self.retry(record["id"]))
            except Exception:
                results.append({"id": key, "history_state": "pending", "history_error": "历史补存检查失败，可稍后重试"})
        return results

    def _page(self, lane, limit):
        """Bound both rows scanned and bodies loaded; cursor survives replacements/restart."""
        limit = max(1, min(200, int(limit)))
        with self.store.lock, self.store.db:
            try:
                cursor = self.store.get("proactive_cursor", lane)
            except AlbumError:
                cursor = {}
            last = cursor.get("last", "")
            predicate = RECOVERABLE if lane == "recover" else HAS_BODY
            rows = self.store.db.execute(
                "SELECT id FROM records WHERE " + predicate + " AND id>? ORDER BY id LIMIT ?", (last, limit)
            ).fetchall()
            if len(rows) < limit and last:
                rows += self.store.db.execute(
                    "SELECT id FROM records WHERE " + predicate + " AND id<=? ORDER BY id LIMIT ?", (last, limit - len(rows))
                ).fetchall()
            if rows:
                self.store.save("proactive_cursor", lane, {"last": rows[-1]["id"]}, cursor.get("revision"))
            return [row["id"] for row in rows]

    def _active_dependency(self, record):
        """Called under the same Store lock as reserve_job and compaction."""
        terminal = "('sent','failed','held','skipped','cancelled')"
        if self.store.db.execute(
            "SELECT 1 FROM jobs j WHERE (j.id=? OR json_extract(j.data,'$.origin_proactive_id')=?) AND ("
            "j.state NOT IN ('ready','failed','cancelled') OR "
            "(json_extract(j.data,'$.deliver')=1 AND NOT ("
            "COALESCE(json_extract(j.data,'$.notification_state'),'')='complete' OR "
            "(json_extract(j.data,'$.notification_state') IS NULL AND COALESCE("
            "json_extract(j.data,CASE WHEN j.state='ready' THEN '$.delivery' ELSE '$.notice' END),'') IN " + terminal + "))) OR "
            "COALESCE(json_extract(j.data,'$.qzone_delivery'),'') IN ('pending','prepared','sending','unknown') OR "
            "EXISTS(SELECT 1 FROM deliveries d WHERE json_extract(d.data,'$.job_id')=j.id AND d.state NOT IN "
            + terminal + ")) LIMIT 1", (record.get("job_id", ""), record["id"])
        ).fetchone():
            return True
        # A scheduled parent may still create its photo/follow-up after sending.
        if record.get("run_id"):
            try:
                run = self.store.get("life_run", record["run_id"])
            except AlbumError:
                return True
            if run.get("state") not in ("sent", "failed", "cancelled", "held", "skipped", "expired", "unknown"):
                return True
            if run.get("state") == "unknown":
                return True
        return False

    @managed
    def compact(self, limit=20, *, at=None):
        """Compact only duplicate bodies after a durable, continuously valid grace period."""
        at = now() if at is None else int(at)
        compacted = []
        for key in self._page("compact", limit):
            with self.store.lock, self.store.db:
                record = self.get(key)
                if record.get("body_compacted"):
                    continue
                reason, retention = "", 0
                if key not in self.inflight and key not in self.tasks and not self._active_dependency(record):
                    if record.get("state") == "sent" and record.get("history_state") == "saved":
                        reason, retention = "saved", DAY
                    elif record.get("chat_deleted_at") and record.get("state") not in ("prepared", "sending", "unknown"):
                        reason, retention = "deleted_chat", 7 * DAY
                    elif record.get("not_sent_confirmed") and record.get("state") in ("failed", "held", "skipped", "cancelled"):
                        reason, retention = "not_sent", 7 * DAY
                old_reason = record.get("body_eligible_reason", "")
                since = record.get("body_eligible_at", 0)
                if reason != old_reason or reason and not since:
                    record = self.store.save(KIND, key, {
                        **record, "body_eligible_reason": reason, "body_eligible_at": at if reason else 0,
                    }, record["revision"])
                    since = at
                if reason and at - since >= retention:
                    self.store.save(KIND, key, {
                        **record, "body_fingerprint": record.get("body_fingerprint") or body_fingerprint(record["text"]),
                        "text": "", "body_compacted": True, "body_compacted_at": at,
                    }, record["revision"])
                    compacted.append(key)
        return compacted

    @managed
    async def maintain(self, limit=20):
        recovered = await self.recover(limit)
        return {"recovered": len(recovered), "compacted": len(self.compact(limit))}

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
