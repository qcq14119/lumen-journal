"""SQLite storage for chat memory, original-turn backups, batches and logs.

Tables (album database, shared connection and lock):
    mem_chats      one row per AstrBot conversation: settings + memory JSON
    mem_turns      original rounds, kept as backup for re-summarizing
    mem_batches    one row per applied summary, with an undo diff
    mem_results    raw model replies saved before they are applied
    mem_snapshots  whole-memory copies before destructive actions (30 days)
    mem_logs       short operational logs, no conversation bodies

Old memory tables from earlier versions are left untouched.
"""

from __future__ import annotations

import json
import hashlib
import re
import threading
import time
import uuid
from contextlib import contextmanager

from ..store import AlbumError
from . import core
from .text import digest, persisted_image_refs, reminder_time, turn_key, verified_image_spans, without_reminder


def identity_refs(turn):
    """Transient identity evidence; keep full raw refs without changing saved keys."""
    if "identity_refs" in turn:
        return tuple(turn["identity_refs"])
    if "messages" not in turn:
        return tuple(turn.get("image_refs", ()))
    return persisted_image_refs(turn)

SNAPSHOT_DAYS = 30
DEFAULTS = {
    "model_bindings": {},
    "summary_trigger_turns": 50,
    "keep_turns": 10,
    "memory_tokens": 50000,
    "retention_days": 30,
    "summary_max_retries": 3,
    "summary_timeout": 180,
    "summary_prompt": "",
    "user_name": "",
    "auto_process": True,
    "inject_enabled": True,
    "vector_enabled": False,
    "query_rewrite": True,
    "recall_candidates": 20,
    "recall_max": 5,
    "recall_rerank_min": 0.90,
    "recall_vector_min": 0.80,
    "image_describe": True,
    "image_keep_turns": 5,
}
LIMITS = {
    "summary_trigger_turns": (5, 300),
    "keep_turns": (0, 100),
    "memory_tokens": (500, 100000),
    "retention_days": (0, 3650),
    "summary_max_retries": (0, 10),
    "summary_timeout": (30, 1200),
    "recall_candidates": (5, 50),
    "recall_max": (1, 10),
    "image_keep_turns": (0, 50),
}
FLOAT_LIMITS = {
    "recall_rerank_min": (0.0, 1.0),
    "recall_vector_min": (0.0, 1.0),
}
PENDING, SUMMARIZED = "pending", "summarized"
# Recall records get their own short window, independent of summary logs.
RECALL_STATES = ("recalled", "recall_fallback", "recall_failed")
RECALL_DAYS, RECALL_ROWS = 7, 300
# Values that were only ever the shipped default; they follow the new default.
OLD_DEFAULTS = {"memory_tokens": (4000,)}


def _settings(raw):
    saved = json.loads(raw)
    for key, values in OLD_DEFAULTS.items():
        if saved.get(key) in values:
            saved.pop(key)
    return {**DEFAULTS, **saved}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _now() -> int:
    return int(time.time())


