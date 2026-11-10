"""Private maintenance receipts and conservative per-item provenance.

All writes use the existing memory savepoint; no summary progress is changed.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid

from ..store import AlbumError
from . import core

COLLECTIONS = ("events", "people", "profiles", "plans", "overview", "keywords")
ACTIVE = {"queued", "reading", "requesting", "validating", "committing"}
PAYLOAD_LIMIT = 1024 * 1024
TOTAL_BYTES = 128 * 1024 * 1024
MAX_SOURCES = 200
RETENTION = 30 * 86400


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value):
    return hashlib.sha256(packed(value).encode()).hexdigest()


def ident(item):
    return item["collection"], item["id"]


def project_items(memory):
    result = {(c, item["id"]): item for c in COLLECTIONS[:4] for item in memory[c]}
    result.update({(c, c): memory[c] for c in COLLECTIONS[4:]})
    return result


def error(code):
    exc = AlbumError(code)
    exc.code = code
    return exc


class MaintenanceStore:
    def __init__(self, store):
        self.store, self.db = store, store.db
        self._cleanup_after = (0, "")
        with store.transaction():
            for table, column, ddl in (
                ("mem_chats", "maintenance_epoch", "TEXT NOT NULL DEFAULT ''"),
                ("mem_batches", "maintenance_claim", "TEXT NOT NULL DEFAULT ''"),
            ):
                if column not in {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            self.db.execute("UPDATE mem_chats SET maintenance_epoch=lower(hex(randomblob(16))) WHERE maintenance_epoch=''")
            self.db.execute("CREATE TABLE IF NOT EXISTS mem_item_guards(scope TEXT NOT NULL,cid TEXT NOT NULL,epoch TEXT NOT NULL,"
                "collection TEXT NOT NULL,item_id TEXT NOT NULL,version INTEGER NOT NULL,content_hash TEXT NOT NULL,data TEXT NOT NULL,"
                "PRIMARY KEY(scope,cid,epoch,collection,item_id))")
            self.db.execute("CREATE TABLE IF NOT EXISTS mem_maintenance_runs(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,scope TEXT NOT NULL,"
                "cid TEXT NOT NULL,epoch TEXT NOT NULL,trigger TEXT NOT NULL,state TEXT NOT NULL,created INTEGER NOT NULL,updated INTEGER NOT NULL,"
                "committed_at INTEGER NOT NULL DEFAULT 0,undone_at INTEGER NOT NULL DEFAULT 0,payload TEXT NOT NULL)")
            self.db.execute("CREATE INDEX IF NOT EXISTS mem_maintenance_chat ON mem_maintenance_runs(scope,cid,created)")
            self.db.execute("UPDATE mem_maintenance_runs SET state='interrupted',updated=? WHERE state IN ('queued','reading','requesting','validating','committing')",
                (int(time.time()),))

    def _epoch(self, scope, cid):
        row = self.store._ensure(scope, cid)
        if not row["maintenance_epoch"]:
            self.db.execute("UPDATE mem_chats SET maintenance_epoch=? WHERE scope=? AND cid=?", (uuid.uuid4().hex, scope, cid))
            row = self.store._row(scope, cid)
        return row["maintenance_epoch"]

    def _guard(self, scope, cid, epoch, collection, item_id, value):
        row = self.db.execute("SELECT * FROM mem_item_guards WHERE scope=? AND cid=? AND epoch=? AND collection=? AND item_id=?",
            (scope, cid, epoch, collection, item_id)).fetchone()
        if row:
            data = json.loads(row["data"])
            return {"collection": collection, "id": item_id, "version": row["version"], "hash": row["content_hash"], **data}
        return {"collection": collection, "id": item_id, "version": 1, "hash": fingerprint(value), "sources": [],
            "source_complete": False, "legacy_unverified": True, "protected": False, "last_writer": {"kind": "legacy", "id": ""}, "dependencies": []}

    def _put_guard(self, scope, cid, epoch, item):
        data = {k: copy.deepcopy(item[k]) for k in ("sources", "source_complete", "legacy_unverified", "protected", "last_writer", "dependencies")}
        self.db.execute("INSERT INTO mem_item_guards VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(scope,cid,epoch,collection,item_id) "
            "DO UPDATE SET version=excluded.version,content_hash=excluded.content_hash,data=excluded.data",
            (scope, cid, epoch, item["collection"], item["id"], item["version"], item["hash"], packed(data)))

    def items(self, scope, cid, keys=None, *, readonly=False):
        with self.store.lock:
            row = self.store._row(scope, cid) if readonly else self.store._ensure(scope, cid)
            if row is None:
                return {"epoch": "", "revision": 0, "items": [], "source_upper": 0, "paused": False}
            epoch = row["maintenance_epoch"] if readonly else self._epoch(scope, cid)
            memory = project_items(core.normalized(json.loads(row["memory"])))
            selected = list(memory) if keys is None else [ident(k) for k in keys]
            items = [{**self._guard(scope, cid, epoch, *key, memory[key]), "value": copy.deepcopy(memory[key])}
                     for key in selected if key in memory]
            if readonly:
                return {"epoch": epoch, "revision": row["revision"], "items": items,
                    "paused": bool(json.loads(row["settings"]).get("maintenance_paused", False))}
            upper = self.db.execute("SELECT coalesce(max(id),0) FROM mem_turns WHERE scope=? AND cid=?", (scope, cid)).fetchone()[0]
        return {"epoch": epoch, "revision": row["revision"], "items": items, "source_upper": upper}

    def changed(self, scope, cid, batches, *, current_only=False):
        result = {}
        with self.store.lock:
            for batch in batches:
                row = self.db.execute("SELECT diff FROM mem_batches WHERE scope=? AND cid=? AND id=? AND state='applied'", (scope, cid, batch)).fetchone()
                if not row:
                    continue
                change = json.loads(row["diff"])
                for collection, values in change.get("items", {}).items():
                    for item_id, _, after in values:
                        if after is not None:
                            result[(collection, item_id)] = {"collection": collection, "id": item_id}
                for name in COLLECTIONS[4:]:
                    if name in change:
                        result[(name, name)] = {"collection": name, "id": name}
            if current_only:
                current = {ident(item) for item in self.items(scope, cid, readonly=True)["items"]}
                result = {key: value for key, value in result.items() if key in current}
        return list(result.values())

    def detail(self, scope, cid, collection, item_id, *, epoch, version, content_hash):
        """One current memory, never retained conversation sources or snapshots."""
        if (collection not in COLLECTIONS or not isinstance(item_id, str) or not item_id or len(item_id) > 256
                or not isinstance(epoch, str) or not epoch or len(epoch) > 100
                or type(version) is not int or version < 1 or not isinstance(content_hash, str) or len(content_hash) != 64):
            raise error("PROTOCOL")
        with self.store.lock:
            snapshot = self.items(scope, cid, [{"collection": collection, "id": item_id}], readonly=True)
            if snapshot["epoch"] != epoch:
                raise error("STALE")
            if not snapshot["items"]:
                raise error("ITEM_MISSING")
            item = snapshot["items"][0]
            if item["version"] != version or item["hash"] != content_hash or fingerprint(item["value"]) != content_hash:
                raise error("STALE")
            value = item["value"]
            references = []
            if isinstance(value, dict):
                # Participants are names in core's schema, not reference IDs.
                # Keep them in value; absence of an identically named ID proves nothing.
                references.extend(("events", event) for event in value.get("causes", []) + value.get("events", []))
                if value.get("person"):
                    references.append(("people", value["person"]))
                references.extend(("people", link.get("to")) for link in value.get("relationships", []))
            relations = []
            for group, identifier in dict.fromkeys(references):
                row = self.db.execute("SELECT j.value FROM mem_chats c,json_each(c.memory,?) j "
                    "WHERE c.scope=? AND c.cid=? AND json_extract(j.value,'$.id')=? LIMIT 1",
                    ("$." + group, scope, cid, identifier)).fetchone()
                referenced = json.loads(row[0]) if row else {}
                label = referenced.get("name") or referenced.get("title") or identifier
                relations.append({"collection": group, "id": identifier, "label": label, "missing": row is None})
            return {"scope": scope, "cid": cid, "epoch": epoch, "revision": snapshot["revision"],
                **{k: item[k] for k in ("collection", "id", "version", "hash", "value")}, "relations": relations}

    def people_candidates(self, scope, cid, query="", offset=0, limit=20):
        """Discover bounded same-chat references, never grant write authority."""
        if (not isinstance(query, str) or len(query) > 100 or type(offset) is not int or not 0 <= offset <= 1000
                or type(limit) is not int or limit < 1):
            raise error("PROTOCOL")
        with self.store.lock:
            rows = self.db.execute(
                "SELECT c.maintenance_epoch,j.value FROM mem_chats c,json_each(c.memory,'$.people') j "
                "WHERE c.scope=? AND c.cid=? AND (instr(coalesce(json_extract(j.value,'$.name'),''),?)>0 "
                "OR EXISTS(SELECT 1 FROM json_each(j.value,'$.aliases') a WHERE instr(a.value,?)>0)) "
                "ORDER BY json_extract(j.value,'$.id') LIMIT ? OFFSET ?",
                (scope, cid, query, query, min(20, limit), offset)).fetchall()
            result = []
            for row in rows:
                value = json.loads(row["value"])
                guard = self._guard(scope, cid, row["maintenance_epoch"], "people", value["id"], value)
                result.append({"id": value["id"], "name": value.get("name", ""), "aliases": value.get("aliases", []),
                    "version": guard["version"], "hash": guard["hash"]})
            return result

    def manual_sources(self, scope, cid, items, batches, upper):
        """One-task evidence from explicitly selected creation diffs only.

        This never reclassifies a legacy item as AI-authored or updates guards.
        A later diff touching unknown old content cannot prove its full origin.
        """
        if (not isinstance(batches, (list, tuple)) or len(batches) > 120
                or any(type(i) is not int or i < 1 for i in batches)
                or type(upper) is not int or upper < 0):
            raise error("PROTOCOL")
        result = copy.deepcopy(items)
        if not batches:
            return result
        with self.store.lock:
            current = {ident(i): i for i in self.items(scope, cid, items)["items"]}
            candidates = {}
            for batch in dict.fromkeys(batches):
                row = self.db.execute("SELECT first,last,diff FROM mem_batches WHERE scope=? AND cid=? AND id=? AND state='applied'",
                    (scope, cid, batch)).fetchone()
                if not row or row["last"] - row["first"] + 1 > MAX_SOURCES:
                    continue
                turns = self.db.execute("SELECT id,ordinal,status,pruned FROM mem_turns WHERE scope=? AND cid=? AND batch=? ORDER BY ordinal LIMIT ?",
                    (scope, cid, batch, MAX_SOURCES + 1)).fetchall()
                if (not turns or len(turns) != row["last"] - row["first"] + 1
                        or [t["ordinal"] for t in turns] != list(range(row["first"], row["last"] + 1))
                        or any(t["id"] > upper or t["status"] != "summarized" or t["pruned"] for t in turns)):
                    continue
                for collection, changes in json.loads(row["diff"]).get("items", {}).items():
                    if collection not in COLLECTIONS[:4]:
                        continue
                    for item_id, before, after in changes:
                        key = collection, item_id
                        actual = current.get(key)
                        if before is None and actual and after == actual["value"]:
                            candidates.setdefault(key, set()).update(t["id"] for t in turns)
            for item in result:
                actual, sources = current.get(ident(item)), candidates.get(ident(item), set())
                if (actual and sources and len(sources) <= MAX_SOURCES and item["version"] == actual["version"]
                        and item["hash"] == actual["hash"] == fingerprint(actual["value"])):
                    item.update(sources=sorted(sources), source_complete=True)
        return result

    def record_write(self, scope, cid, before, after, *, kind, writer="", sources=()):
        """Called inside the actual writer transaction, including indirect edits."""
        epoch = self._epoch(scope, cid)
        old, new = project_items(before), project_items(after)
        for key in old.keys() | new.keys():
            if old.get(key) == new.get(key):
                continue
            guard = self._guard(scope, cid, epoch, *key, old.get(key))
            new_ai = kind == "summary" and key not in old
            all_sources = sorted(set(guard["sources"]) | set(sources)) if kind == "summary" else guard["sources"]
            self._put_guard(scope, cid, epoch, {**guard, "version": guard["version"] + 1,
                "hash": fingerprint(new.get(key)), "sources": all_sources[:MAX_SOURCES],
                "source_complete": (new_ai or guard["source_complete"]) and len(all_sources) <= MAX_SOURCES and bool(all_sources),
                "legacy_unverified": False if new_ai else guard["legacy_unverified"],
                "protected": guard["protected"] or kind == "manual", "last_writer": {"kind": kind, "id": writer},
                "dependencies": sorted(set(guard["dependencies"]) | ({writer} if kind == "summary" else set()))})

    def replace_epoch(self, scope, cid):
        epoch = uuid.uuid4().hex
        self.db.execute("UPDATE mem_chats SET maintenance_epoch=? WHERE scope=? AND cid=?", (epoch, scope, cid))
        self.db.execute("DELETE FROM mem_item_guards WHERE scope=? AND cid=?", (scope, cid))
        self.db.execute("UPDATE mem_batches SET maintenance_claim='' WHERE scope=? AND cid=? AND maintenance_claim IN "
            "(SELECT id FROM mem_maintenance_runs WHERE scope=? AND cid=? AND state='expired')", (scope, cid, scope, cid))
        self.db.execute("DELETE FROM mem_maintenance_runs WHERE scope=? AND cid=? AND state='expired'", (scope, cid))
        self.db.execute("UPDATE mem_maintenance_runs SET state='stale' WHERE scope=? AND cid=? AND state IN ('queued','reading','requesting','validating','committing')", (scope, cid))
        return epoch

    def protect(self, scope, cid, collection, item_id, version, protected, *, epoch):
        if type(protected) is not bool or type(version) is not int or not isinstance(epoch, str):
            raise error("PROTOCOL")
        with self.store.transaction():
            self.store.restore_guard.check_chat(scope, cid)
            snapshot = self.items(scope, cid, [{"collection": collection, "id": item_id}])
            if snapshot["epoch"] != epoch or not snapshot["items"] or snapshot["items"][0]["version"] != version:
                raise error("STALE")
            item = snapshot["items"][0]
            if item["protected"] != protected:
                self._put_guard(scope, cid, snapshot["epoch"], {**item, "version": version + 1, "protected": protected,
                    "last_writer": {"kind": "protect", "id": ""}})
                self.db.execute("UPDATE mem_chats SET revision=revision+1,updated=? WHERE scope=? AND cid=?", (int(time.time()), scope, cid))
        return self.items(scope, cid, [{"collection": collection, "id": item_id}])

    def claim_run(self, scope, cid, run_id, task_id, trigger, keys, *, batches=(), apply=False, allow_protected=False):
        with self.store.transaction():
            existing = self.get_run(scope, cid, run_id)
            if existing:
                return {**existing, "new_claim": False}
            self.cleanup()
            self._capacity(scope=scope, cid=cid, new_unit=run_id != task_id, new_record=True)
            epoch = self._epoch(scope, cid)
            for batch in batches if trigger == "auto" else ():
                row = self.db.execute("SELECT maintenance_claim FROM mem_batches WHERE scope=? AND cid=? AND id=? AND state='applied'", (scope, cid, batch)).fetchone()
                if not row or row[0]:
                    return None
            payload = {"keys": keys, "batches": list(batches), "apply": bool(apply), "allow_protected": bool(allow_protected),
                "counts": {}, "findings": [], "changes": [], "dependencies": [], "expired": False, "resolved": False}
            stamp = int(time.time())
            self.db.execute("INSERT INTO mem_maintenance_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, task_id, scope, cid, epoch, trigger, "queued", stamp, stamp, 0, 0, packed(payload)))
            for batch in batches if trigger == "auto" else ():
                self.db.execute("UPDATE mem_batches SET maintenance_claim=? WHERE id=?", (run_id, batch))
            return {**self.get_run(scope, cid, run_id), "new_claim": True}

    def get_run(self, scope, cid, run_id):
        with self.store.lock:
            row = self.db.execute("SELECT * FROM mem_maintenance_runs WHERE scope=? AND cid=? AND id=?", (scope, cid, run_id)).fetchone()
            if not row:
                return None
            data = dict(row)
            payload = json.loads(data.pop("payload"))
            return {**data, **payload, "committed": bool(row["committed_at"]), "payload": payload}

    def update_run(self, scope, cid, run_id, **values):
        allowed = {"state", "code", "counts", "elapsed_ms", "parameters", "expected"}
        if set(values) - allowed:
            raise error("PROTOCOL")
        with self.store.transaction():
            run = self.get_run(scope, cid, run_id)
            if not run:
                raise error("STALE")
            payload = run["payload"]
            state = values.pop("state", run["state"])
            if run["committed"] and state != run["state"]:
                state = run["state"]
            payload.update(values)
            raw = packed(payload)
            if len(raw.encode()) > PAYLOAD_LIMIT:
                raise error("CAPACITY")
            self.db.execute("UPDATE mem_maintenance_runs SET state=?,updated=?,payload=? WHERE id=?", (state, int(time.time()), raw, run_id))
        return self.get_run(scope, cid, run_id)

    def list_runs(self, scope, cid, limit=100):
        with self.store.lock:
            chat = self.store._row(scope, cid)
            epoch = chat["maintenance_epoch"] if chat else ""
            rows = self.db.execute("SELECT id FROM mem_maintenance_runs WHERE scope=? AND cid=? ORDER BY created DESC,id DESC LIMIT ?", (scope, cid, min(100, max(1, limit)))).fetchall()
            result = []
            for row in rows:
                run = self.get_run(scope, cid, row[0])
                result.append({**{k: run[k] for k in ("id", "task_id", "state", "trigger", "created", "updated", "committed_at", "undone_at")},
                    "counts": run.get("counts", {}), "code": run.get("code", ""), "parameters": run.get("parameters", {}),
                    "elapsed_ms": run.get("elapsed_ms", 0), "expires": run["created"] + RETENTION, "expired": run.get("expired", False),
                    "resolved": run.get("resolved", False), "findings": run.get("findings", []),
                    "changes": [{k: c[k] for k in ("collection", "id", "before", "after", "reason") if k in c} for c in run.get("changes", [])],
                    "sources": [{k: e[k] for k in ("turn_id", "ordinal", "batch") if k in e} for e in run.get("evidence", [])],
                    "can_undo": bool(run["committed"] and not run["undone_at"] and not run.get("expired") and run.get("changes") and run["epoch"] == epoch)})
            return result

    def resolve(self, scope, cid, run_id):
        with self.store.transaction():
            run = self.get_run(scope, cid, run_id)
            if not run or run["state"] in ACTIVE:
                raise error("STALE")
            payload = {**run["payload"], "resolved": True}
            self.db.execute("UPDATE mem_maintenance_runs SET payload=?,updated=? WHERE id=?", (packed(payload), int(time.time()), run_id))

    def source_rows(self, scope, cid, ids, upper):
        if len(ids) > MAX_SOURCES:
            raise error("CAPACITY")
        from .images import ImageIndex
        from .text import IMAGE_PRIVATE, replace_image_markers
        result = []
        remaining = 16 * 1024 * 1024
        with self.store.lock:
            projector = object.__new__(ImageIndex)
            projector.store = self.store
            for turn_id in ids:
                row = self.db.execute("SELECT t.id,t.key,t.kind,t.digest,t.ordinal,t.batch,t.pruned,t.status,b.state AS batch_state,"
                    "length(CAST(t.user AS BLOB))+length(CAST(t.assistant AS BLOB)) AS bytes FROM mem_turns t LEFT JOIN mem_batches b ON b.id=t.batch "
                    "WHERE t.scope=? AND t.cid=? AND t.id=? AND t.id<=?", (scope, cid, turn_id, upper)).fetchone()
                basic = {"turn_id": turn_id, "key": "", "kind": "", "digest": "", "ordinal": 0, "batch": None, "pruned": 1, "status": "missing", "batch_state": "missing"}
                if row:
                    basic.update({k: row[k] for k in basic if k != "turn_id"})
                text, raw_hash, image_signature, derived = "", "", "", False
                messages = []
                complete = bool(row and not row["pruned"] and row["status"] == "summarized" and row["batch_state"] == "applied" and row["bytes"] <= remaining)
                if complete:
                    remaining -= row["bytes"]
                    full = dict(self.db.execute("SELECT * FROM mem_turns WHERE id=?", (turn_id,)).fetchone())
                    raw_hash = fingerprint([full["user"], full["assistant"]])
                    image_rows = []
                    if self.store._table("mem_turn_images"):
                        image_rows = [{k: r[k] for k in r.keys() if k != "turn_id"} for r in self.db.execute("SELECT l.*,i.description,i.status,i.updated FROM mem_turn_images l "
                            "LEFT JOIN mem_images i ON i.scope=? AND i.cid=? AND i.ref=l.ref WHERE l.turn_id=? ORDER BY l.side,l.start_pos,l.end_pos", (scope, cid, turn_id))]
                    image_signature = fingerprint(image_rows)
                    projected = projector.project(scope, cid, [full])[0] if self.store._table("mem_turn_images") else full
                    text = projected["user"] + "\n" + projected["assistant"]
                    complete = not projected.get("_image_unsafe", False) and not bool(IMAGE_PRIVATE.search(text))
                    derived = bool(image_rows) or "[图片" in text or "[曾分享图片" in text
                    if not complete:
                        text = ""
                    else:
                        messages = [{"role": role, "content": projected[role]} for role in ("user", "assistant") if projected[role]]
                # Version the projection in the existing opaque hash field.
                # Old receipts remain unchanged; no new persisted field or
                # source-body migration is required for this role boundary.
                projection_hash = "messages-v1:" + fingerprint({"kind": basic["kind"], "messages": messages})
                stamp = {**basic, "raw_hash": raw_hash, "projection_hash": projection_hash, "image_signature": image_signature, "complete": complete}
                direct_messages = [{**message, "content": replace_image_markers(message["content"], lambda _: "")} for message in messages]
                direct_text = "\n".join(message["content"] for message in direct_messages)
                result.append({**basic, "id": turn_id, "text": text, "direct_text": direct_text,
                    "messages": messages, "direct_messages": direct_messages,
                    "complete": complete, "derived": derived, "evidence": stamp})
        return result

    def search_sources(self, scope, cid, ids, upper, query, offset=0, limit=20):
        # Only metadata leaves search; full reads are mandatory for proof.
        if len(ids) > MAX_SOURCES or not ids:
            return []
        marks = ",".join("?" for _ in ids)
        with self.store.lock:
            rows = self.db.execute(f"SELECT t.id,t.ordinal,t.batch FROM mem_turns t JOIN mem_batches b ON b.id=t.batch "
                f"WHERE t.scope=? AND t.cid=? AND t.id<=? AND t.id IN ({marks}) AND t.pruned=0 AND b.state='applied' "
                "AND (instr(t.user,?)>0 OR instr(t.assistant,?)>0) ORDER BY t.ordinal LIMIT ? OFFSET ?",
                (scope, cid, upper, *ids, query, query, min(20, limit), offset)).fetchall()
            return [{"turn_id": r["id"], "ordinal": r["ordinal"], "batch": r["batch"], "complete": False} for r in rows]

    def _verify_expected(self, scope, cid, expected):
        snapshot = self.items(scope, cid, expected["items"])
        if snapshot["epoch"] != expected["epoch"] or len(snapshot["items"]) != len(expected["items"]):
            raise error("STALE")
        originals = {ident(i): i for i in expected["items"]}
        for item in snapshot["items"]:
            prior = originals[ident(item)]
            if item["version"] != prior["version"] or item["hash"] != prior["hash"] or fingerprint(item["value"]) != prior["hash"]:
                raise error("STALE")
        return snapshot

    def commit(self, scope, cid, run_id, expected, changes, *, evidence=(), findings=(), counts=None, check=None):
        from .maintenance_protocol import FIELDS, candidate, dependencies as references, safe_text

        if counts is not None and (not isinstance(counts, dict) or set(counts) - {"checked", "modified", "findings", "not_checked", "calls",
                "input_tokens", "output_tokens", "tokens_estimated", "source_turns", "tool_calls", "elapsed_ms"}
                or any(type(v) is not int or not 0 <= v < 2**63 for v in counts.values())):
            raise error("PROTOCOL")
        with self.store.transaction():
            run = self.get_run(scope, cid, run_id)
            if not run:
                raise error("STALE")
            if run["committed"]:
                return run
            if run["state"] not in ACTIVE:
                raise error("STALE")
            if check:
                check()
            self.store.restore_guard.check_chat(scope, cid)
            snapshot = self._verify_expected(scope, cid, expected)
            if run["epoch"] != snapshot["epoch"]:
                raise error("STALE")
            current = {ident(i): i for i in snapshot["items"]}
            persistent_guards = dict(current)
            if run["trigger"] == "manual" and run.get("batches"):
                explicit = self.manual_sources(scope, cid, snapshot["items"], run["batches"], expected["source_upper"])
                frozen = {ident(i): i for i in expected["items"]}
                for item in explicit:
                    prior = frozen[ident(item)]
                    if (item["sources"] == prior.get("sources") and item["source_complete"] == prior.get("source_complete")):
                        current[ident(item)] = item
            evidence_map = {e["turn_id"]: e for e in evidence}
            if len(evidence_map) != len(evidence):
                raise error("PROTOCOL")
            actual = self.source_rows(scope, cid, list(evidence_map), expected["source_upper"])
            if any(row["evidence"] != evidence_map[row["turn_id"]] for row in actual):
                raise error("SOURCE_CHANGED")
            allowed = {ident(k) for k in run["keys"]}
            if len(changes) > 6 or len({ident(c) for c in changes}) != len(changes):
                raise error("PROTOCOL")
            memory = self.store.memory(scope, cid)
            receipt, dependencies = [], set()
            for change in changes:
                target = ident(change)
                if target not in allowed or target not in current or target[0] not in COLLECTIONS[:4]:
                    raise error("PROTOCOL")
                item = current[target]
                if not run["apply"] or ((item["protected"] or item["legacy_unverified"]) and not run["allow_protected"]):
                    raise error("PROTECTED")
                if not item["source_complete"] or not item["sources"] or any(s not in evidence_map or not evidence_map[s]["complete"] for s in item["sources"]):
                    raise error("SOURCE_MISSING")
                if change["before"] != item["value"] or change["version"] != item["version"] or change["hash"] != item["hash"] or change["after"].get("id") != target[1]:
                    raise error("STALE")
                if change["before"] == change["after"]:
                    continue
                safe_text(change["reason"], 300)
                supports = change.get("sources", [])
                if not supports or any(e != evidence_map.get(e.get("turn_id")) or not e.get("complete") or e["turn_id"] not in item["sources"] for e in supports):
                    raise error("SOURCE_MISSING")
                source_batches = {e["batch"] for e in supports if e.get("batch")}
                source_batch = next(iter(source_batches)) if len(source_batches) == 1 else None
                patch = {k: v for k, v in change["after"].items() if k in FIELDS[target[0]] and v != change["before"].get(k)}
                if "relationships" in patch:
                    old_rel = {r["to"]: r for r in change["before"].get("relationships", [])}
                    patch["relationships"] = [{"to": r["to"], "text": r["text"]} for r in patch["relationships"]
                        if r["to"] not in old_rel or r != old_rel[r["to"]]]
                if candidate(item, patch, current, source_batch) != change["after"]:
                    raise error("PROTOCOL")
                index = next(i for i, value in enumerate(memory[target[0]]) if value["id"] == target[1])
                memory[target[0]][index] = copy.deepcopy(change["after"])
                dependencies.update(item["dependencies"])
                dependencies.update(e["batch"] for e in evidence if e.get("batch"))
                receipt.append({**copy.deepcopy(change), "previous_writer": item["last_writer"], "after_version": item["version"] + 1})
            merged = {**current, **{ident(c): {**current[ident(c)], "value": c["after"]} for c in receipt}}
            for change in receipt:
                if change["collection"] == "events" and candidate(merged[ident(change)], {"causes": change["after"].get("causes", [])}, merged, None) != change["after"]:
                    raise error("PROTOCOL")
            if core.normalized(memory) != memory:
                raise error("PROTOCOL")
            stamp = int(time.time())
            # Undo protects the actual write/reference closure, not unrelated
            # inspected candidates in the same unit.
            undo_keys = {ident(c) for c in receipt}
            pending = [r for c in receipt for value in (c["before"], c["after"])
                for r in references({"collection": c["collection"], "value": value})]
            while pending:
                key = pending.pop()
                if key in undo_keys:
                    continue
                if key not in current:
                    raise error("PROTOCOL")
                undo_keys.add(key)
                pending.extend(references(current[key]))
            undo_expected = [{k: i[k] for k in ("collection", "id", "version", "hash")} for i in snapshot["items"] if ident(i) in undo_keys]
            for change in receipt:
                item = persistent_guards[ident(change)]
                after_hash = fingerprint(change["after"])
                self._put_guard(scope, cid, snapshot["epoch"], {**item, "version": change["after_version"], "hash": after_hash,
                    "last_writer": {"kind": "maintenance", "id": run_id}})
                for expected_item in undo_expected:
                    if ident(expected_item) == ident(change):
                        expected_item.update(version=change["after_version"], hash=after_hash)
            payload = {**run["payload"], "changes": receipt, "findings": list(findings), "evidence": list(evidence),
                "undo_expected": undo_expected, "dependencies": sorted(dependencies), "expected": {k: v for k, v in expected.items() if k != "items"}}
            payload["counts"] = {**(counts if counts is not None else payload.get("counts", {})), "modified": len(receipt), "findings": len(findings)}
            if counts is not None and "elapsed_ms" in counts:
                payload["elapsed_ms"] = counts["elapsed_ms"]
            if not receipt and not findings:
                payload = {k: payload[k] for k in ("counts", "parameters", "elapsed_ms", "batches") if k in payload}
            raw = packed(payload)
            if len(raw.encode()) > PAYLOAD_LIMIT:
                raise error("CAPACITY")
            self._capacity(max(0, len(raw.encode()) - len(packed(run["payload"]).encode())))
            if receipt:
                self.db.execute("UPDATE mem_chats SET memory=?,revision=revision+1,updated=? WHERE scope=? AND cid=?", (packed(memory), stamp, scope, cid))
            self.db.execute("UPDATE mem_maintenance_runs SET state=?,committed_at=?,updated=?,payload=? WHERE id=?",
                ("completed" if receipt or not findings else "findings_only", stamp, stamp, raw, run_id))
            return self.get_run(scope, cid, run_id)

    def undo(self, scope, cid, run_id):
        from .maintenance_protocol import candidate

        with self.store.transaction():
            self.store.restore_guard.check_chat(scope, cid)
            run = self.get_run(scope, cid, run_id)
            if not run or not run["committed"] or run.get("expired") or not run.get("changes"):
                raise error("UNDO_UNAVAILABLE")
            if run["undone_at"]:
                return run
            self._verify_expected(scope, cid, {"epoch": run["epoch"], "items": run["undo_expected"]})
            memory = self.store.memory(scope, cid)
            updates = {}
            for change in run["changes"]:
                key = ident(change)
                index = next(i for i, v in enumerate(memory[key[0]]) if v["id"] == key[1])
                if memory[key[0]][index] != change["after"]:
                    raise error("UNDO_CONFLICT")
                guard = self._guard(scope, cid, run["epoch"], *key, change["after"])
                if guard["last_writer"] != {"kind": "maintenance", "id": run_id}:
                    raise error("UNDO_CONFLICT")
                memory[key[0]][index] = copy.deepcopy(change["before"])
                updated = {**guard, "version": guard["version"] + 1, "hash": fingerprint(change["before"]),
                    "protected": True, "last_writer": change["previous_writer"]}
                self._put_guard(scope, cid, run["epoch"], updated)
                updates[key] = (change, updated)
            if core.normalized(memory) != memory:
                raise error("UNDO_CONFLICT")
            available = {key: {"collection": key[0], "id": key[1], "value": value} for key, value in project_items(memory).items()}
            for change in run["changes"]:
                if change["collection"] == "events":
                    candidate(available[ident(change)], {"causes": change["before"].get("causes", [])}, available, None)
            # Only a proven direct maintenance successor can advance an older
            # undo expectation; matching text alone is never sufficient.
            predecessors = [r[0] for r in self.db.execute("SELECT id FROM mem_maintenance_runs WHERE scope=? AND cid=? AND epoch=? "
                "AND committed_at>0 AND undone_at=0 AND id<>?", (scope, cid, run["epoch"], run_id))]
            for predecessor in predecessors:
                older = self.get_run(scope, cid, predecessor)
                if not older or older["epoch"] != run["epoch"] or older["undone_at"] or older.get("expired"):
                    continue
                payload = older["payload"]
                advanced = False
                for expected in payload.get("undo_expected", []):
                    if ident(expected) not in updates:
                        continue
                    change, updated = updates[ident(expected)]
                    if expected["version"] == change["version"] and expected["hash"] == fingerprint(change["before"]):
                        expected.update(version=updated["version"], hash=updated["hash"])
                        advanced = True
                if advanced:
                    self.db.execute("UPDATE mem_maintenance_runs SET payload=? WHERE id=?", (packed(payload), predecessor))
            stamp = int(time.time())
            self.db.execute("UPDATE mem_chats SET memory=?,revision=revision+1,updated=? WHERE scope=? AND cid=?", (packed(memory), stamp, scope, cid))
            self.db.execute("UPDATE mem_maintenance_runs SET state='undone',undone_at=?,updated=? WHERE id=?", (stamp, stamp, run_id))
            return self.get_run(scope, cid, run_id)

    def check_summary_undo(self, scope, cid, batch, before, after):
        changed = {k for k in project_items(before).keys() | project_items(after).keys() if project_items(before).get(k) != project_items(after).get(k)}
        epoch = self._epoch(scope, cid)
        for row in self.db.execute("SELECT payload FROM mem_maintenance_runs WHERE scope=? AND cid=? AND epoch=? AND committed_at>0 AND undone_at=0", (scope, cid, epoch)):
            payload = json.loads(row[0])
            if not payload.get("changes") and not payload.get("affected"):
                continue
            affected = {ident(c) for c in payload.get("changes", payload.get("affected", []))}
            if batch in payload.get("dependencies", []) or changed & affected:
                raise error("MAINTENANCE_EXPIRED" if payload.get("expired") else "MAINTENANCE_DEPENDENCY")

    def _capacity(self, incoming=0, *, scope=None, cid=None, new_unit=False, new_record=False):
        total = self.db.execute("SELECT coalesce(sum(length(CAST(payload AS BLOB))),0) FROM mem_maintenance_runs").fetchone()[0]
        if total + incoming > TOTAL_BYTES:
            raise error("CAPACITY")
        if new_unit:
            total_units, chat_units = self.db.execute("SELECT count(*),coalesce(sum(scope=? AND cid=?),0) "
                "FROM mem_maintenance_runs WHERE id<>task_id AND state<>'expired'", (scope, cid)).fetchone()
            if total_units >= 1000 or chat_units >= 100:
                raise error("CAPACITY")
        if new_record:
            total_rows, chat_rows = self.db.execute("SELECT count(*),coalesce(sum(scope=? AND cid=?),0) "
                "FROM mem_maintenance_runs WHERE state<>'expired'", (scope, cid)).fetchone()
            if total_rows >= 2000 or chat_rows >= 200:
                raise error("CAPACITY")

    def _compact(self, row, payload, stamp):
        """One marker per live chat epoch; batch claims remain deduplicated."""
        scope, cid, epoch = row["scope"], row["cid"], row["epoch"]
        marker = "expired:" + epoch
        existing = self.get_run(scope, cid, marker)
        compact = existing["payload"] if existing else {"expired": True, "affected": [], "dependencies": [], "counts": {}}
        current = project_items(self.store.memory(scope, cid))
        affected = {ident(c) for c in compact["affected"]}
        if not row["undone_at"]:
            affected.update(ident(c) for c in payload.get("changes", []))
        dependencies = set(compact["dependencies"])
        if not row["undone_at"]:
            dependencies.update(payload.get("dependencies", []))
        dependencies = [b for b in dependencies if self.db.execute("SELECT 1 FROM mem_batches WHERE scope=? AND cid=? AND id=?", (scope, cid, b)).fetchone()]
        compact.update(affected=[{"collection": c, "id": i} for c, i in sorted(affected & current.keys())], dependencies=sorted(dependencies))
        compact["counts"]["expired_units"] = compact["counts"].get("expired_units", 0) + int(row["id"] != row["task_id"])
        claims = self.db.execute("SELECT 1 FROM mem_batches WHERE maintenance_claim=? LIMIT 1", (row["id"],)).fetchone()
        linked = self.db.execute("SELECT 1 FROM mem_maintenance_runs WHERE task_id=? AND id<>? LIMIT 1", (row["id"], row["id"])).fetchone()
        raw = packed(compact)
        if len(raw.encode()) > PAYLOAD_LIMIT:
            return False
        if existing or compact["affected"] or compact["dependencies"] or claims or linked:
            self.db.execute("INSERT INTO mem_maintenance_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated=excluded.updated",
                (marker, marker, scope, cid, epoch, "cleanup", "expired", stamp, stamp, stamp, 0, raw))
            self.db.execute("UPDATE mem_batches SET maintenance_claim=? WHERE maintenance_claim=?", (marker, row["id"]))
            self.db.execute("UPDATE mem_maintenance_runs SET task_id=? WHERE task_id=? AND id<>?", (marker, row["id"], row["id"]))
            self.db.execute("UPDATE mem_item_guards SET data=json_set(data,'$.last_writer.id',?) WHERE scope=? AND cid=? AND epoch=? "
                "AND json_extract(data,'$.last_writer.kind')='maintenance' AND json_extract(data,'$.last_writer.id')=?", (marker, scope, cid, epoch, row["id"]))
            successors = self.db.execute("SELECT id FROM mem_maintenance_runs r WHERE scope=? AND cid=? AND epoch=? AND EXISTS "
                "(SELECT 1 FROM json_each(r.payload,'$.changes') j WHERE json_extract(j.value,'$.previous_writer.kind')='maintenance' "
                "AND json_extract(j.value,'$.previous_writer.id')=?) LIMIT 1000", (scope, cid, epoch, row["id"])).fetchall()
            for successor in successors:
                value = json.loads(self.db.execute("SELECT payload FROM mem_maintenance_runs WHERE id=?", (successor[0],)).fetchone()[0])
                for change in value.get("changes", []):
                    if change.get("previous_writer") == {"kind": "maintenance", "id": row["id"]}:
                        change["previous_writer"]["id"] = marker
                self.db.execute("UPDATE mem_maintenance_runs SET payload=? WHERE id=?", (packed(value), successor[0]))
        self.db.execute("DELETE FROM mem_maintenance_runs WHERE id=?", (row["id"],))
        return True

    def cleanup(self, stamp=None):
        stamp = int(time.time()) if stamp is None else stamp
        # Called under the existing maintenance transaction/lock.
        with self.store.transaction():
            self.db.execute("DELETE FROM mem_item_guards WHERE NOT EXISTS (SELECT 1 FROM mem_chats c WHERE c.scope=mem_item_guards.scope AND c.cid=mem_item_guards.cid)")
            self.db.execute("DELETE FROM mem_maintenance_runs WHERE NOT EXISTS (SELECT 1 FROM mem_chats c WHERE c.scope=mem_maintenance_runs.scope AND c.cid=mem_maintenance_runs.cid)")
            # Select small metadata pages first; never load all receipt bodies.
            rows = self.db.execute("SELECT r.id,r.task_id,r.scope,r.cid,r.epoch,r.state,r.created,r.undone_at,c.maintenance_epoch,"
                "length(CAST(r.payload AS BLOB)) AS bytes FROM mem_maintenance_runs r JOIN mem_chats c ON c.scope=r.scope AND c.cid=r.cid "
                "WHERE r.state NOT IN ('queued','reading','requesting','validating','committing','expired') "
                "AND (r.created>? OR (r.created=? AND r.id>?)) ORDER BY r.created,r.id LIMIT 200", (*self._cleanup_after[:1], *self._cleanup_after)).fetchall()
            if not rows:
                self._cleanup_after = (0, "")
                return 0
            total = self.db.execute("SELECT coalesce(sum(length(CAST(payload AS BLOB))),0) FROM mem_maintenance_runs").fetchone()[0]
            processed = 0
            for row in rows:
                chat = row["scope"], row["cid"]
                self._cleanup_after = (row["created"], row["id"])
                if self.store.restore_guard.pending("memory", scope=chat[0], cid=chat[1]):
                    continue
                # Count newer units in SQL. Oldest eligible results are removed
                # first, preserving active and restore-pending records.
                total_units, chat_units = self.db.execute("SELECT count(*),coalesce(sum(scope=? AND cid=?),0) FROM mem_maintenance_runs "
                    "WHERE id<>task_id AND state<>'expired' AND (created>? OR (created=? AND id>=?))", (*chat, row["created"], row["created"], row["id"])).fetchone()
                total_rows, chat_rows = self.db.execute("SELECT count(*),coalesce(sum(scope=? AND cid=?),0) FROM mem_maintenance_runs "
                    "WHERE state<>'expired' AND (created>? OR (created=? AND id>=?))", (*chat, row["created"], row["created"], row["id"])).fetchone()
                if row["epoch"] == row["maintenance_epoch"] and row["created"] >= stamp - RETENTION and chat_units < 100 and total_units < 1000 and chat_rows < 200 and total_rows < 2000 and total < TOTAL_BYTES:
                    continue
                payload = json.loads(self.db.execute("SELECT payload FROM mem_maintenance_runs WHERE id=?", (row["id"],)).fetchone()[0])
                if row["epoch"] != row["maintenance_epoch"]:
                    self.db.execute("UPDATE mem_batches SET maintenance_claim='' WHERE maintenance_claim=?", (row["id"],))
                    self.db.execute("DELETE FROM mem_maintenance_runs WHERE id=?", (row["id"],))
                    processed += 1
                elif self._compact(row, payload, stamp):
                    processed += 1
                total -= row["bytes"]
            # Obsolete generations cannot protect a new same-number project.
            self.db.execute("DELETE FROM mem_maintenance_runs WHERE state='expired' AND EXISTS (SELECT 1 FROM mem_chats c "
                "WHERE c.scope=mem_maintenance_runs.scope AND c.cid=mem_maintenance_runs.cid AND c.maintenance_epoch<>mem_maintenance_runs.epoch)")
            return processed
