"""Bounded, cancellable maintenance of one chat; no host histories or agents."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

from ..plan_recovery import classify, retry_delay
from ..store import AlbumError
from .maintenance_protocol import CATEGORIES, CODES, SYSTEM, MaintenanceError, candidate, dependencies, key, keys, packed, parse, quote_index, safe_text
from .text import estimate

MAX_PRIMARY, MAX_MANUAL, MAX_DEPENDENCIES = 6, 120, 24
MAX_READS, MAX_TURNS, REQUEST_BYTES = 18, 200, 16 * 1024 * 1024
TERMINAL = {"completed", "findings_only", "partial", "cancelled", "stale", "failed", "interrupted", "undone"}


def metadata(snapshot):
    return {**snapshot, "items": [{k: v for k, v in item.items() if k != "value"} for item in snapshot["items"]]}


class MemoryMaintenance:
    def __init__(self, service):
        self.service, self.store = service, service.store
        self.data = self.store.maintenance
        self.tasks, self.active, self.previews = {}, {}, {}
        self._gate = asyncio.Lock()
        self._closed = False

    def policy(self):
        return copy.deepcopy(self.store.restore_guard.store.settings.get("memory_maintenance", {}))

    def configured(self, scope, cid, *, manual=True):
        try:
            return self.service.models.resolve_maintenance(scope, cid, manual=manual)
        except AlbumError:
            raise MaintenanceError("CONNECTION_MISSING") from None

    def host_check(self, scope, cid):
        """Exact metadata only; never initialize the host or fetch history."""
        try:
            path = getattr(self.service.context.get_db(), "db_path", None)
            if not isinstance(path, (str, Path)) or str(path) == ":memory:":
                raise ValueError()
            address = Path(path).resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(address, uri=True, timeout=.05)) as db:
                exists = db.execute("SELECT 1 FROM conversations WHERE user_id=? AND conversation_id=? LIMIT 1", (scope, cid)).fetchone()
        except Exception:
            raise MaintenanceError("HOST_METADATA_UNAVAILABLE") from None
        if not exists:
            raise MaintenanceError("CHAT_DELETED")

    def preview(self, scope, cid, selection, batches, *, apply=False, allow_protected=False):
        if any(type(v) is not bool for v in (apply, allow_protected)) or not isinstance(batches, list) or len(batches) > 120 or any(type(i) is not int or i < 1 for i in batches):
            raise MaintenanceError("PROTOCOL")
        keys(selection)
        _, signature = self.configured(scope, cid)
        if not self.policy().get("enabled") or not self.store.state(scope, cid)["enabled"]:
            raise MaintenanceError("DISABLED")
        chosen = {key(k): k for k in selection}
        for item in self.data.changed(scope, cid, batches, current_only=True):
            chosen[key(item)] = {"collection": item["collection"], "id": item["id"]}
        if not chosen or len(chosen) > MAX_MANUAL:
            raise MaintenanceError("CAPACITY")
        snapshot = self.data.items(scope, cid, sorted(chosen.values(), key=key))
        if len(snapshot["items"]) != len(chosen):
            raise MaintenanceError("STALE")
        snapshot["items"] = self.data.manual_sources(scope, cid, snapshot["items"], batches, snapshot["source_upper"])
        policy = self.policy()
        frozen = {"scope": scope, "cid": cid, "expected": metadata(snapshot), "apply": apply,
                  "allow_protected": allow_protected, "created": time.monotonic(), "policy": policy,
                  "signature": signature, "batches": sorted(set(batches))}
        self.previews = {k: v for k, v in self.previews.items() if time.monotonic() - v["created"] < 300}
        if len(self.previews) >= 32:
            raise MaintenanceError("CAPACITY")
        token = uuid.uuid4().hex
        self.previews[token] = frozen
        units = (len(chosen) + MAX_PRIMARY - 1) // MAX_PRIMARY
        return {"token": token, "selected": len(chosen), "units": units, "max_calls": units * policy["max_calls"],
                "total_timeout": policy["manual_timeout"], "protected": sum(bool(i["protected"] or i["legacy_unverified"]) for i in snapshot["items"]),
                "source_missing": sum(not i["source_complete"] for i in snapshot["items"]),
                "vector_enabled": self.store.state(scope, cid)["settings"]["vector_enabled"]}

    def start(self, scope, cid, token, request_id):
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100 or not request_id.isascii() or not all(c.isalnum() or c in "-_" for c in request_id):
            raise MaintenanceError("PROTOCOL")
        task_id = hashlib.sha256(packed([scope, cid, request_id]).encode()).hexdigest()
        if self.data.get_run(scope, cid, task_id):
            return task_id
        frozen = self.previews.get(token)
        if not frozen or (frozen["scope"], frozen["cid"]) != (scope, cid) or time.monotonic() - frozen["created"] >= 300:
            raise MaintenanceError("STALE")
        if frozen["policy"] != self.policy():
            raise MaintenanceError("STALE")
        try:
            _, signature = self.configured(scope, cid)
        except AlbumError:
            raise MaintenanceError("STALE") from None
        if frozen.get("signature") != signature:
            raise MaintenanceError("STALE")
        self._enqueue(scope, cid, task_id, frozen["expected"], "manual", frozen["apply"], frozen["allow_protected"], frozen["batches"])
        self.previews.pop(token, None)
        return task_id

    def after_summary(self, scope, cid, batches):
        policy = self.policy()
        if self._closed or not policy.get("enabled") or not policy.get("auto_after_summary"):
            return
        state = self.store.state(scope, cid)
        if not state["enabled"] or state["settings"].get("maintenance_paused"):
            return
        changed = self.data.changed(scope, cid, batches)
        priority = {"plans": 0, "profiles": 1, "people": 2, "events": 3}
        changed.sort(key=lambda item: (priority.get(item["collection"], 4), item["id"]))
        if not changed:
            return
        snapshot = self.data.items(scope, cid, changed[:MAX_PRIMARY])
        snapshot["uncovered"] = max(0, len(changed) - MAX_PRIMARY)
        task_id = hashlib.sha256(packed([scope, cid, snapshot["epoch"], sorted(batches)]).encode()).hexdigest()
        try:
            self._enqueue(scope, cid, task_id, metadata(snapshot), "auto", True, False, batches)
        except MaintenanceError as exc:
            if exc.code not in {"CONNECTION_MISSING", "QUEUE_FULL", "STALE"}:
                raise
            claim = self.data.claim_run(scope, cid, task_id, task_id, "auto", changed[:MAX_PRIMARY], batches=batches, apply=True)
            if claim and claim.get("new_claim"):
                self.data.update_run(scope, cid, task_id, state="partial", code=exc.code,
                    counts={"not_checked": len(changed), "calls": 0})

    def _enqueue(self, scope, cid, task_id, expected, trigger, apply, allow_protected, batches):
        self.store.restore_guard.check_chat(scope, cid)
        self.store.restore_guard.external_allowed()
        target = (scope, cid)
        if self._closed or target in self.tasks:
            raise MaintenanceError("STALE")
        if len(self.tasks) >= 17:
            raise MaintenanceError("QUEUE_FULL")
        _, signature = self.configured(scope, cid, manual=trigger == "manual")
        items = [{"collection": i["collection"], "id": i["id"]} for i in expected["items"]]
        claim = self.data.claim_run(scope, cid, task_id, task_id, trigger, items, batches=batches,
                                   apply=apply, allow_protected=allow_protected)
        if not claim or not claim.get("new_claim") or claim.get("state") != "queued":
            return
        self.data.update_run(scope, cid, task_id, expected={k: v for k, v in expected.items() if k != "uncovered"})
        policy = self.policy()
        deadline = time.monotonic() + (policy["manual_timeout"] if trigger == "manual" else 300)
        info = {"id": task_id, "trigger": trigger, "cancelled": False, "epoch": expected["epoch"], "state": "queued"}
        self.active[target] = info
        task = asyncio.create_task(self._run(scope, cid, task_id, expected, trigger, apply, allow_protected, deadline, signature, batches))
        self.tasks[target] = task
        def finished(done):
            if done.cancelled():
                # Cancelling before the coroutine's first turn never enters
                # _run's try/finally. Persist that outcome here as well.
                try:
                    row = self.data.get_run(scope, cid, task_id)
                    if row and row["state"] not in TERMINAL and not row["committed"]:
                        self.data.update_run(scope, cid, task_id, state="cancelled", code="CANCELLED",
                            counts={"calls": 0, "not_checked": len(expected["items"]) + expected.get("uncovered", 0)})
                except Exception:
                    pass  # Receipt is retained for restart reconciliation.
            if self.tasks.get(target) is done:
                self.tasks.pop(target, None)
                self.active.pop(target, None)
        task.add_done_callback(finished)

    def cancel(self, scope, cid, task_id=None):
        target = (scope, cid)
        info = self.active.get(target)
        if info and (task_id is None or task_id == info["id"]):
            info["cancelled"] = True
            self.tasks[target].cancel()
            return True
        return False

    async def cancel_wait(self, scope, cid):
        task = self.tasks.get((scope, cid))
        self.cancel(scope, cid)
        if task and task is not asyncio.current_task():
            await asyncio.gather(task, return_exceptions=True)

    async def close(self):
        self._closed = True
        self.previews.clear()
        tasks = list(self.tasks.values())
        for info in self.active.values():
            info["cancelled"] = True
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        self.active.clear()

    async def _wait(self, awaitable, check, deadline, *, request_timeout=False):
        task = asyncio.ensure_future(awaitable)
        try:
            while not task.done():
                check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if request_timeout:
                        raise TimeoutError()
                    raise MaintenanceError("TIME_LIMIT")
                await asyncio.wait({task}, timeout=min(.1, remaining))
            check()
            return task.result()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _run(self, scope, cid, task_id, frozen, trigger, apply, allow_protected, deadline, signature, batches):
        target, total = (scope, cid), {"checked": 0, "modified": 0, "findings": 0, "not_checked": frozen.get("uncovered", 0), "calls": 0}
        started = time.monotonic()
        unit_id, acquired = task_id, False
        accounted, vector_attempted = False, False
        epoch = self.store.restore_guard.epoch
        try:
            host_checked_at = float("-inf")
            def check(force_host=False):
                nonlocal host_checked_at
                if self._closed or self.service._closed or self.active[target]["cancelled"]:
                    raise MaintenanceError("CANCELLED")
                self.store.restore_guard.check_epoch(epoch)
                self.store.restore_guard.check_chat(scope, cid)
                if self.data.items(scope, cid, [])["epoch"] != frozen["epoch"]:
                    raise MaintenanceError("STALE")
                try:
                    current = self.service.models.maintenance_signature(scope, cid, manual=trigger == "manual")
                except AlbumError:
                    raise MaintenanceError("STALE") from None
                if current != signature:
                    raise MaintenanceError("STALE")
                if force_host or time.monotonic() - host_checked_at >= 1:
                    self.host_check(scope, cid)
                    host_checked_at = time.monotonic()
            async def enter_slot():
                nonlocal acquired
                await self._gate.acquire()
                acquired = True
            await self._wait(enter_slot(), check, deadline)
            while target in self.service.tasks:
                await self._wait(asyncio.sleep(.01), check, deadline)
            client, _ = self.configured(scope, cid, manual=trigger == "manual")
            policy = self.policy()
            if trigger == "auto":
                deadline = time.monotonic() + policy["unit_timeout"]
            selections = frozen["items"]
            for offset in range(0, len(selections), MAX_PRIMARY):
                check()
                unit_id = task_id + ":" + str(offset // MAX_PRIMARY)
                accounted, vector_attempted = False, False
                unit = {**frozen, "items": selections[offset:offset + MAX_PRIMARY]}
                self.data.claim_run(scope, cid, unit_id, task_id, trigger,
                    [{"collection": i["collection"], "id": i["id"]} for i in unit["items"]],
                    batches=batches if trigger == "manual" else [], apply=apply, allow_protected=allow_protected)
                self.data.update_run(scope, cid, unit_id, expected={k: v for k, v in unit.items() if k != "uncovered"})
                self.active[target]["state"] = "reading"
                unit_deadline = min(deadline, time.monotonic() + policy["unit_timeout"])
                result = await self._unit(scope, cid, unit_id, unit, client, check, unit_deadline, policy, apply, allow_protected)
                for name in total:
                    total[name] += result.get(name, 0)
                accounted = True
                if result.get("modified"):
                    vector_attempted = True
                    self.service.refresh_vectors(scope, cid)
            self.data.update_run(scope, cid, task_id, state="completed", counts=total, elapsed_ms=int((time.monotonic() - started) * 1000))
        except asyncio.CancelledError:
            self._end(scope, cid, task_id, unit_id, "cancelled", "CANCELLED", total, frozen, started, accounted, vector_attempted)
        except Exception as exc:
            code = getattr(exc, "code", "")
            code = code if code in CODES else "STORAGE"
            if not acquired and code == "TIME_LIMIT":
                code = "QUEUE_EXPIRED"
            state = "partial" if code in {"CALL_LIMIT", "TIME_LIMIT", "CAPACITY"} else "stale" if code in {"STALE", "SOURCE_CHANGED", "CHAT_DELETED"} else "failed"
            self._end(scope, cid, task_id, unit_id, state, code, total, frozen, started, accounted, vector_attempted)
        finally:
            if acquired:
                self._gate.release()

    def _end(self, scope, cid, task_id, unit_id, state, code, counts, frozen, started, accounted, vector_attempted):
        # Query authoritative receipts after cancellation/unknown write outcome.
        current = None
        try:
            current = self.data.get_run(scope, cid, unit_id) if unit_id != task_id else None
        except Exception:
            code = "COMMIT_UNKNOWN"
        unit_counts = (current or {}).get("counts", {})
        if not accounted:
            for name in ("checked", "modified", "findings", "calls"):
                counts[name] += unit_counts.get(name, 0)
        counts["not_checked"] = len(frozen["items"]) + frozen.get("uncovered", 0) - counts["checked"]
        elapsed = int((time.monotonic() - started) * 1000)
        for identifier in dict.fromkeys([unit_id, task_id]):
            try:
                row = self.data.get_run(scope, cid, identifier)
                if row and row.get("state") not in TERMINAL and not row.get("committed"):
                    self.data.update_run(scope, cid, identifier, state=state, code=code,
                        counts=counts if identifier == task_id else {**unit_counts, "not_checked": len(row.get("keys", []))-unit_counts.get("checked", 0)}, elapsed_ms=elapsed)
            except Exception:
                pass  # Startup marks unfinished work interrupted; never resubmit.
        if not vector_attempted and current and current.get("committed") and unit_counts.get("modified"):
            try:
                self.service.refresh_vectors(scope, cid)
            except Exception:
                self.store.log(scope, cid, "maintenance_failed", packed({"code": "STORAGE", "calls": 0}))
        parameters = {k: v for k, v in (current or {}).get("parameters", {}).items()
                      if k in {"max_calls", "window", "unit_timeout", "manual_timeout"} and type(v) is int}
        self.store.log(scope, cid, "maintenance_" + state, packed({"code": code, **counts, **parameters, "elapsed_ms": elapsed}))

    async def _unit(self, scope, cid, unit_id, frozen, client, check, deadline, policy, apply, allow_protected):
        from .service import capacity
        started = time.monotonic()
        selection = [{"collection": i["collection"], "id": i["id"]} for i in frozen["items"]]
        snapshot = self.data.items(scope, cid, selection)
        run = self.data.get_run(scope, cid, unit_id)
        if run["trigger"] == "manual":
            snapshot["items"] = self.data.manual_sources(scope, cid, snapshot["items"], run.get("batches", []), frozen["source_upper"])
        if snapshot["epoch"] != frozen["epoch"]:
            raise MaintenanceError("STALE")
        for original, current in zip(frozen["items"], snapshot["items"]):
            if (key(original), original["version"], original["hash"]) != (key(current), current["version"], current["hash"]):
                raise MaintenanceError("STALE")
        if len(snapshot["items"]) != len(frozen["items"]):
            raise MaintenanceError("STALE")
        available = {key(i): i for i in snapshot["items"]}
        primary = set(available)
        people = {}
        discover_owners = any(i["collection"] == "profiles" for i in snapshot["items"])
        def discover(query="", offset=0):
            if not discover_owners:
                raise MaintenanceError("PROTOCOL")
            safe_text(query, 100)
            if type(offset) is not int or not 0 <= offset <= 1000:
                raise MaintenanceError("PROTOCOL")
            found = self.data.people_candidates(scope, cid, query, offset, 20)
            for item in found:
                prior = people.get(item["id"])
                if prior and (prior["version"], prior["hash"]) != (item["version"], item["hash"]):
                    raise MaintenanceError("STALE")
            ids = set(available) | {("people", i) for i in people} | {("people", i["id"]) for i in found}
            if len(ids - primary) > MAX_DEPENDENCIES:
                raise MaintenanceError("CAPACITY")
            people.update({i["id"]: i for i in found})
            return [{k: i[k] for k in ("id", "name", "aliases", "version")} for i in found]
        person_candidates = discover() if discover_owners else []
        source_ids = set(i for row in snapshot["items"] for i in row.get("sources", []))
        upper = frozen["source_upper"]
        source_ids = {i for i in source_ids if i <= upper}
        handles, read_ids, evidence, tool_results = {}, set(), [], []
        calls, tools_used, retried, input_tokens, output_tokens = 0, 0, False, 0, 0
        window = client.config.get("max_context_tokens") or 32000
        input_limit, output_limit = capacity(window)
        timeout = self.store.state(scope, cid)["settings"]["summary_timeout"]
        secret = client.config.get("api_key", "")
        def read(ids):
            if not isinstance(ids, list) or not ids or any(type(i) is not int or i not in source_ids or i > upper for i in ids):
                raise MaintenanceError("PROTOCOL")
            if len(read_ids | set(ids)) > MAX_TURNS:
                raise MaintenanceError("CAPACITY")
            rows = self.data.source_rows(scope, cid, ids, upper)
            result = []
            for row in rows:
                ident = row.get("turn_id", row.get("id"))
                text = packed({"kind": row["kind"], "messages": row["messages"]})
                if len(text.encode()) > REQUEST_BYTES or estimate(text) > input_limit:
                    result.append({"turn_id": ident, "complete": False, "code": "CAPACITY"})
                    continue
                stamp = row["evidence"]
                handle = hashlib.sha256(packed([unit_id, stamp]).encode()).hexdigest()[:24]
                handles[handle] = row
                if ident not in read_ids:
                    evidence.append(stamp)
                    read_ids.add(ident)
                result.append({"handle": handle, "turn_id": ident, "ordinal": row.get("ordinal"), "batch": row.get("batch"),
                    "complete": row.get("complete", False), "derived": row.get("derived", False),
                    "kind": row["kind"], "messages": row["messages"]})
            return result
        # Preload a small complete page, never load all history for a queue.
        initial = read(sorted(source_ids)[:20]) if source_ids else []
        character, user = await self.service.names(scope, self.store.state(scope, cid)["settings"])
        payload = {"items": snapshot["items"], "source_ids": sorted(source_ids), "sources": initial,
                   "speakers": {"user": user, "assistant": character}, "person_candidates": person_candidates,
                   "mode": "apply" if apply else "report", "remaining_calls": policy["max_calls"]}
        # A page is an upper bound, not a reason to exceed this connection's
        # window. Remove whole preloaded turns; the model may request them later.
        while initial and estimate(SYSTEM) + estimate(packed(payload)) > input_limit:
            removed = initial.pop()
            handle = removed.get("handle")
            if handle:
                ident = handles.pop(handle)["turn_id"]
                read_ids.discard(ident)
                evidence[:] = [e for e in evidence if e["turn_id"] != ident]
        while True:
            check(True)
            if time.monotonic() >= deadline:
                raise MaintenanceError("TIME_LIMIT")
            if calls >= policy["max_calls"]:
                raise MaintenanceError("CALL_LIMIT")
            payload["remaining_calls"] = policy["max_calls"] - calls
            payload["tool_results"] = tool_results
            prompt = packed(payload)
            tokens = estimate(SYSTEM) + estimate(prompt)
            if tokens > input_limit or len(packed({"system": SYSTEM, "prompt": prompt}).encode()) > REQUEST_BYTES:
                raise MaintenanceError("CAPACITY")
            self.data.update_run(scope, cid, unit_id, state="requesting", counts={"calls": calls + 1,
                "input_tokens": input_tokens + tokens, "output_tokens": output_tokens, "tokens_estimated": 1,
                "source_turns": len(read_ids), "tool_calls": tools_used},
                parameters={"max_calls": policy["max_calls"], "window": window, "unit_timeout": policy["unit_timeout"], "manual_timeout": policy["manual_timeout"]})
            self.active[(scope, cid)]["state"] = "requesting"
            calls += 1
            input_tokens += tokens
            try:
                request_limit = min(timeout, deadline-time.monotonic())
                response = await self._wait(client.summarize(system_prompt=SYSTEM, prompt=prompt,
                    contexts=[], timeout=request_limit, max_tokens=output_limit), check, min(deadline, time.monotonic() + timeout),
                    request_timeout=request_limit == timeout)
                check(True)
                output_tokens += estimate(response.completion_text)
                result = parse(response.completion_text)
                # Check the parsed object as a whole, including unknown fields,
                # before any private result may be persisted.
                if secret and secret in packed(result):
                    raise MaintenanceError("CONTENT_SAFETY")
                del response
            except Exception as exc:
                code = getattr(exc, "code", "")
                network_code, retryable, _ = classify(exc)
                format_error = code in {"JSON_FORMAT", "RESPONSE_JSON", "RESPONSE_FORMAT"}
                if retried or not (retryable or format_error) or calls >= policy["max_calls"]:
                    failure = network_code if network_code in {"MODEL_AUTH", "MODEL_CONFIG"} else code if code in CODES else "REQUEST_TIMEOUT" if network_code == "TIMEOUT" else "REQUEST_FAILED"
                    raise MaintenanceError(failure) from None
                retried = True
                payload["feedback"] = "JSON_FORMAT" if format_error else "REQUEST_FAILED"
                if retryable:
                    await self._wait(asyncio.sleep(retry_delay(exc, calls)), check, deadline)
                continue
            if result["type"] == "read":
                for action in result["actions"]:
                    tools_used += 1
                    if tools_used > MAX_READS or not isinstance(action, dict):
                        raise MaintenanceError("CAPACITY")
                    op = action.get("op")
                    if op == "ReadSource" and set(action) == {"op", "ids"}:
                        value = read(action["ids"])
                    elif op == "ReadMemory" and set(action) == {"op", "keys"}:
                        requested = keys(action["keys"], MAX_DEPENDENCIES)
                        allowed = set(available) | {ref for i in available.values() for ref in dependencies(i)} | {("people", i) for i in people}
                        if any(key(i) not in allowed for i in requested):
                            raise MaintenanceError("PROTOCOL")
                        extra = self.data.items(scope, cid, requested)["items"]
                        if len(extra) != len(requested):
                            raise MaintenanceError("PROTOCOL")
                        if len((set(available) | {key(i) for i in extra} | {("people", i) for i in people}) - primary) > MAX_DEPENDENCIES:
                            raise MaintenanceError("CAPACITY")
                        if any(i["collection"] == "people" and i["id"] in people and (i["version"], i["hash"]) !=
                               (people[i["id"]]["version"], people[i["id"]]["hash"]) for i in extra):
                            raise MaintenanceError("STALE")
                        if any(key(i) in available and (i["version"], i["hash"]) !=
                               (available[key(i)]["version"], available[key(i)]["hash"]) for i in extra):
                            raise MaintenanceError("STALE")
                        available.update({key(i): i for i in extra})
                        value = extra
                    elif op == "FindPeople" and set(action) == {"op", "query", "offset"}:
                        value = discover(action["query"], action["offset"])
                    elif op == "SearchSource" and set(action) == {"op", "query", "offset"}:
                        safe_text(action["query"], 100, empty=False)
                        if type(action["offset"]) is not int or not 0 <= action["offset"] <= MAX_TURNS:
                            raise MaintenanceError("PROTOCOL")
                        value = self.data.search_sources(scope, cid, sorted(source_ids), upper, action["query"], action["offset"], 20)
                    else:
                        raise MaintenanceError("PROTOCOL")
                    tool_results.append({"op": op, "result": value})
                continue
            changes, findings, checked, seen = [], [], 0, set()
            for item in result["items"]:
                if not isinstance(item, dict) or not {"collection", "id", "version", "status"} <= set(item):
                    raise MaintenanceError("PROTOCOL")
                ident = key(item)
                if ident not in primary or ident in seen or type(item["version"]) is not int or item["version"] != available[ident]["version"]:
                    raise MaintenanceError("PROTOCOL")
                seen.add(ident)
                status = item["status"]
                fields = {"collection", "id", "version", "status"} | ({"category", "reason", "patch", "evidence"} if status == "proposal" else {"category", "reason"} if status in {"finding", "not_checked"} else set())
                if status not in {"proposal", "checked", "finding", "not_checked"} or set(item) != fields:
                    raise MaintenanceError("PROTOCOL")
                checked += int(status != "not_checked")
                if status == "checked":
                    continue
                if item["category"] not in CATEGORIES:
                    raise MaintenanceError("PROTOCOL")
                safe_text(item["reason"], 300)
                reason = {"collection": ident[0], "id": ident[1], "category": item["category"], "reason": item["reason"]}
                if status != "proposal":
                    findings.append(reason)
                    continue
                refs = item["evidence"]
                if not isinstance(refs, list) or len(refs) > MAX_TURNS:
                    raise MaintenanceError("PROTOCOL")
                supports, quote_indices = [], []
                for ref in refs:
                    if not isinstance(ref, dict) or not isinstance(ref.get("handle"), str) or ref["handle"] not in handles:
                        raise MaintenanceError("PROTOCOL")
                    support = handles[ref["handle"]]
                    quote_indices.append(quote_index(support, ref))
                    supports.append(support)
                batches = {s["batch"] for s in supports if s.get("complete") and s.get("batch")}
                batch = next(iter(batches)) if len(batches) == 1 else None
                original = available[ident]
                try:
                    after = candidate(original, item["patch"], available, batch)
                except MaintenanceError as exc:
                    if exc.code != "SOURCE_MISSING":
                        raise
                    findings.append({**reason, "category": "SOURCE_MISSING"})
                    continue
                complete_ids = {e["turn_id"] for e in evidence if e.get("complete")}
                source_ok = (original["source_complete"] and supports
                    and set(original["sources"]) <= complete_ids
                    and all(s.get("complete") and s["turn_id"] in original["sources"] for s in supports)
                    and any(ref["quote"] in support["direct_messages"][index]["content"]
                            for ref, support, index in zip(refs, supports, quote_indices)))
                protected = original["protected"] or original["legacy_unverified"]
                if not source_ok or (protected and not allow_protected) or not apply:
                    findings.append({**reason, "category": "SOURCE_MISSING" if not source_ok else "PROTECTED" if protected and not allow_protected else reason["category"], "suggestion": item["patch"]})
                elif after != original["value"]:
                    changes.append({**{k: original[k] for k in ("collection", "id", "version", "hash")},
                        "before": original["value"], "after": after, "reason": item["reason"], "sources": [s["evidence"] for s in supports]})
            counts = {"checked": checked, "modified": len(changes), "findings": len(findings),
                      "not_checked": len(primary)-checked, "calls": calls, "input_tokens": input_tokens, "output_tokens": output_tokens,
                      "tokens_estimated": 1, "source_turns": len(read_ids), "tool_calls": tools_used,
                      "elapsed_ms": int((time.monotonic() - started) * 1000)}
            check()
            if time.monotonic() >= deadline:
                raise MaintenanceError("TIME_LIMIT")
            expected = {k: v for k, v in frozen.items() if k != "uncovered"}
            expected["items"] = list(available.values())
            self.active[(scope, cid)]["state"] = "committing"
            self.data.commit(scope, cid, unit_id, expected, changes, evidence=evidence, findings=findings, check=lambda: check(True), counts=counts)
            self.store.log(scope, cid, "maintenance_completed", packed({**counts,
                **{k: policy[k] for k in ("max_calls", "unit_timeout", "manual_timeout")}, "window": window}))
            return counts
