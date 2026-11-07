"""Image descriptions and sidecar links; no image bytes or URLs are persisted."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import OrderedDict

from .models import IMAGE_BATCH_MAX, IMAGE_REQUEST_BYTES, IMAGE_INPUT_TOKENS, IMAGE_OUTPUT_TOKENS

from .text import (
    clean_image_description, history_turns, image_ref, image_url,
    image_body_hash, iter_image_markers, replace_image_markers, persisted_turn_images,
    verified_image_spans,
)

MAX_BATCH = 100
MAX_TASKS = 8
TIMEOUT = 60
UNKNOWN = "[图片，内容未识别]"
UNSAFE_REASON = "旧图片说明边界不明，无法安全还原；原文备份已保留"


def image_batches(refs, metadata, window=32000):
    """A deterministic manual plan using the same hard media bounds as summaries."""
    # 1024 tokens cover the instructions/JSON; 5% is the existing window margin.
    count_limit = min(IMAGE_BATCH_MAX, max(0, (int(window * .95) - 1024) //
                                          (IMAGE_INPUT_TOKENS + IMAGE_OUTPUT_TOKENS)))
    batches, batch, used, blocked = [], [], 0, []
    for ref in dict.fromkeys(refs):
        size = metadata.get(ref, {}).get("bytes", 0)
        if not size:
            continue
        if not count_limit or size + 16384 > IMAGE_REQUEST_BYTES:
            blocked.append(ref)
            continue
        if batch and (len(batch) >= count_limit or used + size + 16384 > IMAGE_REQUEST_BYTES):
            batches.append(batch)
            batch, used = [], 0
        batch.append(ref)
        used += size
    if batch:
        batches.append(batch)
    return batches, blocked


def ambiguous_legacy_media(value):
    """No raw part/span means a stray bracket cannot be assigned to media or prose."""
    markers = list(iter_image_markers(value))
    legacy = [marker for _, _, marker in markers if marker.startswith("[图片描述：")]
    if not legacy:
        return False
    # An old alt may close early and open another image-looking token. Without
    # proven spans, multiple tokens cannot prove where that alt actually ended.
    if len(markers) > 1:
        return True
    if any(marker.count("[") != marker.count("]") for marker in legacy):
        return True
    # A second closing bracket outside recognized tokens can be the real end of
    # an old alt. Do not guess which subsequent words/lines were ordinary text.
    return "]" in replace_image_markers(value, lambda _: "")


def turn_images(turn):
    """Only original image parts that actually survive the backup renderer."""
    return persisted_turn_images(turn)


class ImageIndex:
    def __init__(self, store, context):
        self.store, self.context = store, context
        self.tasks = {}
        self._current = OrderedDict()
        self._results = OrderedDict()
        self._gate = asyncio.Semaphore(1)
        self._closed = False
        with store.transaction():
            store.db.execute(
                "CREATE TABLE IF NOT EXISTS mem_images("
                "scope TEXT NOT NULL,cid TEXT NOT NULL,ref TEXT NOT NULL,"
                "description TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'pending',"
                "attempts INTEGER NOT NULL DEFAULT 0,updated INTEGER NOT NULL,"
                "PRIMARY KEY(scope,cid,ref))"
            )
            store.db.execute(
                "CREATE TABLE IF NOT EXISTS mem_turn_images("
                "turn_id INTEGER NOT NULL,side TEXT NOT NULL,position INTEGER NOT NULL,ref TEXT NOT NULL,"
                "start_pos INTEGER NOT NULL DEFAULT -1,end_pos INTEGER NOT NULL DEFAULT -1,"
                "PRIMARY KEY(turn_id,side,position))"
            )
            columns = {row[1] for row in store.db.execute("PRAGMA table_info(mem_turn_images)")}
            for name in ("source", "body_hash"):
                if name not in columns:
                    store.db.execute(f"ALTER TABLE mem_turn_images ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def refs(turns):
        return {image_ref(url) for turn in turns for _, _, p in turn_images(turn) if (url := image_url(p))}

    def batch_refs(self, scope, cid, turns):
        """Return pending image refs occurring in these frozen backup turns.

        This is deliberately synchronous and contains no media data.  Planning can
        use it for a bounded request estimate; ``current_image_batch`` must be
        called immediately before a model request to obtain the current URLs.
        """
        ids = [t.get("id") for t in turns if t.get("id")]
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        with self.store.lock:
            rows = self.store.db.execute(
                "SELECT DISTINCT l.ref FROM mem_turn_images l "
                "JOIN mem_images i ON i.scope=? AND i.cid=? AND i.ref=l.ref "
                f"WHERE l.turn_id IN ({marks}) AND l.ref<>'' "
                "ORDER BY l.turn_id,l.side,l.position",
                (scope, cid, *ids),
            ).fetchall()
        refs = [row["ref"] for row in rows if re.fullmatch(r"[a-f0-9]{32}", row["ref"])]
        ready = self.ready(scope, cid, refs)
        return [ref for ref in refs if ref not in ready]

    def _image_version(self, scope, cid, ref):
        """A non-secret version token for optimistic joint-result commits."""
        with self.store.lock:
            image = self.store.db.execute(
                "SELECT status,attempts,description FROM mem_images "
                "WHERE scope=? AND cid=? AND ref=?", (scope, cid, ref)
            ).fetchone()
            links = self.store.db.execute(
                "SELECT l.turn_id,l.side,l.position,l.ref,l.start_pos,l.end_pos,l.source,l.body_hash "
                "FROM mem_turn_images l JOIN mem_turns t ON t.id=l.turn_id "
                "WHERE t.scope=? AND t.cid=? AND l.ref=? ORDER BY l.turn_id,l.side,l.position",
                (scope, cid, ref),
            ).fetchall()
        data = [tuple(image) if image else (), [tuple(row) for row in links]]
        return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()[:24]

    async def _media(self, scope, cid, *, refs=None, turns=None, urls=False):
        """Extract scoped media evidence, releasing the decoded host history."""
        history = await self._history(scope, cid)
        current = history_turns(history)
        resolved = self.store.resolve_capture(scope, cid, current)["resolved"]
        wanted = set(refs) if refs is not None else None
        ids = {t["id"] for t in turns} if turns is not None else None
        candidates, result = set(), {}
        for index, turn in enumerate(current):
            row = resolved.get(index)
            for side, position, part in turn_images(turn):
                url = image_url(part)
                if not url:
                    continue
                ref = image_ref(url)
                if wanted is not None and ref not in wanted:
                    continue
                if ids is None or (row and row["id"] in ids):
                    candidates.add(ref)
                item = result.setdefault(ref, {
                    "bytes": len(json.dumps(url, ensure_ascii=False).encode("utf-8")) + 160,
                    "locations": [], "version": self._image_version(scope, cid, ref),
                })
                # The backup identity resolver decides ownership.  For unarchived
                # manual media, the occurrence and its rendered body are evidence.
                item["locations"].append([
                    row["id"] if row else None, index, side, position,
                    hashlib.sha256(json.dumps([turn["user"], turn["assistant"]], ensure_ascii=False).encode()).hexdigest()[:24],
                ])
                if urls:
                    item["url"] = url
        return {ref: item for ref, item in result.items() if ref in candidates}

    async def inventory(self, scope, cid, turns=None):
        """Current image sizes and identity evidence only; no URLs are retained."""
        return await self._media(scope, cid, turns=turns)

    async def current_image_batch(self, scope, cid, refs, *, metadata=None,
                                  max_images=8, max_bytes=8 * 1024 * 1024):
        """Freeze at most one bounded request and distinguish missing from excess."""
        wanted = tuple(dict.fromkeys(
            r for r in refs if isinstance(r, str) and re.fullmatch(r"[a-f0-9]{32}", r)
        ))
        current = await self.inventory(scope, cid)
        ready = self.ready(scope, cid, wanted)
        selected, skipped, overflow = [], [], []
        total = 0
        for ref in wanted:
            item = current.get(ref)
            if not item or ref in ready:
                skipped.append(ref)
                continue
            if metadata is not None and item != metadata.get(ref):
                raise ValueError("图片范围已变化，请重新整理")
            size = item["bytes"]
            if len(selected) >= max_images or total + size > max_bytes:
                overflow.append(ref)
                continue
            selected.append(ref)
            total += size
        # The second read retains URLs for selected refs only.  Check its evidence
        # again: media can change during either host IO await.
        material = await self._media(scope, cid, refs=selected, urls=True) if selected else {}
        items = []
        for ref in selected:
            item = material.get(ref)
            if not item or {k: v for k, v in item.items() if k != "url"} != current[ref]:
                raise ValueError("图片范围已变化，请重新整理")
            items.append({"id": f"image-{len(items) + 1}", "ref": ref, **item})
        return {"images": items, "versions": {r: current[r]["version"] for r in selected},
                "skipped": skipped, "overflow": overflow, "bytes": total}

    async def batch_valid(self, scope, cid, items):
        """Recheck source ownership after awaiting a model, without retaining media."""
        current = await self.inventory(scope, cid)
        return all(current.get(item["ref"]) == {
            k: item[k] for k in ("bytes", "locations", "version")
        } for item in items)

    def commit_image_batch(self, scope, cid, items, descriptions, versions=None):
        """Atomically apply a complete, version-matched description batch.

        Returns ``False`` when any input is missing, unsafe, or stale.  The caller
        can then leave the summary batch pending and retain every original image.
        """
        if not isinstance(items, list) or not isinstance(descriptions, dict):
            return False
        expected = {item.get("ref"): item for item in items if isinstance(item, dict)}
        versions = versions if versions is not None else {ref: item.get("version") for ref, item in expected.items()}
        if not expected or set(expected) != set(descriptions) or set(expected) != set(versions):
            return False
        cleaned = {}
        for ref, value in descriptions.items():
            if not isinstance(ref, str) or not re.fullmatch(r"[a-f0-9]{32}", ref):
                return False
            value = clean_image_description(value)
            if not value:
                return False
            cleaned[ref] = value
        with self.store.transaction():
            for ref in expected:
                if self._image_version(scope, cid, ref) != versions[ref]:
                    return False
                row = self.store.db.execute(
                    "SELECT status,description FROM mem_images WHERE scope=? AND cid=? AND ref=?",
                    (scope, cid, ref)
                ).fetchone()
                if not row or (row["status"] == "ready" and clean_image_description(row["description"])):
                    return False
            stamp = int(time.time())
            for ref, value in cleaned.items():
                self.store.db.execute(
                    "UPDATE mem_images SET description=?,status='ready',updated=? "
                    "WHERE scope=? AND cid=? AND ref=?",
                    (value, stamp, scope, cid, ref),
                )
        return True

    def register(self, scope, cid, turns, resolved):
        """Register images and reliable links, without rewriting any backup text."""
        added = set()
        stamp = int(time.time())
        with self.store.transaction():
            self.store._ensure(scope, cid)
            for index, turn in enumerate(turns):
                entry = resolved["entries"][index] if "entries" in resolved else None
                row = entry["row"] if entry else resolved.get(index)
                match = entry["media_match"] if entry else self.store.describe_match(row, turn)
                compatible = row and not row.get("pruned") and (match["same_body"] or match["same_legacy"])
                for side, position, part in turn_images(turn):
                    url = image_url(part)
                    ref = image_ref(url)
                    description = clean_image_description(part.get("description") or part.get("alt"))
                    if ref:
                        cursor = self.store.db.execute(
                            "INSERT OR IGNORE INTO mem_images(scope,cid,ref,description,status,updated) VALUES(?,?,?,?,?,?)",
                            (scope, cid, ref, description, "ready" if description else "pending", stamp),
                        )
                        if cursor.rowcount:
                            added.add(ref)
                    if compatible:
                        start, end, source, body_hash = -1, -1, "", ""
                        for candidate_source, evidence in turn.get("image_evidence", {}).items():
                            if evidence["bodies"][side] != row[side]:
                                continue
                            span = evidence["spans"][side].get(position)
                            if span and 0 <= span[0] < span[1] <= len(row[side]):
                                start, end = span
                                source, body_hash = candidate_source, image_body_hash(row[side])
                                break
                        if not ref and not source:
                            continue
                        self.store.db.execute(
                            "INSERT OR IGNORE INTO mem_turn_images(turn_id,side,position,ref,start_pos,end_pos,source,body_hash) VALUES(?,?,?,?,?,?,?,?)",
                            (row["id"], side, position, ref, start, end, source, body_hash),
                        )
                        if source:
                            # Source-free old offsets are replaced only by an exact
                            # reconstruction, never by a normalized digest match.
                            self.store.db.execute(
                                "UPDATE mem_turn_images SET start_pos=?,end_pos=?,source=?,body_hash=? WHERE turn_id=? AND side=? AND position=?",
                                (start, end, source, body_hash, row["id"], side, position),
                            )
                        if row["status"] == "pending":
                            # An image edit changes the sidecar version, not the
                            # backup's words or its proven original media span.
                            self.store.db.execute(
                                "UPDATE mem_turn_images SET ref=? WHERE turn_id=? AND side=? AND position=?",
                                (ref, row["id"], side, position),
                            )
        self.observe(scope, cid, self.refs(turns))
        return added

    def ready(self, scope, cid, refs):
        result = {}
        refs = list(set(refs))
        with self.store.lock:
            for start in range(0, len(refs), 400):
                chunk = refs[start : start + 400]
                marks = ",".join("?" * len(chunk))
                for row in self.store.db.execute(
                    f"SELECT ref,description FROM mem_images WHERE scope=? AND cid=? AND status='ready' AND ref IN ({marks})",
                    (scope, cid, *chunk),
                ):
                    value = clean_image_description(row["description"])
                    if value:
                        result[row["ref"]] = value
        return result

    def project(self, scope, cid, turns):
        """Freeze safe text for both planning and prompting; original rows stay intact."""
        ids = [t["id"] for t in turns if t.get("id")]
        links, descriptions = {}, {}
        with self.store.lock:
            for start in range(0, len(ids), 400):
                chunk = ids[start : start + 400]
                marks = ",".join("?" * len(chunk))
                for row in self.store.db.execute(
                    "SELECT l.*,i.description,i.status FROM mem_turn_images l "
                    "JOIN mem_turns t ON t.id=l.turn_id "
                    "LEFT JOIN mem_images i ON i.scope=t.scope AND i.cid=t.cid AND i.ref=l.ref "
                    f"WHERE t.scope=? AND t.cid=? AND l.turn_id IN ({marks})",
                    (scope, cid, *chunk),
                ):
                    links.setdefault((row["turn_id"], row["side"]), []).append(dict(row))
            # New backups contain short refs even if their raw media has disappeared.
            short_refs = {
                m.group(1) for t in turns for side in ("user", "assistant")
                for m in re.finditer(r"\[图片#([a-f0-9]{12})", t.get(side, ""))
            }
            for short in short_refs:
                rows = self.store.db.execute(
                    "SELECT description,status FROM mem_images WHERE scope=? AND cid=? AND ref LIKE ? LIMIT 2",
                    (scope, cid, short + "%"),
                ).fetchall()
                if len(rows) == 1 and rows[0]["status"] == "ready":
                    descriptions[short] = clean_image_description(rows[0]["description"])
        projected = []

        def replace_unverified(marker):
            short = re.match(r"\[图片#([a-f0-9]{12})", marker)
            if short:
                description = descriptions.get(short[1], "")
            elif marker.startswith(("[图片描述：", "[图片：")):
                description = clean_image_description(marker.split("：", 1)[1][:-1])
            else:
                description = ""
            return f"[图片：{description}]" if description else UNKNOWN

        for turn in turns:
            copy = dict(turn)
            for side in ("user", "assistant"):
                value = turn.get(side, "")
                spans = verified_image_spans(value, links.get((turn.get("id"), side), ()))
                gaps, offset = [], 0
                for span in spans:
                    gaps.append(value[offset:span["start_pos"]])
                    offset = span["end_pos"]
                gaps.append(value[offset:])
                # Validate the remaining text as one ordered whole. A genuine
                # image between two old fragments cannot certify their boundary.
                residual = "\n".join(gaps)
                split_marker = any(
                    marker.count("[") != marker.count("]")
                    for gap in gaps for _, _, marker in iter_image_markers(gap)
                )
                if ambiguous_legacy_media(residual) or (spans and split_marker):
                    copy[side] = "[旧图片说明边界不明，此段内容未加入]"
                    copy["_image_unsafe"] = True
                    continue
                parts = []
                for gap, span in zip(gaps, spans):
                    parts.append(replace_image_markers(gap, replace_unverified))
                    if span["ref"]:
                        description = clean_image_description(span["description"]) if span["status"] == "ready" else ""
                        parts.append(f"[图片：{description}]" if description else UNKNOWN)
                    else:
                        parts.append(replace_unverified(value[span["start_pos"]:span["end_pos"]]))
                parts.append(replace_image_markers(gaps[-1], replace_unverified))
                copy[side] = "".join(parts)
            projected.append(copy)
        return projected

    def _rows(self, scope, cid, refs):
        result = {}
        refs = list(set(refs))
        with self.store.lock:
            for start in range(0, len(refs), 400):
                chunk = refs[start : start + 400]
                marks = ",".join("?" * len(chunk))
                result.update({r["ref"]: ("pending" if r["status"] == "ready" and not clean_image_description(r["description"]) else r["status"], r["attempts"]) for r in self.store.db.execute(
                    f"SELECT ref,status,attempts,description FROM mem_images WHERE scope=? AND cid=? AND ref IN ({marks})",
                    (scope, cid, *chunk),
                )})
        return result

    def observe(self, scope, cid, refs):
        """Cache only a bounded candidate list and counts, never URLs/history."""
        rows = self._rows(scope, cid, refs)
        candidates = sorted(ref for ref in refs if rows.get(ref, ("pending",))[0] != "ready")
        key = (scope, cid)
        self._current.pop(key, None)
        self._current[key] = {
            "refs": tuple(candidates[:MAX_BATCH]),
            "unregistered": sum(ref not in rows for ref in refs),
            "window_unknown": sum(ref not in rows for ref in candidates[:MAX_BATCH]),
            "current_pending": len(candidates),
        }
        while len(self._current) > 32:
            self._current.popitem(last=False)

    def status(self, scope, cid):
        current = self._current.get((scope, cid), {})
        rows = self._rows(scope, cid, current.get("refs", ()))
        refs = [ref for ref in current.get("refs", ()) if rows.get(ref, ("pending",))[0] != "ready"]
        with self.store.lock:
            counts = dict(self.store.db.execute(
                "SELECT status,count(*) FROM mem_images WHERE scope=? AND cid=? GROUP BY status", (scope, cid)
            ).fetchall())
        return {
            **{state: counts.get(state, 0) for state in ("ready", "pending", "failed")},
            "running": (scope, cid) in self.tasks,
            "unregistered": current.get("unregistered", 0) - current.get("window_unknown", 0) + sum(ref not in rows for ref in current.get("refs", ())),
            "current_pending": current.get("current_pending", 0) - len(current.get("refs", ())) + len(refs),
            "available": len(refs), "refs": refs,
            "batch_count": current.get("batch_count"),
            "max_requests": current.get("batch_count"),
            "blocked_images": current.get("blocked_images", 0),
            "result": self._results.get((scope, cid)),
        }

    async def _history(self, scope, cid):
        conv = await self.context.conversation_manager.get_conversation(scope, cid)
        if conv is None or conv.user_id != scope or conv.cid != cid:
            return []
        try:
            value = json.loads(conv.history or "[]")
            return value if isinstance(value, list) else []
        except (ValueError, TypeError):
            return []

    async def preview(self, scope, cid, *, window=32000):
        metadata = await self.inventory(scope, cid)
        self.observe(scope, cid, set(metadata))
        status = self.status(scope, cid)
        batches, blocked = image_batches(status["refs"], metadata, window)
        self._current[(scope, cid)].update(batch_count=len(batches), blocked_images=len(blocked))
        return self.status(scope, cid)

    def start(self, scope, cid, refs, model_provider, *, manual=False, window=32000):
        key = (scope, cid)
        refs = tuple(dict.fromkeys(r for r in refs if isinstance(r, str) and re.fullmatch(r"[a-f0-9]{32}", r)))[:MAX_BATCH]
        if self._closed or key in self.tasks or len(self.tasks) >= MAX_TASKS or not refs:
            return False
        self._results.pop(key, None)
        task = asyncio.create_task(self._run(scope, cid, refs, model_provider, manual, window))
        self.tasks[key] = task

        def done(_):
            if self.tasks.get(key) is task:
                self.tasks.pop(key, None)

        task.add_done_callback(done)
        return True

    async def _run(self, scope, cid, refs, model_provider, manual, window):
        completed, failed, skipped, requests = 0, 0, 0, 0
        cancelled = False
        reason = "模型请求或返回内容不可用"
        try:
            initial = self.store.state(scope, cid)
            initial_settings = initial["settings"]
            binding = initial_settings.get("model_bindings", {}).get("summary") or {}
            if not initial["enabled"] or not initial_settings.get("image_describe", True) or binding.get("mode") != "preset" or not binding.get("preset_id"):
                return
            selected_model = model_provider(initial_settings) if callable(model_provider) else model_provider
            frozen_binding = dict(binding)
            frozen_config = json.dumps(getattr(selected_model, "config", {}), sort_keys=True)

            def config_valid():
                current = self.store.state(scope, cid)
                settings = current["settings"]
                if self._closed or not current["enabled"] or not settings.get("image_describe", True):
                    return False
                if settings.get("model_bindings", {}).get("summary") != frozen_binding:
                    return False
                model = model_provider(settings) if callable(model_provider) else model_provider
                return json.dumps(getattr(model, "config", {}), sort_keys=True) == frozen_config

            metadata = await self.inventory(scope, cid)
            rows = self._rows(scope, cid, refs)
            candidates = [ref for ref in refs if ref in metadata and
                          rows.get(ref, ("pending", 0))[0] != "ready" and
                          (manual or not (rows.get(ref, ("pending", 0))[0] == "failed" or
                                          rows.get(ref, ("pending", 0))[1]))]
            skipped = len(refs) - len(candidates)
            batches, blocked = image_batches(candidates, metadata, window)
            failed += len(blocked)
            if blocked:
                reason = "图片超过本批请求限制，原图已保留"
            for group in batches:
                async with self._gate:
                    if not config_valid():
                        return
                    request = await self.current_image_batch(scope, cid, group, metadata=metadata)
                    if request["overflow"]:
                        raise ValueError("图片超过本批请求限制")
                    items = request["images"]
                    skipped += len(request["skipped"])
                    if not items:
                        continue
                    if not config_valid():
                        return
                    # Claim exactly this bounded batch.  Cancellation/failure stays
                    # failed; no worker automatically requeues or falls back to singles.
                    with self.store.transaction():
                        for item in items:
                            self.store.db.execute(
                                "INSERT INTO mem_images(scope,cid,ref,status,attempts,updated) VALUES(?,?,?,'failed',1,?) "
                                "ON CONFLICT(scope,cid,ref) DO UPDATE SET status='failed',attempts=attempts+1,updated=excluded.updated",
                                (scope, cid, item["ref"], int(time.time())),
                            )
                            item["version"] = self._image_version(scope, cid, item["ref"])
                    try:
                        requests += 1
                        descriptions = await asyncio.wait_for(
                            selected_model.describe_images(items, timeout=TIMEOUT,
                                                  max_tokens=max(256, len(items) * IMAGE_OUTPUT_TOKENS)),
                            timeout=TIMEOUT,
                        )
                        if not isinstance(descriptions, dict) or set(descriptions) != {i["id"] for i in items}:
                            raise ValueError
                        descriptions = {i["ref"]: descriptions[i["id"]] for i in items}
                        if not await self.batch_valid(scope, cid, items) or not config_valid():
                            raise ValueError
                        if not self.commit_image_batch(scope, cid, items, descriptions):
                            raise ValueError
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        failed += len(items)
                        reason = "模型服务超时" if isinstance(error, (TimeoutError, asyncio.TimeoutError)) else "模型请求或返回内容不可用"
                    else:
                        completed += len(items)
                    finally:
                        # Never hold the last media payload while loading another batch.
                        items.clear()
                        request.clear()
                        item = None
        except asyncio.CancelledError:
            cancelled = True
            self.store.log(scope, cid, "image_failed", "图片描述已取消，未完成图片可手动补描述")
            raise
        except Exception:
            self.store.log(scope, cid, "image_failed", "图片描述本地处理失败")
        finally:
            key = (scope, cid)
            self._results.pop(key, None)
            self._results[key] = {
                "requested": len(refs), "described": completed, "failed": failed,
                "skipped": skipped, "remaining": len(refs) - completed - failed - skipped,
                "cancelled": cancelled, "requests": requests,
            }
            while len(self._results) > 32:
                self._results.popitem(last=False)
            if completed:
                self.store.log(scope, cid, "image_described", f"已描述{completed}张图片，跳过{skipped}张")
            if failed:
                self.store.log(scope, cid, "image_failed", f"{failed}张图片描述失败：{reason}")

    async def close(self):
        self._closed = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        self._current.clear()
        self._results.clear()
