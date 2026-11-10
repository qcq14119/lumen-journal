"""Explicit preparation, archived originals and reviewed restore operations."""
from __future__ import annotations

import asyncio
from contextlib import closing
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import time
import uuid

from PIL import Image
from starlette.responses import StreamingResponse

from . import backup_data, backup_format, restore_data, restore_journal as journal
from .backup_format import BackupError, CHUNK_BYTES, check_cancel
from .memory.text import history_turns, turn_key, digest, legacy_turn_metadata

MAX_HOST_BYTES = 8 * CHUNK_BYTES
MAX_ARCHIVES = 32
PRIVATE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _need(value, code):
    if not value:
        raise BackupError(code)


class Restores:
    def __init__(self, backups, plugin):
        self.backups, self.plugin, self.store = backups, plugin, plugin.store
        self.root = journal.private_root(self.store.root) / "archives"
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.checks = {}
        self.fault = lambda phase: None  # Tests inject failures; never an API input.

    def _guard(self):
        return self.store.restore_guard

    def begin(self, owner, body):
        result = self.backups.begin_validation(owner, body)
        self.backups.current["operation"] = "import"
        return result

    def _job(self, owner, task_id):
        job = self.backups._get(owner, task_id)
        _need(job["operation"] == "import" and job["state"] == "ready" and not job["release"], "RESTORE_NOT_PREPARED")
        return job

    def _metadata(self, owner, identifier):
        _need(isinstance(identifier, str) and len(identifier) == 64 and all(c in "0123456789abcdef" for c in identifier), "ARCHIVE_NOT_FOUND")
        path = self.root / identifier / "metadata.json"
        _need(path.is_file() and not path.is_symlink() and path.stat().st_size <= CHUNK_BYTES, "ARCHIVE_NOT_FOUND")
        value = json.loads(path.read_bytes())
        _need(value.get("owner") == owner and value.get("id") == identifier, "ARCHIVE_NOT_FOUND")
        return value

    def _read_source(self, job, password):
        checked = job["root"] / "checked"
        checked.mkdir(mode=0o700)
        source = job["root"] / "upload.cab"
        manifest = backup_format.inspect_archive(source, checked, password, job["cancel"])
        verified = backup_data.validate_data(checked, manifest, job["cancel"])
        journal.budget(self.store.root, sum(v["bytes"] for v in manifest["inventory"].values()) + 16 * CHUNK_BYTES)
        identifier = journal.digest_file(source)
        catalog = restore_data.catalog(checked, manifest, job["cancel"])
        catalog["targets"] = self._targets()
        job.update(manifest=manifest, archive_id=identifier)
        summary = self.backups._summary(manifest, verified, source.stat().st_size)
        summary.update(catalog=catalog, archive_id=identifier, restore_supported=True)
        job["import_summary"] = summary
        return summary

    def start(self, owner, task_id):
        job = self.backups._get(owner, task_id)
        _need(job["operation"] == "import" and job["state"] == "uploading" and not job["upload_busy"]
              and job["uploaded_bytes"] == job["expected_bytes"], "UPLOAD_INCOMPLETE")
        job["state"] = "running"
        job["worker"] = asyncio.create_task(self.backups._run(job, lambda: self._read_source(job, job["password"])))
        return {"task_id": task_id, "state": "running"}

    def archives(self, owner):
        result = []
        for path in sorted(self.root.iterdir()):
            if (path.is_dir() and not path.is_symlink() and len(path.name) == 64
                    and all(c in "0123456789abcdef" for c in path.name)):
                value = self._metadata(owner, path.name) if self._owned(path, owner) else None
                if value:
                    with self.store.lock:
                        applied = self.store.db.execute("SELECT 1 FROM records WHERE kind='restore_import' "
                            "AND json_extract(data,'$.archive_id')=? LIMIT 1", (value["id"],)).fetchone()
                    result.append({**value["summary"], "id": value["id"], "state": "imported" if applied else "pending",
                        "bindings": value.get("bindings", [])})
        return {"archives": result}

    @staticmethod
    def _owned(path, owner):
        try:
            meta = path / "metadata.json"
            return not meta.is_symlink() and meta.stat().st_size <= CHUNK_BYTES and json.loads(meta.read_bytes()).get("owner") == owner
        except (OSError, ValueError):
            return False

    def _save_archive(self, job):
        identifier = job["archive_id"]
        destination = self.root / identifier
        if destination.exists():
            meta = self._metadata(job["owner"], identifier)
            _need(journal.digest_file(destination / "original.cab") == identifier, "FILE_HASH")
            if job.get("preview", {}).get("objects"):
                meta["bindings"] = job["preview"]["objects"]
                journal.atomic_json(destination / "metadata.json", meta)
            return meta
        _need(sum(1 for p in self.root.iterdir() if p.is_dir()) < MAX_ARCHIVES, "ARCHIVE_LIMIT")
        source = job["root"] / "upload.cab"
        _need(journal.digest_file(source) == identifier, "SOURCE_CHANGED")
        journal.budget(self.store.root, source.stat().st_size + CHUNK_BYTES)
        temporary = self.root / ("writing-" + uuid.uuid4().hex)
        temporary.mkdir(mode=0o700)
        try:
            shutil.copyfile(source, temporary / "original.cab")
            _need(journal.digest_file(temporary / "original.cab") == identifier, "FILE_HASH")
            summary = {k: v for k, v in job["import_summary"].items() if k not in ("catalog",)}
            summary["source"] = {k: job["manifest"]["source"][k] for k in ("namespace", "captured_at", "settings_revision")}
            meta = {"id": identifier, "owner": job["owner"], "summary": summary, "applied": {},
                    "bindings": job.get("preview", {}).get("objects", [])}
            journal.atomic_json(temporary / "metadata.json", meta)
            temporary.rename(destination)
            return meta
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    async def save_archive(self, owner, body):
        _need(body.get("confirm") is True, "RESTORE_CONFIRM_REQUIRED")
        job = self._job(owner, body.get("task_id"))
        result = await self._operation(job, lambda: self._save_archive(job))
        return {"archive_id": result["id"], "state": "archived", "summary": result["summary"]}

    def open_archive(self, owner, body):
        from .backup import _password
        meta = self._metadata(owner, body.get("archive_id"))
        password = _password(body.get("password"))
        job = self.backups._reserve(owner, "import")
        def work():
            source = self.root / meta["id"] / "original.cab"
            _need(journal.digest_file(source) == meta["id"], "FILE_HASH")
            journal.budget(self.store.root, source.stat().st_size + 16 * CHUNK_BYTES)
            shutil.copyfile(source, job["root"] / "upload.cab")
            return self._read_source(job, password)
        job["worker"] = asyncio.create_task(self.backups._run(job, work))
        return {"task_id": job["task_id"], "state": "running"}

    def download(self, owner, identifier):
        self._metadata(owner, identifier)
        path = self.root / identifier / "original.cab"
        _need(path.is_file() and not path.is_symlink(), "ARCHIVE_NOT_FOUND")
        async def chunks():
            with path.open("rb") as handle:
                while block := await asyncio.to_thread(handle.read, CHUNK_BYTES):
                    yield block
        return StreamingResponse(chunks(), media_type="application/octet-stream", headers={**PRIVATE,
            "Content-Length": str(path.stat().st_size), "Content-Disposition": 'attachment; filename="companion-archive.cab"'})

    async def _operation(self, job, work):
        job["state"] = "running"
        job["worker"] = asyncio.create_task(self.backups._run(job, work))
        await asyncio.shield(job["worker"])
        if job["state"] != "ready":
            raise BackupError(job.get("error", "RESTORE_CANCELLED"))
        return job["summary"]

    def _host_path(self):
        try:
            path = self.plugin.context.get_db().db_path
            return Path(path).resolve().as_uri() + "?mode=ro" if path != ":memory:" else None
        except (AttributeError, TypeError, OSError):
            return None

    def _targets(self):
        targets = {"characters": [], "plans": [], "chats": [],
                   "connections": {"presets": [], "providers": [], "search_schemes": []}}
        with self.store.lock:
            for kind, group in (("character", "characters"), ("life_plan", "plans")):
                values = self.store.db.execute("SELECT id,substr(json_extract(data,'$.name'),1,80) FROM records WHERE kind=? LIMIT 201", (kind,)).fetchall()
                _need(len(values) <= 200, "CATALOG_LIMIT")
                targets[group] = [{"id": row[0], "name": row[1] or ""} for row in values]
        address = self._host_path()
        if address:
            try:
                with closing(sqlite3.connect(address, uri=True)) as db:
                    targets["chats"] = [{"scope": row[0], "cid": row[1], "name": row[2] or ""} for row in db.execute(
                        "SELECT user_id,conversation_id,substr(title,1,80) FROM conversations ORDER BY updated_at DESC LIMIT 200")]
            except sqlite3.Error:
                pass
        # Select only directory fields in SQL. Do not load private model/calendar
        # settings or expose address fragments (which may themselves be secrets).
        with self.store.lock:
            if self.store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_model_settings'").fetchone():
                rows = self.store.db.execute("SELECT json_extract(p.value,'$.id'),substr(json_extract(p.value,'$.name'),1,80),"
                    "substr(json_extract(p.value,'$.type'),1,80),substr(json_extract(p.value,'$.model'),1,120) "
                    "FROM memory_model_settings m,json_each(m.data,'$.presets') p WHERE m.id=1 LIMIT 201").fetchall()
                _need(len(rows) <= 200, "CATALOG_LIMIT")
                targets["connections"]["presets"] = [dict(zip(("id", "name", "type", "model"), (v or "" for v in row))) for row in rows]
            settings = self.store.settings
            _need(len(settings.get("providers", [])) < 200, "CATALOG_LIMIT")
            targets["connections"]["providers"] = [{"id": "primary", "name": str(settings.get("primary_name") or "主站")[:80],
                "type": "image", "model": str(settings.get("model", ""))[:120]}] + [
                {"id": item.get("id", ""), "name": str(item.get("name", ""))[:80], "type": "image",
                 "model": str(item.get("model", ""))[:120]} for item in settings.get("providers", [])]
            rows = self.store.db.execute("SELECT json_extract(p.value,'$.id'),substr(json_extract(p.value,'$.name'),1,80),"
                "substr(json_extract(p.value,'$.model'),1,120) FROM records r,json_each(r.data,'$.search_schemes') p "
                "WHERE r.kind='calendar' AND r.id='default' LIMIT 201").fetchall()
            _need(len(rows) <= 200, "CATALOG_LIMIT")
            targets["connections"]["search_schemes"] = [{"id": row[0] or "", "name": row[1] or "",
                "type": "calendar", "model": row[2] or ""} for row in rows]
        return targets

    def _host_evidence(self, job, selection):
        result = {}
        address = self._host_path()
        for choice in selection.get("chats", []):
            if choice.get("mode") == "archive":
                continue
            key = restore_data.chat_key(choice["source_scope"], choice["source_cid"])
            evidence = {"verified": False, "exists": False, "empty": False, "fingerprint": "", "reason": "HOST_EVIDENCE_UNAVAILABLE"}
            result[key] = evidence
            if not address:
                continue
            try:
                with closing(sqlite3.connect(address, uri=True)) as db:
                    db.execute("BEGIN")
                    params = (choice.get("target_scope"), choice.get("target_cid"))
                    row = db.execute("SELECT length(CAST(content AS BLOB)) FROM conversations WHERE user_id=? AND conversation_id=?", params).fetchone()
                    if row is None:
                        evidence["reason"] = "CHAT_UNAVAILABLE"
                        continue
                    evidence["exists"] = True
                    if (row[0] or 0) > MAX_HOST_BYTES:
                        evidence["reason"] = "HOST_EVIDENCE_LIMIT"
                        continue
                    raw = db.execute("SELECT content FROM conversations WHERE user_id=? AND conversation_id=?", params).fetchone()[0] or "[]"
                history = json.loads(raw)
                _need(isinstance(history, list), "HOST_EVIDENCE_UNAVAILABLE")
                evidence["fingerprint"] = hashlib.sha256(raw.encode()).hexdigest()
                turns = history_turns(history)
                evidence["empty"] = not turns
                if choice.get("mode") == "new":
                    evidence.update(verified=not turns, reason="" if not turns else "NEW_CHAT_NOT_EMPTY")
                    continue
                # Only exact structured round/checkpoint evidence counts. Names
                # and coincident database IDs never establish chat identity.
                matches = 0
                identities, checkpoints = set(), {}
                for turn in turns:
                    identities.add((turn_key(turn), digest(turn["user"], turn["assistant"], turn["kind"])))
                    old = legacy_turn_metadata(turn)
                    identities.add((old["legacy_key"], old["legacy_body_digest"]))
                    if turn.get("checkpoint"):
                        checkpoint = turn["checkpoint"]
                        checkpoints[checkpoint] = checkpoints.get(checkpoint, 0) + 1
                with closing(restore_data._index(job["root"] / "checked", job["manifest"])) as source:
                    for saved in restore_data._rows(source, "mem_turns", key):
                        # Host checkpoints survive safe picture projection and
                        # pruning. Repeated checkpoint IDs are not proof.
                        if checkpoints.get(saved["key"]) == 1:
                            matches += 1
                        elif not saved["pruned"] and (saved["key"], saved["digest"]) in identities:
                            matches += 1
                evidence.update(verified=matches > 0, reason="" if matches else "CHAT_IDENTITY_UNPROVEN")
            except (sqlite3.Error, ValueError, KeyError, TypeError):
                evidence["reason"] = "HOST_EVIDENCE_UNAVAILABLE"
        return result

    async def preview(self, owner, body):
        job = self._job(owner, body.get("task_id"))
        selection = body.get("selection")
        _need(isinstance(selection, dict), "RESTORE_MAPPING_INVALID")
        selection = copy.deepcopy(selection)
        def work():
            _need(journal.digest_file(job["root"] / "upload.cab") == job["archive_id"], "SOURCE_CHANGED")
            evidence = self._host_evidence(job, selection)
            with self.store.lock:
                target = journal.fingerprint(self.store.db, self.store.settings_path)
                receipt = self._receipt(job, selection)
                if receipt:
                    planned = {"mapping": None, "preview": {"objects": [
                        {**item, "action": "keep"} for item in receipt["objects"]],
                        "limitations": [], "already_imported": True}}
                else:
                    planned = restore_data.plan(job["root"] / "checked", job["manifest"], self.store.db,
                        self.store.settings, selection, archive_id=job["archive_id"], host_evidence=evidence)
                for item in planned["preview"]["objects"]:
                    if item["kind"] == "memory":
                        scope, cid = item.get("target_scope"), item.get("target_cid")
                        metadata = self._host_metadata(scope, cid) if scope and cid else None
                        item["name"] = (metadata or {}).get("title") or "拾忆聊天"
                        item["target_summary"] = " · ".join(v for v in (scope, cid) if v)[:512]
                    elif item["kind"] in ("calendar", "settings"):
                        item["name"] = {"calendar": "日历与行程", "settings": "全局设置"}[item["kind"]]
            identifier = uuid.uuid4().hex
            job["preview"] = {"id": identifier, "selection": selection, "evidence": evidence,
                              "target": target, "mapping": planned["mapping"], "receipt": receipt,
                              "objects": [{k: v for k, v in item.items() if k in (
                                  "kind", "name", "target_summary", "source_id", "source_scope", "source_cid", "target_id", "target_scope", "target_cid", "action", "reason")}
                                  for item in planned["preview"]["objects"]]}
            return {"preview_id": identifier, "preview": planned["preview"], "archive_id": job["archive_id"]}
        return await self._operation(job, work)

    def _receipt(self, job, selection):
        row = self.store.db.execute("SELECT data FROM records WHERE kind='restore_import' AND id=?",
            (_hash([job["archive_id"], selection]),)).fetchone()
        return json.loads(row[0]) if row else None

    def _completed_summary(self, job, pending_ids, *, state="pending", applied=None):
        return {**job["import_summary"], "restore": {"state": state, "pending_ids": pending_ids,
            "applied": len(pending_ids) if applied is None else applied, "archived": True}}

    def _prepare_media(self, job, actions):
        output, additions = job["root"] / "derived", []
        output.mkdir(exist_ok=True)
        seen = set()
        for action in actions:
            source = job["root"] / "checked" / action["source"]
            for role in ("original", "delivery", "thumbnail"):
                relative = action[role]
                if relative in seen:
                    continue
                seen.add(relative)
                destination = self.store.root / relative
                if destination.exists():
                    if role == "original":
                        _need(journal.digest_file(source) == journal.digest_file(destination), "IMAGE_HASH")
                    continue
                stage = output / Path(relative).name
                if role == "original":
                    shutil.copyfile(source, stage)
                else:
                    with Image.open(source) as image:
                        converted = Image.new("RGB", image.size, "white")
                        converted.paste(image, mask=image.getchannel("A") if image.mode == "RGBA" else None)
                        converted.thumbnail((1600, 1600) if role == "delivery" else (480, 480))
                        # Distinguish delivery and thumbnail for equal basename.
                        stage = output / (role + "-" + Path(relative).name)
                        converted.save(stage, format="JPEG", quality=85 if role == "delivery" else 75)
                additions.append({"path": relative, "hash": journal.digest_file(stage), "stage": str(stage)})
        journal.budget(self.store.root, sum(Path(item["stage"]).stat().st_size for item in additions))
        return additions

    async def commit(self, owner, body):
        job = self._job(owner, body.get("task_id"))
        preview = job.get("preview")
        _need(preview and preview["id"] == body.get("preview_id") and body.get("keep_archive") is True, "RESTORE_PREVIEW_EXPIRED")
        ticket = await self._guard().acquire()
        job["operation"] = "restore"
        job["state"] = "running"
        async def worker():
            try:
                await self.backups._run(job, lambda: self._commit_work(job, preview))
            finally:
                if job.get("error") != "RECOVERY_REQUIRED":
                    self._guard().release(ticket, changed=bool(job.get("committing")))
        job["worker"] = asyncio.create_task(worker())
        return {"task_id": job["task_id"], "state": "running"}

    def _commit_work(self, job, preview):
        with self._guard().privileged(), self.store.lock:
            check_cancel(job["cancel"])
            _need(journal.fingerprint(self.store.db, self.store.settings_path) == preview["target"], "RESTORE_TARGET_CHANGED")
            _need(journal.digest_file(job["root"] / "upload.cab") == job["archive_id"], "SOURCE_CHANGED")
            _need(self._host_evidence(job, preview["selection"]) == preview["evidence"], "RESTORE_HOST_CHANGED")
            meta = self._save_archive(job)
            if preview.get("receipt"):
                # The receipt is in the same transaction as the restored rows.
                # It survives restart but disappears on rollback. Never replay
                # an old import over changes made since its successful commit.
                _need(self._receipt(job, preview["selection"]) == preview["receipt"], "RESTORE_TARGET_CHANGED")
                live = {item["pending_id"] for item in self._pending()}
                pending_ids = [value for value in preview["receipt"]["pending_ids"] if value in live]
                return self._completed_summary(job, pending_ids, state="unchanged", applied=0)
            # Prepare the merged target in a private DB. Business tables are
            # touched only after the recovery copy and journal are durable.
            size = self.store.db.execute("PRAGMA page_count").fetchone()[0] * self.store.db.execute("PRAGMA page_size").fetchone()[0]
            _need(size <= journal.MAX_TARGET_BYTES, "RESTORE_TARGET_LIMIT")
            source_bytes = sum(value["bytes"] for value in job["manifest"]["inventory"].values())
            # Account before copying: stage + rollback + source growth on both
            # databases + derived/reference copies. The later exact check is
            # additional protection, not permission to exceed this reservation.
            journal.budget(self.store.root, 2 * size + 4 * source_bytes + 32 * CHUNK_BYTES)
            stage = job["root"] / "target.sqlite3"
            with closing(sqlite3.connect(stage)) as staged:
                staged.row_factory = sqlite3.Row
                self.store.db.backup(staged, pages=256)
                with staged:
                    applied = restore_data.apply(job["root"] / "checked", job["manifest"], staged, self.store.settings, preview["mapping"])
                    pending_ids = []
                    for pending in applied["pending"]:
                        identifier = _hash([job["archive_id"], pending])[:32]
                        value = {**pending, "archive_id": job["archive_id"], "target_id": pending.get("target_id", pending.get("id", ""))}
                        staged.execute("INSERT OR REPLACE INTO records VALUES('restore_pending',?,?,1)", (identifier, _json(value)))
                        pending_ids.append(identifier)
                    _need(staged.execute("SELECT count(*) FROM records WHERE kind='restore_pending'").fetchone()[0] <= 200,
                          "RESTORE_CATALOG_LIMIT")
                _need(staged.execute("PRAGMA quick_check").fetchone()[0] == "ok", "DATA_SCHEMA_INVALID")
            if not applied["pending"]:
                return self._completed_summary(job, [], state="archived", applied=0)
            self.fault("staged")
            additions = self._prepare_media(job, applied["media"])
            check_cancel(job["cancel"])
            before = journal.prepare(self.store, archive_id=job["archive_id"], preview_id=preview["id"],
                new_media=[{k: item[k] for k in ("path", "hash")} for item in additions],
                affected=[{k: v for k, v in p.items() if k in ("kind", "target_id", "scope", "cid")} for p in applied["pending"]])
            self.fault("prepared")
            check_cancel(job["cancel"])
            job["committing"] = True
            journal.write(self.store.root, before, "applying")
            try:
                self.fault("applying")
                for item in additions:
                    destination = self.store.root / item["path"]
                    destination.parent.mkdir(exist_ok=True)
                    Path(item["stage"]).replace(destination)
                self.fault("media")
                # Merge changed rows rather than replacing the target shared DB.
                # apply is deterministic against the frozen target and disk map.
                with self.store.db:
                    result = restore_data.apply(job["root"] / "checked", job["manifest"], self.store.db, self.store.settings, preview["mapping"])
                    for identifier, pending in zip(pending_ids, result["pending"]):
                        value = {**pending, "archive_id": job["archive_id"], "target_id": pending.get("target_id", pending.get("id", ""))}
                        self.store.db.execute("INSERT OR REPLACE INTO records VALUES('restore_pending',?,?,1)", (identifier, _json(value)))
                    receipt = {"archive_id": job["archive_id"], "pending_ids": pending_ids,
                               "objects": [self._public_pending({"pending_id": i, **p}) for i, p in zip(pending_ids, result["pending"])]}
                    self.store.db.execute("INSERT INTO records VALUES('restore_import',?,?,1)",
                        (_hash([job["archive_id"], preview["selection"]]), _json(receipt)))
                self.fault("database")
                journal.atomic_json(self.store.settings_path, result["settings"])
                self.fault("settings")
                self.store.settings = result["settings"]
                self.fault("cache")
                after = journal.fingerprint(self.store.db, self.store.settings_path)
                journal.write(self.store.root, before, "committed", after_fingerprint=after)
                job["committed"] = True
                job["committed_summary"] = self._completed_summary(job, pending_ids)
                self.fault("committed")
                meta["applied"][_hash(preview["selection"])] = {"fingerprint": after, "pending_ids": pending_ids}
                try:
                    journal.atomic_json(self.root / meta["id"] / "metadata.json", meta)
                except OSError:
                    # Optional archive display metadata is not the authority
                    # for a committed result or for duplicate prevention.
                    job["committed_summary"]["restore"]["warning"] = "ARCHIVE_METADATA_PENDING"
                self._prune(before["id"])
                return job["committed_summary"]
            except BaseException:
                if not job.get("committed"):
                    try:
                        journal.rollback(self.store, before)
                    except Exception as exc:
                        raise BackupError("RECOVERY_REQUIRED") from exc
                raise

    def _prune(self, current_id):
        try:
            journal.prune_previous(self.store.root, current_id)
        except BackupError as exc:
            raise BackupError("RECOVERY_REQUIRED") from exc
        except OSError:
            # Keeping an older verified copy is safe; the next operation's
            # cumulative disk budget still accounts for it.
            pass

    def _pending(self, ids=None):
        rows = self.store.db.execute("SELECT id,data FROM records WHERE kind='restore_pending' ORDER BY rowid LIMIT 201").fetchall()
        _need(len(rows) <= 200, "CATALOG_LIMIT")
        values = [{"pending_id": row[0], **json.loads(row[1])} for row in rows]
        if ids is not None:
            _need(isinstance(ids, list) and 0 < len(ids) <= 200 and all(isinstance(i, str) for i in ids)
                  and len(set(ids)) == len(ids), "RESTORE_SELECTION_INVALID")
            _need(set(ids) <= {v["pending_id"] for v in values}, "RESTORE_TARGET_CHANGED")
            values = [v for v in values if v["pending_id"] in ids]
        return values

    def _public_pending(self, value):
        result = {k: value[k] for k in ("pending_id", "kind", "target_id", "scope", "cid", "archive_id") if k in value}
        if value.get("kind") in ("character", "life_plan") and value.get("target_id"):
            row = self.store.db.execute("SELECT substr(json_extract(data,'$.name'),1,80),"
                                        "substr(json_extract(data,'$.scope'),1,512),"
                                        "substr(json_extract(data,'$.origin_cid'),1,256) FROM records WHERE kind=? AND id=?",
                                        (value["kind"], value["target_id"])).fetchone()
            if row:
                result["name"] = row[0] or ""
                if value["kind"] == "life_plan":
                    result["target_summary"] = " · ".join(v for v in (row[1], row[2]) if v)[:512]
                else:
                    sessions = value.get("intent", {}).get("sessions", [])
                    result["target_summary"] = " · ".join(sessions[:3])[:512]
        if value.get("kind") == "memory":
            result["target_summary"] = " · ".join(str(v) for v in (value.get("scope", ""), value.get("cid", "")) if v)[:512]
            metadata = self._host_metadata(value.get("scope", ""), value.get("cid", ""))
            result["name"] = str((metadata or {}).get("title") or "拾忆聊天")[:80]
        elif value.get("kind") in ("calendar", "settings"):
            result["name"] = {"calendar": "日历与行程", "settings": "全局设置"}[value["kind"]]
        if value.get("kind") == "calendar" and value.get("intent", {}).get("calendar_scope"):
            result["calendar_scope"] = value["intent"]["calendar_scope"]
        for field in ("errors", "can_enable", "reason"):
            if field in value:
                result[field] = value[field]
        return result

    def pending(self):
        with self.store.lock:
            return {"objects": [self._public_pending(v) for v in self._pending()]}

    def _host_metadata(self, scope, cid=""):
        address = self._host_path()
        if not address:
            return None
        try:
            with closing(sqlite3.connect(address, uri=True)) as db:
                db.row_factory = sqlite3.Row
                columns = {r[1] for r in db.execute("PRAGMA table_info(conversations)")}
                persona = "persona_id" if "persona_id" in columns else "NULL AS persona_id"
                title = "substr(title,1,80) AS title" if "title" in columns else "NULL AS title"
                sql = f"SELECT user_id,conversation_id,{persona},{title} FROM conversations WHERE user_id=?"
                args = [scope]
                if cid:
                    sql += " AND conversation_id=?"
                    args.append(cid)
                row = db.execute(sql + " ORDER BY updated_at DESC LIMIT 1", args).fetchone()
                return dict(row) if row else None
        except sqlite3.Error:
            return None

    def _host_fingerprint(self, scope, cid):
        address = self._host_path()
        if not address:
            return None
        try:
            with closing(sqlite3.connect(address, uri=True)) as db:
                db.execute("BEGIN")
                row = db.execute("SELECT length(CAST(content AS BLOB)) FROM conversations WHERE user_id=? AND conversation_id=?", (scope, cid)).fetchone()
                if row is None or (row[0] or 0) > MAX_HOST_BYTES:
                    return None
                raw = db.execute("SELECT content FROM conversations WHERE user_id=? AND conversation_id=?", (scope, cid)).fetchone()[0] or "[]"
                return hashlib.sha256(raw.encode()).hexdigest()
        except (sqlite3.Error, ValueError, AttributeError):
            return None

    def _check_snapshot(self, ids=None):
        with self.store.lock:
            return self._pending(ids) if ids is not None else [], journal.fingerprint(self.store.db, self.store.settings_path)

    async def _host_dependencies(self, scope, cid=""):
        metadata = await asyncio.to_thread(self._host_metadata, scope, cid)
        if not metadata:
            return ["CHAT_UNAVAILABLE"]
        errors = []
        context = self.plugin.context
        try:
            provider_id = await context.get_current_chat_provider_id(scope)
            if not provider_id or context.get_provider_by_id(provider_id) is None:
                errors.append("PROVIDER_UNAVAILABLE")
        except (AttributeError, ValueError, KeyError):
            errors.append("PROVIDER_UNAVAILABLE")
        try:
            _, persona, _, _ = await context.persona_manager.resolve_selected_persona(
                umo=scope, conversation_persona_id=metadata["persona_id"], platform_name="")
            if not persona:
                errors.append("PERSONA_UNAVAILABLE")
        except (AttributeError, ValueError, KeyError):
            errors.append("PERSONA_UNAVAILABLE")
        return errors

    def _memory_dependencies(self, settings):
        from .memory.models import MemoryModels, MemoryModelError, MODELS
        # A read-only view: invoking the constructor here would create tables.
        models = object.__new__(MemoryModels)
        models.store = self.store
        errors = []
        data = models.private()
        for role in MODELS:
            try:
                binding = models.selection(role, settings)
                mode = binding["mode"]
                if mode in ("disabled", "current", "summary"):
                    continue
                if mode == "host":
                    if self.plugin.context.get_provider_by_id(binding["provider_id"]) is None:
                        errors.append("PROVIDER_UNAVAILABLE")
                    continue
                config = models._selected_config(role, settings, data)
                if not all(config.get(field) for field in ("base_url", "model", "api_key")):
                    errors.append("CREDENTIALS_REQUIRED")
            except (MemoryModelError, ValueError, KeyError, AttributeError):
                errors.append("CREDENTIALS_REQUIRED")
        return errors

    async def _plan_assistant_dependencies(self, settings):
        """Check only configured task owners, without selecting another chat."""
        binding = settings.get("plan_recovery", {}).get("assistant_binding", {"mode": "summary"})
        if binding.get("mode") == "preset":
            return self._memory_dependencies({"model_bindings": {"summary": binding}})
        with self.store.lock:
            rows = self.store.db.execute("SELECT id,data FROM records WHERE kind='life_plan' "
                "AND json_extract(data,'$.archived') IS NOT 1 LIMIT 201").fetchall()
            pending = self.store.db.execute("SELECT json_extract(data,'$.target_id') FROM records "
                "WHERE kind='restore_pending' AND json_extract(data,'$.kind')='life_plan' "
                "AND json_extract(data,'$.intent.enabled')=1 LIMIT 201").fetchall()
        if len(rows) > 200 or len(pending) > 200:
            return ["RESTORE_CATALOG_LIMIT"]
        enabled_pending = {row[0] for row in pending}
        owners, errors = set(), []
        for row in rows:
            plan = json.loads(row["data"])
            if plan.get("enabled") or row["id"] in enabled_pending:
                owners.add((plan.get("scope", ""), plan.get("origin_cid", "")))
        for scope, cid in owners:
            if scope and not cid:
                try:
                    cid = await self.plugin.context.conversation_manager.get_curr_conversation_id(scope)
                except (AttributeError, ValueError, KeyError):
                    cid = ""
            with self.store.lock:
                exists = self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name='mem_chats'").fetchone()
                row = self.store.db.execute("SELECT settings FROM mem_chats WHERE scope=? AND cid=?", (scope, cid)).fetchone() if exists else None
            policy = json.loads(row[0]) if row else {}
            chosen = policy.get("model_bindings", {}).get("summary", {})
            if chosen.get("mode") != "preset" or not chosen.get("preset_id"):
                errors.append("CREDENTIALS_REQUIRED")
            else:
                errors += self._memory_dependencies({"model_bindings": {"summary": chosen}})
        return errors

    def _session_binding_conflicts(self, values):
        """Return target character ids whose final session bindings collide."""
        with self.store.lock:
            return restore_data.character_binding_conflicts(self.store.db,
                {v["target_id"]: v["intent"].get("sessions", []) for v in values if v["kind"] == "character"})

    def _reference_available(self, identifier):
        from .store import AlbumError
        try:
            character = self.store.get("character", identifier)
            active = next((v for v in character.get("versions", []) if v.get("id") == character.get("active_version")), None)
            if not active or not active.get("asset_ids"):
                return False
            for asset_id in active["asset_ids"]:
                asset = self.store.get("asset", asset_id)
                if asset.get("character_id") != identifier:
                    return False
                path = self.store.asset_path(asset)
                if not path.is_file() or path.is_symlink() or journal.digest_file(path) != asset.get("hash"):
                    return False
            return True
        except (AlbumError, OSError, KeyError, ValueError):
            return False

    def _life_activation(self, value, plan, at):
        """Validate retained adjustments, without resuming any source queue."""
        intent, errors, adjustments = value["intent"], [], []
        if (intent.get("missing_binding") or not plan.get("scope") or not plan.get("character_id")
            or plan.get("run_once") and plan.get("source") in ("chat", "chain") and not plan.get("origin_cid")):
            errors.append("BINDING_REQUIRED")
        if intent.get("adjustment_conflict"):
            errors.append("ADJUSTMENT_CONFLICT")
        seen = set()
        for identifier in intent.get("adjustment_ids", []):
            row = self.store.db.execute("SELECT data FROM records WHERE kind='life_adjustment' AND id=?", (identifier,)).fetchone()
            if row is None:
                errors.append("RESTORE_ADJUSTMENT_MISSING")
                continue
            item = json.loads(row[0])
            anchor = item.get("anchor_ts")
            valid = (item.get("plan_id") == value["target_id"] and item.get("plan_revision") == plan.get("revision")
                and item.get("character_id") == plan.get("character_id") and item.get("scope") == plan.get("scope")
                and bool(item.get("cid")) and (not plan.get("origin_cid") or item["cid"] == plan["origin_cid"])
                and type(anchor) is int and anchor not in seen and type(item.get("effective_ts")) is int
                and (not plan.get("run_once") or anchor == plan.get("trigger_at"))
                and item.get("restore_origin", {}).get("plan_revision") == plan.get("restore_origin", {}).get("revision"))
            if not valid:
                errors.append("ADJUSTMENT_CONFLICT")
                continue
            seen.add(anchor)
            adjustments.append((identifier, item))
        effective = plan.get("trigger_at", 0)
        if plan.get("run_once"):
            if adjustments:
                effective = adjustments[0][1]["effective_ts"]
            if type(effective) is not int or effective <= at:
                errors.append("AGREEMENT_EXPIRED")
        return sorted(set(errors)), adjustments, effective

    async def check(self, owner, body):
        _need(isinstance(body.get("ids"), list) and body["ids"], "RESTORE_SELECTION_INVALID")
        pending, version = await asyncio.to_thread(self._check_snapshot, body["ids"])
        objects, all_errors = [], []
        now = int(time.time())
        selected = {(v["kind"], v["target_id"]) for v in pending}
        binding_conflicts = self._session_binding_conflicts(pending)
        for value in pending:
            item = self._public_pending(value)
            errors, intent = [], value["intent"]
            kind = value["kind"]
            if kind == "memory":
                errors += await self._host_dependencies(value["scope"], value["cid"])
                current_host = await asyncio.to_thread(self._host_fingerprint, value["scope"], value["cid"])
                if not current_host or current_host != intent.get("host_fingerprint"):
                    errors.append("RESTORE_HOST_CHANGED")
                with self.store.lock:
                    row = self.store.db.execute("SELECT settings FROM mem_chats WHERE scope=? AND cid=?", (value["scope"], value["cid"])).fetchone()
                    _need(row is not None, "RESTORE_TARGET_CHANGED")
                    settings = json.loads(row[0])
                    errors += self._memory_dependencies(settings)
                    retention, keep = settings.get("retention_days", 30), max(1, settings.get("keep_turns", 10))
                    floor = self.store.db.execute("SELECT coalesce(min(ordinal),0) FROM (SELECT ordinal FROM mem_turns "
                        "WHERE scope=? AND cid=? AND status='summarized' ORDER BY ordinal DESC LIMIT ?)",
                        (value["scope"], value["cid"], keep)).fetchone()[0]
                    expired = self.store.db.execute("SELECT count(*) FROM mem_turns WHERE scope=? AND cid=? "
                        "AND status='summarized' AND pruned=0 AND summarized_at>0 AND summarized_at<? AND ordinal<?",
                        (value["scope"], value["cid"], now - retention * 86400, floor)).fetchone()[0] if retention else 0
                    snapshots = self.store.db.execute("SELECT count(*) FROM mem_snapshots WHERE scope=? AND cid=? AND created<?",
                        (value["scope"], value["cid"], now - 30 * 86400)).fetchone()[0]
                item["retention"] = {"scope": value["scope"], "cid": value["cid"], "retention_days": retention,
                                     "expired_turns": expired, "expired_snapshots": snapshots}
            elif kind == "character":
                if value.get("target_id") in binding_conflicts:
                    errors.append("SESSION_BINDING_CONFLICT")
                if not await asyncio.to_thread(self._reference_available, value["target_id"]):
                    errors.append("REFERENCE_UNAVAILABLE")
                for scope in intent.get("sessions", []):
                    errors += await self._host_dependencies(scope)
            elif kind == "life_plan":
                with self.store.lock:
                    row = self.store.db.execute("SELECT data,revision FROM records WHERE kind='life_plan' AND id=?", (value["target_id"],)).fetchone()
                    plan = {**json.loads(row[0]), "revision": row[1]} if row else {}
                    life_errors, adjustments, effective = self._life_activation(value, plan, now)
                errors += life_errors
                if "BINDING_REQUIRED" not in errors:
                    errors += await self._host_dependencies(plan["scope"], plan.get("origin_cid", ""))
                    for _, adjustment in adjustments:
                        errors += await self._host_dependencies(plan["scope"], adjustment["cid"])
                    if self._guard().pending("character", plan["character_id"]) and ("character", plan["character_id"]) not in selected:
                        errors.append("REFERENCE_PENDING")
                    if not await asyncio.to_thread(self._reference_available, plan["character_id"]):
                        errors.append("REFERENCE_UNAVAILABLE")
                from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
                try:
                    ZoneInfo(plan.get("timezone", ""))
                except (ZoneInfoNotFoundError, ValueError):
                    errors.append("TIMEZONE_INVALID")
                if plan.get("run_once"):
                    item["effective_ts"] = effective
                item["future_confirmation"] = bool(intent.get("requires_future_confirmation"))
            elif kind == "settings":
                settings = self.store.settings
                providers = [{**settings, "id": "primary"}, *settings.get("providers", [])]
                for provider in providers:
                    wanted = intent.get("primary_enabled") if provider["id"] == "primary" else intent.get("providers", {}).get(provider["id"])
                    if wanted and not all(provider.get(field) for field in ("base_url", "model", "api_key")):
                        errors.append("CREDENTIALS_REQUIRED")
                if intent.get("plan_assistant_enabled"):
                    errors += await self._plan_assistant_dependencies(settings)
                if intent.get("memory_maintenance_enabled"):
                    binding = settings.get("memory_maintenance", {}).get("binding", {"mode": "summary"})
                    if binding.get("mode") == "preset":
                        errors += self._memory_dependencies({"model_bindings": {"summary": binding}})
            elif kind == "calendar":
                with self.store.lock:
                    row = self.store.db.execute("SELECT revision FROM records WHERE kind='calendar' AND id='default'").fetchone()
                if (row[0] if row else 0) != intent["base_revision"]:
                    errors.append("CALENDAR_CHANGED")
            item.update(errors=sorted(set(errors)), can_enable=not errors)
            all_errors.extend(errors)
            objects.append(item)
        _, current_version = await asyncio.to_thread(self._check_snapshot)
        _need(version == current_version, "RESTORE_TARGET_CHANGED")
        identifier = uuid.uuid4().hex
        self.checks = {identifier: {"owner": owner, "target": version, "ids": body["ids"], "at": time.monotonic(), "can_enable": not all_errors}}
        return {"check_id": identifier, "objects": objects, "can_enable": not all_errors, "errors": sorted(set(all_errors)),
                "retention": [v["retention"] for v in objects if "retention" in v]}

    async def _finish_thread(self, function):
        task = asyncio.create_task(asyncio.to_thread(function))
        while True:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # Committing work owns the maintenance ticket until the real
                # thread has finished, even after repeated client cancellation.
                continue

    async def enable(self, owner, body):
        checked = self.checks.get(body.get("check_id"), {})
        _need(checked.get("owner") == owner and checked.get("can_enable") and time.monotonic() - checked.get("at", 0) <= 120,
              "RESTORE_CHECK_EXPIRED")
        _need(all(body.get(k) is True for k in ("old_instance_stopped", "retention_confirmed", "confirm_future")), "RESTORE_CONFIRM_REQUIRED")
        # Read-only dependencies are checked again immediately before locking.
        fresh = await self.check(owner, {"ids": checked["ids"]})
        _need(fresh["can_enable"], "RESTORE_DEPENDENCY_MISSING")
        ticket = await self._guard().acquire()
        recovery_failed = False
        try:
            result = await self._finish_thread(lambda: self._enable_work(checked))
        except BackupError as exc:
            recovery_failed = exc.code == "RECOVERY_REQUIRED"
            raise
        finally:
            if not recovery_failed:
                self._guard().release(ticket, changed=True)
        return result

    def _enable_work(self, checked):
        with self._guard().privileged(), self.store.lock:
            _need(checked["target"] == journal.fingerprint(self.store.db, self.store.settings_path), "RESTORE_TARGET_CHANGED")
            values = self._pending(checked["ids"])
            _need(not self._session_binding_conflicts(values), "SESSION_BINDING_CONFLICT")
            for value in values:
                if value["kind"] == "memory":
                    _need(self._host_fingerprint(value["scope"], value["cid"]) == value["intent"].get("host_fingerprint"), "RESTORE_HOST_CHANGED")
            # Activation has the same cross-medium journal as restoration.
            previous_restore = journal.load(self.store.root, rollback_copy=True)
            before = journal.prepare(self.store, archive_id=values[0]["archive_id"], preview_id=uuid.uuid4().hex, new_media=[],
                affected=[{k: v for k, v in p.items() if k in ("kind", "target_id", "scope", "cid")} for p in values], operation="activate")
            journal.write(self.store.root, before, "applying")
            settings = copy.deepcopy(self.store.settings)
            now = int(time.time())
            committed = False
            try:
                with self.store.db:
                    for value in values:
                        kind, intent = value["kind"], value["intent"]
                        if kind == "memory":
                            self.store.db.execute("UPDATE mem_chats SET enabled=?,revision=revision+1,generation=generation+1 WHERE scope=? AND cid=?",
                                (int(intent["enabled"]), value["scope"], value["cid"]))
                        elif kind in ("character", "life_plan"):
                            row = self.store.db.execute("SELECT data,revision FROM records WHERE kind=? AND id=?", (kind, value["target_id"])).fetchone()
                            payload, revision = json.loads(row[0]), row[1] + 1
                            if kind == "character":
                                payload["sessions"] = intent["sessions"]
                            else:
                                life_errors, adjustments, _ = self._life_activation(value, {**payload, "revision": row[1]}, now)
                                _need(not life_errors, life_errors[0] if life_errors else "RESTORE_DEPENDENCY_MISSING")
                                payload.update(enabled=intent["enabled"], activated_at=now + 1, restore_cutoff=now)
                                for aid, updated in adjustments:
                                    updated.update(plan_revision=revision, explicit=True, evidence_revision=None, restore_confirmed_at=now)
                                    self.store.db.execute("UPDATE records SET data=?,revision=revision+1 WHERE kind='life_adjustment' AND id=?", (_json(updated), aid))
                            self.store.db.execute("UPDATE records SET data=?,revision=? WHERE kind=? AND id=?", (_json(payload), revision, kind, value["target_id"]))
                        elif kind == "calendar":
                            self.store.db.execute("INSERT OR REPLACE INTO records VALUES('calendar','default',?,?)",
                                (_json(intent["calendar_payload"]), intent["base_revision"] + 1))
                        elif kind == "settings":
                            for field in ("qzone_enabled", "generated_qzone_allowed", "primary_enabled"):
                                settings[field] = intent[field]
                            for provider in settings.get("providers", []):
                                provider["enabled"] = intent["providers"].get(provider["id"], False)
                            if "plan_recovery" in settings:
                                settings["plan_recovery"]["assistant_enabled"] = bool(intent.get("plan_assistant_enabled", False))
                            if "memory_maintenance" in settings:
                                settings["memory_maintenance"].update(enabled=bool(intent.get("memory_maintenance_enabled", False)),
                                    auto_after_summary=bool(intent.get("memory_maintenance_auto", False)))
                            settings["settings_revision"] = settings.get("settings_revision", 0) + 1
                        self.store.db.execute("DELETE FROM records WHERE kind='restore_pending' AND id=?", (value["pending_id"],))
                journal.atomic_json(self.store.settings_path, settings)
                self.store.settings = settings
                after = journal.fingerprint(self.store.db, self.store.settings_path)
                if previous_restore and previous_restore.get("after_fingerprint") == checked["target"]:
                    previous_restore["after_fingerprint"] = after
                    before["previous_restore"] = previous_restore
                journal.write(self.store.root, before, "committed", after_fingerprint=after)
                committed = True
                self._prune(before["id"])
            except BaseException as failure:
                if committed:
                    raise BackupError("RECOVERY_REQUIRED") from failure
                try:
                    journal.rollback(self.store, before)
                except Exception as exc:
                    raise BackupError("RECOVERY_REQUIRED") from exc
                raise
        return {"state": "enabled", "enabled": len(values)}

    def rollback_preview(self, owner):
        with self.store.lock:
            saved = journal.load(self.store.root, rollback_copy=True)
            available = bool(saved and saved["state"] == "committed" and saved.get("after_fingerprint") == journal.fingerprint(self.store.db, self.store.settings_path))
        identifier = uuid.uuid4().hex
        self.checks = {identifier: {"owner": owner, "journal_id": saved["id"] if saved else "", "available": available}}
        return {"preview_id": identifier, "available": available, "reason": "" if available else "RESTORE_TARGET_CHANGED"}

    async def rollback(self, owner, body):
        preview = self.checks.get(body.get("preview_id"), {})
        _need(preview.get("owner") == owner and preview.get("available"), "RESTORE_PREVIEW_EXPIRED")
        ticket = await self._guard().acquire()
        def work():
            with self._guard().privileged(), self.store.lock:
                saved = journal.load(self.store.root, rollback_copy=True)
                _need(saved and saved["id"] == preview["journal_id"] and saved["state"] == "committed"
                    and saved["after_fingerprint"] == journal.fingerprint(self.store.db, self.store.settings_path), "RESTORE_TARGET_CHANGED")
                try:
                    journal.rollback(self.store, saved, advance=True)
                except Exception as exc:
                    raise BackupError("RECOVERY_REQUIRED") from exc
        recovery_failed = False
        try:
            await self._finish_thread(work)
        except BackupError as exc:
            recovery_failed = exc.code == "RECOVERY_REQUIRED"
            raise
        finally:
            if not recovery_failed:
                self._guard().release(ticket, changed=True)
        return {"state": "rolled_back"}