class MemoryStore:
    def __init__(self, album_store):
        self.db = album_store.db
        self.lock = album_store.lock
        # Operational logs follow the plugin's 任务记录 retention settings.
        self.log_policy = lambda: (
            int(album_store.settings.get("records_retention_days", 90)),
            int(album_store.settings.get("records_max_count", 1000)),
        )
        self._depth = threading.local()
        with self.lock:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS mem_chats(
                    scope TEXT NOT NULL, cid TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0,
                    generation INTEGER NOT NULL DEFAULT 1,
                    settings TEXT NOT NULL DEFAULT '{}', memory TEXT NOT NULL DEFAULT '{}',
                    next_ordinal INTEGER NOT NULL DEFAULT 1, created INTEGER NOT NULL,
                    updated INTEGER NOT NULL, PRIMARY KEY(scope,cid));
                CREATE TABLE IF NOT EXISTS mem_turns(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL, cid TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    key TEXT NOT NULL, digest TEXT NOT NULL, kind TEXT NOT NULL,
                    at INTEGER NOT NULL, user TEXT NOT NULL, assistant TEXT NOT NULL,
                    status TEXT NOT NULL, batch INTEGER, pruned INTEGER NOT NULL DEFAULT 0,
                    archived_at INTEGER NOT NULL, summarized_at INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(scope,cid,key));
                CREATE INDEX IF NOT EXISTS mem_turns_order ON mem_turns(scope,cid,ordinal);
                CREATE INDEX IF NOT EXISTS mem_turns_status ON mem_turns(scope,cid,status,ordinal);
                CREATE TABLE IF NOT EXISTS mem_turn_aliases(
                    scope TEXT NOT NULL, cid TEXT NOT NULL, key TEXT NOT NULL,
                    turn_id INTEGER NOT NULL, PRIMARY KEY(scope,cid,key));
                CREATE TABLE IF NOT EXISTS mem_batches(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL, cid TEXT NOT NULL, first INTEGER NOT NULL,
                    last INTEGER NOT NULL, state TEXT NOT NULL, diff TEXT NOT NULL,
                    stats TEXT NOT NULL, model TEXT NOT NULL, created INTEGER NOT NULL,
                    undone_at INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS mem_batches_chat ON mem_batches(scope,cid,id);
                CREATE TABLE IF NOT EXISTS mem_results(
                    scope TEXT NOT NULL, cid TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    raw TEXT NOT NULL, model TEXT NOT NULL, created INTEGER NOT NULL,
                    PRIMARY KEY(scope,cid,fingerprint));
                CREATE TABLE IF NOT EXISTS mem_snapshots(
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, cid TEXT NOT NULL,
                    reason TEXT NOT NULL, memory TEXT NOT NULL, turns TEXT NOT NULL,
                    created INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS mem_logs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL,
                    cid TEXT NOT NULL, at INTEGER NOT NULL, state TEXT NOT NULL,
                    detail TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS mem_logs_chat ON mem_logs(scope,cid,id);
                CREATE TABLE IF NOT EXISTS memory_view_bindings(
                    username TEXT PRIMARY KEY, scope TEXT NOT NULL);
            """)
            self.db.commit()

    # ------------------------------------------------------------ basics --

    @contextmanager
    def transaction(self):
        """Savepoint on the shared connection; never held across an await."""
        with self.lock:
            depth = getattr(self._depth, "value", 0)
            name = "mem_" + uuid.uuid4().hex
            self.db.execute(f"SAVEPOINT {name}")
            self._depth.value = depth + 1
            try:
                yield
            except BaseException:
                self.db.execute(f"ROLLBACK TO {name}")
                self.db.execute(f"RELEASE {name}")
                raise
            else:
                self.db.execute(f"RELEASE {name}")
                if depth == 0:
                    self.db.commit()
            finally:
                self._depth.value = depth

    @staticmethod
    def _check(scope, cid):
        if not isinstance(scope, str) or not scope or len(scope) > 512:
            raise AlbumError("会话对象不正确")
        if not isinstance(cid, str) or not cid or len(cid) > 256:
            raise AlbumError("请选择一份聊天")

    def _row(self, scope, cid):
        return self.db.execute(
            "SELECT * FROM mem_chats WHERE scope=? AND cid=?", (scope, cid)
        ).fetchone()

    def _ensure(self, scope, cid):
        """Create the chat row, importing a legacy backup once if one exists."""
        self._check(scope, cid)
        row = self._row(scope, cid)
        if row:
            return row
        with self.transaction():
            row = self._row(scope, cid)
            if row:
                return row
            settings, enabled, generation = {}, 0, 1
            legacy = self._table("memory_spaces") and self.db.execute(
                "SELECT * FROM memory_spaces WHERE scope=? AND cid=?", (scope, cid)
            ).fetchone()
            if legacy and not legacy["deleted_at"]:
                old = json.loads(legacy["settings"] or "{}")
                enabled, generation = int(bool(legacy["enabled"])), legacy["generation"]
                # Never start paid summarizing just because the plugin updated.
                settings["auto_process"] = bool(
                    old.get("auto_process") and old.get("auto_confirmed")
                )
                if isinstance(old.get("model_bindings"), dict):
                    settings["model_bindings"] = {
                        k: v for k, v in old["model_bindings"].items() if k == "summary"
                    }
                for name, target in (
                    ("summary_trigger_turns", "summary_trigger_turns"),
                    ("recent_turns", "keep_turns"),
                    ("summary_max_retries", "summary_max_retries"),
                ):
                    value = old.get(name)
                    low, high = LIMITS[target]
                    if type(value) is int and low <= value <= high:
                        settings[target] = value
            stamp = _now()
            self.db.execute(
                "INSERT INTO mem_chats(scope,cid,enabled,generation,settings,memory,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                (scope, cid, enabled, generation, _json(settings), _json(core.empty()), stamp, stamp),
            )
            if legacy and not legacy["deleted_at"]:
                self._import_legacy(scope, cid, legacy["generation"])
                self._log(scope, cid, "imported", "已从旧版备份导入原文，旧版记忆未导入")
        return self._row(scope, cid)

    def _table(self, name):
        return bool(
            self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
        )

    def _import_legacy(self, scope, cid, generation):
        """Copy complete legacy original rounds as unsummarized backup turns."""
        if not self._table("memory_sources"):
            return
        import zlib

        rows = self.db.execute(
            "SELECT parent_key,source_key,ordinal,at,checkpoint,status,kind,blob FROM memory_sources "
            "WHERE scope=? AND cid=? AND generation=? AND active=1 AND pruned=0 AND blob IS NOT NULL ORDER BY ordinal",
            (scope, cid, generation),
        ).fetchall()
        rounds = {}
        for row in rows:
            try:
                body = json.loads(zlib.decompress(row["blob"]))
            except (zlib.error, ValueError, TypeError):
                continue
            item = rounds.setdefault(
                row["parent_key"],
                {"user": "", "assistant": "", "at": row["at"], "checkpoint": row["checkpoint"],
                 "kind": row["kind"], "status": row["status"], "ordinal": row["ordinal"]},
            )
            item["user"] += body.get("user", "")
            item["assistant"] += body.get("assistant", "")
        turns = []
        for item in sorted(rounds.values(), key=lambda x: x["ordinal"]):
            kind = {"chat": "chat", "proactive": "proactive"}.get(item["kind"], "business")
            if item["kind"] in ("legacy_summary", "imported") or not item["assistant"]:
                continue
            if item["status"] not in ("completed", "complete", "sent", "generated"):
                continue
            from .text import reminder_time, turn_key

            turns.append(
                {
                    "key": turn_key(
                        {
                            "checkpoint": item["checkpoint"],
                            "digest": digest(item["user"], item["assistant"], kind),
                            "user": item["user"],
                        }
                    ),
                    "kind": kind,
                    "user": item["user"],
                    "assistant": item["assistant"],
                    "at": reminder_time(item["user"]) or item["at"],
                }
            )
        self._insert_turns(scope, cid, turns)

    # ------------------------------------------------------------- state --

    def state(self, scope, cid):
        with self.lock:
            row = self._ensure(scope, cid)
            counts = dict(
                self.db.execute(
                    "SELECT count(*) AS turns,"
                    "coalesce(sum(status='pending'),0) AS pending,"
                    "coalesce(sum(status='summarized'),0) AS summarized,"
                    "coalesce(sum(pruned),0) AS pruned,"
                    "coalesce(max(CASE WHEN status='summarized' THEN ordinal END),0) AS through "
                    "FROM mem_turns WHERE scope=? AND cid=?",
                    (scope, cid),
                ).fetchone()
            )
            images = {"ready": 0, "pending": 0, "failed": 0}
            if self._table("mem_images"):
                images.update(dict(self.db.execute(
                    "SELECT status,count(*) FROM mem_images WHERE scope=? AND cid=? GROUP BY status", (scope, cid)
                )))
            return {
                "scope": scope,
                "cid": cid,
                "enabled": bool(row["enabled"]),
                "deleted_at": 0,
                "revision": row["revision"],
                # Album and plan jobs compare this epoch; memory edits no longer
                # replace whole generations, so it is constant.
                "generation": 1,
                "settings": _settings(row["settings"]),
                "counts": counts,
                "images": images,
            }

    def memory(self, scope, cid):
        with self.lock:
            return core.normalized(json.loads(self._ensure(scope, cid)["memory"]))

    def configure(self, scope, cid, patch, revision):
        if not isinstance(patch, dict) or set(patch) - {*DEFAULTS, "enabled"}:
            raise AlbumError("记忆设置字段不正确")
        with self.transaction():
            row = self._ensure(scope, cid)
            if revision is not None and revision != row["revision"]:
                raise AlbumError("记忆版本已变化，请刷新后重试")
            settings = {**json.loads(row["settings"])}
            enabled = row["enabled"]
            for key, value in patch.items():
                if key == "enabled" or key in (
                    "auto_process", "inject_enabled", "vector_enabled", "query_rewrite", "image_describe"
                ):
                    if type(value) is not bool:
                        raise AlbumError("开关格式不正确")
                    if key == "enabled":
                        enabled = int(value)
                    else:
                        settings[key] = value
                elif key in FLOAT_LIMITS:
                    low, high = FLOAT_LIMITS[key]
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                        raise AlbumError(f"设置超出范围（{low}—{high}）")
                    settings[key] = round(float(value), 2)
                elif key in LIMITS:
                    low, high = LIMITS[key]
                    if type(value) is not int or not low <= value <= high:
                        raise AlbumError(f"设置超出范围（{low}—{high}）")
                    settings[key] = value
                elif key == "summary_prompt":
                    if not isinstance(value, str) or len(value) > 20000:
                        raise AlbumError("写作要求最多20000字")
                    settings[key] = value
                elif key == "user_name":
                    if not isinstance(value, str) or len(value.strip()) > 40:
                        raise AlbumError("用户称呼最多40字")
                    settings[key] = value.strip()
                elif key == "model_bindings":
                    if not isinstance(value, dict) or set(value) - {"summary", "embedding", "rerank"}:
                        raise AlbumError("模型选用格式不正确")
                    for binding in value.values():
                        if not isinstance(binding, dict) or binding.get("mode") != "preset" or set(binding) - {"mode", "preset_id"}:
                            raise AlbumError("模型选用格式不正确")
                    settings[key] = value
            self.db.execute(
                "UPDATE mem_chats SET enabled=?,settings=?,revision=revision+1,updated=? WHERE scope=? AND cid=?",
                (enabled, _json(settings), _now(), scope, cid),
            )
        return self.state(scope, cid)

    def save_memory(self, scope, cid, memory, revision):
        """Replace memory content after a user edit, with a revision check."""
        with self.transaction():
            row = self._ensure(scope, cid)
            if revision != row["revision"]:
                raise AlbumError("记忆版本已变化，请刷新后重试")
            self.db.execute(
                "UPDATE mem_chats SET memory=?,revision=revision+1,updated=? WHERE scope=? AND cid=?",
                (_json(core.normalized(memory)), _now(), scope, cid),
            )
        return self.state(scope, cid)

    # ------------------------------------------------------------- turns --

    def body_digest(self, row):
        """Compare words through the same verified media boundaries as projection."""
        bodies = {side: row[side] for side in ("user", "assistant")}
        if self._table("mem_turn_images"):
            links = self.db.execute(
                "SELECT * FROM mem_turn_images WHERE turn_id=?",
                (row["id"],),
            ).fetchall()
            for side in bodies:
                # source-less legacy offsets stay unknown, including before
                # ImageIndex adds the provenance columns on an old database.
                spans = verified_image_spans(bodies[side], (link for link in links if link["side"] == side))
                for span in reversed(spans):
                    start, end = span["start_pos"], span["end_pos"]
                    bodies[side] = bodies[side][:start] + "[曾分享图片，内容未记录]" + bodies[side][end:]
        return digest(bodies["user"], bodies["assistant"], row["kind"])

    def _legacy_candidates(self, scope, cid, identities):
        candidates = {}
        if not identities:
            return candidates
        for row in self.db.execute(
            "SELECT * FROM mem_turns WHERE scope=? AND cid=? AND key LIKE 'd:%'", (scope, cid)
        ):
            match = re.fullmatch(r"d:([0-9a-f]{64}):(\d+)", row["key"])
            if match:
                body = row["digest"] if row["pruned"] else self.body_digest(row)
                identity = (body, int(match[2]))
                if identity in identities:
                    candidates.setdefault(identity, []).append(dict(row))
        return candidates

    def describe_match(self, row, turn):
        """Content relationship, separate from identity and from media provenance."""
        if not row or row.get("pruned"):
            return {"same_body": False, "same_legacy": False}
        legacy = hashlib.sha256(json.dumps(
            [without_reminder(row["user"]), row["assistant"].strip(), row["kind"]], ensure_ascii=False,
        ).encode()).hexdigest()
        return {
            "same_body": self.body_digest(row) == digest(turn["user"], turn["assistant"], turn["kind"]),
            "same_legacy": turn.get("legacy_body_digest") == legacy,
        }

    def resolve_capture(self, scope, cid, turns):
        """One identity decision per evidence group, mapped back to every occurrence."""
        turns = list(turns)
        keys = [turn.get("key") or turn_key(turn) for turn in turns]
        entries, groups, uncertain = [], {}, set()
        with self.lock:
            aliases = {r["key"]: r["turn_id"] for r in self.db.execute(
                "SELECT key,turn_id FROM mem_turn_aliases WHERE scope=? AND cid=?", (scope, cid)
            )}
            rows = {}
            for key in set(keys) | {t.get("legacy_key", "") for t in turns}:
                row = self.db.execute("SELECT * FROM mem_turns WHERE scope=? AND cid=? AND key=?", (scope, cid, key)).fetchone()
                if row:
                    rows[key] = dict(row)
            legacy_claims = {}
            signatures, references, saved_refs = [], [], {}
            for index, turn in enumerate(turns):
                legacy = turn.get("legacy_key")
                row = rows.get(keys[index]) or rows.get(legacy)
                observed_refs = identity_refs(turn)
                saved = set()
                if row and self._table("mem_turn_images"):
                    if row["id"] not in saved_refs:
                        saved_refs[row["id"]] = tuple(r[0] for r in self.db.execute(
                            "SELECT ref FROM mem_turn_images WHERE turn_id=? AND ref<>'' "
                            "ORDER BY CASE side WHEN 'user' THEN 0 ELSE 1 END,position", (row["id"],)
                        ))
                    saved = set(saved_refs[row["id"]])
                # Text can quote a short image token without owning that image.
                # Only a unique link on this exact record upgrades it to evidence.
                refs = []
                for ref in observed_refs:
                    if len(ref) == 32:
                        refs.append(ref)
                    else:
                        matches = {saved_ref for saved_ref in saved if saved_ref.startswith(ref)}
                        if len(matches) == 1:
                            refs.append(next(iter(matches)))
                refs = tuple(refs)
                references.append(refs)
                signature = (legacy, keys[index], refs)
                signatures.append(signature)
                if legacy:
                    legacy_claims.setdefault(legacy, set()).add(signature)
            for index, turn in enumerate(turns):
                key, legacy = keys[index], turn.get("legacy_key")
                old, current = rows.get(legacy), rows.get(key)
                skipped = turn.get("complete") is False or turn["kind"] == "host_summary"
                # Computed legacy hashes only split groups when that old row exists.
                group_id = ("skipped", index) if skipped else (
                    ("legacy", signatures[index]) if old and not current else ("current", key)
                )
                group = groups.setdefault(group_id, {"indices": [], "row": None, "decision": "new", "turn": turn,
                                                    "key": key, "refs": references[index]})
                group["indices"].append(index)
                entry = {"occurrence": index, "group_id": group_id, "decision": "skipped", "row": None, "turn": turn}
                entries.append(entry)
                if skipped:
                    group["decision"] = "skipped"
                    continue
                prior = group["turn"]
                if (old and current and old["id"] != current["id"]) or (
                    references[index] != group["refs"]
                ) or (
                    current and not turn.get("checkpoint") and saved_refs.get(current["id"])
                    and references[index] != saved_refs[current["id"]]
                ) or (
                    turn.get("checkpoint") and any(turn.get(k) != prior.get(k) for k in ("user", "assistant"))
                ):
                    group["decision"], group["row"] = "conflict", None
                    uncertain.update(r["id"] for r in (old, current) if r)
                elif group["decision"] != "conflict" and current:
                    group.update(row=current, decision="matched")
            claimed = {g["row"]["id"] for g in groups.values() if g["row"]}
            fallback = {}
            for group in groups.values():
                if group["row"] or group["decision"] in ("conflict", "skipped"):
                    continue
                turn, key = group["turn"], group["key"]
                if not re.fullmatch(r"d:[0-9a-f]{64}:\d+(?::i:[0-9a-f]{16})?", key):
                    continue
                legacy = turn.get("legacy_key")
                old = rows.get(legacy)
                if old and len(legacy_claims[legacy]) == 1 and old["id"] not in claimed and not any(
                    target == old["id"] and alias != key for alias, target in aliases.items()
                ):
                    group.update(row=old, decision="matched")
                    claimed.add(old["id"])
                    continue
                if old:
                    uncertain.add(old["id"])
                identity = (digest(turn["user"], turn["assistant"], turn["kind"]), reminder_time(turn["user"]))
                fallback.setdefault(identity, []).append(group)
            candidates = self._legacy_candidates(scope, cid, fallback)
            for identity, waiting in fallback.items():
                matches = candidates.get(identity, [])
                for group in waiting:
                    identifier = aliases.get(group["key"])
                    if identifier and identifier not in claimed and all(r["id"] == identifier for r in matches):
                        row = self.db.execute("SELECT * FROM mem_turns WHERE id=? AND scope=? AND cid=?", (identifier, scope, cid)).fetchone()
                        if row:
                            group.update(row=dict(row), decision="matched")
                            claimed.add(identifier)
                remaining = [g for g in waiting if not g["row"]]
                available = [r for r in matches if r["id"] not in claimed and r["id"] not in aliases.values()]
                if len(remaining) == len(available) == 1:
                    remaining[0].update(row=available[0], decision="matched")
                    claimed.add(available[0]["id"])
                elif remaining:
                    uncertain.update(r["id"] for r in matches)
            result = {"entries": entries, "groups": groups, "uncertain_ids": uncertain, "added": 0}
            self._capture_entries(result)
            return result

    def _capture_entries(self, result):
        result["resolved"] = {}
        for group_id, group in result["groups"].items():
            row = group["row"]
            for index in group["indices"]:
                match = self.describe_match(row, result["entries"][index]["turn"])
                result["entries"][index].update(group_id=group_id, decision=group["decision"], row=row, media_match=match)
                if row:
                    result["resolved"][index] = row

    def resolve_turns(self, scope, cid, turns):
        return self.resolve_capture(scope, cid, turns)["resolved"]

    def capture_turns(self, scope, cid, turns):
        with self.transaction():
            self._ensure(scope, cid)
            return self._capture_turns(scope, cid, turns)

    def _capture_turns(self, scope, cid, turns):
        turns = list(turns)
        added = 0
        row = self._row(scope, cid)
        ordinal = row["next_ordinal"]
        stamp = _now()
        result = self.resolve_capture(scope, cid, turns)
        resolved = result["resolved"]
        aliases, identities = {}, {}
        for index, row in resolved.items():
            turn = turns[index]
            key = turn.get("key") or turn_key(turn)
            aliases.setdefault(key, set()).add(row["id"])
            if row["key"] != key:
                identity = (digest(turn["user"], turn["assistant"], turn["kind"]), reminder_time(turn["user"]))
                identities.setdefault(identity, set()).add(key)
        # A subset of history must not create a unique alias for a globally
        # ambiguous old body/time identity. Exact legacy keys remain usable.
        for identity, rows in self._legacy_candidates(scope, cid, identities).items():
            for key in identities[identity]:
                aliases[key].update(row["id"] for row in rows)
        for key, targets in aliases.items():
            if len(targets) > 1:
                self.db.execute("DELETE FROM mem_turn_aliases WHERE scope=? AND cid=? AND key=?", (scope, cid, key))
        for group in result["groups"].values():
            if group["decision"] in ("skipped", "conflict"):
                continue
            turn = {**group["turn"], "key": group["key"]}
            existing = group["row"]
            body_digest = digest(turn["user"], turn["assistant"], turn["kind"])
            if existing:
                # Edited text of a round not yet summarized replaces the backup.
                previous_digest = self.body_digest(existing)
                legacy_body = hashlib.sha256(json.dumps(
                    [without_reminder(existing["user"]), existing["assistant"].strip(), existing["kind"]], ensure_ascii=False,
                ).encode()).hexdigest()
                unchanged_legacy = turn.get("legacy_body_digest") == legacy_body
                if existing["status"] == PENDING and previous_digest != body_digest and not unchanged_legacy and not existing["pruned"]:
                    self.db.execute(
                        "UPDATE mem_turns SET user=?,assistant=?,digest=?,at=CASE WHEN ?>0 THEN ? ELSE at END WHERE id=?",
                        (turn["user"], turn["assistant"], body_digest, turn["at"], turn["at"], existing["id"]),
                    )
                    if self._table("mem_turn_images"):
                        self.db.execute("DELETE FROM mem_turn_images WHERE turn_id=?", (existing["id"],))
                elif existing["status"] == PENDING and not existing["pruned"] and self._table("mem_turn_images"):
                    previous_refs = [r[0][:12] for r in self.db.execute(
                        "SELECT ref FROM mem_turn_images WHERE turn_id=? AND ref<>'' ORDER BY CASE side WHEN 'user' THEN 0 ELSE 1 END,position",
                        (existing["id"],),
                    )]
                    current_refs = [ref[:12] for ref in group["refs"]]
                    if previous_refs and current_refs and previous_refs != current_refs:
                        # Preserve backup words while invalidating an in-flight old-image batch.
                        version = hashlib.sha256(_json([existing["digest"], current_refs]).encode()).hexdigest()
                        self.db.execute("UPDATE mem_turns SET digest=? WHERE id=?", (version, existing["id"]))
                key = turn["key"]
                if existing["key"] != key and len(aliases[key]) == 1:
                    self.db.execute(
                        "INSERT OR IGNORE INTO mem_turn_aliases VALUES(?,?,?,?)",
                        (scope, cid, key, existing["id"]),
                    )
            else:
                cursor = self.db.execute(
                "INSERT INTO mem_turns(scope,cid,ordinal,key,digest,kind,at,user,assistant,status,archived_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (scope, cid, ordinal, turn["key"], body_digest, turn["kind"],
                 turn["at"] or stamp, turn["user"], turn["assistant"], PENDING, stamp),
                )
                existing = {"id": cursor.lastrowid}
                ordinal += 1
                added += 1
            group["row"] = dict(self.db.execute("SELECT * FROM mem_turns WHERE id=?", (existing["id"],)).fetchone())
        if added:
            self.db.execute(
                "UPDATE mem_chats SET next_ordinal=? WHERE scope=? AND cid=?",
                (ordinal, scope, cid),
            )
        result["added"] = added
        self._capture_entries(result)
        return result

    def _insert_turns(self, scope, cid, turns):
        return self._capture_turns(scope, cid, turns)["added"]

    def archive(self, scope, cid, turns):
        """Back up complete rounds once, in order. Returns the number added."""
        with self.transaction():
            self._ensure(scope, cid)
            return self._insert_turns(scope, cid, turns)

    def turn_status(self, scope, cid, keys):
        """{key: (status, ordinal)} for the given round identities."""
        result = {}
        keys = list(dict.fromkeys(keys))
        with self.lock:
            for start in range(0, len(keys), 400):
                chunk = keys[start : start + 400]
                marks = ",".join("?" * len(chunk))
                for row in self.db.execute(
                    f"SELECT key,status,ordinal FROM mem_turns WHERE scope=? AND cid=? AND key IN ({marks})",
                    (scope, cid, *chunk),
                ):
                    result[row["key"]] = (row["status"], row["ordinal"])
                for row in self.db.execute(
                    f"SELECT a.key,t.status,t.ordinal FROM mem_turn_aliases a "
                    f"JOIN mem_turns t ON t.id=a.turn_id WHERE a.scope=? AND a.cid=? AND a.key IN ({marks})",
                    (scope, cid, *chunk),
                ):
                    result.setdefault(row["key"], (row["status"], row["ordinal"]))
        return result

    def pending_turns(self, scope, cid, limit=10000):
        with self.lock:
            return [
                dict(r)
                for r in self.db.execute(
                    "SELECT * FROM mem_turns WHERE scope=? AND cid=? AND status=? AND pruned=0 ORDER BY ordinal LIMIT ?",
                    (scope, cid, PENDING, limit),
                )
            ]

    # ------------------------------------------------------------ batches --

    def save_result(self, scope, cid, fingerprint, raw, model):
        with self.transaction():
            self.db.execute(
                "INSERT OR REPLACE INTO mem_results VALUES(?,?,?,?,?,?)",
                (scope, cid, fingerprint, raw, model, _now()),
            )

    def saved_result(self, scope, cid, fingerprint):
        with self.lock:
            row = self.db.execute(
                "SELECT raw FROM mem_results WHERE scope=? AND cid=? AND fingerprint=?",
                (scope, cid, fingerprint),
            ).fetchone()
        return row["raw"] if row else None

    def apply_batch(self, scope, cid, turns, parsed, model):
        """Merge one parsed result onto the latest memory and mark its turns done."""
        ids = [t["id"] for t in turns]
        with self.transaction():
            row = self._ensure(scope, cid)
            marks = ",".join("?" * len(ids))
            current = self.db.execute(
                f"SELECT id,digest,status FROM mem_turns WHERE id IN ({marks})", ids
            ).fetchall()
            expected = {t["id"]: t["digest"] for t in turns}
            if len(current) != len(ids) or any(
                r["status"] != PENDING or r["digest"] != expected[r["id"]] for r in current
            ):
                raise AlbumError("整理期间这批原文已变化，结果未保存")
            first, last = turns[0]["ordinal"], turns[-1]["ordinal"]
            cursor = self.db.execute(
                "INSERT INTO mem_batches(scope,cid,first,last,state,diff,stats,model,created) VALUES(?,?,?,?,?,?,?,?,?)",
                (scope, cid, first, last, "applying", "{}", "{}", model, _now()),
            )
            batch = cursor.lastrowid
            memory, change, stats = core.merge(
                json.loads(row["memory"]),
                parsed,
                first=first,
                last=last,
                times={t["ordinal"]: t["at"] for t in turns},
                batch=batch,
            )
            self.db.execute(
                "UPDATE mem_batches SET state='applied',diff=?,stats=? WHERE id=?",
                (_json(change), _json(stats), batch),
            )
            self.db.execute(
                f"UPDATE mem_turns SET status=?,batch=?,summarized_at=? WHERE id IN ({marks})",
                (SUMMARIZED, batch, _now(), *ids),
            )
            self.db.execute(
                "UPDATE mem_chats SET memory=?,revision=revision+1,updated=? WHERE scope=? AND cid=?",
                (_json(memory), _now(), scope, cid),
            )
            self.db.execute(
                "DELETE FROM mem_results WHERE scope=? AND cid=?", (scope, cid)
            )
            return batch, stats

    def batches(self, scope, cid, limit=50):
        with self.lock:
            rows = self.db.execute(
                "SELECT b.id,b.first,b.last,b.state,b.stats,b.model,b.created,b.undone_at,"
                "(SELECT count(*) FROM mem_turns t WHERE t.batch=b.id AND t.pruned=1) AS pruned "
                "FROM mem_batches b WHERE scope=? AND cid=? AND state IN ('applied','undone') ORDER BY id DESC LIMIT ?",
                (scope, cid, limit),
            ).fetchall()
        return [{**dict(r), "stats": json.loads(r["stats"])} for r in rows]

    def undo_last(self, scope, cid, revision, *, force=False):
        """Revert the latest applied batch; its rounds become pending again."""
        with self.transaction():
            row = self._ensure(scope, cid)
            if revision != row["revision"]:
                raise AlbumError("记忆版本已变化，请刷新后重试")
            batch = self.db.execute(
                "SELECT * FROM mem_batches WHERE scope=? AND cid=? AND state='applied' ORDER BY id DESC LIMIT 1",
                (scope, cid),
            ).fetchone()
            if not batch:
                raise AlbumError("没有可以撤销的整理")
            if self.db.execute(
                "SELECT 1 FROM mem_turns WHERE batch=? AND pruned=1 LIMIT 1", (batch["id"],)
            ).fetchone():
                raise AlbumError("这批原文备份已超过保留期被清理，不能撤销")
            memory, conflicts = core.undo(json.loads(row["memory"]), json.loads(batch["diff"]), force=force)
            if memory is None:
                raise AlbumError(
                    "这次整理的内容之后被修改过："
                    + "、".join(conflicts[:5])
                    + "。确认仍要撤销请选择“仍然撤销”"
                )
            self.db.execute(
                "UPDATE mem_turns SET status=?,batch=NULL,summarized_at=0 WHERE batch=?",
                (PENDING, batch["id"]),
            )
            self.db.execute(
                "UPDATE mem_batches SET state='undone',undone_at=? WHERE id=?",
                (_now(), batch["id"]),
            )
            self.db.execute(
                "UPDATE mem_chats SET memory=?,revision=revision+1,updated=? WHERE scope=? AND cid=?",
                (_json(memory), _now(), scope, cid),
            )
            self._log(scope, cid, "undone", f"已撤销第{batch['first']}—{batch['last']}轮的整理")
        return self.state(scope, cid)

    def reset_for_resummary(self, scope, cid, revision):
        """Snapshot, clear memory, and mark every kept original round pending."""
        with self.transaction():
            row = self._ensure(scope, cid)
            if revision != row["revision"]:
                raise AlbumError("记忆版本已变化，请刷新后重试")
            pruned = self.db.execute(
                "SELECT count(*) FROM mem_turns WHERE scope=? AND cid=? AND pruned=1", (scope, cid)
            ).fetchone()[0]
            self._snapshot(scope, cid, "重新整理前", row)
            self.db.execute(
                "UPDATE mem_turns SET status=?,batch=NULL,summarized_at=0 WHERE scope=? AND cid=? AND pruned=0",
                (PENDING, scope, cid),
            )
            self.db.execute(
                "UPDATE mem_batches SET state='superseded' WHERE scope=? AND cid=? AND state IN ('applied','undone')",
                (scope, cid),
            )
            memory = core.empty()
            self.db.execute(
                "UPDATE mem_chats SET memory=?,revision=revision+1,updated=? WHERE scope=? AND cid=?",
                (_json(memory), _now(), scope, cid),
            )
            self._log(
                scope, cid, "reset",
                "已清空记忆，等待从备份重新整理" + (f"；{pruned}轮原文已过保留期，无法重新整理" if pruned else ""),
            )
        return self.state(scope, cid)

    # ---------------------------------------------------------- snapshots --

    def _snapshot(self, scope, cid, reason, row=None):
        row = row or self._row(scope, cid)
        turns = [
            [r["id"], r["status"], r["batch"]]
            for r in self.db.execute(
                "SELECT id,status,batch FROM mem_turns WHERE scope=? AND cid=?", (scope, cid)
            )
        ]
        batches = [
            [r["id"], r["state"]]
            for r in self.db.execute(
                "SELECT id,state FROM mem_batches WHERE scope=? AND cid=?", (scope, cid)
            )
        ]
        self.db.execute(
            "INSERT INTO mem_snapshots VALUES(?,?,?,?,?,?,?)",
            (
                "snap_" + uuid.uuid4().hex,
                scope,
                cid,
                reason,
                row["memory"],
                _json({"turns": turns, "batches": batches}),
                _now(),
            ),
        )

    def snapshots(self, scope, cid):
        with self.lock:
            rows = self.db.execute(
                "SELECT id,reason,created,memory FROM mem_snapshots WHERE scope=? AND cid=? ORDER BY created DESC LIMIT 20",
                (scope, cid),
            ).fetchall()
        result = []
        for r in rows:
            memory = core.normalized(json.loads(r["memory"]))
            result.append(
                {
                    "id": r["id"],
                    "reason": r["reason"],
                    "created": r["created"],
                    "expires": r["created"] + SNAPSHOT_DAYS * 86400,
                    "events": len(memory["events"]),
                    "people": len(memory["people"]),
                    "profiles": len(memory["profiles"]),
                    "plans": len(memory["plans"]),
                }
            )
        return result

    def restore_snapshot(self, scope, cid, snapshot_id, revision):
        with self.transaction():
            row = self._ensure(scope, cid)
            if revision != row["revision"]:
                raise AlbumError("记忆版本已变化，请刷新后重试")
            snap = self.db.execute(
                "SELECT * FROM mem_snapshots WHERE id=? AND scope=? AND cid=?",
                (snapshot_id, scope, cid),
            ).fetchone()
            if not snap:
                raise AlbumError("恢复点不存在或已过期")
            self._snapshot(scope, cid, "恢复前", row)
            state = json.loads(snap["turns"])
            known = {
                r["id"]: r["ordinal"]
                for r in self.db.execute(
                    "SELECT id,ordinal FROM mem_turns WHERE scope=? AND cid=?", (scope, cid)
                )
            }
            restored = set()
            for turn_id, status, batch in state["turns"]:
                if turn_id in known:
                    self.db.execute(
                        "UPDATE mem_turns SET status=?,batch=? WHERE id=?", (status, batch, turn_id)
                    )
                    restored.add(turn_id)
            # Rounds archived after the snapshot keep waiting to be summarized.
            for turn_id in set(known) - restored:
                self.db.execute(
                    "UPDATE mem_turns SET status=?,batch=NULL WHERE id=? AND pruned=0", (PENDING, turn_id)
                )
            for batch_id, batch_state in state["batches"]:
                self.db.execute("UPDATE mem_batches SET state=? WHERE id=?", (batch_state, batch_id))
            self.db.execute(
                "UPDATE mem_batches SET state='superseded' WHERE scope=? AND cid=? AND id NOT IN (%s)"
                % (",".join(str(int(b[0])) for b in state["batches"]) or "0"),
                (scope, cid),
            )
            self.db.execute(
                "UPDATE mem_chats SET memory=?,revision=revision+1,updated=? WHERE scope=? AND cid=?",
                (snap["memory"], _now(), scope, cid),
            )
            self._log(scope, cid, "restored", "已恢复到“" + snap["reason"] + "”的记忆")
        return self.state(scope, cid)

    def copy_to(self, scope, cid, target_scope, target_cid, target_revision):
        """Overwrite another chat's memory with this chat's memory (snapshot first)."""
        if (scope, cid) == (target_scope, target_cid):
            raise AlbumError("请选择另一份聊天")
        with self.transaction():
            source = self._ensure(scope, cid)
            target = self._ensure(target_scope, target_cid)
            if target_revision != target["revision"]:
                raise AlbumError("目标聊天的记忆已变化，请刷新后重试")
            self._snapshot(target_scope, target_cid, "被覆盖前", target)
            self.db.execute(
                "UPDATE mem_chats SET memory=?,revision=revision+1,updated=? WHERE scope=? AND cid=?",
                (source["memory"], _now(), target_scope, target_cid),
            )
            self._log(target_scope, target_cid, "copied", "已用另一份聊天的记忆覆盖")
        return self.state(target_scope, target_cid)

    # ------------------------------------------------------------- cleanup --

    def cleanup(self):
        """Prune old summarized originals and expired snapshots/results."""
        stamp = _now()
        pruned = 0
        with self.transaction():
            for row in self.db.execute("SELECT scope,cid,settings FROM mem_chats").fetchall():
                settings = _settings(row["settings"])
                days = settings["retention_days"]
                if not days:
                    continue
                keep = settings["keep_turns"]
                # The latest kept rounds stay in the chat file; never prune them.
                floor = self.db.execute(
                    "SELECT coalesce(min(ordinal),0) FROM (SELECT ordinal FROM mem_turns WHERE scope=? AND cid=? AND status=? ORDER BY ordinal DESC LIMIT ?)",
                    (row["scope"], row["cid"], SUMMARIZED, max(1, keep)),
                ).fetchone()[0]
                cursor = self.db.execute(
                    "UPDATE mem_turns SET user='',assistant='',pruned=1 WHERE scope=? AND cid=? AND status=? AND pruned=0 "
                    "AND summarized_at>0 AND summarized_at<? AND ordinal<?",
                    (row["scope"], row["cid"], SUMMARIZED, stamp - days * 86400, floor),
                )
                pruned += cursor.rowcount
            self.db.execute(
                "DELETE FROM mem_snapshots WHERE created<?", (stamp - SNAPSHOT_DAYS * 86400,)
            )
            self.db.execute("DELETE FROM mem_results WHERE created<?", (stamp - 7 * 86400,))
            self.db.execute("DELETE FROM mem_turn_aliases WHERE turn_id NOT IN (SELECT id FROM mem_turns)")
            if self._table("mem_turn_images"):
                self.db.execute("DELETE FROM mem_turn_images WHERE turn_id NOT IN (SELECT id FROM mem_turns WHERE pruned=0)")
            if self._table("mem_images"):
                self.db.execute(
                    "DELETE FROM mem_images WHERE NOT EXISTS "
                    "(SELECT 1 FROM mem_chats c WHERE c.scope=mem_images.scope AND c.cid=mem_images.cid)"
                )
            self.prune_logs(stamp)
        return pruned

    def prune_logs(self, stamp=None):
        """Recall records keep 7 days / 300 rows; other logs follow the record policy."""
        stamp = stamp or _now()
        days, count = self.log_policy()
        recall = ",".join("?" * len(RECALL_STATES))
        for where, params, keep_days, keep_rows in (
            (f"state IN ({recall})", RECALL_STATES, RECALL_DAYS, RECALL_ROWS),
            (f"state NOT IN ({recall})", RECALL_STATES, days, count),
        ):
            self.db.execute(f"DELETE FROM mem_logs WHERE {where} AND at<?", (*params, stamp - keep_days * 86400))
            self.db.execute(
                f"DELETE FROM mem_logs WHERE {where} AND id NOT IN "
                f"(SELECT id FROM mem_logs WHERE {where} ORDER BY id DESC LIMIT ?)",
                (*params, *params, keep_rows),
            )

    # ---------------------------------------------------------------- logs --

    def _log(self, scope, cid, state, detail=""):
        self.db.execute(
            "INSERT INTO mem_logs(scope,cid,at,state,detail) VALUES(?,?,?,?,?)",
            (scope, cid, _now(), state, str(detail)[:400]),
        )

    def log(self, scope, cid, state, detail=""):
        with self.transaction():
            self._log(scope, cid, state, detail)

    def logs(self, scope="", cid="", *, failed=False, query="", offset=0, limit=20):
        where, params = [], []
        if scope:
            where.append("scope=?")
            params.append(scope)
        if cid:
            where.append("cid=?")
            params.append(cid)
        if failed:
            where.append("state LIKE '%failed%'")
        if query:
            where.append("(state LIKE ? OR detail LIKE ? OR CAST(id AS TEXT)=?)")
            params += [f"%{query}%", f"%{query}%", query.lstrip("#")]
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self.lock:
            total = self.db.execute(f"SELECT count(*) FROM mem_logs{clause}", params).fetchone()[0]
            rows = self.db.execute(
                f"SELECT * FROM mem_logs{clause} ORDER BY id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return {"items": [dict(r) for r in rows], "total": total, "offset": offset}

    # ------------------------------------------------------------ bindings --

    def view_binding(self, username):
        with self.lock:
            row = self.db.execute(
                "SELECT scope FROM memory_view_bindings WHERE username=?", (username,)
            ).fetchone()
        return row[0] if row else ""

    def bind_view(self, username, scope):
        with self.transaction():
            if scope:
                self.db.execute(
                    "INSERT INTO memory_view_bindings VALUES(?,?) ON CONFLICT(username) DO UPDATE SET scope=excluded.scope",
                    (username, scope),
                )
            else:
                self.db.execute("DELETE FROM memory_view_bindings WHERE username=?", (username,))

    def chats_using_preset(self):
        """Preset ids referenced by any chat, for connection deletion checks."""
        used = {}
        with self.lock:
            for row in self.db.execute("SELECT settings FROM mem_chats"):
                for binding in (json.loads(row["settings"]).get("model_bindings") or {}).values():
                    if isinstance(binding, dict) and binding.get("mode") == "preset" and binding.get("preset_id"):
                        used[binding["preset_id"]] = used.get(binding["preset_id"], 0) + 1
        return used

